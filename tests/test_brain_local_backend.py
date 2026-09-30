"""LocalBackend adapter against a scratch repository (no qmd, no network)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from brain_surface import BrainSurface  # noqa: E402
from brain_surface import local_backend as lb  # noqa: E402

QUOTE = "The manuscript called Nil was first drafted in 2019."


def rec(id_, title, body, extra=""):
    return f"---\nid: {id_}\ntype: object\ntitle: {title}\nstatus: active\n{extra}---\n\n# {title}\n\n{body}\n"


@pytest.fixture()
def corpus(tmp_path, monkeypatch):
    for d in ("02-sources", "03-objects", "05-claims", "07-genesis",
              "_proposals", "_captures", "00-system/policies"):
        (tmp_path / d).mkdir(parents=True)
    (tmp_path / "00-system/policies/proposal_schema.json").write_text(
        (ROOT / "00-system/policies/proposal_schema.json").read_text())
    (tmp_path / "03-objects/nil.md").write_text(rec("w-nil", "Nil", QUOTE + " See [[w-clm]]."))
    (tmp_path / "05-claims/clm.md").write_text(rec("w-clm", "Claim", "Title is fixed. About [[w-nil]] and [[missing-thing]]."))
    (tmp_path / "03-objects/old.md").write_text(rec("w-old", "Old", "Older formulation of [[w-nil]].", "relation_status: superseded\n"))
    (tmp_path / "07-genesis/g1.md").write_text("Genesis note that mentions w-nil.\n")
    (tmp_path / "_proposals/proposals.jsonl").write_text(json.dumps(
        {"id": "prop-1", "kind": "object-note", "status": "new",
         "body": "about w-nil"}) + "\n")
    monkeypatch.setattr(lb.wms, "ROOT", tmp_path)
    monkeypatch.setattr(lb.wms, "PROPOSALS_DIR", tmp_path / "_proposals")
    monkeypatch.setattr(lb.wms, "PROPOSALS_LOG", tmp_path / "_proposals/proposals.jsonl")
    monkeypatch.setattr(lb.cap, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(lb.cap, "CAPTURES_ROOT", tmp_path / "01-inbox/captures")
    monkeypatch.setattr(lb.cap, "ORPHAN_QUARANTINE", tmp_path / "01-inbox/captures/.quarantine")
    backend = lb.LocalBackend(tmp_path, search_fn=lambda q, n: [
        {"file": "qmd://wiki/nil.md", "score": 0.9, "title": "Nil"}]
        if "nil" in q.lower() else [])
    kinds = __import__("brain_surface.contract", fromlist=["x"]).remote_proposal_kinds(
        tmp_path / "00-system/policies/proposal_schema.json")
    return tmp_path, BrainSurface(backend, proposal_kinds=kinds)


def test_search_read_exact_and_scopes(corpus):
    root, s = corpus
    r = s.call("brain_search", {"query": "Nil", "scope": "all"})
    assert r["groups"]["canonical"]["results"][0]["ref"] == "rec:w-nil"
    ex = s.call("brain_search", {"query": "first drafted", "mode": "exact"})
    assert ex["groups"]["canonical"]["results"][0]["ref"] == "rec:w-nil"
    assert ex["groups"]["canonical"]["results"][0]["line"] >= 1
    rd = s.call("brain_read", {"ref": "rec:w-nil"})
    assert QUOTE in rd["content"] and rd["authority_level"] == 4
    assert s.call("brain_read", {"ref": "rec:../../etc/passwd"})["error"]["code"] == "E_BAD_REF"
    assert s.call("brain_read", {"ref": "rec:ghost"})["error"]["code"] == "E_NOT_FOUND"


def test_capture_roundtrip_is_verbatim_channel_mcp_and_searchable(corpus):
    root, s = corpus
    c = s.call("brain_capture", {"text": "پروژه نیل تا پایان دست‌نویس ثابت می‌ماند", "language_hint": "fa"})
    assert c["ok"] and c["persisted"] and c["commit_state"] == "not_attempted"
    ref = c["ref"]
    rd = s.call("brain_read", {"ref": ref})
    assert "پروژه نیل" in rd["content"] and rd["authority_level"] == 7
    assert rd["metadata"]["language_hint"] == "fa"
    text = next((root / "01-inbox/captures").rglob("*.md")).read_text(encoding="utf-8")
    assert "capture_channel: mcp" in text
    hit = s.call("brain_search", {"query": "دست‌نویس", "scope": "captures"})
    assert hit["groups"]["captures"]["results"][0]["ref"] == ref
    dup = s.call("brain_capture", {"text": "پروژه نیل تا پایان دست‌نویس ثابت می‌ماند", "language_hint": "fa"})
    assert dup["duplicate_of"] == ref
    # captures never appear in the canonical group
    assert s.call("brain_search", {"query": "دست‌نویس", "scope": "canonical"})["groups"]["canonical"]["count"] == 0


def test_propose_verbatim_evidence_written_in_audit_compatible_shape(corpus):
    root, s = corpus
    ok = s.call("brain_propose", {
        "kind": "object-note",
        "structured_fields": {"object_id": "w-nil", "note": "Drafting year is a fact worth noting.", "why": "dating"},
        "evidence_refs": [{"ref": "rec:w-nil", "quote": QUOTE}]})
    assert ok["ok"], ok
    lines = (root / "_proposals/proposals.jsonl").read_text().splitlines()
    rec_ = json.loads(lines[-1])
    assert rec_["authority_tier"] == "candidate" and rec_["status"] == "new"
    body = json.loads(rec_["body"])
    assert body["source_passage"] == {"path": "03-objects/nil.md", "quote": QUOTE}
    # the existing audit accepts what the surface writes
    import evidence_audit as ea
    schema = json.loads((root / "00-system/policies/proposal_schema.json").read_text())
    assert not ea._check_passage(root, body["source_passage"])
    assert set(schema["kinds"]["object-note"]["required"]) - {"source_passage"} <= set(body)
    bad = s.call("brain_propose", {
        "kind": "object-note",
        "structured_fields": {"object_id": "w-nil", "note": "n", "why": "w"},
        "evidence_refs": [{"ref": "rec:w-nil", "quote": "a paraphrase that is not in the record"}]})
    assert bad["ok"] is False and len(lines) == len((root / "_proposals/proposals.jsonl").read_text().splitlines())


def test_reconcile_sections_reasons_and_no_verdict(corpus):
    root, s = corpus
    r = s.call("brain_reconcile_context", {"seed_ref": "rec:w-nil", "depth": "focused"})
    assert r["ok"] and r["verdict"] is None
    sec = r["sections"]
    assert [i["id"] for i in sec["canonical_records"]["items"]] == ["w-nil"]
    assert [i["id"] for i in sec["claims"]["items"]] == ["w-clm"]
    assert [i["id"] for i in sec["superseded"]["items"]] == ["w-old"]
    assert [i["id"] for i in sec["open_proposals"]["items"]] == ["prop-1"]
    assert [i["id"] for i in sec["chronology"]["items"]] == ["g1"]
    assert any("missing-thing" in i["id"] for i in sec["unresolved"]["items"])
    assert all(i["reason"] for x in sec.values() for i in x["items"])
    seed = sec["canonical_records"]["items"][0]
    assert seed["reason"] == "seed" and seed["distance"] == 0


def test_status_reports_needs_and_uncommitted_truthfully(corpus):
    root, s = corpus
    s.call("brain_capture", {"text": "a note"})
    st = s.call("brain_status", {})
    assert st["counts"]["canonical_records"] == 3
    assert st["counts"]["captures_by_state"] == {"received": 1}
    assert st["validator"]["state"] == "not_checked"
    assert st["proposals"] == {"new": 1}
    assert st["missing"] == []
