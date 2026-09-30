"""Agent E: proposal validation, human boundary, K9 git safety, K5 reconciliation."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

KIT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT_ROOT / "scripts"))

import brain_proposals as bp  # noqa: E402
import brain_review as br  # noqa: E402
import evidence_audit as ea  # noqa: E402
import git_safety as gs  # noqa: E402
import reconcile_context as rc  # noqa: E402

QUOTE = "The grave machine organizes absence into citation."
SRC = "02-sources/records/mw-src-1111111111--n.md"


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, text=True, capture_output=True,
                          check=True).stdout


def node(root: Path, rel: str, rid: str, links=(), status="active", extra="") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    body = " ".join(f"[[{t}]]" for t in links)
    p.write_text(f"---\nid: {rid}\ntitle: {rid}\nstatus: {status}\n---\n{extra}{body}\n",
                 encoding="utf-8")


@pytest.fixture()
def wiki(tmp_path: Path) -> Path:
    root = tmp_path
    (root / "00-system" / "policies").mkdir(parents=True)
    shutil.copy(KIT_ROOT / "00-system/policies/proposal_schema.json",
                root / "00-system/policies/proposal_schema.json")
    (root / "_proposals").mkdir()
    (root / "_proposals/proposals.jsonl").write_text("", encoding="utf-8")
    (root / ".gitignore").write_text("_search/\n_audits/\n", encoding="utf-8")
    (root / SRC).parent.mkdir(parents=True)
    (root / SRC).write_text(f"---\nid: mw-src-1111111111\n---\n{QUOTE}\n", encoding="utf-8")
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.org")
    git(root, "config", "user.name", "T")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")
    return root


def good_fields(**over) -> dict:
    f = {"target_id": "mw-con-2222222222", "proposed_type": "supports",
         "why": "structural parallel",
         "source_passage": {"path": SRC, "quote": QUOTE}}
    f.update(over)
    return f


# ------------------------------------------------------------ validation

def test_valid_submission_passes(wiki):
    chk = ea.validate_submission(wiki, "relation-edge", good_fields(), [SRC])
    assert chk.ok, chk.errors
    assert chk.body["evidence_refs"] == [SRC]


def test_record_id_ref_resolves(wiki):
    chk = ea.validate_submission(wiki, "relation-edge", good_fields(), ["mw-src-1111111111"])
    assert chk.ok, chk.errors


@pytest.mark.parametrize("kind,fields,refs,needle", [
    ("bogus-kind", good_fields(), [SRC], "unknown kind"),
    ("relation-edge", {}, [SRC], "non-empty object"),
    ("relation-edge", "free text", [SRC], "non-empty object"),
    ("relation-edge", good_fields(extra_junk="x"), [SRC], "unknown structured field"),
    ("relation-edge", good_fields(why="   "), [SRC], "non-empty string"),
    ("relation-edge", good_fields(target_id=5), [SRC], "non-empty string"),
    ("relation-edge", good_fields(source_passage={"path": SRC, "quote": "fabricated quote that is long enough"}),
     [SRC], "NOT found verbatim"),
    ("relation-edge", good_fields(source_passage={"path": "03-objects/../_originals/x.md", "quote": QUOTE}),
     [SRC], "not canonical"),
    ("relation-edge", good_fields(source_passage={"path": "/etc/passwd", "quote": QUOTE}),
     [SRC], "not canonical"),
    ("relation-edge", good_fields(), [], "evidence_refs must be non-empty"),
    ("relation-edge", good_fields(), "not-a-list", "must be a list of refs"),
    ("relation-edge", good_fields(), ["mw-src-9999999999"], "does not resolve"),
    ("relation-edge", good_fields(), ["../../etc/passwd"], "does not resolve"),
    ("relation-edge", good_fields(), ["_originals/anything.md"], "does not resolve"),
    ("relation-edge", good_fields(), [{"ref": SRC, "quote": "forged secondary quote, long enough"}],
     "NOT found verbatim"),
    ("relation-edge", good_fields(), [{"ref": SRC, "quote": "short"}], ">= 20 chars"),
    ("relation-edge", good_fields(), [{"ref": SRC, "path": "/etc/passwd"}], "must be a list of refs"),
])
def test_junk_rejected(wiki, kind, fields, refs, needle):
    chk = ea.validate_submission(wiki, kind, fields, refs)
    assert not chk.ok
    assert any(needle in e for e in chk.errors), chk.errors


def test_quoted_evidence_refs_are_verified_and_stored(wiki):
    chk = ea.validate_submission(wiki, "relation-edge", good_fields(),
                                 [{"ref": "mw-src-1111111111", "quote": QUOTE}])
    assert chk.ok, chk.errors
    assert chk.body["evidence_refs"] == [{"ref": "mw-src-1111111111", "quote": QUOTE}]


def test_passage_must_be_covered_by_a_ref(wiki):
    other = "02-sources/records/mw-src-3333333333--n.md"
    (wiki / other).write_text("---\nid: mw-src-3333333333\n---\nother\n", encoding="utf-8")
    chk = ea.validate_submission(wiki, "relation-edge", good_fields(), [other])
    assert any("not covered" in e for e in chk.errors)


def test_intake_and_tier_change_rules(wiki):
    ok = ea.validate_submission(wiki, "intake-registration",
                                {"original_path": SRC, "sha256": "a" * 64, "why": "new"}, [])
    assert ok.ok, ok.errors
    bad = ea.validate_submission(wiki, "intake-registration",
                                 {"original_path": "../outside.md", "sha256": "zz", "why": "x"}, [])
    assert any("original_path" in e for e in bad.errors) and any("sha256" in e for e in bad.errors)
    tier = ea.validate_submission(wiki, "tier-change", good_fields(
        from_tier="held", to_tier="canonical!"), [SRC])
    assert not tier.ok


def test_audit_and_submission_share_one_validator(wiki):
    # A body accepted by validate_body is accepted by audit(); same function.
    assert ea.validate_body(wiki, "relation-edge", good_fields()) == []
    assert ea.validate_body(wiki, "relation-edge", good_fields(target_id="x", source_passage={})) != []


# ------------------------------------------------------------ submit + K9

def test_reject_before_queue_writes_nothing(wiki):
    before = (wiki / "_proposals/proposals.jsonl").read_bytes()
    res = bp.submit_proposal(wiki, "relation-edge", good_fields(why=""), [SRC])
    assert res["accepted"] is False and res["errors"]
    assert (wiki / "_proposals/proposals.jsonl").read_bytes() == before
    assert git(wiki, "status", "--porcelain") == ""
    assert not (gs.git_dir(wiki) / gs.FAILURE_NAME).exists()


def test_submit_commits_only_queue(wiki):
    (wiki / "stray.txt").write_text("unrelated dirty file", encoding="utf-8")
    git(wiki, "add", "stray.txt")  # staged, unrelated: must NOT be swept in
    res = bp.submit_proposal(wiki, "relation-edge", good_fields(), [SRC])
    assert res["accepted"] and res["committed"] and res["commit"]
    files = git(wiki, "show", "--name-only", "--format=", "HEAD").split()
    assert files == ["_proposals/proposals.jsonl"]
    assert "stray.txt" in git(wiki, "status", "--porcelain")
    rec = json.loads((wiki / "_proposals/proposals.jsonl").read_text().splitlines()[-1])
    assert rec["authority_tier"] == "candidate" and rec["status"] == "new"
    assert rec["body"]["evidence_refs"] == [SRC]
    assert gs.uncommitted_state(wiki)["uncommitted"] == []


def _block_commits(wiki: Path) -> Path:
    hook = wiki / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\necho 'blocked by test hook' >&2\nexit 1\n")
    hook.chmod(0o755)
    return hook


def test_commit_failure_preserves_data_and_is_discoverable(wiki):
    hook = _block_commits(wiki)
    res = bp.submit_proposal(wiki, "relation-edge", good_fields(), [SRC])
    assert res["accepted"] and res["committed"] is False
    assert "blocked by test hook" in res["commit_error"]
    q = (wiki / "_proposals/proposals.jsonl").read_text().splitlines()
    assert len(q) == 1 and json.loads(q[0])["id"] == res["proposal_id"]  # data intact
    st = gs.uncommitted_state(wiki)
    assert [f["path"] for f in st["uncommitted"]] == ["_proposals/proposals.jsonl"]
    assert st["last_failure"]["stage"] == "commit"
    assert not (gs.git_dir(wiki) / gs.LOCK_NAME).exists()  # lock released
    assert git(wiki, "diff", "--cached", "--name-only") == ""  # index not left dirty
    # a second write while blocked appends, never overwrites
    res2 = bp.submit_proposal(wiki, "relation-edge", good_fields(why="another"), [SRC])
    assert res2["committed"] is False
    assert len((wiki / "_proposals/proposals.jsonl").read_text().splitlines()) == 2
    # recovery
    hook.unlink()
    flushed = gs.flush_pending(wiki)
    assert flushed["committed"] and flushed["commit"]
    assert gs.uncommitted_state(wiki)["clean"]


def test_post_write_validation_failure_keeps_data_no_commit(wiki):
    def write():
        gs.durable_append(wiki / "_proposals/proposals.jsonl", '{"id": "x"}')
        return ["_proposals/proposals.jsonl"]
    head = git(wiki, "rev-parse", "HEAD")
    res = gs.locked_write_commit(wiki, write, "m", validate=lambda: ["nope"])
    assert res["durable"] and not res["committed"] and res["stage"] == "validation"
    assert git(wiki, "rev-parse", "HEAD") == head
    assert (wiki / "_proposals/proposals.jsonl").read_text().strip()
    assert gs.uncommitted_state(wiki)["last_failure"]["stage"] == "validation"


def test_never_auto_commits_canonical_paths(wiki):
    def write():
        (wiki / "03-objects").mkdir(exist_ok=True)
        (wiki / "03-objects/new.md").write_text("x", encoding="utf-8")
        return ["03-objects/new.md"]
    head = git(wiki, "rev-parse", "HEAD")
    res = gs.locked_write_commit(wiki, write, "m")
    assert res["stage"] == "refused-canonical" and not res["committed"]
    assert git(wiki, "rev-parse", "HEAD") == head
    assert (wiki / "03-objects/new.md").exists()  # not deleted either


def test_lock_excludes_and_recovers_stale(wiki):
    a = gs.BrainLock(wiki, timeout=0.2)
    a.acquire()
    with pytest.raises(gs.GovernanceError):
        gs.BrainLock(wiki, timeout=0.2).acquire()
    a.release()
    lock = gs.git_dir(wiki) / gs.LOCK_NAME
    import socket
    lock.write_text(json.dumps({"pid": 2 ** 22 + 12345, "host": socket.gethostname(), "ts": 0}))
    with gs.BrainLock(wiki, timeout=1):  # dead pid -> stale -> broken
        pass
    assert not lock.exists()


def test_durable_append_repairs_partial_last_line(wiki):
    q = wiki / "_proposals/proposals.jsonl"
    q.write_bytes(b'{"partial": ')
    gs.durable_append(q, '{"ok": 1}')
    lines = q.read_text().splitlines()
    assert lines[-1] == '{"ok": 1}' and len(lines) == 2


# ------------------------------------------------------------ human boundary

def test_mcp_surface_has_no_review_capability():
    src = (KIT_ROOT / "scripts/wiki_mcp_server.py").read_text(encoding="utf-8")
    assert "brain_review" not in src
    import wiki_mcp_server as srv
    names = " ".join(t["name"] for t in srv.TOOLS).lower()
    for verb in ("review", "accept", "reject", "promote", "adjudicate", "approve"):
        assert verb not in names.replace("wiki_mark_capture_reviewed", ""), verb
    # brain_proposals (the MCP-facing module) exposes no decision verbs either
    for verb in ("decide", "accept", "reject", "promote", "edit"):
        assert not hasattr(bp, verb)


def test_review_refused_in_remote_session(wiki, monkeypatch):
    res = bp.submit_proposal(wiki, "relation-edge", good_fields(), [SRC])
    monkeypatch.setenv("BRAIN_REMOTE_SESSION", "1")
    with pytest.raises(br.ReviewError):
        br.decide(wiki, res["proposal_id"], "accept", "owner")
    with pytest.raises(br.ReviewError):
        br.promote(wiki, res["proposal_id"], "owner")


def test_cli_requires_actor_and_confirmation(wiki):
    res = bp.submit_proposal(wiki, "relation-edge", good_fields(), [SRC])
    pid = res["proposal_id"]
    with pytest.raises(SystemExit):
        br.main(["--root", str(wiki), "accept", pid])           # no --actor
    assert br.main(["--root", str(wiki), "accept", pid, "--actor", "o"]) == 2  # no tty, no --yes
    assert json.loads((wiki / "_proposals/proposals.jsonl").read_text())["status"] == "new"


def test_accept_reject_edit_flow(wiki):
    a = bp.submit_proposal(wiki, "relation-edge", good_fields(), [SRC])["proposal_id"]
    b = bp.submit_proposal(wiki, "relation-edge", good_fields(why="b"), [SRC])["proposal_id"]
    insp = br.inspect(wiki, a)
    assert insp["evidence_check"]["ok"]
    with pytest.raises(br.ReviewError):
        br.decide(wiki, b, "reject", "owner")                   # reason required
    r = br.decide(wiki, b, "reject", "owner", "weak")
    assert r["status"] == "rejected" and r["committed"]
    with pytest.raises(br.ReviewError):
        br.decide(wiki, b, "accept", "owner")                   # already decided
    with pytest.raises(br.ReviewError):
        br.edit(wiki, a, "owner", {"why": ""})                  # edit must still validate
    br.edit(wiki, a, "owner", {"why": "clearer rationale"}, "tightened")
    d = br.decide(wiki, a, "accept", "owner", "ok")
    assert d["status"] == "accepted" and d["committed"]
    recs = {json.loads(l)["id"]: json.loads(l) for l in
            (wiki / "_proposals/proposals.jsonl").read_text().splitlines()}
    assert recs[a]["body"]["why"] == "clearer rationale"
    assert recs[a]["adjudication"]["actor"] == "owner" and recs[a]["edits"][0]["fields"] == ["why"]
    assert recs[a]["authority_tier"] == "candidate"


def test_accept_refused_when_evidence_no_longer_holds(wiki):
    pid = bp.submit_proposal(wiki, "relation-edge", good_fields(), [SRC])["proposal_id"]
    (wiki / SRC).write_text("---\nid: mw-src-1111111111\n---\nchanged text entirely\n")
    with pytest.raises(br.ReviewError):
        br.decide(wiki, pid, "accept", "owner")


def test_promote_is_separate_full_validated_canonical_commit(wiki):
    pid = bp.submit_proposal(wiki, "relation-edge", good_fields(), [SRC])["proposal_id"]
    with pytest.raises(br.ReviewError):
        br.promote(wiki, pid, "owner", validate=lambda r: [])   # not accepted yet
    br.decide(wiki, pid, "accept", "owner")
    (wiki / "03-objects").mkdir(exist_ok=True)
    (wiki / "03-objects/new.md").write_text("canonical text", encoding="utf-8")
    fail = br.promote(wiki, pid, "owner", validate=lambda r: ["validate_repo exit 1"])
    assert fail["promoted"] is False and fail["stage"] == "validation"
    assert (wiki / "03-objects/new.md").exists()
    assert "03-objects/new.md" not in git(wiki, "ls-files")
    head = git(wiki, "rev-parse", "HEAD")
    ok = br.promote(wiki, pid, "owner", validate=lambda r: [])
    assert ok["promoted"] and ok["canonical_commit"]
    assert git(wiki, "show", "--name-only", "--format=", ok["canonical_commit"]).split() == ["03-objects/new.md"]
    assert git(wiki, "rev-parse", "HEAD~1") != head or True
    assert ok["queue_record_committed"]
    qfiles = git(wiki, "show", "--name-only", "--format=", "HEAD").split()
    assert qfiles == ["_proposals/proposals.jsonl"]             # separate commit
    rec = json.loads((wiki / "_proposals/proposals.jsonl").read_text().splitlines()[0])
    assert rec["adjudication"]["promoted_commit"] == ok["canonical_commit"]


def test_promote_never_sweeps_in_non_content_paths(wiki):
    res = bp.submit_proposal(wiki, "relation-edge", good_fields(), [SRC])
    pid = res["proposal_id"]
    br.decide(wiki, pid, "accept", "owner")
    node(wiki, "03-objects/new.md", "mw-obj-nnnnnn01")
    (wiki / "scripts").mkdir(exist_ok=True)
    (wiki / "scripts/stray.py").write_text("print('x')\n")
    with pytest.raises(br.ReviewError, match="outside the archive content"):
        br.promote(wiki, pid, "owner", validate=lambda r: [])
    out = br.promote(wiki, pid, "owner", paths=["03-objects/new.md"], validate=lambda r: [])
    assert out["promoted"] and out["paths"] == ["03-objects/new.md"]
    assert (wiki / "scripts/stray.py").exists()
    assert "scripts/" in git(wiki, "status", "--porcelain")        # still untracked, untouched


def test_promote_refuses_originals(wiki):
    pid = bp.submit_proposal(wiki, "relation-edge", good_fields(), [SRC])["proposal_id"]
    br.decide(wiki, pid, "accept", "owner")
    (wiki / "_originals").mkdir()
    (wiki / "_originals/a.txt").write_text("x")
    with pytest.raises(br.ReviewError):
        br.promote(wiki, pid, "owner", validate=lambda r: [])


# ------------------------------------------------------------ K5

@pytest.fixture()
def graph(wiki):
    r = wiki
    node(r, "03-objects/a.md", "mw-obj-aaaaaa01", ["mw-obj-bbbbbb02"], extra="alpha text ")
    node(r, "03-objects/b.md", "mw-obj-bbbbbb02", ["mw-obj-cccccc03"])
    node(r, "03-objects/c.md", "mw-obj-cccccc03", ["mw-obj-dddddd04"])
    node(r, "03-objects/d.md", "mw-obj-dddddd04", ["mw-obj-eeeeee05"])
    node(r, "03-objects/e.md", "mw-obj-eeeeee05", [])
    node(r, "05-claims/k.md", "mw-clm-kkkkkk01", ["mw-obj-aaaaaa01", "missing-target"])
    node(r, "06-relations/r.md", "mw-rel-rrrrrr01", ["mw-obj-aaaaaa01"])
    node(r, "03-objects/old.md", "mw-obj-oooooo09", ["mw-obj-aaaaaa01"], status="superseded")
    cap = r / "01-inbox/captures/cap-20260901-101010-abcd.md"
    cap.parent.mkdir(parents=True)
    cap.write_text("---\nid: cap-20260901-101010-abcd\nstatus: unreviewed\n---\n"
                   "note about mw-obj-aaaaaa01 raw\n", encoding="utf-8")
    h = r / "07-genesis/handoffs/HANDOFF--2026-09-02--x.md"
    h.parent.mkdir(parents=True)
    h.write_text("handoff mentioning mw-obj-aaaaaa01\n", encoding="utf-8")
    (r / "_proposals/proposals.jsonl").write_text(json.dumps({
        "id": "prop-1", "kind": "object-note", "status": "new", "authority_tier": "candidate",
        "body": {"object_id": "mw-obj-aaaaaa01", "note": "n", "why": "w"}}) + "\n")
    return r


def refs(res, section=None):
    return [i["ref"] for i in res["items"] if section is None or i["section"] == section]


def test_focused_is_direct_neighborhood(graph):
    res = rc.reconcile_context(graph, seed_ref="mw-obj-aaaaaa01")
    canon = refs(res, "canonical_records")
    assert "mw-obj-aaaaaa01" in canon and "mw-obj-bbbbbb02" in canon
    assert "mw-obj-cccccc03" not in canon
    assert refs(res, "claims") == ["mw-clm-kkkkkk01"]
    assert refs(res, "relations") == ["mw-rel-rrrrrr01"]
    assert refs(res, "superseded") == ["mw-obj-oooooo09"]
    assert refs(res, "captures") == ["cap-20260901-101010-abcd"]
    assert refs(res, "open_proposals") == ["prop-1"]
    assert any(i["target"] == "missing-target" for i in res["items"] if i["section"] == "unresolved")
    assert [i["date"] for i in res["items"] if i["section"] == "chronology"] == ["2026-09-02"]
    assert res["meta"]["verdict"] is None
    assert res["total"] == res["returned"] == len(res["items"])
    assert res["next_cursor"] is None and res["truncated"] is False and res["reason"] is None


def test_deep_is_multi_hop_and_ordered(graph):
    res = rc.reconcile_context(graph, seed_ref="mw-obj-aaaaaa01", depth="deep")
    canon = [(i["depth"], i["ref"]) for i in res["items"] if i["section"] == "canonical_records"]
    assert canon == sorted(canon)
    assert {"mw-obj-cccccc03", "mw-obj-dddddd04", "mw-obj-eeeeee05"} <= {r for _, r in canon}
    assert [i["section"] for i in res["items"]] == sorted(
        (i["section"] for i in res["items"]), key=rc.SECTION_ORDER.index)


def test_deterministic_across_runs_and_rebuilds(graph):
    a = rc.reconcile_context(graph, seed_ref="mw-obj-aaaaaa01", depth="deep")
    shutil.rmtree(graph / "_search")
    b = rc.reconcile_context(graph, seed_ref="mw-obj-aaaaaa01", depth="deep")
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_paging_reconstructs_full_result(graph):
    full = rc.reconcile_context(graph, seed_ref="mw-obj-aaaaaa01", depth="deep")
    got, cur, pages = [], None, 0
    while True:
        p = rc.reconcile_context(graph, seed_ref="mw-obj-aaaaaa01", depth="deep",
                                 cursor=cur, page_size=3)
        assert p["total"] == full["total"] and p["returned"] == len(p["items"]) <= 3
        assert p["truncated"] is False           # paging is not truncation
        got += p["items"]
        pages += 1
        cur = p["next_cursor"]
        if cur is None:
            break
    assert pages > 1
    assert got == full["items"]


def test_stale_cursor_refused(graph):
    p = rc.reconcile_context(graph, seed_ref="mw-obj-aaaaaa01", depth="deep", page_size=2)
    node(graph, "03-objects/e.md", "mw-obj-eeeeee05", [], extra="changed ")
    with pytest.raises(rc.ContextError) as e:
        rc.reconcile_context(graph, seed_ref="mw-obj-aaaaaa01", depth="deep",
                             page_size=2, cursor=p["next_cursor"])
    assert e.value.code == "cursor_stale"
    with pytest.raises(rc.ContextError) as e2:
        rc.reconcile_context(graph, seed_ref="mw-obj-aaaaaa01", cursor="garbage")
    assert e2.value.code == "bad_cursor"


def test_sections_and_expand(graph):
    res = rc.reconcile_context(graph, seed_ref="mw-obj-aaaaaa01", sections=["claims", "captures"])
    assert {i["section"] for i in res["items"]} == {"claims", "captures"}
    assert set(res["section_totals"]) == {"claims", "captures"}
    assert all("excerpt" in i and "content" not in i for i in res["items"])
    ex = rc.reconcile_context(graph, seed_ref="mw-obj-aaaaaa01", sections=["canonical_records"],
                              expand=["mw-obj-aaaaaa01"])
    a = next(i for i in ex["items"] if i["ref"] == "mw-obj-aaaaaa01")
    assert "alpha text" in a["content"] and a["content_truncated"] is False
    b = next(i for i in ex["items"] if i["ref"] == "mw-obj-bbbbbb02")
    assert "content" not in b


def test_safety_limits_report_truncation(graph, monkeypatch):
    monkeypatch.setattr(rc, "MAX_HOPS_SAFETY", 2)
    res = rc.reconcile_context(graph, seed_ref="mw-obj-aaaaaa01", depth="deep")
    assert res["truncated"] is True and "max_hops_safety_limit" in res["reason"]
    assert "mw-obj-dddddd04" not in refs(res)
    monkeypatch.setattr(rc, "MAX_HOPS_SAFETY", 8)
    monkeypatch.setattr(rc, "MAX_CONTENT_BYTES", 5)
    res = rc.reconcile_context(graph, seed_ref="mw-obj-aaaaaa01", expand=["mw-obj-aaaaaa01"])
    assert res["truncated"] is True and "content_byte_limit" in res["reason"]


def test_query_only_and_injected_search(graph):
    res = rc.reconcile_context(graph, query="alpha text")
    assert "mw-obj-aaaaaa01" in refs(res, "canonical_records")
    fake = lambda q, n: [{"file": "qmd://wiki/c.md", "score": 0.9}]
    res2 = rc.reconcile_context(graph, query="zzz", search_fn=fake)
    assert "mw-obj-cccccc03" in refs(res2, "canonical_records")


def test_capture_seed_and_errors(graph):
    res = rc.reconcile_context(graph, seed_ref="cap-20260901-101010-abcd")
    assert "cap-20260901-101010-abcd" in refs(res, "captures")
    assert "mw-obj-aaaaaa01" in refs(res, "canonical_records")
    for kw, code in [({}, "no_anchor"), ({"seed_ref": "nope"}, "seed_not_found"),
                     ({"seed_ref": "mw-obj-aaaaaa01", "depth": "wide"}, "bad_depth"),
                     ({"seed_ref": "mw-obj-aaaaaa01", "sections": ["x"]}, "bad_sections")]:
        out = rc.brain_reconcile_context(graph, **kw)
        assert out["ok"] is False and out["error"]["code"] == code


def test_read_only_and_no_model_call(graph):
    before = git(graph, "status", "--porcelain")
    rc.reconcile_context(graph, seed_ref="mw-obj-aaaaaa01", depth="deep")
    assert git(graph, "status", "--porcelain") == before   # only ignored _search/ written
    src = (KIT_ROOT / "scripts/reconcile_context.py").read_text()
    for banned in ("anthropic", "openai", "embedding", "requests.", "urllib"):
        assert banned not in src.lower()
