"""Frozen-K3 surface over the integrated backend (Agent B surface wired to
C search, D capture, E governance). Every test runs against a real
throwaway wiki (git repo); nothing is faked.
"""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import commit_all, record

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from brain_surface import BrainSurface, TOOLS, TOOL_NAMES  # noqa: E402
from brain_surface import contract as C  # noqa: E402
from brain_surface import refs as R  # noqa: E402
from brain_surface.backend import WikiBackend  # noqa: E402

needs_qmd = pytest.mark.skipif(shutil.which("qmd") is None, reason="qmd not installed")

QUOTE = "Project Nil is the working title of the second novel."
CLAIM_QUOTE = "the novel's title is fixed as 'Nil' (permission: may-describe)"


def seed_corpus(root: Path) -> None:
    record(root, "03-objects/works/nil.md", "obj-nil", "Project Nil",
           QUOTE + " Decision of 2026-03: the title stays 'Nil' until the manuscript is complete.",
           links=["clm-nil"])
    record(root, "05-claims/clm-nil.md", "clm-nil", "Nil title claim",
           "Claim: " + CLAIM_QUOTE + ".", links=["obj-nil", "missing-target"])
    record(root, "06-relations/rel-nil.md", "rel-nil", "Nil relation", "Relates [[obj-nil]].")
    record(root, "03-objects/works/old.md", "obj-old", "Old formulation",
           "An older formulation of Nil.", links=["obj-nil"], status="superseded")
    record(root, "02-sources/records/src-kolan.md", "src-kolan", "Kolan letter",
           "Letter mentioning Kolan and Nil.")
    d = root / "02-sources/text/kolan-letter.extracted.md"
    d.parent.mkdir(parents=True, exist_ok=True)
    d.write_text("# Extracted from kolan.pdf\n\n> method: pymupdf | sha256: " + "a" * 64
                 + "\n\nThe ferry to Kolan left every Thursday before dawn.\n", encoding="utf-8")
    h = root / "07-genesis/handoffs/HANDOFF--2026-09-01--nil-decision.md"
    h.parent.mkdir(parents=True, exist_ok=True)
    h.write_text("Handoff: decided obj-nil title handling.\n", encoding="utf-8")
    commit_all(root, "corpus")


@pytest.fixture()
def env(brain_wiki):
    seed_corpus(brain_wiki)
    return brain_wiki, BrainSurface(WikiBackend(brain_wiki))


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, text=True, capture_output=True,
                          check=True).stdout


def walk(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, v
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)


# ------------------------------------------------------------ K3 exactness

def test_tool_names_and_args_are_exactly_k3():
    assert TOOL_NAMES == ("brain_search", "brain_read", "brain_capture", "brain_ingest_file",
                          "brain_reconcile_context", "brain_propose", "brain_status")
    args = {t["name"]: set(t["inputSchema"]["properties"]) for t in TOOLS}
    assert args == {
        "brain_search": {"query", "mode", "scope", "n"},
        "brain_read": {"ref"},
        "brain_capture": {"text", "language_hint"},
        "brain_ingest_file": {"upload_ref", "title", "description"},
        "brain_reconcile_context": {"seed_ref", "query", "depth", "cursor", "sections", "expand"},
        "brain_propose": {"kind", "structured_fields", "evidence_refs"},
        "brain_status": set(),
    }
    for t in TOOLS:
        assert t["inputSchema"]["additionalProperties"] is False


def test_no_review_path_actor_or_channel_anywhere():
    blob = json.dumps(TOOLS)
    for word in ('"path"', '"actor"', '"channel"', '"file"'):
        assert word not in blob
    for verb in ("review", "accept", "promote", "approve", "adjudicate"):
        assert not any(verb in n for n in TOOL_NAMES)


def test_enums_and_sections_are_frozen_values():
    s = {t["name"]: t["inputSchema"]["properties"] for t in TOOLS}
    assert s["brain_search"]["mode"]["enum"] == ["lexical", "exact"]
    assert s["brain_search"]["scope"]["enum"] == ["canonical", "captures", "all"]
    assert s["brain_reconcile_context"]["depth"]["enum"] == ["focused", "deep"]
    import reconcile_context as rc
    assert tuple(s["brain_reconcile_context"]["sections"]["items"]["enum"]) == rc.SECTION_ORDER


