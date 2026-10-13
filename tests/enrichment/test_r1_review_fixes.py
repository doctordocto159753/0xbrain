"""Regression tests for the independent R1 review (REVIEW_FIX_REPORT.md).

Each test reproduces one finding on the REAL stack (SQLite store, annotation
service, FTS5 index, retrieval engine, extension surface) over the seeded
`enrichment_wiki`, and fails on the integrated commit before the fix.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from brain_enrichment import contract as C  # noqa: E402
from test_e2e_extension import CFG, build_index, find_pending, stack, submit  # noqa: E402,F401
from test_upgrade import rig  # noqa: E402,F401  (fixture)


def _hits(out):
    return [h for g in out["groups"].values() for h in g["results"]]


# ------------------------------------------------- F1: page-by-page annotation
def test_page_by_page_annotation_keeps_earlier_pages_metadata(stack):
    """Hermes annotates a long source page by page: each page is its own
    revision (CONTRACT-E1 section 5, harness/hermes/SKILL.md "Long sources").
    The metadata of page 1 must stay searchable and readable after page 2."""
    w, ext, _ = stack
    ref = w.refs["long"]
    item, _ = find_pending(ext, ref)
    chunks = item["page"]["chunks"]
    assert len(chunks) >= 2
    c0, rest = chunks[0], chunks[1:]
    page1 = {"chunk_annotations": [{"chunk_ref": c0["chunk_ref"], "summary": "page one",
                                    "tags": ["zanzibarpageone"]}],
             "entities": [{"name": "Quokkaentity", "kind": "concept",
                           "mention": {"chunk_ref": c0["chunk_ref"], "text": c0["text"][2:12]}}]}
    r1 = submit(ext, item, page1, key="page-one-0001")
    assert r1["ok"] and r1["annotation_revision"] == 1, r1
    page2 = {"summary": "whole document read", "tags": ["wholedoctag"],
             "chunk_annotations": [{"chunk_ref": c["chunk_ref"], "summary": "later page"} for c in rest]}
    r2 = submit(ext, {**item, "expected_annotation_revision": 1}, page2, key="page-two-0001")
    assert r2["ok"] and r2["annotation_revision"] == 2, r2
    assert r2["coverage"] == {"annotated_chunks": len(chunks), "total_chunks": len(chunks)}
    build_index(ext)

    for query in ("zanzibarpageone", "Quokkaentity", "wholedoctag"):
        out = ext.call("brain_retrieve", {"query": query})
        assert out["ok"] and out["path"] == "enrichment", out
        assert any(h["source_ref"] == ref and "metadata" in h["inclusion_reason"]["paths"]
                   for h in _hits(out)), (query, out)
    one = ext.call("brain_artifact_read", {"source_ref": ref, "chunk_ref": c0["chunk_ref"]})
    assert one["ok"] and one["annotation_for_chunk"]["tags"] == ["zanzibarpageone"], one
    art = ext.call("brain_artifact_read", {"source_ref": ref})
    assert art["ok"] and art["artifact"]["current_annotation_revision"] == 2


HUMAN = {"actor": "sina@test", "actor_name": "sina", "os_user": "test", "remote": False, "tty": True,
         "confirmation": "interactive", "via": "brain_enrichment.cli"}


def test_rejecting_the_latest_page_hides_only_that_revision(stack):
    """CONTRACT-E1 section 11: `rejected` hides THAT revision; the earlier,
    non-rejected page stays usable for retrieval."""
    w, ext, _ = stack
    ref = w.refs["long"]
    item, _ = find_pending(ext, ref)
    chunks = item["page"]["chunks"]
    c0, c1 = chunks[0], chunks[1]
    assert submit(ext, item, {"chunk_annotations": [{"chunk_ref": c0["chunk_ref"], "tags": ["keptpagetag"]}]},
                  key="keep-page-0001")["ok"]
    assert submit(ext, {**item, "expected_annotation_revision": 1},
                  {"chunk_annotations": [{"chunk_ref": c1["chunk_ref"], "tags": ["rejectedpagetag"]}]},
                  key="drop-page-0001")["ok"]
    store = ext.s.store()
    store.record_review({"source_ref": ref, "annotation_revision": 2, "decision": "rejected",
                         "note": "wrong reading"}, HUMAN)
    build_index(ext)
    kept = ext.call("brain_retrieve", {"query": "keptpagetag"})
    assert any(h["chunk_ref"] == c0["chunk_ref"] and "metadata" in h["inclusion_reason"]["paths"]
               for h in _hits(kept)), kept
    dropped = ext.call("brain_retrieve", {"query": "rejectedpagetag"})
    assert not any("metadata" in h["inclusion_reason"]["paths"] for h in _hits(dropped)), dropped


def test_compose_effective_rules():
    from brain_enrichment.store import compose_effective

    order = ["chk:" + "a" * 32, "chk:" + "b" * 32]

    def rev(n, ann, state="unreviewed"):
        return {"revision": n, "annotation": ann, "derived": {"entities": [], "links": []}, "review_state": state}

    page1 = rev(1, {"chunk_annotations": [{"chunk_ref": order[0], "tags": ["p1"]}], "tags": ["t1"]})
    page2 = rev(2, {"chunk_annotations": [{"chunk_ref": order[1], "tags": ["p2"]}], "summary": "s"})
    eff = compose_effective([page1, page2], order)
    assert [c["tags"] for c in eff["annotation"]["chunk_annotations"]] == [["p1"], ["p2"]]
    assert eff["annotation"]["tags"] == ["t1"] and eff["annotation"]["summary"] == "s"
    assert eff["revision"] == 2 and eff["window"] == [[1, "unreviewed"], [2, "unreviewed"]]
    # a complete re-annotation replaces the earlier pages
    full = rev(3, {"chunk_annotations": [{"chunk_ref": c, "tags": ["full"]} for c in order]})
    eff = compose_effective([page1, page2, full], order)
    assert eff["window"] == [[3, "unreviewed"]] and "summary" not in eff["annotation"]
    # rejected revisions never contribute; all rejected -> nothing usable
    eff = compose_effective([page1, rev(2, page2["annotation"], "rejected")], order)
    assert eff["window"] == [[1, "unreviewed"]] and "summary" not in eff["annotation"]
    eff = compose_effective([rev(1, page1["annotation"], "rejected")], order)
    assert eff["annotation"] is None and eff["review_state"] == "rejected"
    # a single-chunk source: the newest revision is the whole annotation (unchanged behaviour)
    one = [order[0]]
    eff = compose_effective([rev(1, {"summary": "old", "tags": ["x"]}), rev(2, {"tags": ["y"]})], one)
    assert eff["annotation"] == {"tags": ["y"]}


# ------------------------------------------- F2: query-time refresh is bounded
class _Reader:
    """Freshness oracle for a store seeded directly (no corpus files)."""

    def __init__(self):
        self.p = {}

    def load(self, ref):
        if ref not in self.p:
            raise C.EnrichmentError("E_NOT_FOUND", "gone")
        return self.p[ref]

    def list_sources(self, cursor, limit):
        return C.SourcePage((), None, 1)


def _bulk(tmp_path, n):
    from brain_enrichment.chunking import Chunker
    from brain_enrichment.store import SQLiteProjectionStore
    from support import make_projection

    root = tmp_path / "w"
    (root / "00-system/registers").mkdir(parents=True)
    reader = _Reader()
    store = SQLiteProjectionStore(root, reader=reader)
    for i in range(n):
        ref = f"cap:cap-20261001-{i:06d}-abcd"
        p = make_projection(ref, [("user-text", ref, f"item {i} token{i} " + "body words " * 30, None)])
        reader.p[ref] = p
        store.upsert_projection(p, Chunker().build(p, C.ChunkPolicy()))
    return root, store, reader


class _Counting:
    def __init__(self, store):
        self._s, self.full_pages, self.fetched = store, 0, []

    def __getattr__(self, name):
        return getattr(self._s, name)

    def list_current(self, after, limit):
        self.full_pages += 1
        return self._s.list_current(after, limit)

    def get_current_items(self, refs):
        self.fetched.append(len(list(refs)))
        return self._s.get_current_items(refs)


def test_query_time_refresh_reads_no_text_beyond_its_batch(tmp_path):
    from brain_enrichment.config import EnrichmentConfig
    from brain_enrichment.surface import build_extension

    root, store, _ = _bulk(tmp_path, 120)
    counting = _Counting(store)
    ext = build_extension(root, EnrichmentConfig(mode="on", retrieval=True), store=counting)
    for q in range(3):
        counting.full_pages, counting.fetched = 0, []
        out = ext.call("brain_retrieve", {"query": f"token{q}"})
        assert out["ok"] and out["path"] == "enrichment", out
        assert out["index"]["state"] == "lagging"
        # no full-text scan of the store per query; text only for the batch being written
        assert counting.full_pages == 0, counting.full_pages
        assert sum(counting.fetched) <= C.INDEX_REFRESH_BATCH, counting.fetched


def test_light_fingerprint_equals_full_fingerprint(stack):
    w, ext, _ = stack
    ref = w.refs["long"]
    item, _ = find_pending(ext, ref)
    c0 = item["page"]["chunks"][0]
    assert submit(ext, item, {"chunk_annotations": [{"chunk_ref": c0["chunk_ref"], "tags": ["x1"]}]},
                  key="fp-page-0001")["ok"]
    store = ext.s.store()
    store.record_review({"source_ref": ref, "annotation_revision": 1, "decision": "rejected", "note": "n"}, HUMAN)
    idx = ext.s.index(create=True)
    full = {it["projection"].source_ref: idx._fingerprint(it, "s") for it in idx._iter_items(store)}
    light = {r: fp for r, fp, _ in idx._scan(store, "s")}
    assert full and full == light


def test_query_time_refresh_never_waits_for_another_writer(tmp_path):
    import fcntl
    import os
    import time as _t

    from brain_enrichment.indexing import IndexBuilder

    root, store, _ = _bulk(tmp_path, 30)
    ib = IndexBuilder(store, root / C.INDEX_REL, wiki_root=root)
    assert ib.refresh(5).activated                          # generation 1 exists, 25 still lagging
    fd = os.open(ib._lock_path(), os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)                          # "the CLI" holds the writer lock
    try:
        t0 = _t.monotonic()
        busy = ib.refresh(25, blocking=False)
        assert _t.monotonic() - t0 < 1.0 and not busy.activated and "skipped" in busy.lag
        with ib.open_reader() as rd:                        # readers keep the healthy generation
            assert rd.generation == 1
    finally:
        os.close(fd)
    assert ib.refresh(25, blocking=False).activated


def test_query_time_refresh_time_cap_activates_what_it_wrote(tmp_path):
    from brain_enrichment.indexing import IndexBuilder

    root, store, _ = _bulk(tmp_path, 12)
    ib = IndexBuilder(store, root / C.INDEX_REL, wiki_root=root)
    r = ib.refresh(25, max_seconds=0.0)
    assert r.activated and r.artifacts_indexed == 1 and r.artifacts_pending == 11 and r.lag["stopped"] == "time cap"
    assert ib.status()["lag"]["pending_artifacts"] == 11


# --------------------------------- F3: the release must pass its own allowlist
def test_the_r1_release_diff_is_inside_the_upgrade_allowlist():
    """`upgrade-enrichment.py preflight/plan` STOPs on any path of base..target outside the
    allowlist. The integrated release itself touched docs/UPGRADE.md and tests/conftest.py, so
    the runbook commands stopped on the very release they ship (seen on a real rehearsal)."""
    import importlib.util
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    base = "a245b3b2620800b1d2d6cc4be0dba6e44fb5b103"
    have = subprocess.run(["git", "-C", str(root), "cat-file", "-e", base + "^{commit}"], capture_output=True)
    if have.returncode != 0:
        pytest.skip("base release commit not in this clone (shallow history)")
    spec = importlib.util.spec_from_file_location("upgrade_enrichment_r", root / "scripts/upgrade-enrichment.py")
    up = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = up                     # dataclasses resolve their module by name
    spec.loader.exec_module(up)
    paths = subprocess.run(["git", "-C", str(root), "diff", "--name-only", "--no-renames", base, "HEAD"],
                           capture_output=True, text=True, check=True).stdout.split()
    refused = {p: up.classify_path(p) for p in paths if up.classify_path(p)[0] in ("outside", "forbidden")}
    assert not refused, refused


def test_level2_rollback_keeps_the_sidecar_and_says_when_it_is_untracked(rig, tmp_path):
    """After a level-2 rollback a pre-R1 .gitignore no longer ignores _enrichment/ (kept: it is
    data). The operator must be told, since a manual `git add -A` would commit private text."""
    import importlib.util
    import sys

    rc, o, e, plan_path = rig.plan()
    copy = rig.root / "copy"
    assert rig.run("apply", "--wiki", rig.wiki, "--plan", plan_path, "--copy-dir", copy)[0] == 0
    (copy / "_enrichment").mkdir()
    (copy / "_enrichment" / "store-v1.sqlite3").write_bytes(b"x")
    work = rig.root / "copy.work"
    rc, o, e = rig.run("rollback", "--wiki", rig.wiki, "--level", 2, "--plan", plan_path, "--work-dir", work, "--copy-dir", copy)
    rc, o, e = rig.run("rollback", "--wiki", rig.wiki, "--level", 2, "--plan", plan_path, "--work-dir", work, "--copy-dir", copy,
                       "--execute", "--confirm", rig.token(o))
    assert rc == 0 and "ROLLBACK OK" in o, (o, e)
    assert (copy / "_enrichment" / "store-v1.sqlite3").read_bytes() == b"x"      # data is never removed
    up = sys.modules.get("upgrade_enrichment_r")
    if up is None:
        root = Path(__file__).resolve().parents[2]
        spec = importlib.util.spec_from_file_location("upgrade_enrichment_r", root / "scripts/upgrade-enrichment.py")
        up = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = up
        spec.loader.exec_module(up)
    restored_ignores = "_enrichment/" in (copy / ".gitignore").read_text().splitlines()
    assert ("no longer git-ignored" in o) == (not restored_ignores), o
    w = tmp_path / "w"
    (w / "_enrichment").mkdir(parents=True)
    (w / ".gitignore").write_text("_search/\n")                          # a pre-R1 .gitignore
    assert "no longer git-ignored" in up.sidecar_untracked_warning(w)
    (w / ".gitignore").write_text("_search/\n_enrichment/\n")
    assert up.sidecar_untracked_warning(w) is None


def test_level3_salvage_includes_entry_pages_changed_after_the_backup(rig):
    """An ingest after the backup updates the entry-page markers (HOME/README/SYSTEM_DESIGN/CLAUDE)
    together with the registers. A disaster restore must salvage them with the other inputs."""
    from test_upgrade import run_backup

    dest = rig.root / "bk"
    run_backup(rig, dest)
    arch = next(dest.glob("*.tar.gz"))
    home = rig.wiki / "HOME.md"
    home.write_text(home.read_text(encoding="utf-8") + "\nmarker moved by a post-backup ingest\n", encoding="utf-8")
    work, restore = rig.root / "w3", rig.root / "restored"
    args = ["rollback", "--wiki", rig.wiki, "--level", 3, "--work-dir", work, "--disaster-restore", "--archive", arch,
            "--restore-dir", restore]
    rc, o, e = rig.run(*args)
    rc, o, e = rig.run(*args, "--execute", "--confirm", rig.token(o))
    assert rc == 0 and "DISASTER RESTORE OK" in o, (o, e)
    salv = next(work.glob("post-backup-inputs-*"))
    assert (salv / "files" / "HOME.md").read_bytes() == home.read_bytes()


# ------------------------------------------------ data preservation (review gate 4)
def _all_hashes(root):
    """sha256 + mode of EVERY file under root, .git included, tracked/untracked/ignored alike.
    Derived, git-ignored locations are listed separately: the enrichment sidecar (`_enrichment/`,
    `_search/enrichment/`) and the base search's own rebuildable QMD index (`_search/qmd/`, used
    when WIKI_QMD_HOME is unset; production keeps it in the state dir)."""
    import hashlib
    import os

    keep, sidecar = {}, set()
    for dp, _dn, fns in os.walk(root):
        for f in fns:
            p = Path(dp) / f
            rel = p.relative_to(root).as_posix()
            if rel.startswith(("_enrichment/", "_search/enrichment/", "_search/qmd/")):
                sidecar.add(rel)
                continue
            keep[rel] = (hashlib.sha256(p.read_bytes()).hexdigest(), os.lstat(p).st_mode)
    return keep, sidecar


def test_scan_annotate_index_retrieve_change_no_corpus_byte(stack):
    import subprocess

    from test_e2e_extension import scan_all
    import wiki_capture as wc

    w, ext, _ = stack
    root = w.root
    # an ignored runtime file, an untracked note and an uncommitted capture next to the committed corpus
    (root / "_audits/runtime").mkdir(parents=True, exist_ok=True)
    (root / "_audits/runtime/report.json").write_text('{"ignored": true}\n', encoding="utf-8")
    (root / "01-inbox/untracked-note.md").write_text("untracked note\n", encoding="utf-8")
    loose = wc.capture_text("یادداشت ثبت‌نشده: هنوز commit نشده است.", "mcp", "fa")
    status = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--ignored"],
                            capture_output=True, text=True).stdout
    assert "!! _audits/" in status and "?? 01-inbox/untracked-note.md" in status
    h0, side0 = _all_hashes(root)
    assert not side0

    scan_all(w)                                                     # discovery + projection (CLI)
    h1, side1 = _all_hashes(root)
    assert h1 == h0 and side1 and all(p.startswith("_enrichment/") for p in side1)

    item, _ = find_pending(ext, w.refs["fa_short"])                 # annotation of a capture
    assert submit(ext, item, {"summary": "plan", "tags": ["fitness"]}, key="keep-bytes-0001")["ok"]
    h2, _ = _all_hashes(root)
    assert h2 == h0

    build_index(ext)                                                # derived index
    h3, side3 = _all_hashes(root)
    assert h3 == h0 and any(p.startswith("_search/enrichment/") for p in side3)
    assert all(p.startswith(("_enrichment/", "_search/enrichment/", "_search/qmd/")) for p in side3)

    for q in ("فیت پلن", "fitness", "ferry Kolan", "Project Nil", "هنوز commit نشده"):
        assert ext.call("brain_retrieve", {"query": q})["ok"]
    assert ext.call("brain_artifact_read", {"source_ref": w.refs["held_ok"]})["ok"]
    h4, _ = _all_hashes(root)
    assert h4 == h0

    # --- the corpus moves on (owner/promotion edits, a removal, a new capture DURING a backfill pass)
    canon = root / w.paths["canonical"]
    old_chunk = ext.call("brain_artifact_read", {"source_ref": w.refs["canonical"]})["chunks"]["items"][0]["chunk_ref"]
    dup_b = ext.call("brain_artifact_read", {"source_ref": w.refs["dup_b"]})["chunks"]["items"][0]["chunk_ref"]
    canon.write_text(canon.read_text(encoding="utf-8").replace("second novel", "second novel, renamed Nile Delta"),
                     encoding="utf-8")
    (root / w.paths["dup_b"]).unlink()
    ext.call("brain_enrichment_pending", {"limit": 1})                # a backfill pass is under way ...
    fresh = wc.capture_text("ورودی تازه حین backfill: Zebrafish protocol", "mcp", "fa")   # ... a capture arrives
    h5, _ = _all_hashes(root)

    # stale and deleted chunks are never served as current evidence
    stale = ext.call("brain_artifact_read", {"source_ref": w.refs["canonical"], "chunk_ref": old_chunk})
    assert not stale["ok"] and stale["error"]["code"] == "E_STALE_SOURCE", stale
    gone = ext.call("brain_artifact_read", {"source_ref": w.refs["dup_b"], "chunk_ref": dup_b})
    assert not gone["ok"] and gone["error"]["code"] in ("E_NOT_FOUND", "E_STALE_SOURCE"), gone
    out = ext.call("brain_retrieve", {"query": "Project Nil working title"})
    assert all(h["chunk_ref"] != old_chunk for h in _hits(out))
    # the new capture is findable before the backfill reaches it (explicit source-level fallback) ...
    early = ext.call("brain_retrieve", {"query": "Zebrafish protocol"})
    assert any(h["source_ref"] == f"cap:{fresh['id']}" for h in _hits(early)), early
    # ... and as chunk text once discovery has run; the edited record serves its NEW text
    scan_all(w)
    build_index(ext)
    late = ext.call("brain_retrieve", {"query": "Zebrafish protocol"})
    assert any(h["source_ref"] == f"cap:{fresh['id']}" and h["chunk_ref"] for h in _hits(late)), late
    nile = ext.call("brain_retrieve", {"query": "Nile Delta"})
    assert any(h["source_ref"] == w.refs["canonical"] and "Nile Delta" in h["text"] for h in _hits(nile)), nile
    assert ext.call("brain_artifact_read", {"source_ref": w.refs["dup_b"]})["error"]["code"] == "E_NOT_FOUND"
    h6, _ = _all_hashes(root)
    assert h6 == h5, sorted(k for k in set(h5) | set(h6) if h5.get(k) != h6.get(k))
    assert (root / loose["path"]).is_file() and (root / "_audits/runtime/report.json").is_file()


# ------------------------------- F6: an edited source is never a silent "not found"
def test_source_edited_after_indexing_is_found_before_the_next_discovery(stack):
    """A promotion edits a canonical record; a transcript is added to a voice capture. Until the
    next discovery pass the index holds the old version: retrieval must neither serve the old
    chunk nor answer an empty, 'fresh' result while the base search sees the new text."""
    from test_e2e_extension import scan_all
    import wiki_capture as wc

    w, ext, base = stack
    scan_all(w)
    build_index(ext)
    canon = w.root / w.paths["canonical"]
    canon.write_text(canon.read_text(encoding="utf-8").replace("second novel", "second novel, renamed Nile Delta"),
                     encoding="utf-8")
    wc.record_transcript(w.refs["voice_plain"][4:], "متن پیاده‌شده: جلسهٔ Quasar ساعت نه است.\n", "fixture-stt", "1",
                         reviewed=False)
    assert base.call("brain_search", {"query": "Nile Delta", "mode": "exact", "scope": "all"})["groups"]["canonical"]["count"]

    new = ext.call("brain_retrieve", {"query": "Nile Delta"})
    hits = _hits(new)
    assert any(h["source_ref"] == w.refs["canonical"] and h["hit_level"] == "source" and h["chunk_ref"] is None
               for h in hits), new
    assert new["coverage"]["source_level_fallback_hits"] >= 1
    voice = ext.call("brain_retrieve", {"query": "Quasar"})
    assert any(h["source_ref"] == w.refs["voice_plain"] for h in _hits(voice)), voice

    old = ext.call("brain_retrieve", {"query": "working title second novel"})
    assert old["coverage"]["stale_dropped"] >= 1 and old["index"]["fresh"] is False
    assert any("changed on disk" in n for n in old["notes"]), old["notes"]
    assert all(h["chunk_ref"] is None for h in _hits(old) if h["source_ref"] == w.refs["canonical"])


def test_cursor_pages_keep_the_supplemented_order(stack):
    """Page 1 found nothing in the index and added base-search hits for sources edited after
    indexing; later pages must rebuild that same order (the cursor says so): every hit once."""
    from test_e2e_extension import scan_all
    from support import record

    w, ext, _ = stack
    record(w.root, "03-objects/works/kavir.md", "obj-kavir", "Project Kavir", "Kavir is a desert novel.")
    scan_all(w)
    build_index(ext)
    for rel in (w.paths["canonical"], "03-objects/works/kavir.md"):
        p = w.root / rel
        p.write_text(p.read_text(encoding="utf-8") + "\nZanzibar quokka appendix.\n", encoding="utf-8")
    seen, cursor = [], None
    for _ in range(10):
        page = ext.call("brain_retrieve", {"query": "Zanzibar quokka", "limit": 1, **({"cursor": cursor} if cursor else {})})
        assert page["ok"], page
        seen += [h["source_ref"] for v in page["groups"].values() for h in v["results"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert sorted(seen) == sorted({w.refs["canonical"], "rec:obj-kavir"}), seen


def test_a_held_file_is_one_holding_in_supplemented_results(stack):
    """With the index lagging, base-search hits are added for uncovered sources. The derivative
    `doc:` of an ingested file whose `rec:` holding is indexed is covered: never a second entry."""
    from test_e2e_extension import scan_all
    import wiki_capture as wc

    w, ext, _ = stack
    scan_all(w)
    build_index(ext)
    wc.capture_text("a new capture makes the index lag: ferry", "mcp", "en")
    out = ext.call("brain_retrieve", {"query": "Kolan"})
    assert out["index"]["state"] == "lagging" and out["coverage"]["source_level_fallback_hits"] >= 0
    refs = [h["source_ref"] for h in _hits(out)]
    assert w.refs["held_ok"] in refs, out
    assert w.refs["held_ok_text"] not in refs, refs


# ------------------------------------------- F8: the input-size limit reaches the store
def test_max_input_chars_config_bounds_the_pending_page(enrichment_wiki):
    """BRAIN_ENRICHMENT_MAX_INPUT_CHARS caps a page of text offered to the annotator; the real
    store factory ignored it and always used the 24000 default."""
    from brain_enrichment.config import EnrichmentConfig
    from brain_enrichment.surface import build_extension

    w = enrichment_wiki
    cfg = EnrichmentConfig.from_env({"BRAIN_ENRICHMENT": "on", "BRAIN_ENRICHMENT_MAX_INPUT_CHARS": "2000"})
    assert cfg.max_input_chars == 2000 and not cfg.warnings
    ext = build_extension(w.root, cfg)
    item, cursor = None, None
    for _ in range(20):
        out = ext.call("brain_enrichment_pending", {"limit": 1, **({"cursor": cursor} if cursor else {})})
        assert out["ok"], out
        item = next((i for i in out["items"] if i["source_ref"] == w.refs["long"]), None)
        if item:
            break
        cursor = out["next_cursor"]
    assert item is not None
    page = item["page"]
    assert page["total"] >= 2 and page["returned"] == 1 and page["truncated"], page


def test_chunker_fuzz_partition_substring_bounds_crlf_determinism():
    """Random Persian/English/CRLF/lone-CR/table/front-matter/page-marker texts: chunks partition
    the layer, each is the exact substring, none exceeds HARD_MAX, a CRLF pair is never split and
    the output is deterministic (review gate: offsets and coverage)."""
    import random

    from brain_enrichment.chunking import Chunker
    from support import make_projection

    pieces = ["# عنوان\n", "## Page 3\n", "## Slide 2\n", "\n", "\r\n", "\r", "| a | b |\n", "|---|---|\n",
              "پاراگراف فارسی با نیم‌فاصله و ۱۲۳ رقم. ", "English sentence here. ", "no-boundary-" * 50, "؟ ",
              "---\nid: x\n---\n", "\\## escaped\n", "‌", "\U0001F600", "x" * 700, "\n\n", "line\r\nline2\r\n"]
    rng = random.Random(20261010)
    ref = "cap:cap-20261001-000000-abcd"
    for _ in range(400):
        text = "".join(rng.choice(pieces) for _ in range(rng.choice([1, 5, 50, 200, 600, 1500])))
        p = make_projection(ref, [("user-text", ref, text, None)])
        chunks = Chunker().build(p, C.ChunkPolicy())
        pos = 0
        for c in chunks:
            assert c.start_char == pos and c.text == text[c.start_char:c.end_char]
            assert 0 < c.end_char - c.start_char <= C.HARD_MAX
            assert not (c.start_char and text[c.start_char - 1] == "\r" and text[c.start_char] == "\n")
            pos = c.end_char
        assert pos == len(text)
        assert [(c.chunk_ref, c.start_char) for c in Chunker().build(p, C.ChunkPolicy())] == \
               [(c.chunk_ref, c.start_char) for c in chunks]


def test_corrupt_store_and_index_never_break_the_seven_base_tools(enrichment_wiki):
    """Garbage in both sidecar files (disk fault, half-copied restore): through the real adapter
    with the feature on, the base tools keep working, the extension answers with a clean error
    (no path, no traceback) and brain_retrieve falls back to the base search explicitly."""
    from remote_mcp import adapter as A

    w = enrichment_wiki
    env = {"BRAIN_ENRICHMENT": "on", "BRAIN_ENRICHMENT_WRITES": "1", "BRAIN_ENRICHMENT_RETRIEVAL": "1"}
    tools = {t.name: t for t in A.semantic_adapter(w.root, env=env)}
    assert tools["brain_enrichment_pending"].handler({"limit": 1})["ok"]          # create a real store
    for rel in (C.STORE_REL, C.INDEX_REL):
        p = w.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(str(p) + suffix).unlink(missing_ok=True)
        p.write_bytes(b"this is not a sqlite database" * 100)
    tools = {t.name: t for t in A.semantic_adapter(w.root, env=env)}            # a fresh server process
    assert len(tools) == 11
    cap = tools["brain_capture"].handler({"text": "capture while the sidecar is broken", "language_hint": "en"})
    assert cap["ok"] and cap["persisted"], cap
    assert tools["brain_read"].handler({"ref": cap["ref"]})["ok"]
    assert tools["brain_search"].handler({"query": "sidecar is broken", "mode": "exact", "scope": "captures"})["ok"]
    assert tools["brain_status"].handler({})["ok"]
    pend = tools["brain_enrichment_pending"].handler({"limit": 1})
    assert pend["ok"] is False and pend["error"]["code"] in ("E_UNAVAILABLE", "E_SCHEMA_UNSUPPORTED"), pend
    assert str(w.root) not in json.dumps(pend) and "Traceback" not in json.dumps(pend)
    ret = tools["brain_retrieve"].handler({"query": "sidecar is broken"})
    assert ret["ok"] and ret["path"].startswith("fallback-") and ret["query_model_calls"] == 0, ret
    assert any(h["source_ref"] == cap["ref"] for g in ret["groups"].values() for h in g["results"]), ret
