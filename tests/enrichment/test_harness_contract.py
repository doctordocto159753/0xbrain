"""Hermes harness package (WP-B): the loop, the fake annotator, the shipped
skill/prompts/job/install files. Drives the REAL AnnotationService against
FakeProjectionStore through a tool_call shim; no model, no network."""
from __future__ import annotations

import ast
import copy
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest
import yaml

from brain_enrichment import contract as C
from brain_enrichment import tools_spec as T
from brain_enrichment.annotation import AnnotationService
from brain_enrichment.config import EnrichmentConfig
from brain_surface import contract as BC
from support import FA_SHORT, FakeProjectionStore, make_projection

ROOT = Path(__file__).resolve().parents[2]
HERMES = ROOT / "harness" / "hermes"
FIX = Path(__file__).resolve().parent / "fixtures"
ON = EnrichmentConfig(mode="on", writes=True)
UPLOAD_REF = "upl-" + "A" * 43


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"hermes_{name}", HERMES / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


L = _load("loop")
FA = _load("fake_annotator")


# --------------------------------------------------------------- server shim
def chunks_for(proj: C.SourceProjection, cuts: list[int]):
    layer = proj.layers[0]
    bounds = [0, *cuts, len(layer.text)]
    out = []
    for i in range(len(bounds) - 1):
        s, e = bounds[i], bounds[i + 1]
        dg = C.chunk_digest(proj.source_ref, proj.source_version, layer.layer_id, s, e)
        out.append(C.Chunk(C.chunk_ref(dg), dg, proj.source_ref, proj.source_version, layer.text_ref,
                           layer.layer_id, i, s, e, layer.text[s:e]))
    return [C.Chunk(**{**c.__dict__, "prev_chunk_ref": out[i - 1].chunk_ref if i else None,
                       "next_chunk_ref": out[i + 1].chunk_ref if i + 1 < len(out) else None})
            for i, c in enumerate(out)]


class Server:
    """What the extension tools would do, over the fake store + real AnnotationService."""

    def __init__(self, page_chunks: int = 2):
        self.store = FakeProjectionStore()
        self.svc = AnnotationService(self.store, config=ON, resolve_source=lambda r: True)
        self.page_chunks = page_chunks
        self.calls: list[tuple[str, dict]] = []
        self.captures: list[dict] = []
        self.ingests: list[dict] = []

    def add(self, source_ref: str, text: str, cuts: list[int] | None = None):
        proj = make_projection(source_ref, [("user-text", source_ref, text, None)])
        self.store.upsert_projection(proj, chunks_for(proj, cuts or []))
        return proj

    # -- helpers
    def _annotated(self, ref):
        ver = self.store.projections[ref].source_version
        return {c["chunk_ref"] for r in self.store.revisions.get(ref, []) if r["source_version"] == ver
                for c in r["annotation"].get("chunk_annotations", [])}

    def _unannotated_sources(self):
        for ref, proj in self.store.projections.items():
            done = self._annotated(ref)
            if any(c.chunk_ref not in done for c in self.store.chunks[ref]):
                yield ref

    def revisions(self, ref):
        return len(self.store.revisions.get(ref, []))

    # -- tool_call
    def __call__(self, name: str, args: dict) -> dict:
        self.calls.append((name, copy.deepcopy(args)))
        if name == "brain_enrichment_pending":
            return self._pending(args)
        if name == "brain_enrichment_submit":
            return self._submit(args)
        if name == "brain_capture":
            self.captures.append(args)
            return {"ok": True, "persisted": True, "ref": f"cap:cap-{len(self.captures):04d}", "commit_state": "committed"}
        if name == "brain_ingest_file":
            self.ingests.append(args)
            return {"ok": True, "persisted": True, "source_ref": "rec:zb-src-fixture", "commit_state": "committed"}
        return {"ok": False, "error": {"code": "E_BAD_ARGUMENTS", "message": "unknown tool"}}

    def _pending(self, args):
        refs = list(self._unannotated_sources())
        if args.get("cursor"):
            refs = [C.decode_cursor(args["cursor"], "pending")["ref"]]
        if not refs:
            return {"ok": True, "items": [], "total": 0, "returned": 0, "truncated": False, "next_cursor": None}
        ref = refs[0]
        proj = self.store.projections[ref]
        done = self._annotated(ref)
        chunks = self.store.chunks[ref]
        todo = [c for c in chunks if c.chunk_ref not in done]
        page = todo[: self.page_chunks]
        more = len(todo) > len(page)
        cur = self.store._current_rev(ref, proj.source_version)
        n = cur["revision"] if cur else 0
        item = {"source_ref": ref, "source_version": proj.source_version, "current_annotation_revision": n,
                "expected_annotation_revision": n,
                "page": {"chunks": [{"chunk_ref": c.chunk_ref, "text": c.text, "index": c.index,
                                     "start_char": c.start_char, "annotated": False} for c in page],
                         "offset": page[0].index, "returned": len(page), "total": len(chunks), "truncated": more}}
        return {"ok": True, "items": [item], "total": 1, "returned": 1, "truncated": more,
                "next_cursor": C.encode_cursor("pending", ref=ref) if more else None}

    def _submit(self, args):
        try:
            r = self.svc.submit(args)
        except C.EnrichmentError as e:
            return {**e.to_public(), "persisted": False}
        return {"ok": True, "persisted": True, "source_ref": r.source_ref, "source_version": r.source_version,
                "annotation_revision": r.annotation_revision, "idempotent_replay": r.idempotent_replay,
                "coverage": r.coverage, "processing_state": r.processing_state, "index_dirty": True}