def test_propose_kinds_derive_from_schema_minus_intake():
    schema = json.loads((ROOT / "00-system/policies/proposal_schema.json").read_text())
    kinds = next(t for t in TOOLS if t["name"] == "brain_propose")["inputSchema"][
        "properties"]["kind"]["enum"]
    assert set(kinds) == set(schema["kinds"]) - {"intake-registration"}


# ------------------------------------------------------------ ref boundary

BAD_REFS = ["../_originals/x.md", "03-objects/../_originals/a.md", "/etc/passwd",
            "C:\\Windows\\win.ini", "C:/Windows/win.ini", "\\\\server\\share\\x.md",
            "03-objects/works/nil.md", "nil.md", "rec:../x", "rec:a/b", "rec:a.md",
            "rec:..", "rec:", "REC:obj-nil", "doc:xyz", "doc:../../etc", "cap:x/../y",
            "hist:../../README", "rec:obj-nil\n", "rec:obj nil", "", None, 5, ["rec:obj-nil"]]


@pytest.mark.parametrize("bad", BAD_REFS, ids=[repr(b)[:30] for b in BAD_REFS])
def test_read_rejects_paths_and_malformed_refs(env, bad):
    _, s = env
    out = s.call("brain_read", {"ref": bad})
    assert out["ok"] is False and out["error"]["code"] == "E_BAD_REF"


def test_read_unknown_but_wellformed_ref_is_not_found(env):
    _, s = env
    for ref in ("rec:nope", "cap:cap-20990101-000000-abcd", "prop:nope", "hist:nope",
                "doc:" + "0" * 20):
        assert s.call("brain_read", {"ref": ref})["error"]["code"] == "E_NOT_FOUND"


def test_forbidden_arguments_rejected_everywhere(env):
    _, s = env
    for tool, args in [("brain_read", {"ref": "rec:obj-nil", "path": "03-objects/works/nil.md"}),
                       ("brain_capture", {"text": "x", "channel": "telegram-hermes"}),
                       ("brain_capture", {"text": "x", "actor": "owner"}),
                       ("brain_propose", {"kind": "object-note", "structured_fields": {},
                                          "evidence_refs": [], "actor": "owner"}),
                       ("brain_status", {"verbose": True})]:
        out = s.call(tool, args)
        assert out["error"]["code"] == "E_UNKNOWN_ARGUMENT", (tool, out)
        if tool in ("brain_capture", "brain_propose"):
            assert out["persisted"] is False


# ------------------------------------------------------------ read

def test_read_record_document_history_labels(env):
    root, s = env
    r = s.call("brain_read", {"ref": "rec:obj-nil"})
    assert r["ok"] and QUOTE in r["content"] and r["authority_level"] == 4
    assert r["tier"] == "canonical-record" and r["kind"] == "record"
    doc_ref = R.public_ref_for_path(root, "02-sources/text/kolan-letter.extracted.md")
    assert doc_ref.startswith("doc:")
    d = s.call("brain_read", {"ref": doc_ref})
    assert d["ok"] and d["tier"] == "derivative" and d["authority_level"] == 5
    h = s.call("brain_read", {"ref": "hist:HANDOFF--2026-09-01--nil-decision"})
    assert h["ok"] and h["kind"] == "history"
    for _, v in walk([r, d, h]):
        assert str(root) not in str(v)


# ------------------------------------------------------------ capture (D + K9)

@pytest.mark.parametrize("text", ["سلام؛ این تصمیم نهایی است.\n", "Decision: appendix moves.",
                                  "mixed متن و English\r\n## Review notes\r\n"])
