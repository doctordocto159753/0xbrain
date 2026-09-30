"""Frozen-K3 surface tests: contract exactness, validation, authority rules,
K5 envelope, and the local adapter against a scratch corpus."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from brain_surface import BrainSurface, TOOLS, TOOL_NAMES  # noqa: E402
from brain_surface import contract as C  # noqa: E402
from brain_surface.backend import FakeBackend  # noqa: E402


def make(page_limit=40):
    b = FakeBackend()
    b.add_record("obj-a", "Alpha", "Alpha object. The quick brown fox jumps over.",
                 edges=["obj-b"])
    b.add_record("obj-b", "Beta", "Beta object relates to alpha.", zone="04-notes",
                 level=6)
    b.add_capture("cap-fake-9000", "note mentioning alpha in a capture")
    return BrainSurface(b, page_limit=page_limit), b


# ------------------------------------------------------------ K3 exactness
def test_tool_names_and_args_are_exactly_k3():
    expect = {
        "brain_search": {"query", "mode", "scope", "n"},
        "brain_read": {"ref"},
        "brain_capture": {"text", "language_hint"},
        "brain_reconcile_context": {"seed_ref", "query", "depth", "cursor",
                                    "sections", "expand"},
        "brain_propose": {"kind", "structured_fields", "evidence_refs"},
        "brain_status": set(),
    }
    assert list(TOOL_NAMES) == list(expect)
    for t in TOOLS:
        assert set(t["inputSchema"]["properties"]) == expect[t["name"]]
        assert t["inputSchema"]["additionalProperties"] is False


def test_no_review_or_path_tool_or_argument():
    for t in TOOLS:
        assert "review" not in t["name"] and "media" not in t["name"]
        assert not {"path", "channel", "actor"} & set(t["inputSchema"]["properties"])


def test_enums_match_frozen_values():
    p = {t["name"]: t["inputSchema"]["properties"] for t in TOOLS}
    assert p["brain_search"]["mode"]["enum"] == ["lexical", "exact"]
    assert p["brain_search"]["scope"]["enum"] == ["canonical", "captures", "all"]
    assert p["brain_reconcile_context"]["depth"]["enum"] == ["focused", "deep"]


def test_propose_kinds_derive_from_shipped_schema_minus_intake():
    schema = json.loads((ROOT / "00-system/policies/proposal_schema.json").read_text())
    kinds = set(C.remote_proposal_kinds())
    assert kinds == set(schema["kinds"]) - {"intake-registration"}


# -------------------------------------------------------------- arguments
@pytest.mark.parametrize("bad", ["../etc/passwd", "03-objects/a.md", "rec:../x",
                                 "rec:a/b", "C:\\x", "rec:a.md", "", "obj-a",
                                 "rec:", "file:rec:a"])
def test_read_rejects_paths_and_malformed_refs(bad):
    s, _ = make()
    r = s.call("brain_read", {"ref": bad})
    assert r["ok"] is False and r["error"]["code"] == "E_BAD_REF"


def test_unknown_and_forbidden_arguments_rejected():
    s, _ = make()
    for name, args in [("brain_read", {"ref": "rec:obj-a", "path": "x.md"}),
                       ("brain_capture", {"text": "x", "channel": "telegram-hermes"}),
                       ("brain_status", {"verbose": True})]:
        r = s.call(name, args)
        assert r["error"]["code"] == "E_UNKNOWN_ARGUMENT"
    assert s.call("nope", {})["error"]["code"] == "E_UNKNOWN_TOOL"


def test_search_validation_and_grouping():
    s, _ = make()
    assert s.call("brain_search", {"query": "alpha", "scope": "bogus"})["ok"] is False
    assert s.call("brain_search", {"query": "alpha", "mode": "semantic"})["ok"] is False
    r = s.call("brain_search", {"query": "alpha", "scope": "all"})
    assert set(r["groups"]) == {"canonical", "captures"}
    assert all(h["ref"].startswith("rec:") for h in r["groups"]["canonical"]["results"])
    assert all(h["tier"] == "candidate" and h["authority_level"] == 7
               for h in r["groups"]["captures"]["results"])
    only = s.call("brain_search", {"query": "alpha"})
    assert set(only["groups"]) == {"canonical"}


def test_search_zero_hits_notes_not_absence():
    s, _ = make()
    r = s.call("brain_search", {"query": "zzzz"})
    assert r["ok"] and r["groups"]["canonical"]["count"] == 0
    assert any("absence" in n for n in r["notes"])


def test_capture_leak_into_canonical_group_is_dropped():
    s, b = make()
    real = b.search
    b.search = lambda q, m, sc, n: {"canonical": [
        {"ref": "cap:cap-fake-9000", "zone": "01-inbox/captures"},
        {"ref": "rec:obj-a", "zone": "03-objects", "title": "Alpha"}]}
    r = s.call("brain_search", {"query": "alpha"})
    refs = [h["ref"] for h in r["groups"]["canonical"]["results"]]
    assert refs == ["rec:obj-a"] and any("wrong group" in n for n in r["notes"])
    b.search = real


def test_read_labels_authority_and_media_limits():
    s, b = make()
    b.add_capture("cap-fake-9100", "[photo]", media="image", needs=["needs_description"])
    r = s.call("brain_read", {"ref": "cap:cap-fake-9100"})
    assert r["authority_level"] == 7 and r["media"]["inspectable"] is False
    assert "base64" not in json.dumps(r)
    assert s.call("brain_read", {"ref": "rec:missing"})["error"]["code"] == "E_NOT_FOUND"


# ---------------------------------------------------------------- capture
def test_capture_verbatim_fixed_channel_and_honest_states():
    s, b = make()
    r = s.call("brain_capture", {"text": "  exact words\n", "language_hint": "fa"})
    assert r["ok"] and r["persisted"] and r["ref"].startswith("cap:")
    assert b.captures[r["ref"][4:]]["text"] == "  exact words\n"  # no rewrite
    b.commit_state = "uncommitted"
    assert s.call("brain_capture", {"text": "again"})["commit_state"] == "uncommitted"
    dup = s.call("brain_capture", {"text": "exact words"})  # different text
    assert dup["duplicate_of"] is None
    assert s.call("brain_capture", {"text": "  exact words\n"})["duplicate_of"]


@pytest.mark.parametrize("args", [{"text": ""}, {"text": "   "},
                                  {"text": "x", "language_hint": "de"},
                                  {"text": 5}])
def test_capture_rejects_bad_input_and_reports_not_persisted(args):
    s, b = make()
    r = s.call("brain_capture", args)
    assert r["ok"] is False and r["persisted"] is False and len(b.captures) == 1


def test_capture_too_large_and_backend_outage_not_persisted():
    s, b = make()
    r = s.call("brain_capture", {"text": "x" * (500_001)})
    assert r["error"]["code"] == "E_TOO_LARGE" and r["persisted"] is False
    b.unavailable.add("capture")
    r = s.call("brain_capture", {"text": "hello"})
    assert r["error"]["code"] == "E_UNAVAILABLE" and r["persisted"] is False


def test_capture_unconfirmed_persistence_is_error():
    s, b = make()
    b.capture = lambda t, l: {"ref": "cap:x", "persisted": False}
    r = s.call("brain_capture", {"text": "hi"})
    assert r["ok"] is False and r["persisted"] is False


# ---------------------------------------------------------------- propose
GOOD = {"kind": "relation-edge",
        "structured_fields": {"target_id": "obj-b", "proposed_type": "unclassified",
                              "why": "Alpha and beta appear together."},
        "evidence_refs": [{"ref": "rec:obj-a", "quote": "The quick brown fox jumps over."}]}


def test_propose_success_is_candidate_and_stores_structured_body():
    s, b = make()
    r = s.call("brain_propose", GOOD)
    assert r["ok"] and r["authority_tier"] == "candidate" and r["ref"].startswith("prop:")
    rec = b.proposals[0]
    assert rec["authority_tier"] == "candidate" and rec["status"] == "new"
    assert rec["body"]["source_passage"]["quote"] == GOOD["evidence_refs"][0]["quote"]
    assert "path" in rec["body"]["source_passage"]  # server-resolved, not caller-given


@pytest.mark.parametrize("mut,frag", [
    (lambda a: a.update(kind="wiki-edit"), "unsupported kind"),
    (lambda a: a.update(kind="intake-registration"), "unsupported kind"),
    (lambda a: a["structured_fields"].pop("why"), "missing required field: why"),
    (lambda a: a["structured_fields"].update(extra="x"), "unknown field"),
    (lambda a: a.update(evidence_refs=[]), "at least one item"),
    (lambda a: a["evidence_refs"][0].update(quote="too short"), ">= 20 chars"),
    (lambda a: a["evidence_refs"][0].update(quote="a paraphrased sentence, not verbatim"), "NOT found verbatim"),
    (lambda a: a["evidence_refs"][0].update(ref="rec:ghost"), "unknown ref"),
    (lambda a: a["evidence_refs"][0].update(ref="/etc/passwd"), "not a valid ref"),
    (lambda a: a["evidence_refs"][0].update(ref="cap:cap-fake-9000",
        quote="note mentioning alpha"), "captures alone do not count"),
])
def test_propose_rejects_and_stores_nothing(mut, frag):
    import copy
    s, b = make()
    a = copy.deepcopy(GOOD)
    mut(a)
    r = s.call("brain_propose", a)
    assert r["ok"] is False and r["persisted"] is False
    assert frag in json.dumps(r["error"])
    assert b.proposals == []


def test_propose_capture_supporting_evidence_kept_separate():
    import copy
    s, b = make()
    a = copy.deepcopy(GOOD)
    a["evidence_refs"].append({"ref": "cap:cap-fake-9000",
                               "quote": "note mentioning alpha"})
    assert s.call("brain_propose", a)["ok"]
    body = b.proposals[0]["body"]
    assert body["supporting_captures"][0]["ref"] == "cap:cap-fake-9000"
    assert len(body.get("additional_passages", [])) == 0


def test_tier_change_enum_enforced():
    s, _ = make()
    r = s.call("brain_propose", {
        "kind": "tier-change",
        "structured_fields": {"target_id": "obj-a", "from_tier": "held",
                              "to_tier": "canonical", "why": "x"},
        "evidence_refs": GOOD["evidence_refs"]})
    assert r["ok"] is False and "to_tier" in json.dumps(r["error"])


# -------------------------------------------------------------- reconcile
def test_reconcile_requires_seed_or_query_and_valid_args():
    s, _ = make()
    assert s.call("brain_reconcile_context", {})["ok"] is False
    assert s.call("brain_reconcile_context", {"seed_ref": "rec:obj-a", "depth": "x"})["ok"] is False
    assert s.call("brain_reconcile_context", {"seed_ref": "rec:obj-a", "sections": ["bogus"]})["ok"] is False
    assert s.call("brain_reconcile_context", {"seed_ref": "rec:ghost"})["error"]["code"] == "E_NOT_FOUND"


def test_focused_is_direct_neighborhood_no_verdict():
    s, b = make()
    b.add_record("obj-c", "Gamma", "far", edges=["obj-b"])
    r = s.call("brain_reconcile_context", {"seed_ref": "rec:obj-a", "depth": "focused"})
    ids = {i["id"] for sec in r["sections"].values() for i in sec["items"]}
    assert ids == {"obj-a", "obj-b"} and r["verdict"] is None and r["hops"] == 1
    deep = s.call("brain_reconcile_context", {"seed_ref": "rec:obj-a", "depth": "deep"})
    ids = {i["id"] for sec in deep["sections"].values() for i in sec["items"]}
    assert "obj-c" in ids
    for sec in deep["sections"].values():
        for it in sec["items"]:
            assert {"id", "ref", "authority_level", "reason"} <= set(it)


def test_pagination_order_totals_and_cursor_contract():
    s, b = make(page_limit=3)
    for i in range(8):
        b.add_record(f"n{i}", f"N{i}", "x", zone="04-notes", level=6, edges=["obj-a"])
    seen, cursor, pages = [], None, 0
    while True:
        args = {"seed_ref": "rec:obj-a", "depth": "deep"}
        if cursor:
            args["cursor"] = cursor
        r = s.call("brain_reconcile_context", args)
        assert r["ok"]
        sec = r["sections"]["canonical_records"]
        assert sec["total"] == 10 and sec["returned"] == len(sec["items"]) <= 3
        seen += [i["id"] for i in sec["items"]]
        pages += 1
        if r["truncated"]:
            assert r["next_cursor"] and r["truncation"][0]["reason"]
        else:
            assert r["next_cursor"] is None
        cursor = r["next_cursor"]
        if not cursor:
            break
    assert pages == 4 and len(seen) == len(set(seen)) == 10
    # deterministic order: authority level, then distance, then id
    lv = [b.records[i]["level"] for i in seen]
    assert lv == sorted(lv)


def test_cursor_bound_to_params_and_snapshot():
    s, b = make(page_limit=1)
    b.add_record("obj-c", "C", "x", edges=["obj-a"])
    r = s.call("brain_reconcile_context", {"seed_ref": "rec:obj-a"})
    cur = r["next_cursor"]
    assert cur
    assert s.call("brain_reconcile_context", {"seed_ref": "rec:obj-b", "cursor": cur})["error"]["code"] == "E_STALE_CURSOR"
    assert s.call("brain_reconcile_context", {"seed_ref": "rec:obj-a", "cursor": "!!"})["error"]["code"] == "E_BAD_CURSOR"
    b.snapshot = "changed"
    assert s.call("brain_reconcile_context", {"seed_ref": "rec:obj-a", "cursor": cur})["error"]["code"] == "E_STALE_CURSOR"


def test_sections_filter_and_expand():
    s, b = make()
    b.add_record("obj-z", "Z", "z", edges=["obj-q"])
    b.add_record("obj-q", "Q", "q")
    r = s.call("brain_reconcile_context", {"seed_ref": "rec:obj-a", "expand": ["rec:obj-z"],
                                            "sections": ["canonical_records"]})
    assert list(r["sections"]) == ["canonical_records"]
    ids = {i["id"] for i in r["sections"]["canonical_records"]["items"]}
    assert {"obj-z", "obj-q"} <= ids


def test_backend_shape_violation_is_reported_not_swallowed():
    s, b = make()
    b.reconcile = lambda *a, **k: {"snapshot": "s", "sections": {"claims": [{"id": "x"}]}}
    assert s.call("brain_reconcile_context", {"seed_ref": "rec:obj-a"})["error"]["code"] == "E_BACKEND_SHAPE"


# ----------------------------------------------------------------- status
def test_status_never_invents_and_flags_degraded():
    s, b = make()
    b.status = lambda: {"snapshot": "s", "counts": {}, "uncommitted": ["01-inbox/captures/x.md"]}
    r = s.call("brain_status", {})
    assert r["validator"] is None and "validator" in r["missing"] and r["degraded"] is True


def test_outage_never_raises():
    s, b = make()
    b.unavailable |= {"search", "read", "status"}
    for name, args in [("brain_search", {"query": "a"}), ("brain_read", {"ref": "rec:obj-a"}),
                       ("brain_status", {})]:
        assert s.call(name, args)["error"]["code"] == "E_UNAVAILABLE"