class Clock:
    def __init__(self, step: float = 0.0):
        self.t, self.step, self.sleeps = 1000.0, step, []

    def __call__(self):
        self.t += self.step
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)


def budget(**kw):
    return L.Budget(**{"max_seconds": 600, "max_turn_cost": 10.0, "max_daily_cost": 100.0, **kw})


def run(server, annotator=FA.annotate, bud=None, clock=None, **kw):
    clock = clock or Clock()
    return L.run_turn(server, annotator, bud or budget(), clock=clock, sleep=clock.sleep, **kw), clock


# --------------------------------------------------------------- happy paths
def test_one_turn_annotates_and_records_a_receipt_then_goes_idle():
    s = Server()
    s.add("cap:cap-20261001-120000-ab12", FA_SHORT + "\n")
    got = []
    res, _ = run(s, record=got.append)
    assert res.stopped == "done" and res.ok and res.pages == 1
    (rc,) = res.receipts
    assert rc["annotation_revision"] == 1 and rc["idempotent_replay"] is False and got == [rc]
    assert rc["coverage"] == {"annotated_chunks": 1, "total_chunks": 1}
    rev = s.store.revisions["cap:cap-20261001-120000-ab12"][0]
    assert rev["annotation"]["producer_reported"]["harness"] == "fake-annotator"       # a claim, not identity
    assert [e["name"] for e in rev["derived"]["entities"]] == ["فیت پلن"]
    again, _ = run(s)
    assert again.stopped == "idle" and s.revisions("cap:cap-20261001-120000-ab12") == 1
    assert {n for n, _ in s.calls} == set(L.ALLOWED_TOOLS) - {L.ARTIFACT_READ}


def test_loop_fills_producer_reported_only_as_a_claim_when_missing():
    s = Server()
    s.add("cap:cap-1", "plain words for the annotator to read\n")
    run(s, annotator=lambda item: {"chunk_annotations": [{"chunk_ref": item["page"]["chunks"][0]["chunk_ref"],
                                                          "summary": "x"}]},
        bud=budget(harness="hermes", model_label="m1"))
    pr = s.store.revisions["cap:cap-1"][0]["annotation"]["producer_reported"]
    assert pr == {"harness": "hermes", "model": "m1", "prompt_version": "annotate-1"}


def test_long_artifact_is_annotated_page_by_page_with_coverage_and_no_partial_summary():
    ref = "cap:cap-20261001-130000-cd34"
    text = "".join(f"بخش {i}: این پاراگراف شماره {i} درباره پروژه نیل است و ادامه دارد.\n" for i in range(5))
    cuts = [text.index(f"بخش {i}:") for i in range(1, 5)]
    s = Server(page_chunks=2)
    s.add(ref, text, cuts)
    res, _ = run(s, bud=budget(max_pages=4))
    assert res.stopped == "done" and res.pages == 3
    assert [r["coverage"]["annotated_chunks"] for r in res.receipts] == [2, 4, 5]
    assert [r["annotation_revision"] for r in res.receipts] == [1, 2, 3]
    assert all("summary" not in rev["annotation"] for rev in s.store.revisions[ref])    # partial pages: no artifact summary
    keys = [c[1]["idempotency_key"] for c in s.calls if c[0] == "brain_enrichment_submit"]
    assert len(set(keys)) == 3                                                             # one stable key per unit
    # pages are requested through the cursor the previous page returned
    pend = [c[1] for c in s.calls if c[0] == "brain_enrichment_pending"]
    assert "cursor" not in pend[0] and "cursor" in pend[1] and "cursor" in pend[2]


def test_page_cap_stops_the_turn_and_the_next_turn_resumes():
    ref = "cap:cap-20261001-130000-cd34"
    text = "".join(f"بخش {i}: متن شمارهٔ {i} برای آزمایش صفحه‌بندی طولانی\n" for i in range(6))
    s = Server(page_chunks=2)
    s.add(ref, text, [text.index(f"بخش {i}:") for i in range(1, 6)])
    res, _ = run(s, bud=budget(max_pages=2))
    assert res.stopped == "page_cap" and res.pages == 2 and s.revisions(ref) == 2
    res2, _ = run(s, bud=budget(max_pages=2))
    assert res2.stopped == "done" and res2.pages == 1 and res2.receipts[0]["coverage"] == {"annotated_chunks": 6, "total_chunks": 6}