def test_capture_verbatim_fixed_channel_committed(env, text):
    root, s = env
    out = s.call("brain_capture", {"text": text, "language_hint": "mixed"})
    assert out["ok"] and out["persisted"] and out["status"] == "received"
    assert out["channel"] == "mcp" and out["commit_state"] == "committed"
    got = s.call("brain_read", {"ref": out["ref"]})
    assert got["sections"]["User-supplied text"] == text.rstrip("\n") + "\n"
    assert got["metadata"]["capture_channel"] == "mcp"
    assert got["metadata"]["sha256"] == out["sha256"]
    changed = git(root, "show", "--name-only", "--format=", "HEAD").split()
    assert len(changed) == 1 and changed[0].startswith("01-inbox/captures/")
    assert git(root, "status", "--porcelain").strip() == ""


def test_capture_commit_failure_keeps_data_and_surfaces_in_status(env, tmp_path):
    root, s = env
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "pre-commit").write_text("#!/bin/sh\necho blocked by test hook >&2\nexit 1\n")
    (hooks / "pre-commit").chmod(0o755)
    git(root, "config", "core.hooksPath", str(hooks))
    out = s.call("brain_capture", {"text": "must not be lost"})
    assert out["ok"] and out["persisted"] and out["commit_state"] == "uncommitted"
    assert "blocked by test hook" in out["commit_error"]
    got = s.call("brain_read", {"ref": out["ref"]})
    assert got["sections"]["User-supplied text"] == "must not be lost\n"
    st = s.call("brain_status", {})
    assert st["uncommitted"]["count"] == 1 and st["degraded"]
    assert "uncommitted noncanonical writes" in st["degraded_reasons"]
    assert str(root) not in json.dumps(st)


@pytest.mark.parametrize("args", [{"text": ""}, {"text": "   "}, {"text": 5},
                                  {"text": "x", "language_hint": "de"}])
def test_capture_rejects_bad_input_and_stores_nothing(env, args):
    root, s = env
    out = s.call("brain_capture", args)
    assert out["ok"] is False and out["persisted"] is False
    assert not (root / "01-inbox/captures").exists() or not list(
        (root / "01-inbox/captures").rglob("cap-*.md"))


def test_capture_duplicate_is_flagged_not_merged(env):
    _, s = env
    a = s.call("brain_capture", {"text": "same words"})
    b = s.call("brain_capture", {"text": "same words"})
    assert b["duplicate_of"] == a["ref"] and b["ref"] != a["ref"]


# ------------------------------------------------------------ search (C)

def test_exact_search_scopes_and_labels(env):
    _, s = env
    s.call("brain_capture", {"text": "Kolan came up again in conversation."})
    out = s.call("brain_search", {"query": "Kolan", "mode": "exact", "scope": "all"})
    assert out["ok"] and set(out["groups"]) == {"canonical", "captures"}
    canon = out["groups"]["canonical"]["results"]
    caps = out["groups"]["captures"]["results"]
    assert all(not h["ref"].startswith("cap:") for h in canon)
    assert caps and all(h["ref"].startswith("cap:") and h["authority_level"] == 7 for h in caps)
    tiers = {h["tier"] for h in canon}
    assert {"source-record", "derivative"} <= tiers
    der = next(h for h in canon if h["tier"] == "derivative")
    assert der["authority_level"] == 5 and der["ref"].startswith("doc:")
    assert "derivative" in out["groups"]["canonical"]["authority_note"]


def test_search_validation(env):
    _, s = env
    for args in ({"query": ""}, {"query": "x", "mode": "semantic"},
                 {"query": "x", "scope": "everything"}, {"query": "x", "n": 0},
                 {"query": "x", "n": True}):
        assert s.call("brain_search", args)["error"]["code"] == "E_BAD_ARGUMENTS"


@needs_qmd
def test_lexical_search_through_c_and_no_hits_note(env):
    root, s = env
    import search_lexical as sl
    sl.configure(root)
    out = s.call("brain_search", {"query": "Nil title", "scope": "canonical"})
    refs = [h["ref"] for h in out["groups"]["canonical"]["results"]]
    assert "rec:obj-nil" in refs and out["model_free"] is True
    none = s.call("brain_search", {"query": "zzqqxx", "scope": "all"})
    assert any("retry" in n for n in none["notes"])


