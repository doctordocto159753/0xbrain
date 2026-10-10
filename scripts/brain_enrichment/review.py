"""Local, human-only review of candidate annotations (CONTRACT-E1 section 11).

A review decision is a row in the enrichment store saying "the owner checked
this interpretation": `confirmed` or `rejected`, for ONE (source_ref,
annotation_revision), with a note. It is written only by this module through
`ProjectionStore.record_review`, only from the local CLI, never from a tool.

What a decision does NOT do (and the receipt says so every time):
  * it never promotes anything: `canonical_promotion` is always false;
  * it never raises the tier / authority of the source or of the model;
  * it never touches the corpus, a capture, a source record or a proposal.

Promotion of canonical content stays `scripts/brain_review.py` (a different
tool, with its own gates). This module deliberately never imports it.

Human-context policy (all must hold, checked before the store is touched):
  * BRAIN_REMOTE_SESSION is not set (the remote server always sets it);
  * the actor is a real, named person: non-empty, <= 80 chars, no control
    characters, not a machine identity (claude, hermes, mcp, ...); the stored
    actor is `<name>@<os-user>` so a decision always carries the OS account
    that ran the CLI;
  * a terminal is attached and the person types the confirmation, or --yes is
    given on an explicit non-interactive run; a non-interactive run inside a
    known agent harness (CLAUDECODE ...) is refused even with --yes.
"""
from __future__ import annotations

import getpass
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import contract as C
from .surface import SOURCE_REF_RE, revision_of

DECISIONS = ("confirmed", "rejected")
AUDIT_REL = "_enrichment/review-audit.jsonl"
NOTE_MAX = 1000
ACTOR_MAX = 80
MACHINE_ACTORS = frozenset({
    "claude", "anthropic", "assistant", "hermes", "mcp", "bot", "system", "gpt", "chatgpt", "openai",
    "model", "agent", "llm", "annotator", "enrichment", "server", "remote", "telegram", "codex",
    "opencode", "gemini", "unknown", "anonymous", "owner-bot"})
AGENT_ENV_MARKERS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "BRAIN_AGENT_SESSION")
NO_PROMOTION_MESSAGE = ("Recorded a human review decision on a CANDIDATE annotation. canonical_promotion=false: "
                        "nothing was promoted, no tier or authority changed, and no corpus file was written. "
                        "'confirmed' only means the owner checked this interpretation.")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _forbidden(message: str, **extra) -> C.EnrichmentError:
    return C.EnrichmentError("E_REVIEW_FORBIDDEN", message, **extra)


def os_user(env: dict | None = None) -> str:
    env = os.environ if env is None else env
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - no passwd entry in some containers
        return env.get("USER") or env.get("USERNAME") or ""


def validate_actor(actor: object) -> str:
    if not isinstance(actor, str) or not actor.strip():
        raise _forbidden("review needs a named human actor (--actor)")
    name = actor.strip()
    if len(name) > ACTOR_MAX or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise _forbidden("actor must be a short single-line name")
    if {t for t in re.split(r"[^\w]+", name.casefold()) if t} & MACHINE_ACTORS:
        raise _forbidden("actor must be a human, not a machine identity")
    return name


def resolve_human_context(actor: object, *, env: dict | None = None, stdin=None, stdout=None,
                          yes: bool = False, user: str | None = None) -> dict:
    """The context handed to `ProjectionStore.record_review`. Raises
    E_REVIEW_FORBIDDEN unless the policy in the module docstring holds."""
    env = os.environ if env is None else env
    if env.get("BRAIN_REMOTE_SESSION"):
        raise _forbidden("review is forbidden in a remote session")
    name = validate_actor(actor)
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    tty = bool(getattr(stdin, "isatty", lambda: False)() and getattr(stdout, "isatty", lambda: False)())
    if not tty:
        if not yes:
            raise _forbidden("no interactive terminal: run this in a terminal, or pass --yes for an "
                             "explicit non-interactive run")
        if any(env.get(m) for m in AGENT_ENV_MARKERS):
            raise _forbidden("non-interactive review inside an agent harness is refused; run it in your "
                             "own terminal")
    user = os_user(env) if user is None else user
    if not user:
        raise _forbidden("the operating-system account could not be determined")
    return {"actor": f"{name}@{user}", "actor_name": name, "os_user": user, "remote": False,
            "tty": tty, "confirmation": "flag" if yes else "interactive", "via": "brain_enrichment.cli"}


def _audit_path(root: Path) -> Path:
    """The audit file, after proving the enrichment directory is a real directory
    inside the wiki root (no symlink escape). Created 0700."""
    path = Path(root) / AUDIT_REL
    if path.parent.is_symlink() or path.parent.resolve() != Path(root).resolve() / C.ENRICHMENT_DIR:
        raise C.EnrichmentError("E_UNAVAILABLE", "the enrichment directory is not inside the wiki root")
    path.parent.mkdir(mode=0o700, exist_ok=True)
    return path