def test_idempotency_key_is_a_stable_function_of_the_unit_and_matches_the_contract_regex():
    k = L.idempotency_key("cap:cap-1", "a" * 64, 0, 0)
    assert k == L.idempotency_key("cap:cap-1", "a" * 64, 0, 0) and C.IDEMPOTENCY_KEY_RE.fullmatch(k)
    assert len({k, L.idempotency_key("cap:cap-1", "a" * 64, 1, 0), L.idempotency_key("cap:cap-1", "b" * 64, 0, 0),
                L.idempotency_key("cap:cap-2", "a" * 64, 0, 0), L.idempotency_key("cap:cap-1", "a" * 64, 0, 2)}) == 5


def test_retry_after_a_lost_submit_response_creates_no_duplicate_revision():
    s = Server()
    ref = "cap:cap-20261001-120000-ab12"
    s.add(ref, FA_SHORT + "\n")
    lost = {"n": 0}

    def flaky(name, args):
        out = s(name, args)
        if name == "brain_enrichment_submit" and lost["n"] == 0:
            lost["n"] += 1
            raise ConnectionError("response lost after the write")
        return out
    clock = Clock()
    res = L.run_turn(flaky, FA.annotate, budget(retry_spacing_seconds=7), clock=clock, sleep=clock.sleep)
    assert res.stopped == "done"
    assert s.revisions(ref) == 1                                            # exactly one revision
    assert res.receipts[0]["idempotent_replay"] is True and res.receipts[0]["annotation_revision"] == 1
    assert clock.sleeps == [7]                                              # one spaced retry, same key
    subs = [a for n, a in s.calls if n == "brain_enrichment_submit"]
    assert len(subs) == 2 and subs[0]["idempotency_key"] == subs[1]["idempotency_key"]
    assert res.annotator_calls == 1                                         # the model was not paid twice


def test_transport_failures_stop_after_two_attempts():
    clock = Clock()

    def down(name, args):
        raise TimeoutError("connector unreachable")
    res = L.run_turn(down, FA.annotate, budget(), clock=clock, sleep=clock.sleep)
    assert res.stopped == "tool_transport_error" and res.calls == ["brain_enrichment_pending"] * 2
    assert len(clock.sleeps) == 1


# ------------------------------------------------------------ provider outage
def _outage(exc, server=None):
    s = server or Server()
    s.add("cap:cap-20261001-120000-ab12", FA_SHORT + "\n")
    calls = {"n": 0}

    def annotator(item):
        calls["n"] += 1
        raise exc
    res, clock = run(s, annotator=annotator, bud=budget(retry_spacing_seconds=30))
    return s, res, clock, calls["n"]


def test_credit_exhausted_stops_at_once_and_capture_and_ingest_still_work():
    s = Server()
    first = L.capture_text(s, "یادداشت قبل از قطعی", origin="user_typed", language_hint="fa")
    s2, res, clock, n = _outage(L.CreditExhausted(), s)
    assert res.stopped == "credit_exhausted" and n == 1 and clock.sleeps == []
    assert res.receipts == [] and s.revisions("cap:cap-20261001-120000-ab12") == 0
    assert [n_ for n_, _ in s.calls if n_ == "brain_enrichment_submit"] == []
    # capture and ingest are independent of the provider
    again = L.capture_text(s, "یادداشت بعد از قطعی", origin="transcript", user_confirmed=True, language_hint="fa")
    ing = L.ingest_uploaded_file(s, UPLOAD_REF, user_confirmed=True, title="نامه")
    assert first["persisted"] and again["persisted"] and ing["persisted"]
    assert [c["text"] for c in s.captures] == ["یادداشت قبل از قطعی", "یادداشت بعد از قطعی"]
    # nothing about the failure was written anywhere
    assert "credit" not in json.dumps(s.captures) and "credit" not in json.dumps(s.store.revisions)
    # and the loop itself never calls capture/ingest/anything outside its allow-list
    assert {n_ for n_ in res.calls} <= set(L.ALLOWED_TOOLS)


@pytest.mark.parametrize("status", [429, 503, 504, None])
def test_transient_errors_get_exactly_two_spaced_attempts_then_stop(status):
    s, res, clock, n = _outage(L.ProviderError("busy", status=status))
    assert res.stopped == "provider_unavailable" and n == 2 and clock.sleeps == [30]
    assert s.revisions("cap:cap-20261001-120000-ab12") == 0 and res.receipts == []
    assert "brain_enrichment_submit" not in res.calls and s.captures == []


@pytest.mark.parametrize("status,reason", [(402, "credit_exhausted"), (401, "provider_error"),
                                           (403, "provider_error"), (400, "provider_error")])
def test_credit_and_auth_errors_are_not_retried(status, reason):
    s, res, clock, n = _outage(L.ProviderError("nope", status=status))
    assert res.stopped == reason and n == 1 and clock.sleeps == []