def test_lexical_without_qmd_is_unavailable_not_silently_empty(env, monkeypatch):
    _, s = env
    monkeypatch.setattr(shutil, "which", lambda name: None)
    out = s.call("brain_search", {"query": "Nil"})
    assert out["error"]["code"] == "E_UNAVAILABLE" and "exact" in out["error"]["message"]


# ------------------------------------------------------------ proposals (E)

def good(**over):
    f = {"claim_id": "clm-nil", "amendment": "lower permission to may-note",
         "why": "Title decision was described as provisional."}
    f.update(over)
    return f


def queue(root):
    p = root / "_proposals/proposals.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def test_propose_success_is_candidate_committed_and_readable(env):
    root, s = env
    before = len(queue(root))
    out = s.call("brain_propose", {"kind": "claim-amendment", "structured_fields": good(),
                                   "evidence_refs": [{"ref": "rec:clm-nil", "quote": CLAIM_QUOTE}]})
    assert out["ok"] and out["authority_tier"] == "candidate" and out["status"] == "new"
    assert out["persisted"] and out["commit_state"] == "committed"
    q = queue(root)
    assert len(q) == before + 1
    body = q[-1]["body"]
    assert body["source_passage"] == {"path": "05-claims/clm-nil.md", "quote": CLAIM_QUOTE}
    assert body["evidence_refs"] == [{"ref": "clm-nil", "quote": CLAIM_QUOTE}]
    got = s.call("brain_read", {"ref": out["ref"]})
    assert got["ok"] and got["kind"] == "proposal" and got["authority_level"] == 7
    changed = git(root, "show", "--name-only", "--format=", "HEAD").split()
    assert changed == ["_proposals/proposals.jsonl"]


def test_propose_doc_ref_evidence_and_capture_support(env):
    root, s = env
    c = s.call("brain_capture", {"text": "Owner thinks the Kolan ferry schedule matters for Nil."})
    doc = R.public_ref_for_path(root, "02-sources/text/kolan-letter.extracted.md")
    out = s.call("brain_propose", {
        "kind": "relation-edge",
        "structured_fields": {"target_id": "src-kolan", "proposed_type": "unclassified",
                              "why": "both mention the ferry"},
        "evidence_refs": [{"ref": doc, "quote": "The ferry to Kolan left every Thursday"},
                          {"ref": c["ref"], "quote": "the Kolan ferry schedule matters"}]})
    assert out["ok"], out


BAD_PROPOSALS = [
    ("forged quote", {"evidence_refs": [{"ref": "rec:clm-nil", "quote": "the novel title is definitely settled"}]}, "NOT found verbatim"),
    ("forged secondary", {"evidence_refs": [{"ref": "rec:clm-nil", "quote": CLAIM_QUOTE},
                                            {"ref": "rec:obj-nil", "quote": "a sentence that is not there at all"}]}, "NOT found verbatim"),
    ("path ref", {"evidence_refs": [{"ref": "05-claims/clm-nil.md", "quote": CLAIM_QUOTE}]}, "not a valid ref"),
    ("traversal ref", {"evidence_refs": [{"ref": "../05-claims/clm-nil.md", "quote": CLAIM_QUOTE}]}, "not a valid ref"),
    ("traversal inside id", {"evidence_refs": [{"ref": "rec:../../etc/passwd", "quote": CLAIM_QUOTE}]}, "not a valid ref"),
    ("unknown record", {"evidence_refs": [{"ref": "rec:nope", "quote": CLAIM_QUOTE}]}, "does not resolve"),
    ("no quote at all", {"evidence_refs": [{"ref": "rec:clm-nil"}]}, "source_passage"),
    ("capture only", {"evidence_refs": [{"ref": "cap:cap-20990101-000000-abcd", "quote": CLAIM_QUOTE}]}, "does not resolve"),
    ("hist as evidence", {"evidence_refs": [{"ref": "hist:HANDOFF--2026-09-01--nil-decision", "quote": "Handoff: decided obj-nil title"}]}, "not evidence"),
    ("empty evidence", {"evidence_refs": []}, "at least one"),
    ("source_passage smuggled", {"structured_fields": good(source_passage="03-objects/x.md")}, "source_passage is derived"),
    ("unknown field", {"structured_fields": good(extra="x")}, "unknown structured field"),
    ("missing field", {"structured_fields": {"claim_id": "clm-nil"}}, "missing"),
    ("non-string field", {"structured_fields": good(why=5)}, "must be a string"),
    ("extra key in ref", {"evidence_refs": [{"ref": "rec:clm-nil", "quote": CLAIM_QUOTE, "path": "/etc/passwd"}]}, "must be an object"),
]


