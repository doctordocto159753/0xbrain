"""WP-C indexing: FTS5 probe, generations, atomic activation, bounded refresh,
crash safety, rebuildability, annotation exclusion (CONTRACT-E1 section 7)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from brain_enrichment import indexing as IX
from brain_enrichment.indexing import IndexBuilder
from retrieval_support import Corpus, DerivedStore, cap_ref
from support import corpus_hashes

FIT = "فیت پلن: هر روز ۳۰ دقیقه پیاده‌روی و دو بار در هفته تمرین قدرتی انجام بدهم."


@pytest.fixture()
def corpus():
    c = Corpus(DerivedStore())
    c.capture(FIT)
    c.capture("Meeting with the translator about the Kolan ferry chapter.")
    c.canonical("obj-nil", "# Project Nil\n\nProject Nil is the working title of the second novel.\n")
    return c


@pytest.fixture()
def ib(corpus, tmp_path):
    return IndexBuilder(corpus.store, tmp_path / "_search/enrichment/search-v1.sqlite3")


def chunk_hits(builder, q):
    with builder.open_reader() as rd:
        return [r["chunk_ref"] for r in rd.search_chunks(q, 20)]


def row_counts(path):
    con = sqlite3.connect(path)
    try:
        return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in IX.ROW_TABLES}
    finally:
        con.close()


# ------------------------------------------------------------------ probe
def test_fts5_probe_and_unavailable_is_never_an_exception(ib, monkeypatch):
    assert IX.fts5_available() is True
    monkeypatch.setattr(IX, "fts5_available", lambda: False)
    st = ib.status()
    assert st["state"] == "unavailable" and st["generation"] is None
    receipt = ib.refresh(5)
    assert receipt.activated is False and receipt.lag["state"] == "unavailable"
    assert receipt.artifacts_pending == 3
    assert not ib.index_path.exists()                    # nothing half-created
    with pytest.raises(IX.IndexUnavailable) as e:
        ib.open_reader()
    assert e.value.state == "unavailable"


# ------------------------------------------------------------- generations
def test_first_refresh_builds_generation_one_and_second_is_a_noop(ib):
    assert ib.status()["state"] == "missing"
    r1 = ib.refresh(10)
    assert (r1.generation, r1.activated, r1.artifacts_indexed, r1.artifacts_pending) == (1, True, 3, 0)
    st = ib.status()
    assert st["state"] == "ok" and st["generation"] == 1 and st["indexed_artifacts"] == 3 and st["fresh"]
    r2 = ib.refresh(10)
    assert r2.activated is False and r2.generation == 1 and r2.artifacts_indexed == 0
    assert chunk_hits(ib, '"فیت" AND "پلن"')


def test_changes_make_a_new_generation_and_old_rows_are_compacted(corpus, ib):
    ib.refresh(10)
    n0 = row_counts(ib.index_path)
    ref = next(iter(corpus.proj))
    old = corpus.store.chunks[ref][0].chunk_ref
    corpus.replace_text(ref, "کاملاً متن تازه دربارهٔ باغچه و گل رز")
    r = ib.refresh(10)
    assert r.activated and r.generation == 2 and r.artifacts_indexed == 1
    assert old not in chunk_hits(ib, '"فیت"')
    assert chunk_hits(ib, '"باغچه"')
    assert row_counts(ib.index_path) == n0               # retired rows are physically gone after activation
    fts_rows = sqlite3.connect(ib.index_path).execute("SELECT COUNT(*) FROM fts_chunks").fetchone()[0]
    assert fts_rows == n0["chunk_rows"]


def test_refresh_is_bounded_and_reports_lag(corpus, tmp_path):
    for i in range(7):
        corpus.capture(f"capture number {i} about gardens", cap_ref(100 + i))
    ib = IndexBuilder(corpus.store, tmp_path / "idx.sqlite3")
    r = ib.refresh(4)
    assert r.artifacts_indexed == 4 and r.artifacts_pending == 6 and r.lag["pending_artifacts"] == 6
    st = ib.status()
    assert st["state"] == "lagging" and st["lag"]["pending_artifacts"] == 6 and not st["fresh"]
    r = ib.refresh(4)
    assert r.artifacts_indexed == 4 and r.artifacts_pending == 2
    r = ib.refresh(4)
    assert r.artifacts_indexed == 2 and r.artifacts_pending == 0
    assert ib.status()["state"] == "ok" and ib.status()["generation"] == 3


def test_a_reader_keeps_one_complete_generation_while_a_new_one_activates(corpus, ib):
    ib.refresh(10)
    rd = ib.open_reader()
    try:
        before = [r["chunk_ref"] for r in rd.search_chunks('"فیت"', 10)]
        ref = next(iter(corpus.proj))
        corpus.replace_text(ref, "متن دیگر دربارهٔ باغچه")
        assert ib.refresh(10).generation == 2
        assert rd.generation == 1
        assert [r["chunk_ref"] for r in rd.search_chunks('"فیت"', 10)] == before   # still gen 1, complete
    finally:
        rd.close()
    assert not chunk_hits(ib, '"فیت"')                   # a new reader sees generation 2


def test_crash_mid_build_keeps_previous_generation_readable(corpus, ib):
    ib.refresh(10)
    ref0 = next(iter(corpus.proj))
    expected = chunk_hits(ib, '"فیت" AND "پلن"')
    for i in range(3):
        corpus.capture(f"new capture {i} about gardens", cap_ref(200 + i))
    corpus.replace_text(ref0, "متن بازنویسی‌شده دربارهٔ باغچه")
    calls = []

    def crash():
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("simulated crash after the second artifact committed")

    ib._after_artifact = crash
    with pytest.raises(RuntimeError):
        ib.refresh(10)
    ib._after_artifact = None
    st = ib.status()
    assert st["generation"] == 1 and st["state"] == "lagging" and st["indexed_artifacts"] == 3
    assert chunk_hits(ib, '"فیت" AND "پلن"') == expected      # previous generation, unchanged
    assert not chunk_hits(ib, '"باغچه"')                       # nothing of the half-built one is visible
    con = sqlite3.connect(ib.index_path)                       # orphan rows exist but are invisible
    assert con.execute("SELECT COUNT(*) FROM artifacts WHERE gen_from > 1").fetchone()[0] >= 1
    con.close()
    r = ib.refresh(10)                                          # next build discards them and completes
    assert r.activated and r.generation == 2 and r.artifacts_pending == 0
    con = sqlite3.connect(ib.index_path)
    assert con.execute("SELECT COUNT(*) FROM artifacts WHERE gen_to IS NULL").fetchone()[0] == 6
    assert con.execute("SELECT COUNT(*) FROM artifacts WHERE gen_from > 2").fetchone()[0] == 0
    con.close()
    assert chunk_hits(ib, '"باغچه"')


def test_crash_before_any_activation_leaves_index_missing_not_half_built(corpus, ib):
    def crash():
        raise RuntimeError("boom")

    ib._after_artifact = crash
    with pytest.raises(RuntimeError):
        ib.refresh(10)
    assert ib.status()["state"] == "missing"
    ib._after_artifact = None
    assert ib.refresh(10).generation == 1 and ib.status()["state"] == "ok"


# ------------------------------------------------------------ rebuildability
def test_deleting_the_index_loses_nothing(corpus, ib):
    ib.refresh(10)
    hits = chunk_hits(ib, '"فیت" AND "پلن"')
    ib.index_path.unlink()
    for suffix in ("-wal", "-shm"):
        Path(str(ib.index_path) + suffix).unlink(missing_ok=True)
    assert ib.status()["state"] == "missing"
    r = ib.rebuild_full()
    assert r.activated and r.artifacts_indexed == 3
    assert chunk_hits(ib, '"فیت" AND "پلن"') == hits


def test_rebuild_full_is_explicit_and_re_adds_everything(corpus, ib):
    ib.refresh(10)
    r = ib.rebuild_full()
    assert r.generation == 2 and r.artifacts_indexed == 3
    assert ib.status()["indexed_artifacts"] == 3
    assert row_counts(ib.index_path)["artifacts"] == 3


def test_corrupt_index_is_reported_unavailable_and_only_rebuild_replaces_it(corpus, ib):
    ib.refresh(10)
    for suffix in ("-wal", "-shm"):
        Path(str(ib.index_path) + suffix).unlink(missing_ok=True)
    ib.index_path.write_bytes(b"this is not a sqlite database" * 100)
    st = ib.status()
    assert st["state"] == "unavailable" and "corrupt" in st["reason"]
    r = ib.refresh(10)
    assert r.activated is False and r.lag["state"] == "unavailable"
    assert ib.index_path.read_bytes().startswith(b"this is not")        # refresh never destroys it
    assert ib.rebuild_full().activated
    assert ib.status()["state"] == "ok"


def test_unsupported_index_schema_is_unavailable(corpus, ib):
    ib.refresh(10)
    con = sqlite3.connect(ib.index_path)
    con.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
    con.commit()
    con.close()
    assert ib.status()["state"] == "unavailable"


# --------------------------------------------------------------- annotations
def test_annotation_metadata_is_indexed_and_rejected_revision_is_excluded(tmp_path):
    c = Corpus(DerivedStore())
    a = c.capture(FIT)
    rev = c.annotate(a.source_ref, {"summary": "برنامهٔ ورزش", "topics": ["ورزش"],
                                    "entities": [{"name": "فیت پلن", "kind": "project", "aliases": ["fit plan"],
                                                  "mention": {"chunk_ref": "FIRST", "text": "فیت پلن"}}]})
    ib = IndexBuilder(c.store, tmp_path / "i.sqlite3")
    ib.refresh(10)
    with ib.open_reader() as rd:
        assert rd.search_meta('"fit" AND "plan"', 5)
        art = rd.artifact_by_ref(a.source_ref)
        assert art["annotation_revision"] == rev
        assert any(e["label"] == "mentions" and e["edge_kind"] == "semantic"
                   for e in rd.neighbors("art:" + a.artifact_id))
    c.store.review_state_of[(a.source_ref, rev)] = "rejected"
    r = ib.refresh(10)                                   # the review state changes the fingerprint
    assert r.activated and r.artifacts_indexed == 1
    with ib.open_reader() as rd:
        assert not rd.search_meta('"fit" AND "plan"', 5)
        assert not rd.search_meta('"ورزش"', 5)
        assert rd.artifact_by_ref(a.source_ref)["annotation_revision"] is None
        assert rd.neighbors("art:" + a.artifact_id) == []
        assert rd.search_chunks('"فیت"', 5)              # the text itself stays searchable


def test_stale_annotation_revision_is_never_indexed_as_current(tmp_path):
    c = Corpus(DerivedStore())
    a = c.capture(FIT)
    c.annotate(a.source_ref, {"topics": ["ورزش"], "tags": ["walking"]})
    c.replace_text(a.source_ref, "متن عوض شد دربارهٔ باغچه")          # new source_version: old revision is stale
    ib = IndexBuilder(c.store, tmp_path / "i.sqlite3")
    ib.refresh(10)
    with ib.open_reader() as rd:
        assert not rd.search_meta('"walking"', 5) and not rd.search_meta('"ورزش"', 5)
        assert rd.artifact_by_ref(a.source_ref)["annotation_revision"] is None


def test_removed_artifact_leaves_the_index(corpus, ib):
    ib.refresh(10)
    ref = next(iter(corpus.proj))
    corpus.delete(ref)
    r = ib.refresh(10)
    assert r.activated and r.lag["removed_artifacts"] == 1
    with ib.open_reader() as rd:
        assert rd.artifact_by_ref(ref) is None and rd.counts()["artifacts"] == 2


def test_no_text_artifact_is_indexed_without_chunks(tmp_path):
    c = Corpus(DerivedStore())
    c.no_text("src-bbbbbbbbbbbb")
    c.capture("hello world text")
    ib = IndexBuilder(c.store, tmp_path / "i.sqlite3")
    ib.refresh(10)
    with ib.open_reader() as rd:
        assert rd.counts() == {"artifacts": 2, "chunks": 1}
        assert rd.artifact_by_ref("rec:src-bbbbbbbbbbbb")["chunk_count"] == 0


def test_index_chunk_refs_equal_store_chunk_refs_and_text_is_not_stored_as_evidence(corpus, ib):
    ib.refresh(10)
    with ib.open_reader() as rd:
        for ref, chunks in corpus.store.chunks.items():
            for ch in chunks:
                info = rd.chunk_info(ch.chunk_ref)
                assert info and info["source_ref"] == ref and info["source_version"] == ch.source_version
                assert (info["start_char"], info["end_char"]) == (ch.start_char, ch.end_char)


# ------------------------------------------------------------------ real data
def test_index_over_real_fixture_corpus_does_not_mutate_it(enrichment_wiki, tmp_path):
    w = enrichment_wiki
    before = corpus_hashes(w.root)
    c = Corpus(DerivedStore())
    for name in ("fa_short", "crlf", "escaped_headings", "dup_a", "dup_b"):
        c.capture(w.texts[name], w.refs[name])
    c.ingested(w.refs["held_ok"][4:], w.texts["held_ok_text"], doc=w.refs["held_ok_text"])
    c.canonical("obj-nil", (w.root / w.paths["canonical"]).read_text(encoding="utf-8"))
    ib = IndexBuilder(c.store, w.root / "_search/enrichment/search-v1.sqlite3", wiki_root=w.root)
    assert ib.refresh(25).artifacts_indexed == 7
    assert corpus_hashes(w.root) == before                     # _search/ is outside the witness, the rest identical
    with ib.open_reader() as rd:
        assert rd.search_chunks('"فیت" AND "پلن"', 5)
        dup = rd.search_chunks('"same" AND "words"', 5)
        assert len({r["artifact_id"] for r in dup}) == 2       # duplicate captures stay two artifacts