def test_transient_then_success_submits_exactly_once():
    s = Server()
    ref = "cap:cap-20261001-120000-ab12"
    s.add(ref, FA_SHORT + "\n")
    seq = iter([L.ProviderError("busy", status=503), None])

    def annotator(item):
        e = next(seq)
        if e:
            raise e
        return FA.annotate(item)
    res, clock = run(s, annotator=annotator)
    assert res.stopped == "done" and res.annotator_calls == 2 and clock.sleeps == [30]
    assert s.revisions(ref) == 1


def test_unexpected_annotator_failure_stops_without_leaking_its_message():
    class Weird(Exception):
        pass
    s, res, clock, n = _outage(Weird("sk-SECRET-KEY-123 leaked in provider message"))
    assert res.stopped == "annotator_failed" and n == 1 and res.detail == {"error_type": "Weird"}
    assert "SECRET" not in json.dumps(res.__dict__, default=str) and s.captures == []


@pytest.mark.parametrize("bad", [None, "text", [], {}, 5])
def test_unusable_model_output_is_not_submitted(bad):
    s = Server()
    s.add("cap:cap-1", "words words words words\n")
    res, _ = run(s, annotator=lambda item: bad)
    assert res.stopped == "annotator_output_invalid" and "brain_enrichment_submit" not in res.calls


def test_annotator_output_error_is_not_retried():
    def bad(item):
        raise L.AnnotatorOutputError("not json")
    s = Server()
    s.add("cap:cap-1", "words words words words\n")
    res, _ = run(s, annotator=bad)
    assert res.stopped == "annotator_output_invalid" and res.annotator_calls == 1


# -------------------------------------------------------------------- budgets
def test_turn_time_cap_stops_between_pages():
    ref = "cap:cap-20261001-130000-cd34"
    text = "".join(f"بخش {i}: متن شمارهٔ {i} برای آزمایش سقف زمان\n" for i in range(6))
    s = Server(page_chunks=2)
    s.add(ref, text, [text.index(f"بخش {i}:") for i in range(1, 6)])
    clock = Clock(step=10.0)                                       # every clock read costs 10 s
    res = L.run_turn(s, FA.annotate, budget(max_seconds=60, max_pages=10), clock=clock, sleep=clock.sleep)
    assert res.stopped == "time_cap" and 0 < res.pages < 3


def test_turn_cost_cap_and_daily_cost_cap():
    s = Server()
    s.add("cap:cap-1", "words words words words\n")
    s.add("cap:cap-2", "other words other words\n")
    res, _ = run(s, bud=budget(est_cost_per_call=0.1, max_turn_cost=0.15))
    assert res.stopped == "turn_cost_cap" and res.pages == 1 and res.annotator_calls == 1
    assert res.cost_spent == pytest.approx(0.1)
    s2 = Server()
    s2.add("cap:cap-1", "words words words words\n")
    res2, _ = run(s2, bud=budget(est_cost_per_call=0.05, max_daily_cost=1.0, daily_spent=0.98))
    assert res2.stopped == "daily_cost_cap" and s2.calls == []              # nothing was even fetched


def test_failed_provider_calls_are_charged():
    _, res, _, _ = _outage(L.ProviderError("busy", status=503))
    assert res.cost_spent == pytest.approx(0.04)


def test_credit_probe_false_stops_before_any_call():
    s = Server()
    s.add("cap:cap-1", "words words words words\n")
    res, _ = run(s, bud=budget(credit_ok=lambda: False))
    assert res.stopped == "credit_exhausted" and s.calls == [] and res.annotator_calls == 0


# ----------------------------------------------------- server-side rejections
def test_validation_rejection_is_recorded_and_a_unit_rejected_twice_is_blocked():
    ref = "cap:cap-20261001-120000-ab12"
    s = Server()
    s.add(ref, FA_SHORT + "\n")

    def forger(item):
        c = item["page"]["chunks"][0]
        return {"links": [{"relation": "mentions", "target": {"source_ref": ref + "x"},
                           "evidence": [{"chunk_ref": c["chunk_ref"], "quote": "این جمله در متن نیست اصلا"}]}]}
    blocked: dict = {}
    r1, _ = run(s, annotator=forger, blocked=blocked)
    assert r1.stopped == "validation_rejected" and r1.rejections[0]["reasons"]
    assert s.revisions(ref) == 0
    r2, _ = run(s, annotator=forger, blocked=blocked)
    assert r2.stopped == "validation_rejected"
    r3, _ = run(s, annotator=forger, blocked=blocked)
    assert r3.stopped == "unit_blocked" and r3.annotator_calls == 0          # no more money on this unit


