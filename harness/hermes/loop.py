"""Reference driver for the 0xBrain annotation turn (pure stdlib, no model SDK).

This file documents and tests the cycle that SKILL.md asks the Hermes harness
to perform. It is NOT the production scheduler and it holds no credentials:
the harness injects

    tool_call(name, args) -> dict      one MCP call to the 0xBrain connector
    annotator(item)       -> dict      the model step (returns the annotation
                                       object) or raises ProviderError /
                                       CreditExhausted / AnnotatorOutputError

One turn: pending(limit=1) -> check budget -> annotate -> submit
(source_version + expected_annotation_revision + a stable idempotency_key) ->
record the receipt -> next page / next source, until a cap is reached.

Hard rules encoded here (and covered by tests/enrichment/test_harness_contract.py):
* The loop may call ONLY the three enrichment read/submit tools. It cannot
  capture, ingest, propose, review, approve or promote.
* A provider/credit/402/429/503 failure STOPS the turn. Transient failures get at
  most `budget.max_attempts` (default 2) attempts, spaced; credit/402/auth
  failures get exactly one. No retry loop, no fallback model, no error text
  is ever turned into a capture.
* The idempotency key is a function of the unit of work (source, version,
  expected revision, page offset), so a retried submit can never create a
  second revision.
* A provider outage never blocks capture or file ingestion: those are separate
  helpers below that do not depend on the annotator at all.
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

PENDING = "brain_enrichment_pending"
SUBMIT = "brain_enrichment_submit"
ARTIFACT_READ = "brain_artifact_read"
ALLOWED_TOOLS = (PENDING, SUBMIT, ARTIFACT_READ)

TRANSIENT_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
CREDIT_STATUS = frozenset({402})
PROMPT_VERSION = "annotate-1"
UPLOAD_REF_RE = re.compile(r"^upl-[A-Za-z0-9_-]{43}$")
LANG_HINTS = ("fa", "en", "mixed", "unknown")


# ------------------------------------------------------------------ errors
class ProviderError(Exception):
    """The model provider failed. `status` is the HTTP status when known."""

    def __init__(self, message: str = "provider error", status: int | None = None,
                 transient: bool | None = None):
        super().__init__(message)
        self.status = status
        self.transient = (status in TRANSIENT_STATUS or status is None) if transient is None else transient


class CreditExhausted(ProviderError):
    """Out of credit / 402. Never retried."""

    def __init__(self, message: str = "credit exhausted", status: int | None = 402):
        super().__init__(message, status=status, transient=False)


class AnnotatorOutputError(Exception):
    """The model answered but not with a usable JSON object. Not retried."""


class PolicyViolation(Exception):
    """A harness-side rule (capture confirmation, tool allow-list) was about to be broken."""


# ------------------------------------------------------------------ budget
@dataclass
class Budget:
    """Caps for ONE turn plus the daily ledger. Costs are estimates the harness
    owns; the server never sees them. `credit_ok()` is an optional cheap probe
    (return False to stop before spending anything)."""
    max_seconds: float = 240.0
    max_turn_cost: float = 0.25
    est_cost_per_call: float = 0.02
    max_daily_cost: float = 1.0
    daily_spent: float = 0.0
    max_pages: int = 3
    max_attempts: int = 2
    retry_spacing_seconds: float = 30.0
    credit_ok: Callable[[], bool] | None = None
    harness: str = "hermes"
    model_label: str = "unspecified"
    spent: float = field(default=0.0, init=False)

    def charge(self, amount: float | None = None) -> None:
        self.spent += self.est_cost_per_call if amount is None else amount


@dataclass
class TurnResult:
    stopped: str = ""                      # why the turn ended (see STOP_REASONS)
    detail: dict = field(default_factory=dict)
    receipts: list = field(default_factory=list)
    rejections: list = field(default_factory=list)
    calls: list = field(default_factory=list)       # tool names, in order
    annotator_calls: int = 0
    pages: int = 0
    cost_spent: float = 0.0

    @property
    def ok(self) -> bool:
        return self.stopped in ("idle", "done")


STOP_REASONS = (
    "idle", "done", "time_cap", "turn_cost_cap", "daily_cost_cap", "page_cap", "credit_exhausted",
    "provider_error", "provider_unavailable", "annotator_output_invalid", "annotator_failed",
    "pending_error", "tool_transport_error", "submit_error", "validation_rejected",
    "idempotency_conflict", "conflict_limit", "no_progress", "unit_blocked", "not_enabled")


def idempotency_key(source_ref: str, source_version: str, expected_revision: int, offset: int = 0) -> str:
    """Stable per unit of work. Matches ^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$."""
    digest = hashlib.sha256(f"{source_ref}\0{source_version}\0{expected_revision}\0{offset}".encode()).hexdigest()
    return "ann-" + digest[:40]


# -------------------------------------------------------------------- turn
def run_turn(tool_call: Callable[[str, dict], dict], annotator: Callable[[dict], dict], budget: Budget,
             *, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
             record: Callable[[dict], None] | None = None,
             blocked: dict | None = None) -> TurnResult:
    """Run one bounded annotation turn. `record(receipt)` is called for each
    persisted receipt. `blocked` is a caller-persisted {unit: rejection count}
    so a unit the server rejected twice is not retried (and not paid for) forever."""
    res = TurnResult()
    started = clock()
    blocked = blocked if blocked is not None else {}
    submitted: set[tuple] = set()
    conflicts = 0

    def call(name: str, args: dict) -> dict | None:
        """One tool call with the bounded transport-retry policy. None = gave up."""
        if name not in ALLOWED_TOOLS:
            raise PolicyViolation(f"the annotation loop may not call {name}")
        for attempt in range(1, budget.max_attempts + 1):
            res.calls.append(name)
            try:
                out = tool_call(name, args)
            except (ConnectionError, TimeoutError, OSError):
                if attempt >= budget.max_attempts:
                    return None
                sleep(budget.retry_spacing_seconds)
                continue
            return out if isinstance(out, dict) else {"ok": False, "error": {"code": "E_INTERNAL"}}
        return None

    def over_budget() -> str | None:
        if clock() - started >= budget.max_seconds:
            return "time_cap"
        if budget.spent + budget.est_cost_per_call > budget.max_turn_cost:
            return "turn_cost_cap"
        if budget.daily_spent + budget.spent + budget.est_cost_per_call > budget.max_daily_cost:
            return "daily_cost_cap"
        if budget.credit_ok is not None and not budget.credit_ok():
            return "credit_exhausted"
        return None

    def finish(reason: str, **detail: Any) -> TurnResult:
        res.stopped, res.detail, res.cost_spent = reason, detail, budget.spent
        return res

    cursor: str | None = None
    while True:
        why = over_budget()
        if why:
            return finish(why)
        if res.pages >= budget.max_pages:
            return finish("page_cap")

        args: dict = {"limit": 1}
        if cursor:
            args["cursor"] = cursor
        resp = call(PENDING, args)
        if resp is None:
            return finish("tool_transport_error", tool=PENDING)
        if not resp.get("ok"):
            return finish("pending_error", code=(resp.get("error") or {}).get("code"))
        items = resp.get("items") or []
        if not items:
            return finish("idle" if res.pages == 0 else "done")
        item = items[0]
        page = item.get("page") or {}
        offset = int(page.get("offset") or 0)
        ref, ver = item["source_ref"], item["source_version"]
        expected = int(item.get("expected_annotation_revision", 0))
        unit = (ref, ver, expected, offset)
        if unit in submitted:
            return finish("no_progress", source_ref=ref)
        if blocked.get(unit, 0) >= 2:
            return finish("unit_blocked", source_ref=ref)

        # -- model step: bounded, never looped on provider failure --------
        annotation = None
        for attempt in range(1, budget.max_attempts + 1):
            why = over_budget()
            if why:
                return finish(why)
            res.annotator_calls += 1
            try:
                annotation = annotator(item)
                budget.charge()
                break
            except CreditExhausted as e:
                budget.charge()
                return finish("credit_exhausted", status=e.status)
            except ProviderError as e:
                budget.charge()
                if e.status in CREDIT_STATUS:
                    return finish("credit_exhausted", status=e.status)
                if not e.transient:
                    return finish("provider_error", status=e.status)
                if attempt >= budget.max_attempts:
                    return finish("provider_unavailable", status=e.status, attempts=attempt)
                sleep(budget.retry_spacing_seconds)
            except AnnotatorOutputError:
                budget.charge()
                return finish("annotator_output_invalid")
            except Exception as e:  # noqa: BLE001 - class name only: messages may carry provider text
                budget.charge()
                return finish("annotator_failed", error_type=type(e).__name__)
        if not isinstance(annotation, dict) or not annotation:
            return finish("annotator_output_invalid")
        annotation = {**annotation}
        annotation.setdefault("producer_reported", {"harness": budget.harness, "model": budget.model_label,
                                                    "prompt_version": PROMPT_VERSION})

        # -- submit: same key on every retry of this unit ------------------
        payload = {"source_ref": ref, "source_version": ver, "expected_annotation_revision": expected,
                   "idempotency_key": idempotency_key(ref, ver, expected, offset), "annotation": annotation}
        out = call(SUBMIT, payload)
        if out is None:
            return finish("tool_transport_error", tool=SUBMIT)
        if out.get("ok") and out.get("persisted"):
            receipt = {k: out.get(k) for k in ("source_ref", "source_version", "annotation_revision",
                                               "idempotent_replay", "coverage", "processing_state")}
            res.receipts.append(receipt)
            res.pages += 1
            submitted.add(unit)
            if record is not None:
                record(receipt)
            cursor = resp.get("next_cursor")
            continue
        err = out.get("error") or {}
        code = err.get("code")
        if code in ("E_CONFLICT", "E_STALE_SOURCE"):
            conflicts += 1                 # the source or revision moved: re-read, at most once per turn
            if conflicts > 1:
                return finish("conflict_limit", code=code)
            cursor = None
            continue
        if code == "E_VALIDATION":
            blocked[unit] = blocked.get(unit, 0) + 1
            res.rejections.append({"source_ref": ref, "reasons": err.get("reasons", [])})
            return finish("validation_rejected", source_ref=ref)
        if code == "E_IDEMPOTENCY_CONFLICT":
            return finish("idempotency_conflict", source_ref=ref)
        if code == "E_DISABLED":
            return finish("not_enabled")
        return finish("submit_error", code=code)


# ------------------------------------------- capture / ingest (policy gates)
# These do not involve the annotator or any model provider, so a provider or
# credit outage cannot block them. They exist to make the confirmation rules
# executable; the harness uses the same rules in its chat flow.
def capture_text(tool_call: Callable[[str, dict], dict], text: str, *, origin: str,
                 user_confirmed: bool = False, language_hint: str = "unknown") -> dict:
    """Store the user's words. origin 'user_typed' = the user wrote this text;
    origin 'transcript' = machine speech-to-text, which must be shown to the
    user and explicitly confirmed (or corrected, then confirmed) first. Nothing
    else (an error message, a summary, a model reply) may be captured."""
    if origin not in ("user_typed", "transcript"):
        raise PolicyViolation("only the user's own words may be captured (origin user_typed or transcript)")
    if origin == "transcript" and user_confirmed is not True:
        raise PolicyViolation("an unconfirmed transcript is never captured")
    if not isinstance(text, str) or not text.strip():
        raise PolicyViolation("nothing to capture")
    if language_hint not in LANG_HINTS:
        raise PolicyViolation("bad language_hint")
    return tool_call("brain_capture", {"text": text, "language_hint": language_hint})


def ingest_uploaded_file(tool_call: Callable[[str, dict], dict], upload_ref: str, *,
                         user_confirmed: bool, title: str | None = None) -> dict:
    """Ingest a file the owner already uploaded. The user confirms the file
    first; the harness only forwards the upload_ref the upload page returned."""
    if user_confirmed is not True:
        raise PolicyViolation("a file is ingested only after the user confirms it")
    if not isinstance(upload_ref, str) or not UPLOAD_REF_RE.fullmatch(upload_ref):
        raise PolicyViolation("upload_ref must be the value returned by the upload page")
    args: dict = {"upload_ref": upload_ref}
    if title:
        args["title"] = title
    return tool_call("brain_ingest_file", args)
