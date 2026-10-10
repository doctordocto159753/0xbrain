"""Regression tests for the independent R1 review (REVIEW_FIX_REPORT.md).

Each test reproduces one finding on the REAL stack (SQLite store, annotation
service, FTS5 index, retrieval engine, extension surface) over the seeded
`enrichment_wiki`, and fails on the integrated commit before the fix.
"""
from __future__ import annotations

import pytest

from brain_enrichment import contract as C  # noqa: E402
from test_e2e_extension import CFG, build_index, find_pending, stack, submit  # noqa: E402,F401


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