def test_revision_conflict_refetches_once_then_stops():
    ref = "cap:cap-20261001-120000-ab12"
    s = Server()
    s.add(ref, FA_SHORT + "\n")
    proj = s.store.projections[ref]
    moves = {"n": 0}

    def racing(name, args):
        if name == "brain_enrichment_submit" and moves["n"] < 1:
            moves["n"] += 1                                         # another writer lands first
            s.store.append_annotation({"source_ref": ref, "source_version": proj.source_version,
                                       "expected_annotation_revision": 0, "idempotency_key": "other-writer-1",
                                       "annotation": {"summary": "other"}})
        return s(name, args)
    clock = Clock()
    res = L.run_turn(racing, FA.annotate, budget(), clock=clock, sleep=clock.sleep)
    # the other writer's revision has no chunk notes, so the page is offered again at revision 1
    assert res.stopped == "done" and [r["annotation_revision"] for r in res.receipts] == [2]

    s2 = Server()
    s2.add(ref, FA_SHORT + "\n")
    n = {"i": 0}

    def always_racing(name, args):
        if name == "brain_enrichment_submit":
            n["i"] += 1
            s2.store.append_annotation({"source_ref": ref, "source_version": proj.source_version,
                                        "expected_annotation_revision": n["i"] - 1,
                                        "idempotency_key": f"other-writer-{n['i']}", "annotation": {"summary": "o"}})
        return s2(name, args)
    res2 = L.run_turn(always_racing, FA.annotate, budget(), clock=clock, sleep=clock.sleep)
    assert res2.stopped == "conflict_limit" and res2.pages == 0


def test_changed_idempotency_content_is_reported_not_worked_around():
    ref = "cap:cap-20261001-120000-ab12"
    s = Server()
    s.add(ref, FA_SHORT + "\n")
    proj = s.store.projections[ref]
    item_args = s._pending({"limit": 1})["items"][0]
    key = L.idempotency_key(ref, proj.source_version, 0, item_args["page"]["offset"])
    s.store.append_annotation({"source_ref": ref, "source_version": proj.source_version,
                               "expected_annotation_revision": 0, "idempotency_key": key,
                               "annotation": {"summary": "different content"}})
    s.store.revisions[ref].clear()                                  # keep the receipt, forget the revision
    res, _ = run(s)
    assert res.stopped == "idempotency_conflict" and res.receipts == []


def test_server_disabled_writes_stop_the_turn():
    s = Server()
    s.add("cap:cap-1", "words words words words\n")
    s.svc = AnnotationService(s.store, config=EnrichmentConfig(mode="on", writes=False))
    res, _ = run(s)
    assert res.stopped == "not_enabled" and res.receipts == []


def test_pending_error_stops_without_retry():
    def broken(name, args):
        return {"ok": False, "error": {"code": "E_DISABLED", "message": "off"}}
    res = L.run_turn(broken, FA.annotate, budget(), clock=Clock(), sleep=lambda s: None)
    assert res.stopped == "pending_error" and res.detail == {"code": "E_DISABLED"} and res.annotator_calls == 0


# ------------------------------------------------------ capture / ingest policy
def test_unconfirmed_transcript_is_never_captured():
    s = Server()
    for confirmed in (False, None, "yes", 1, "true"):
        with pytest.raises(L.PolicyViolation):
            L.capture_text(s, "متن پیاده‌شده", origin="transcript", user_confirmed=confirmed)
    with pytest.raises(L.PolicyViolation):
        L.capture_text(s, "متن پیاده‌شده", origin="transcript")
    assert s.calls == [] and s.captures == []
    ok = L.capture_text(s, "متن تأییدشده", origin="transcript", user_confirmed=True, language_hint="fa")
    assert ok["persisted"] and s.captures == [{"text": "متن تأییدشده", "language_hint": "fa"}]


@pytest.mark.parametrize("origin", ["assistant", "error", "provider_error", "summary", "", None])
def test_only_the_users_own_words_can_be_captured(origin):
    s = Server()
    with pytest.raises(L.PolicyViolation):
        L.capture_text(s, "Error 402: credit exhausted", origin=origin, user_confirmed=True)
    assert s.calls == []


def test_capture_rejects_blank_text_and_bad_language_hint():
    s = Server()
    for text, hint in (("  \n", "fa"), ("", "fa"), (None, "fa"), ("متن", "klingon")):
        with pytest.raises(L.PolicyViolation):
            L.capture_text(s, text, origin="user_typed", language_hint=hint)
    assert s.calls == []


def test_file_ingest_needs_confirmation_and_a_real_upload_ref():
    s = Server()
    for confirmed in (False, None, "yes"):
        with pytest.raises(L.PolicyViolation):
            L.ingest_uploaded_file(s, UPLOAD_REF, user_confirmed=confirmed)
    for bad in ("upl-short", "/tmp/file.pdf", "", None, "upl-" + "A" * 44):
        with pytest.raises(L.PolicyViolation):
            L.ingest_uploaded_file(s, bad, user_confirmed=True)
    assert s.calls == []
    assert L.ingest_uploaded_file(s, UPLOAD_REF, user_confirmed=True)["persisted"]
    assert s.ingests == [{"upload_ref": UPLOAD_REF}]