@pytest.mark.parametrize("name,over,needle", BAD_PROPOSALS, ids=[b[0] for b in BAD_PROPOSALS])
def test_invalid_proposal_is_rejected_and_not_appended(env, name, over, needle):
    root, s = env
    args = {"kind": "claim-amendment", "structured_fields": good(),
            "evidence_refs": [{"ref": "rec:clm-nil", "quote": CLAIM_QUOTE}], **over}
    before = (root / "_proposals/proposals.jsonl").read_bytes()
    head = git(root, "rev-parse", "HEAD")
    out = s.call("brain_propose", args)
    assert out["ok"] is False and out["persisted"] is False, out
    assert out["error"]["code"] == "E_PROPOSAL_REJECTED"
    assert any(needle in r for r in out["error"]["reasons"]), out["error"]["reasons"]
    assert (root / "_proposals/proposals.jsonl").read_bytes() == before
    assert git(root, "rev-parse", "HEAD") == head


def test_intake_registration_not_remotely_proposable(env):
    _, s = env
    out = s.call("brain_propose", {"kind": "intake-registration",
                                   "structured_fields": {"original_path": "_originals/x",
                                                         "sha256": "a" * 64, "why": "w"},
                                   "evidence_refs": [{"ref": "rec:obj-nil"}]})
    assert out["error"]["code"] == "E_PROPOSAL_REJECTED" and out["persisted"] is False


# ------------------------------------------------------------ reconcile (E, K5)

def test_focused_package_public_refs_no_paths_no_verdict(env):
    root, s = env
    out = s.call("brain_reconcile_context", {"seed_ref": "rec:obj-nil"})
    assert out["ok"] and out["verdict"] is None and out["depth"] == "focused"
    assert list(out["sections"]) == list(C.RECONCILE_SECTIONS)
    canon = [i["ref"] for i in out["sections"]["canonical_records"]["items"]]
    assert "rec:obj-nil" in canon
    assert [i["ref"] for i in out["sections"]["claims"]["items"]] == ["rec:clm-nil"]
    assert [i["ref"] for i in out["sections"]["superseded"]["items"]] == ["rec:obj-old"]
    assert any(i["ref"] == "hist:HANDOFF--2026-09-01--nil-decision"
               for i in out["sections"]["chronology"]["items"])
    for k, v in walk(out):
        assert k != "path"
        if k == "ref" and v is not None:
            assert R.REF_RE.match(v), v
        assert str(root) not in str(v)
    for sec in out["sections"].values():
        for it in sec["items"]:
            assert {"id", "ref", "authority_level", "reason"} <= set(it)


def test_deep_paging_reconstructs_full_result_and_cursor_rules(brain_wiki):
    root = brain_wiki
    seed_corpus(root)
    for i in range(7):
        record(root, f"04-notes/n{i}.md", f"note-{i}", f"Note {i}", "about nil", links=["obj-nil"])
    commit_all(root)
    s = BrainSurface(WikiBackend(root, page_size=4))
    full = BrainSurface(WikiBackend(root)).call(
        "brain_reconcile_context", {"seed_ref": "rec:obj-nil", "depth": "deep"})
    first = s.call("brain_reconcile_context", {"seed_ref": "rec:obj-nil", "depth": "deep"})
    assert first["returned"] == 4 and first["next_cursor"] and first["total"] == full["total"]
    seen, cur = [], None
    while True:
        page = s.call("brain_reconcile_context", {"seed_ref": "rec:obj-nil", "depth": "deep",
                                                  **({"cursor": cur} if cur else {})})
        assert page["ok"], page
        seen += [(n, i["id"]) for n, sec in page["sections"].items() for i in sec["items"]]
        cur = page["next_cursor"]
        if not cur:
            break
    assert len(seen) == full["total"] and len(set(seen)) == len(seen)
    # a cursor is bound to its arguments and to the corpus
    other = s.call("brain_reconcile_context", {"seed_ref": "rec:clm-nil", "depth": "deep",
                                               "cursor": first["next_cursor"]})
    assert other["error"]["code"] == "E_STALE_CURSOR"
    garbage = s.call("brain_reconcile_context", {"seed_ref": "rec:obj-nil", "depth": "deep",
                                                 "cursor": base64.urlsafe_b64encode(b"{}").decode()})
    assert garbage["error"]["code"] == "E_BAD_CURSOR"