def _audit(root: Path, line: dict) -> None:
    """Append one JSON line per decision attempt that passed the human gate."""
    path = _audit_path(root)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def record_decision(store, root: Path, *, source_ref: str, annotation_revision: int, decision: str,
                    note: str, human_context: dict, source_version: str | None = None,
                    confirm: "callable | None" = None) -> dict:
    """Validate, check the displayed revision is still current, record, audit.

    `confirm(prompt) -> str` is the interactive typed confirmation (None when
    the run was explicitly non-interactive). Returns the receipt dict; raises
    EnrichmentError otherwise (E_REVIEW_FORBIDDEN, E_BAD_REF, E_BAD_ARGUMENTS,
    E_NOT_FOUND, E_CONFLICT{current_revision}, E_STALE_SOURCE).
    """
    if not human_context or human_context.get("remote") or not isinstance(human_context.get("actor"), str):
        raise _forbidden("review needs a resolved local human context")
    validate_actor(human_context.get("actor_name") or human_context["actor"].split("@", 1)[0])
    if os.environ.get("BRAIN_REMOTE_SESSION"):
        raise _forbidden("review is forbidden in a remote session")
    if not (isinstance(source_ref, str) and SOURCE_REF_RE.fullmatch(source_ref)):
        raise C.EnrichmentError("E_BAD_REF", "source_ref must be a typed ref such as cap:<id>")
    if not (isinstance(annotation_revision, int) and not isinstance(annotation_revision, bool)
            and annotation_revision >= 1):
        raise C.EnrichmentError("E_BAD_ARGUMENTS", "annotation_revision must be an integer >= 1")
    if decision not in DECISIONS:
        raise C.EnrichmentError("E_BAD_ARGUMENTS", f"decision must be one of {list(DECISIONS)}")
    if not isinstance(note, str) or not note.strip():
        raise C.EnrichmentError("E_BAD_ARGUMENTS", "a note (the reason for the decision) is required")
    if len(note) > NOTE_MAX:
        raise C.EnrichmentError("E_BAD_ARGUMENTS", f"note exceeds {NOTE_MAX} characters")
    if source_version is not None and not (isinstance(source_version, str) and C.SHA256_RE.fullmatch(source_version)):
        raise C.EnrichmentError("E_BAD_ARGUMENTS", "source_version must be 64 hex characters")

    view = store.get(source_ref)                                  # E_NOT_FOUND
    current = revision_of(view)
    base = {"at": _now(), "actor": human_context["actor"], "os_user": human_context.get("os_user"),
            "tty": bool(human_context.get("tty")), "confirmation": human_context.get("confirmation"),
            "source_ref": source_ref, "annotation_revision": annotation_revision,
            "decision": decision, "note": note.strip()[:NOTE_MAX], "via": human_context.get("via")}
    problem = None
    if view.stale or view.projection.get("processing_state") == "stale":
        problem = C.EnrichmentError("E_STALE_SOURCE", "the source text changed after this annotation was "
                                    "written; it describes older text and cannot be reviewed as current")
    elif not current:
        problem = C.EnrichmentError("E_NOT_FOUND", f"{source_ref} has no annotation revision to review")
    elif current != annotation_revision:
        problem = C.EnrichmentError("E_CONFLICT", f"the annotation moved since you looked: current revision "
                                    f"is {current}, you reviewed {annotation_revision}; inspect it again",
                                    current_revision=current)
    elif source_version is not None and source_version != view.projection.get("source_version"):
        problem = C.EnrichmentError("E_STALE_SOURCE", "source_version differs from the current one")
    if problem is not None:
        _audit(root, {**base, "outcome": problem.code})
        raise problem
    _audit_path(root)                       # fail before the store is written if the audit trail is unavailable
    if confirm is not None:
        expected = f"{source_ref}@{annotation_revision}"
        if confirm(f"{decision} {expected}? Type it to confirm: ").strip() != expected:
            _audit(root, {**base, "outcome": "aborted"})
            raise _forbidden("confirmation did not match; nothing was recorded")
    try:
        receipt = store.record_review({"source_ref": source_ref, "annotation_revision": annotation_revision,
                                       "decision": decision, "note": note.strip()}, human_context)
    except C.EnrichmentError as exc:
        _audit(root, {**base, "outcome": exc.code})
        raise
    if getattr(receipt, "canonical_promotion", False) is not False:
        _audit(root, {**base, "outcome": "E_INTERNAL"})
        raise C.EnrichmentError("E_INTERNAL", "store reported a promotion; review must never promote")
    out = {"ok": True, "recorded": True, "source_ref": receipt.source_ref,
           "annotation_revision": receipt.annotation_revision, "decision": receipt.decision,
           "reviewed_by": receipt.reviewed_by, "reviewed_at": receipt.reviewed_at,
           "canonical_promotion": False, "promoted": False, "message": NO_PROMOTION_MESSAGE}
    try:
        _audit(root, {**base, "outcome": "recorded", "reviewed_at": receipt.reviewed_at})
    except OSError:
        out["audit_warning"] = "the decision was recorded but its audit line could not be written"
    return out