def test_the_loop_cannot_call_capture_ingest_or_anything_outside_its_allow_list():
    tree = ast.parse((HERMES / "loop.py").read_text(encoding="utf-8"))
    uses = {}
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        for node in ast.walk(fn):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("brain_"):
                uses.setdefault(node.value, set()).add(fn.name)
    assert uses == {"brain_capture": {"capture_text"}, "brain_ingest_file": {"ingest_uploaded_file"}}
    assert set(L.ALLOWED_TOOLS) == {"brain_enrichment_pending", "brain_enrichment_submit", "brain_artifact_read"}

    # the guard inside run_turn: even a (hypothetical) programming error cannot reach another tool
    src = (HERMES / "loop.py").read_text(encoding="utf-8")
    assert "if name not in ALLOWED_TOOLS" in src and "raise PolicyViolation" in src


def test_policy_text_requires_confirmation_before_capture():
    cap = (HERMES / "prompts" / "capture_policy.md").read_text(encoding="utf-8").lower()
    for phrase in ("show the transcript", "never auto-capture an unconfirmed transcript", "confirm",
                   "upload_ref", "brain_ingest_file", "never capture it first",
                   "do not capture your own replies, status messages, provider or tool errors"):
        assert phrase in cap, phrase
    skill = (HERMES / "SKILL.md").read_text(encoding="utf-8")
    assert "Never call `brain_capture` from the loop" in skill and "captured only after the" in skill
    assert re.search(r"never (write an )?error message", skill, re.I) or "Never write an error message" in skill


def test_retrieval_policy_text():
    t = (HERMES / "prompts" / "retrieval_policy.md").read_text(encoding="utf-8")
    for needle in ("brain_retrieve", "scope `all`", "brain_search", "brain_read", "never quotes", "cite"):
        assert needle in t, needle


# ------------------------------------------------------------- shipped files
def test_skill_loop_and_stop_rules_are_stated():
    skill = (HERMES / "SKILL.md").read_text(encoding="utf-8")
    assert skill.startswith("---\n") and "\nname: 0xbrain-annotator\n" in skill.split("\n---\n")[0]
    for needle in ("limit\": 1", "idempotency_key", "expected_annotation_revision", "source_version",
                   "402", "429", "503", "2 attempts", "No retry loop", "Never ask the owner to approve",
                   "never review", "producer_reported", "next_cursor", "chunk by chunk", "daily cost cap"):
        assert needle.lower() in " ".join(skill.lower().split()), needle
    assert re.search(r"never\s+review,\s*approve", skill, re.I)


def _tool_names(text: str) -> set[str]:
    return set(re.findall(r"\bbrain_[a-z_]+\b", text))


def test_tool_names_in_the_harness_files_exist():
    known = set(T.EXTENSION_TOOL_NAMES) | set(BC.TOOL_NAMES)
    for f in (HERMES / "SKILL.md", *(HERMES / "prompts").glob("*.md"), HERMES / "job.template.yaml",
              HERMES / "loop.py", HERMES / "install.sh", ROOT / "docs" / "enrichment" / "hermes.md"):
        names = _tool_names(f.read_text(encoding="utf-8"))
        assert names <= known, (f.name, names - known)
    assert _tool_names((HERMES / "SKILL.md").read_text(encoding="utf-8")) >= \
        {"brain_enrichment_pending", "brain_enrichment_submit", "brain_artifact_read", "brain_capture",
         "brain_ingest_file", "brain_retrieve", "brain_search", "brain_read"}
    # the SKILL never mentions a review/approve/promote tool
    assert not [n for n in _tool_names((HERMES / "SKILL.md").read_text(encoding="utf-8"))
                if re.search(r"review|approve|promote|accept", n)]


def _walk_keys(obj, path=()):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield (*path, str(k))
            yield from _walk_keys(v, (*path, str(k)))
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_keys(v, path)


def test_job_template_is_disabled_bounded_and_has_no_credential_fields():
    raw = (HERMES / "job.template.yaml").read_text(encoding="utf-8")
    job = yaml.safe_load(raw)
    assert job["enabled"] is False and re.search(r"(?m)^enabled: false\b", raw)
    assert job["concurrency"] == 1 and job["batch_size"] == 1
    assert 0 < job["timeout_seconds"] <= 600 and job["max_pages_per_turn"] >= 1
    assert 0 < job["cost"]["max_usd_per_day"] and 0 < job["cost"]["max_usd_per_turn"] <= job["cost"]["max_usd_per_day"]
    assert job["retry"]["max_attempts_transient"] == 2 and job["retry"]["on_credit_exhausted"] == "stop"
    assert set(job["tools"]["allow"]) == set(L.ALLOWED_TOOLS) and set(job["tools"]["allow"]) <= set(T.EXTENSION_TOOL_NAMES)
    assert {"shell", "docker", "filesystem"} <= set(job["tools"]["deny"])
    bad = [p for p in _walk_keys(job) if re.search(r"key|token|secret|password|credential|auth|bearer|env", p[-1], re.I)]
    assert not bad, bad