def test_reconcile_sections_expand_and_argument_errors(env):
    _, s = env
    out = s.call("brain_reconcile_context", {"seed_ref": "rec:obj-nil", "sections": ["claims"],
                                             "expand": ["rec:clm-nil"]})
    assert list(out["sections"]) == ["claims"]
    assert CLAIM_QUOTE in out["sections"]["claims"]["items"][0]["content"]
    for args, code in [({}, "E_BAD_ARGUMENTS"), ({"seed_ref": "03-objects/works/nil.md"}, "E_BAD_REF"),
                       ({"seed_ref": "rec:nope"}, "E_NOT_FOUND"),
                       ({"query": "x", "sections": ["canonical"]}, "E_BAD_ARGUMENTS"),
                       ({"query": "x", "depth": "infinite"}, "E_BAD_ARGUMENTS"),
                       ({"query": "x", "expand": ["../x"]}, "E_BAD_REF")]:
        assert s.call("brain_reconcile_context", args)["error"]["code"] == code, args


def test_capture_seeded_reconciliation(env):
    _, s = env
    c = s.call("brain_capture", {"text": "Actually obj-nil is now called Zero."})
    out = s.call("brain_reconcile_context", {"seed_ref": c["ref"]})
    assert out["ok"] and out["seed"]["kind"] == "capture"
    assert "rec:obj-nil" in [i["ref"] for i in out["sections"]["canonical_records"]["items"]]


# ------------------------------------------------------------ status

def test_status_is_truthful_and_private(env):
    root, s = env
    s.call("brain_capture", {"text": "pending interpretation"})
    st = s.call("brain_status", {})
    assert st["ok"] and st["snapshot"] == "wiki-corpus-empty"
    for k in ("counts", "captures_pending", "proposals", "search", "validation",
              "uncommitted", "git", "degraded"):
        assert k in st
    assert st["counts"]["captures"] == 1 and st["captures_pending"][0]["status"] == "received"
    assert st["search"]["model_free"] is True
    assert st["validation"]["state"] in ("pass", "fail")
    assert st["uncommitted"]["clean"] is True
    blob = json.dumps(st)
    assert str(root) not in blob and "SECRET" not in blob.upper().replace("SECRETS", "")
    again = s.call("brain_status", {})
    assert again["validation"]["cached"] is True


def test_crlf_capture_survives_commit_and_fresh_clone(env, tmp_path):
    """.gitattributes marks captures -text: Git must not normalise CRLF on
    commit or checkout, or a restored/cloned capture would fail its SHA-256."""
    root, s = env
    text = "line one\r\n## Review notes\r\nمتن فارسی\r\n"
    out = s.call("brain_capture", {"text": text})
    assert out["commit_state"] == "committed"
    rel = git(root, "show", "--name-only", "--format=", "HEAD").split()[0]
    original = (root / rel).read_bytes()
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(root), str(clone)], check=True)
    assert (clone / rel).read_bytes() == original
    assert b"\r\n" in original
    import wiki_capture as wc
    fm, sections = wc.parse_record_text(wc.read_record_file(clone / rel))
    import hashlib
    assert sections["User-supplied text"] == text
    assert hashlib.sha256(text.encode()).hexdigest() == fm["sha256"] == out["sha256"]