def test_no_secret_or_api_key_anywhere_in_the_harness_package():
    files = [p for p in HERMES.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    files.append(ROOT / "docs" / "enrichment" / "hermes.md")
    assert len(files) >= 9
    pat = re.compile(r"(api[_-]?key|secret|password|passwd|bearer|access[_-]?token|auth[_-]?token)\s*[:=]\s*[\"']?[A-Za-z0-9]", re.I)
    for f in files:
        text = f.read_text(encoding="utf-8")
        assert not pat.search(text), f.name
        assert not re.search(r"\bsk-[A-Za-z0-9_-]{12,}", text), f.name
        assert not re.search(r"(ANTHROPIC|OPENAI|OPENROUTER)_API_KEY\s*=", text), f.name
    # no model SDK, no network client, no subprocess in the package code
    for f in HERMES.glob("*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        mods = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | \
               {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        assert not mods & {"anthropic", "openai", "requests", "httpx", "urllib", "socket", "subprocess", "os", "shutil"}, (f.name, mods)


# ---------------------------------------------------------------- install.sh
def _tree(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "dir"
            for p in sorted(root.rglob("*"))}


def _install(*args):
    bash = shutil.which("bash")
    assert bash, "bash is required to test install.sh"
    return subprocess.run([bash, str(HERMES / "install.sh"), *args], capture_output=True, text=True, timeout=60)


def test_install_defaults_to_dry_run_and_writes_nothing(tmp_path):
    home = tmp_path / "hermes-home"
    home.mkdir()
    (home / "keep.txt").write_text("existing\n")
    before = _tree(tmp_path)
    r = _install("--hermes-home", str(home), "--account", "owner-1")
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("DRY-RUN") and "would write:" in r.stdout and "wrote:" not in r.stdout
    assert "0xbrain-annotator.yaml" in r.stdout and "skills/0xbrain-annotator/SKILL.md" in r.stdout
    assert _tree(tmp_path) == before                                        # byte-for-byte untouched


def test_install_apply_installs_only_skill_and_a_disabled_job(tmp_path):
    home = tmp_path / "hermes-home"
    home.mkdir()
    r = _install("--hermes-home", str(home), "--account", "owner-1", "--apply")
    assert r.returncode == 0, r.stderr
    got = sorted(k for k, v in _tree(home).items() if v != "dir")
    assert got == ["accounts/owner-1/jobs/0xbrain-annotator.yaml", "skills/0xbrain-annotator/SKILL.md",
                   "skills/0xbrain-annotator/prompts/annotate.md", "skills/0xbrain-annotator/prompts/capture_policy.md",
                   "skills/0xbrain-annotator/prompts/retrieval_policy.md"]
    assert yaml.safe_load((home / "accounts/owner-1/jobs/0xbrain-annotator.yaml").read_text())["enabled"] is False
    # a second run never overwrites; an enabled job is never replaced, not even with --force
    skill = home / "skills/0xbrain-annotator/SKILL.md"
    skill.write_text("local edits\n")
    r2 = _install("--hermes-home", str(home), "--account", "owner-1", "--apply")
    assert "skip (exists" in r2.stdout and skill.read_text() == "local edits\n"
    job = home / "accounts/owner-1/jobs/0xbrain-annotator.yaml"
    job.write_text("enabled: true\nname: mine\n")
    _install("--hermes-home", str(home), "--account", "owner-1", "--apply", "--force")
    assert job.read_text() == "enabled: true\nname: mine\n"
    _install("--hermes-home", str(home), "--account", "owner-1", "--apply", "--force")
    assert skill.read_text() != "local edits\n"                                     # --force refreshed the skill only


@pytest.mark.parametrize("args", [
    [], ["--hermes-home", "/tmp"], ["--account", "x"],
    ["--hermes-home", "relative/dir", "--account", "x"], ["--hermes-home", "/", "--account", "x"],
    ["--hermes-home", "/tmp/../etc", "--account", "x"], ["--hermes-home", "{home}", "--account", "bad/name"],
    ["--hermes-home", "{home}", "--account", ""], ["--hermes-home", "{home}/missing", "--account", "x"],
    ["--hermes-home", "{home}", "--account", "x", "--bogus"],
])
def test_install_refuses_bad_arguments_and_writes_nothing(tmp_path, args):
    home = tmp_path / "home"
    home.mkdir()
    before = _tree(tmp_path)
    r = _install(*[a.format(home=home) for a in args], *([] if "--apply" in args else ["--apply"]))
    assert r.returncode != 0
    assert _tree(tmp_path) == before


def test_install_script_has_no_package_manager_docker_network_or_destructive_commands():
    src = (HERMES / "install.sh").read_text(encoding="utf-8")
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    for banned in (r"\bpip3?\b", r"\bdocker\b", r"\bcompose\b", r"\bsudo\b", r"\bcurl\b", r"\bwget\b", r"\brm\b",
                   r"chmod\s+-R", r"\bchown\b", r"\bgit\b", r"\bnpm\b", r"\bapt(-get)?\b", r"\beval\b", r"\bsource\b"):
        assert not re.search(banned, code), banned
    assert "set -euo pipefail" in src and "enabled: false" in src
    assert "--apply" in src and "DRY-RUN" in src
    assert (HERMES / "install.sh").stat().st_mode & 0o111


# --------------------------------------------------- prompts and fake annotator
def _json_blocks(md: str) -> list:
    return [json.loads(b) for b in re.findall(r"```json\n(.*?)\n```", md, re.S)]


def test_annotate_prompt_worked_example_is_exactly_the_shared_fixture_and_validates():
    md = (HERMES / "prompts" / "annotate.md").read_text(encoding="utf-8")
    page, answer = _json_blocks(md)[:2]
    assert answer == json.loads((FIX / "example_annotation.json").read_text(encoding="utf-8"))
    jsonschema.validate(answer, T.load_schema("annotation.schema.json"))
    ch = page["page"]["chunks"][0]
    assert "‌" in ch["text"] and "‌" in answer["links"][0]["evidence"][0]["quote"]   # ZWNJ survives in the doc
    # run the example through the real service: exact substrings, refs, entity declared
    proj = make_projection(page["source_ref"], [("user-text", page["source_ref"], ch["text"], None)])
    proj = type(proj)(**{**proj.__dict__, "source_version": page["source_version"]})
    dg = "579361262bfb6bf54ac16b115738eeaf" + "0" * 32
    chunk = C.Chunk(ch["chunk_ref"], dg, proj.source_ref, proj.source_version, proj.layers[0].text_ref,
                    "user-text", 0, 0, len(ch["text"]), ch["text"])
    store = FakeProjectionStore()
    store.upsert_projection(proj, [chunk])
    svc = AnnotationService(store, config=ON)
    r = svc.submit({"source_ref": proj.source_ref, "source_version": proj.source_version,
                    "expected_annotation_revision": page["expected_annotation_revision"],
                    "idempotency_key": "key-prompt-example", "annotation": answer})
    assert r.annotation_revision == 1
    for rule in ("character for", "ي", "zero-width", "one line", "producer_reported", "candidate", "authority"):
        assert rule in md, rule


def _items_from(text: str, size: int):
    proj = make_projection("cap:cap-fixture-0001", [("user-text", "cap:cap-fixture-0001", text, None)])
    cuts = list(range(size, len(text), size))
    chunks = chunks_for(proj, cuts)
    return proj, chunks, {"source_ref": proj.source_ref, "source_version": proj.source_version,
                          "page": {"chunks": [{"chunk_ref": c.chunk_ref, "text": c.text, "annotated": False}
                                              for c in chunks[:20]], "offset": 0, "returned": min(20, len(chunks)),
                                   "total": len(chunks), "truncated": len(chunks) > 20}}


@pytest.mark.parametrize("name", ["fa_short", "crlf", "escaped", "dup", "long"])
def test_fake_annotator_output_is_schema_valid_deterministic_and_accepted_by_the_service(name):
    from support import CRLF_TEXT, DUP_TEXT, ESCAPED_HEADINGS, long_text
    text = {"fa_short": FA_SHORT + "\n", "crlf": CRLF_TEXT, "escaped": ESCAPED_HEADINGS,
            "dup": DUP_TEXT + "\n", "long": long_text()}[name]
    proj, chunks, item = _items_from(text, 3000 if name == "long" else 4000)
    out = FA.annotate(item)
    assert out == FA.annotate(copy.deepcopy(item))                                  # deterministic
    jsonschema.validate(out, T.load_schema("annotation.schema.json"))
    assert len(json.dumps(out, ensure_ascii=False).encode()) < 65536
    store = FakeProjectionStore()
    store.upsert_projection(proj, chunks)
    r = AnnotationService(store, config=ON).submit({
        "source_ref": proj.source_ref, "source_version": proj.source_version, "expected_annotation_revision": 0,
        "idempotency_key": "key-fake-annot", "annotation": out})
    assert r.annotation_revision == 1
    if name == "fa_short":
        assert [e["name"] for e in out["entities"]] == ["فیت پلن"] and out["links"][0]["relation"] == "about"
    if name == "long":
        assert "summary" not in out or len(chunks) <= 20          # summary only for whole-artifact pages


def test_fake_annotator_with_nothing_to_do_still_returns_a_valid_object():
    out = FA.annotate({"page": {"offset": 2, "returned": 1, "total": 5, "truncated": True,
                                "chunks": [{"chunk_ref": "chk:" + "0" * 32, "text": "x", "annotated": True}]}})
    jsonschema.validate(out, T.load_schema("annotation.schema.json"))
    assert out["uncertainty"]


def test_docs_hermes_md_exists_and_makes_no_compatibility_claim():
    doc = (ROOT / "docs" / "enrichment" / "hermes.md").read_text(encoding="utf-8")
    low = doc.lower()
    for needle in ("install.sh", "--hermes-home", "--account", "dry-run", "enabled: false", "daily cost cap",
                   "compatibility checklist", "python", "yaml", "oauth", "not verified", "402", "429", "503"):
        assert needle in low, needle
    assert not re.search(r"\b(is|are)\s+(known to be\s+)?(compatible|working)\b|\bworks with\b", low)
