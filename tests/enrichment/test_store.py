"""SQLiteProjectionStore (CONTRACT-E1 section 6): real SQLite files, real
threads, real subprocess kills. Synthetic projections use a tiny StubReader
only where a test needs a source that never existed on disk."""
from __future__ import annotations

import hashlib
import io
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import brain_enrichment.store as S
from brain_enrichment import contract as C
from brain_enrichment.chunking import Chunker
from brain_enrichment.refs import resolve_chunk_ref
from brain_enrichment.source import SourceReader
from brain_enrichment.store import SQLiteProjectionStore, open_store
from support import add_held_file, corpus_hashes, long_text, make_projection

POLICY = C.ChunkPolicy()
REF_A = "cap:cap-20260101-000000-aaaa"
REF_B = "cap:cap-20260101-000000-bbbb"
HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[1] / "scripts"


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def err(fn, *a, **kw) -> C.EnrichmentError:
    with pytest.raises(C.EnrichmentError) as e:
        fn(*a, **kw)
    return e.value


class StubReader:
    """Serves given projections; load() can be told to fail. Nothing on disk."""

    def __init__(self, *projections):
        self.items = {p.source_ref: p for p in projections}
        self.fail: dict[str, C.EnrichmentError] = {}
        self.loads = 0

    def put(self, p):
        self.items[p.source_ref] = p

    def list_sources(self, cursor, limit):
        refs = sorted(self.items)
        after = C.decode_cursor(cursor, "sources")["a"] if cursor else None
        rest = [r for r in refs if after is None or r > after]
        page = rest[:limit]
        nxt = C.encode_cursor("sources", a=page[-1]) if len(rest) > limit else None
        return C.SourcePage(tuple(page), nxt, 1, len(refs))

    def load(self, ref):
        self.loads += 1
        if ref in self.fail:
            raise self.fail[ref]
        if ref not in self.items:
            raise C.EnrichmentError("E_NOT_FOUND", f"{ref}: gone")
        return self.items[ref]


def proj(ref=REF_A, text="سلام دنیا. hello world.\n", **kw):
    return make_projection(ref, [("user-text", ref, text, None)], **kw)


def chunks_of(p):
    return Chunker().build(p, POLICY)


def seeded(w, **kw) -> SQLiteProjectionStore:
    s = open_store(w.root, **kw)
    s.refresh(budget=1000)
    return s


def request(p, rev=0, key="key-0000001", ann=None, **extra):
    return {"source_ref": p.source_ref, "source_version": p.source_version, "expected_annotation_revision": rev,
            "idempotency_key": key, "annotation": ann or {"summary": "خلاصه", "tags": ["t1"]}, **extra}


def db(w) -> sqlite3.Connection:
    cx = sqlite3.connect(f"file:{w.root / '_enrichment' / S.DB_NAME}?mode=ro", uri=True)
    cx.row_factory = sqlite3.Row
    return cx


def craft_capture(w, cid: str, text: str | None = None) -> Path:
    """A valid capture record file with a new id, copied from the fixture's fa_short."""
    good = (w.root / w.paths["fa_short"]).read_bytes().decode("utf-8")
    new = good.replace(w.refs["fa_short"][4:], cid)
    if text is not None:
        old = w.texts["fa_short"]
        new = new.replace(old, text)
    d = w.root / "01-inbox/captures" / cid[4:8] / cid[4:10]
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{cid}.md"
    p.write_bytes(new.encode("utf-8"))
    return p


@pytest.fixture()
def w(enrichment_wiki):
    return enrichment_wiki


# ================================================================ schema
def test_store_is_created_lazily_with_the_documented_schema(w):
    s = open_store(w.root)
    assert not (w.root / "_enrichment").exists()
    assert s.status() == {"exists": False, "state": "missing", "schema_version": None, "counts": {}, "by_state": {}}
    assert s.change_marker() == 0 and s.list_current(None, 10) == [] and s.entity_exists("ent:" + "a" * 24) is False
    assert err(s.get, REF_A).code == "E_NOT_FOUND" and err(s.get_chunk, "chk:" + "a" * 32).code == "E_NOT_FOUND"
    assert s.verify()["exists"] is False
    s.refresh(budget=1000)
    d, f = w.root / "_enrichment", w.root / "_enrichment" / "store-v1.sqlite3"
    assert d.stat().st_mode & 0o777 == 0o700 and f.stat().st_mode & 0o777 == 0o600
    cx = db(w)
    assert cx.execute("PRAGMA user_version").fetchone()[0] == C.STORE_SCHEMA_VERSION == 1
    assert cx.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    tables = {r[0] for r in cx.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables >= set(S.REQUIRED_TABLES)
    assert not any(p.suffix == ".md" for p in d.rglob("*"))
    assert s.status()["state"] == "ready" and s.verify()["ok"] is True


def test_documented_ddl_is_exactly_the_code_ddl():
    text = (HERE.parents[1] / "docs/enrichment/store-schema.md").read_text(encoding="utf-8")
    block = text.split("<!-- DDL:begin -->")[1].split("<!-- DDL:end -->")[0]
    assert block.strip("\n").replace("```sql\n", "").replace("\n```", "").strip() == ";\n\n".join(S.DDL).strip() + ";"


def test_newer_schema_makes_only_the_feature_unavailable_and_changes_nothing(w):
    s = seeded(w)
    f = w.root / "_enrichment" / S.DB_NAME
    cx = sqlite3.connect(f)
    cx.execute("PRAGMA user_version=2")
    cx.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    cx.close()
    before = {p.name: sha(p.read_bytes()) for p in f.parent.iterdir() if p.is_file()}
    p = proj()
    calls = [lambda: s.pending(None, 1), lambda: s.upsert_projection(p, chunks_of(p)), lambda: s.get(REF_A),
             lambda: s.get_chunks(REF_A), lambda: s.get_chunk("chk:" + "a" * 32), lambda: s.list_current(None, 5),
             lambda: s.verify(), lambda: s.export_jsonl(io.StringIO()), lambda: s.backup(w.root.parent / "b.sqlite3"),
             lambda: s.append_annotation(request(p)), lambda: s.record_review(
                 {"source_ref": REF_A, "annotation_revision": 1, "decision": "confirmed"}, {"actor": "sina"}),
             lambda: s.refresh(), lambda: s.change_marker(), lambda: s.entity_exists("ent:" + "a" * 24)]
    for call in calls:
        e = err(call)
        assert e.code == "E_SCHEMA_UNSUPPORTED" and e.extra["found"] == 2 and e.extra["supported"] == 1
    assert s.status()["state"] == "unsupported" and s.status()["schema_version"] == 2
    assert {p.name: sha(p.read_bytes()) for p in f.parent.iterdir() if p.is_file()} == before     # never altered
    assert SourceReader(w.root).load(w.refs["fa_short"]).layers      # the base tools/reader are unaffected


def test_unusable_store_files_are_reported_and_left_untouched(w):
    d = w.root / "_enrichment"
    d.mkdir(mode=0o700)
    f = d / S.DB_NAME
    f.write_bytes(b"this is not an sqlite database " * 40)
    before = sha(f.read_bytes())
    s = open_store(w.root)
    assert err(s.pending, None, 1).code == "E_UNAVAILABLE" and err(s.get_chunk, "chk:" + "a" * 32).code == "E_UNAVAILABLE"
    assert sha(f.read_bytes()) == before
    f.unlink()
    cx = sqlite3.connect(f)
    cx.execute("CREATE TABLE someone_elses(x)")
    cx.commit()
    cx.close()
    assert err(s.pending, None, 1).code == "E_SCHEMA_UNSUPPORTED"          # unversioned file with tables: not ours
    assert [r[0] for r in sqlite3.connect(f).execute("SELECT name FROM sqlite_master")] == ["someone_elses"]
    f.unlink()
    f.write_bytes(b"")                                                     # an empty file is a fresh store
    assert open_store(w.root).refresh(budget=1000)["scanned"] == 12


def test_symlinked_enrichment_dir_is_refused(w, tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    os.symlink(target, w.root / "_enrichment")
    assert err(open_store(w.root).refresh).code == "E_UNAVAILABLE"
    assert list(target.iterdir()) == []


# ======================================================= projection upsert
def test_upsert_created_unchanged_new_version_and_no_text(w):
    rd = StubReader()
    s = open_store(w.root, reader=rd)
    p = proj()
    ch = chunks_of(p)
    rd.put(p)
    r1 = s.upsert_projection(p, ch)
    assert (r1.state, r1.chunk_count, r1.previous_version, r1.source_version) == ("created", 1, None, p.source_version)
    r2 = s.upsert_projection(p, ch)
    assert r2.state == "unchanged" and r2.source_version == p.source_version
    marker = s.change_marker()
    s.upsert_projection(p, ch)
    assert s.change_marker() == marker                     # a no-op upsert moves nothing
    p2 = proj(text="سلام دنیا. changed.\n")
    rd.put(p2)                                             # the file on disk moves first, the store follows
    r3 = s.upsert_projection(p2, chunks_of(p2))
    assert (r3.state, r3.previous_version) == ("new_version", p.source_version) and s.change_marker() > marker
    assert s.get(REF_A).projection["source_version"] == p2.source_version
    assert [c.chunk_ref for c in s.get_chunks(REF_A, p2.source_version)] == [c.chunk_ref for c in chunks_of(p2)]
    e = err(s.get, REF_A, p.source_version)
    assert e.code == "E_STALE_SOURCE" and e.extra["source_ref"] == REF_A
    assert err(s.get, REF_A, "f" * 64).code == "E_NOT_FOUND"
    empty = make_projection(REF_B, [])
    rd.put(empty)
    r4 = s.upsert_projection(empty, [])
    assert r4.state == "no_text" and r4.chunk_count == 0
    assert s.get(REF_B).projection["processing_state"] == "no_text" and s.verify()["ok"]
    # the same text under a different source_ref is a different artifact, never merged
    clone = proj(REF_B, text="سلام دنیا. changed.\n")
    rd.put(clone)
    s.upsert_projection(clone, chunks_of(clone))
    assert clone.artifact_id != p2.artifact_id and {c.chunk_ref for c in chunks_of(clone)}.isdisjoint(
        {c.chunk_ref for c in chunks_of(p2)})


def test_superseded_version_is_retained_for_audit_but_not_served(w):
    rd = StubReader()
    s = open_store(w.root, reader=rd)
    p = proj()
    old_chunks = chunks_of(p)
    rd.put(p)
    s.upsert_projection(p, old_chunks)
    p2 = proj(text="new text here\n")
    rd.put(p2)
    s.upsert_projection(p2, chunks_of(p2))
    cx = db(w)
    rows = cx.execute("SELECT source_version, status FROM projections WHERE source_ref=? ORDER BY created_at, status",
                      (REF_A,)).fetchall()
    assert {r["source_version"]: r["status"] for r in rows} == {p.source_version: "stale", p2.source_version: "current"}
    assert cx.execute("SELECT COUNT(*) FROM chunks WHERE source_version=?", (p.source_version,)).fetchone()[0] == 1
    e = err(s.get_chunk, old_chunks[0].chunk_ref)
    assert e.code == "E_STALE_SOURCE"
    assert [c.chunk_ref for c in s.get_chunks(REF_A)] == [c.chunk_ref for c in chunks_of(p2)]
    # reverting to the old text revives that version and its chunk refs
    rd.put(p)
    r = s.upsert_projection(p, old_chunks)
    assert r.state == "new_version" and s.get_chunk(old_chunks[0].chunk_ref).text == p.layers[0].text


@pytest.mark.parametrize("mutate,needle", [
    (lambda p, ch: (p, [C.Chunk(**{**ch[0].__dict__, "text": "tampered"})] + ch[1:]), "layer.text"),
    (lambda p, ch: (p, [C.Chunk(**{**ch[0].__dict__, "end_char": ch[0].end_char - 1, "text": ch[0].text[:-1]})]), "digest"),
    (lambda p, ch: (p, ch[:1] + [C.Chunk(**{**ch[1].__dict__, "start_char": ch[1].start_char + 1,
                                           "text": ch[1].text[1:]})]) if len(ch) > 1 else (p, ch), "digest"),
    (lambda p, ch: (p, ch[1:]), "index"),
    (lambda p, ch: (p, ch[:-1]), "cover"),
    (lambda p, ch: (p, ch + ch[:1]), "index"),
    (lambda p, ch: (p, [C.Chunk(**{**ch[0].__dict__, "chunk_ref": "chk:" + "0" * 32})] + ch[1:]), "chunk_ref"),
    (lambda p, ch: (p, [C.Chunk(**{**ch[0].__dict__, "source_version": "0" * 64})] + ch[1:]), "belong"),
    (lambda p, ch: (p, [C.Chunk(**{**ch[0].__dict__, "next_chunk_ref": "chk:" + "9" * 32})] + ch[1:]), "link"),
])
def test_store_refuses_inconsistent_chunks_and_stores_nothing(w, mutate, needle):
    s = open_store(w.root, reader=StubReader())
    p = proj(text=long_text())
    good = chunks_of(p)
    assert len(good) == 2
    bp, bc = mutate(p, good)
    e = err(s.upsert_projection, bp, bc)
    assert e.code == "E_VALIDATION" and needle in e.message
    assert s.status()["state"] in ("missing", "ready") and s.status().get("counts", {}).get("projections", 0) == 0


def test_store_refuses_a_projection_whose_hashes_or_version_lie(w):
    s = open_store(w.root, reader=StubReader())
    p = proj()
    for bad in (C.SourceProjection(**{**p.__dict__, "source_version": "a" * 64}),
                C.SourceProjection(**{**p.__dict__, "source_ref": "../etc"}),
                C.SourceProjection(**{**p.__dict__, "source_kind": "novel"}),
                C.SourceProjection(**{**p.__dict__, "layers": (C.Layer(**{**p.layers[0].__dict__, "text_sha256": "a" * 64}),)})):
        assert err(s.upsert_projection, bad, chunks_of(p)).code == "E_VALIDATION"
    twin = (p.layers[0], p.layers[0])
    assert err(s.upsert_projection, C.SourceProjection(**{**p.__dict__, "layers": twin}), []).code == "E_VALIDATION"


def test_chunk_ref_collision_and_append_only_are_enforced_by_the_database(w, monkeypatch):
    s = open_store(w.root, reader=StubReader(proj()))
    p = proj()
    c = chunks_of(p)[0]
    s.upsert_projection(p, [c])
    f = w.root / "_enrichment" / S.DB_NAME
    cx = sqlite3.connect(f, isolation_level=None)             # autocommit: no lock is held between statements
    cx.execute("PRAGMA foreign_keys=ON")
    with pytest.raises(sqlite3.IntegrityError):                    # ref must be the first 32 hex of the digest
        cx.execute("UPDATE chunks SET chunk_ref=? WHERE chunk_ref=?", ("chk:" + "0" * 32, c.chunk_ref))
    s.append_annotation(request(p))
    for sql in ("UPDATE annotation_revisions SET annotation='{}'", "DELETE FROM annotation_revisions"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            cx.execute(sql)
    # a colliding ref from different coordinates is refused by the store, never overwritten
    p2 = proj(REF_B, text="other\n")
    c2 = chunks_of(p2)[0]
    forged = C.Chunk(**{**c2.__dict__, "chunk_ref": c.chunk_ref})
    assert err(s.upsert_projection, p2, [forged]).code == "E_VALIDATION"
    # ...and even if validation were bypassed, the insert path itself refuses to overwrite a ref
    with monkeypatch.context() as m:
        m.setattr(SQLiteProjectionStore, "_validate", staticmethod(lambda projection, chunks: None))
        e = err(s.upsert_projection, p2, [forged])
    assert e.code == "E_INTERNAL" and "collision" in e.message
    assert cx.execute("SELECT COUNT(*) FROM projections").fetchone()[0] == 1       # rolled back as a whole
    assert cx.execute("SELECT source_ref FROM chunks WHERE chunk_ref=?", (c.chunk_ref,)).fetchone()[0] == REF_A
    cx.close()


def test_tampered_database_is_detected_by_verify_and_get_chunk(w):
    p = proj(text=long_text())
    s = open_store(w.root, reader=StubReader(p))
    s.upsert_projection(p, chunks_of(p))
    assert s.verify()["ok"]
    cx = sqlite3.connect(w.root / "_enrichment" / S.DB_NAME)
    cx.execute("UPDATE chunks SET end_char = end_char - 1 WHERE idx = 0")
    cx.commit()
    cx.close()
    rep = s.verify()
    codes = {x["code"] for x in rep["problems"]}
    assert not rep["ok"] and {"chunk_digest", "chunk_partition"} <= codes
    assert err(s.get_chunk, chunks_of(p)[0].chunk_ref).code == "E_INTERNAL"
    cx = sqlite3.connect(w.root / "_enrichment" / S.DB_NAME)
    cx.execute("UPDATE layers SET text = text || 'x'")
    cx.commit()
    cx.close()
    assert {"layer_hash", "layer_size"} <= {x["code"] for x in s.verify()["problems"]}
    cx = sqlite3.connect(w.root / "_enrichment" / S.DB_NAME)
    cx.execute("UPDATE layers SET text_sha256 = ?", ("a" * 64,))
    cx.commit()
    cx.close()
    assert "source_version" in {x["code"] for x in s.verify()["problems"]}


# ================================================== annotation: CAS & friends
def annotated_store(w):
    p = proj()
    rd = StubReader(p)
    s = open_store(w.root, reader=rd)
    s.upsert_projection(p, chunks_of(p))
    return s, p, rd


def test_append_cas_idempotency_and_replay(w):
    s, p, rd = annotated_store(w)
    r1 = s.append_annotation(request(p, 0, "key-0000001"))
    assert (r1.annotation_revision, r1.idempotent_replay, r1.processing_state, r1.index_dirty) == (1, False, "annotated", True)
    assert r1.coverage == {"annotated_chunks": 1, "total_chunks": 1} and r1.source_version == p.source_version
    again = s.append_annotation(request(p, 0, "key-0000001"))             # same key, same payload
    assert again.annotation_revision == 1 and again.idempotent_replay is True and again.coverage == r1.coverage
    e = err(s.append_annotation, request(p, 0, "key-0000001", ann={"summary": "different"}))
    assert e.code == "E_IDEMPOTENCY_CONFLICT"
    e = err(s.append_annotation, request(p, 0, "key-0000002"))             # stale expected revision
    assert e.code == "E_CONFLICT" and e.extra["current_revision"] == 1
    r2 = s.append_annotation(request(p, 1, "key-0000003", ann={"tags": ["b"]}))
    assert r2.annotation_revision == 2
    cx = db(w)
    assert [r["revision"] for r in cx.execute("SELECT revision FROM annotation_revisions ORDER BY revision")] == [1, 2]
    # replay after the revision moved still answers with the ORIGINAL receipt
    assert s.append_annotation(request(p, 0, "key-0000001")).annotation_revision == 1
    # derived is excluded from the idempotency digest
    assert s.append_annotation(request(p, 0, "key-0000001", derived={"entities": []})).idempotent_replay is True
    # the same key on another source is a different operation
    q = proj(REF_B, "second\n")
    rd.put(q)
    s.upsert_projection(q, chunks_of(q))
    assert s.append_annotation(request(q, 0, "key-0000001")).annotation_revision == 1


def test_append_rejects_malformed_requests_and_unknown_sources(w):
    s, p, rd = annotated_store(w)
    good = request(p)
    for bad, code in (({**good, "extra": 1}, "E_BAD_ARGUMENTS"), ({**good, "annotation": {}}, "E_BAD_ARGUMENTS"),
                      ({**good, "expected_annotation_revision": -1}, "E_BAD_ARGUMENTS"),
                      ({**good, "expected_annotation_revision": True}, "E_BAD_ARGUMENTS"),
                      ({**good, "idempotency_key": "short"}, "E_BAD_ARGUMENTS"),
                      ({**good, "source_version": "xyz"}, "E_BAD_ARGUMENTS"), ({**good, "source_ref": "../x"}, "E_BAD_REF"),
                      ({**good, "derived": {"entities": [{"entity_id": "nope"}]}}, "E_VALIDATION"),
                      ({**good, "annotation": {"summary": "x" * 1_200_000}}, "E_TOO_LARGE"), ("nope", "E_BAD_ARGUMENTS")):
        assert err(s.append_annotation, bad).code == code
    unknown = request(proj(REF_B, "x\n"))
    assert err(s.append_annotation, unknown).code == "E_NOT_FOUND"
    assert db(w).execute("SELECT COUNT(*) FROM annotation_revisions").fetchone()[0] == 0


def test_two_threads_with_the_same_expected_revision_exactly_one_wins(w):
    s, p, rd = annotated_store(w)
    barrier = threading.Barrier(8)
    results, errors = [], []

    def worker(i):
        st = open_store(w.root, reader=rd)                    # independent handles, like independent workers
        barrier.wait()
        try:
            results.append(st.append_annotation(request(p, 0, f"thread-key-{i:04d}", ann={"summary": f"w{i}"})))
        except C.EnrichmentError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(results) == 1 and results[0].annotation_revision == 1
    assert len(errors) == 7 and {e.code for e in errors} == {"E_CONFLICT"} and {e.extra["current_revision"] for e in errors} == {1}
    assert db(w).execute("SELECT COUNT(*) FROM annotation_revisions").fetchone()[0] == 1
    assert s.verify()["ok"]


def test_concurrent_identical_retries_create_exactly_one_revision(w):
    s, p, rd = annotated_store(w)
    barrier = threading.Barrier(6)
    out = []

    def worker():
        st = open_store(w.root, reader=rd)
        barrier.wait()
        out.append(st.append_annotation(request(p, 0, "same-retry-key")))

    ts = [threading.Thread(target=worker) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(r.idempotent_replay for r in out) == [False] + [True] * 5
    assert {r.annotation_revision for r in out} == {1}


def test_fresh_source_version_is_rechecked_inside_the_commit(w):
    s, p, rd = annotated_store(w)
    changed = proj(text="the file changed on disk, store not yet refreshed\n")
    rd.put(changed)                                            # disk moved; the store still holds p
    e = err(s.append_annotation, request(p, 0, "key-fresh-001"))
    assert e.code == "E_STALE_SOURCE"
    assert db(w).execute("SELECT COUNT(*) FROM annotation_revisions").fetchone()[0] == 0
    # a request for a version the store never had is stale as well, and so is an unverifiable source
    rd.put(p)
    assert err(s.append_annotation, request(changed, 0, "key-fresh-002")).code == "E_STALE_SOURCE"
    rd.fail[REF_A] = C.EnrichmentError("E_VALIDATION", "broken")
    assert err(s.append_annotation, request(p, 0, "key-fresh-003")).code == "E_STALE_SOURCE"
    del rd.fail[REF_A]
    # deletion: E_NOT_FOUND, and the store learns the source is gone
    del rd.items[REF_A]
    assert err(s.append_annotation, request(p, 0, "key-fresh-004")).code == "E_NOT_FOUND"
    assert err(s.get, REF_A).code == "E_NOT_FOUND"
    assert s.list_current(None, 10) == []


def test_annotation_state_coverage_and_views(w):
    p = proj(text=long_text())
    ch = chunks_of(p)
    rd = StubReader(p)
    s = open_store(w.root, reader=rd)
    s.upsert_projection(p, ch)
    assert s.get(REF_A).coverage == {"annotated_chunks": 0, "total_chunks": 2} and s.get(REF_A).annotation is None
    ann = {"summary": "s", "chunk_annotations": [{"chunk_ref": ch[0].chunk_ref, "summary": "first half"}]}
    r1 = s.append_annotation(request(p, 0, "key-cover-001", ann=ann))
    assert r1.coverage == {"annotated_chunks": 1, "total_chunks": 2}
    view = s.get(REF_A)
    assert view.annotation["revision"] == 1 and view.annotation["annotation"] == ann and view.stale is False
    assert view.coverage == r1.coverage and view.review == {"state": "unreviewed", "revision": 1}
    assert view.projection["processing_state"] == "annotated" and view.projection["current_annotation_revision"] == 1
    r2 = s.append_annotation(request(p, 1, "key-cover-002", ann={"chunk_annotations": [
        {"chunk_ref": ch[1].chunk_ref, "summary": "second half"}, {"chunk_ref": "chk:" + "7" * 32, "summary": "alien"}]}))
    assert r2.coverage == {"annotated_chunks": 2, "total_chunks": 2}        # union; refs of other versions never count
    # one-chunk artifacts: any revision covers the only chunk
    q = proj(REF_B, "short\n")
    rd.put(q)
    s.upsert_projection(q, chunks_of(q))
    assert s.append_annotation(request(q, 0, "key-cover-003")).coverage == {"annotated_chunks": 1, "total_chunks": 1}


def test_entities_are_recorded_from_the_derived_block(w):
    s, p, rd = annotated_store(w)
    eid = C.entity_id(REF_A, C.normal_form("فیت پلن"))
    assert s.entity_exists(eid) is False
    derived = {"entities": [{"entity_id": eid, "name": "فیت پلن", "normal": C.normal_form("فیت پلن"), "kind": "project",
                             "aliases": [], "mention": {}}], "links": []}
    s.append_annotation(request(p, 0, "key-entity-01", derived=derived))
    assert s.entity_exists(eid) is True and s.entity_exists("ent:" + "0" * 24) is False
    assert s.entity_exists("not an id") is False
    item = s.list_current(None, 5)[0]
    assert item["derived"] == derived and item["annotation_revision"] == 1 and item["review_state"] == "unreviewed"


# ================================================================ review
def test_review_is_human_local_append_only_and_never_promotes(w, monkeypatch):
    s, p, rd = annotated_store(w)
    s.append_annotation(request(p, 0, "key-review-01"))
    req = {"source_ref": REF_A, "annotation_revision": 1, "decision": "confirmed", "note": "checked against the text"}
    for ctx in (None, {}, {"actor": ""}, {"actor": "  "}, {"actor": "sina", "remote": True}, {"remote": False},
                {"actor": "x\ny"}, "sina"):
        assert err(s.record_review, req, ctx).code == "E_REVIEW_FORBIDDEN"
    monkeypatch.setenv("BRAIN_REMOTE_SESSION", "1")
    assert err(s.record_review, req, {"actor": "sina"}).code == "E_REVIEW_FORBIDDEN"
    monkeypatch.delenv("BRAIN_REMOTE_SESSION")
    assert db(w).execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0
    before = s.change_marker()
    rec = s.record_review(req, {"actor": "sina", "remote": False})
    assert (rec.decision, rec.reviewed_by, rec.canonical_promotion, rec.annotation_revision) == ("confirmed", "sina", False, 1)
    assert s.change_marker() > before
    assert s.get(REF_A).review == {"state": "confirmed", "revision": 1}
    rec2 = s.record_review({**req, "decision": "rejected", "note": None}, {"actor": "sina"})
    assert rec2.decision == "rejected" and s.get(REF_A).review["state"] == "rejected"
    cx = db(w)
    assert cx.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 2        # history kept, nothing replaced
    assert cx.execute("SELECT canonical_promotion FROM reviews").fetchall()[0][0] == 0
    raw = sqlite3.connect(w.root / "_enrichment" / S.DB_NAME, isolation_level=None)
    for sql in ("UPDATE reviews SET decision='confirmed'", "DELETE FROM reviews",
                "INSERT INTO reviews(source_ref, annotation_revision, decision, reviewed_by, reviewed_at, canonical_promotion)"
                " VALUES ('cap:cap-20260101-000000-aaaa', 1, 'confirmed', 'x', 'now', 1)"):
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute(sql)
    raw.close()
    assert s.list_current(None, 5)[0]["review_state"] == "rejected"
    assert err(s.record_review, {**req, "annotation_revision": 9}, {"actor": "sina"}).code == "E_NOT_FOUND"
    for bad in ({**req, "decision": "promoted"}, {**req, "annotation_revision": 0}, {**req, "source_ref": "x"},
                {**req, "extra": 1}, {**req, "note": "n" * 501}):
        assert err(s.record_review, bad, {"actor": "sina"}).code in ("E_BAD_ARGUMENTS", "E_BAD_REF")


# ================================================== real corpus: state machine
def test_edit_marks_annotation_stale_and_old_refs_stale(w):
    s = seeded(w)
    ref, rel = w.refs["fa_short"], w.paths["fa_short"]
    view = s.get(ref)
    chunk = s.get_chunks(ref)[0]
    s.append_annotation({"source_ref": ref, "source_version": view.projection["source_version"],
                         "expected_annotation_revision": 0, "idempotency_key": "edit-test-key1",
                         "annotation": {"summary": "قدیمی"}})
    assert s.get(ref).projection["processing_state"] == "annotated"
    p = w.root / rel
    p.write_bytes(p.read_bytes().replace("پیاده‌روی".encode(), "دویدن".encode()))
    # even BEFORE the next pass the old ref and the old version are refused (fresh check on disk)
    assert err(s.get_chunk, chunk.chunk_ref).code == "E_STALE_SOURCE"
    assert err(s.append_annotation, {"source_ref": ref, "source_version": view.projection["source_version"],
                                     "expected_annotation_revision": 1, "idempotency_key": "edit-test-key2",
                                     "annotation": {"summary": "late"}}).code == "E_STALE_SOURCE"
    assert s.get(ref).stale is True
    s.pending(None, 5)                                         # discovery re-projects the source
    new_view = s.get(ref)
    assert new_view.projection["source_version"] != view.projection["source_version"]
    assert new_view.projection["processing_state"] == "stale" and new_view.stale is True
    assert new_view.annotation is None and new_view.review == {"state": "unreviewed", "revision": None}
    assert err(s.get_chunk, chunk.chunk_ref).code == "E_STALE_SOURCE"            # old ref stays stale
    new_chunk = s.get_chunks(ref)[0]
    assert new_chunk.chunk_ref != chunk.chunk_ref and "دویدن" in new_chunk.text
    item = next(i for i in drain(s, limit=5)[0] if i["source_ref"] == ref)       # offered again
    assert (item["current_annotation_revision"], item["expected_annotation_revision"]) == (0, 1)
    assert item["existing_annotation"] is None and item["source_version"] == new_view.projection["source_version"]
    r = s.append_annotation({"source_ref": ref, "source_version": item["source_version"],
                             "expected_annotation_revision": 1, "idempotency_key": "edit-test-key3",
                             "annotation": {"summary": "جدید"}})
    assert r.annotation_revision == 2 and s.get(ref).projection["processing_state"] == "annotated"
    cx = db(w)
    assert [(x["revision"], x["source_version"]) for x in cx.execute(
        "SELECT revision, source_version FROM annotation_revisions ORDER BY revision")] == \
        [(1, view.projection["source_version"]), (2, item["source_version"])]       # history kept
    assert cx.execute("SELECT status FROM projections WHERE source_version=?",
                      (view.projection["source_version"],)).fetchone()[0] == "stale"
    assert s.verify()["ok"]


def test_deleted_source_is_not_found_and_leaves_the_index_feed(w):
    s = seeded(w)
    ref = w.refs["fa_short"]
    chunk = s.get_chunks(ref)[0]
    s.append_annotation({"source_ref": ref, "source_version": chunk.source_version, "expected_annotation_revision": 0,
                         "idempotency_key": "delete-key-001", "annotation": {"summary": "x"}})
    (w.root / w.paths["fa_short"]).unlink()
    e = err(s.get_chunk, chunk.chunk_ref)
    assert e.code == "E_NOT_FOUND"
    assert err(s.get, ref).code == "E_NOT_FOUND" and err(s.get_chunks, ref).code == "E_NOT_FOUND"
    assert ref not in [i["projection"].source_ref for i in s.list_current(None, 100)]
    assert ref not in [i["source_ref"] for i in s.pending(None, 5).items]
    row = db(w).execute("SELECT present, current_version, state FROM sources WHERE source_ref=?", (ref,)).fetchone()
    assert (row["present"], row["current_version"]) == (0, None)
    assert db(w).execute("SELECT COUNT(*) FROM annotation_revisions").fetchone()[0] == 1       # kept for audit
    assert s.status()["counts"]["missing_sources"] == 1 and s.verify()["ok"]


def test_deletion_found_by_a_completed_pass_and_revival(w):
    s = seeded(w)
    ref, rel = w.refs["dup_a"], w.paths["dup_a"]
    original = (w.root / rel).read_bytes()
    chunk = s.get_chunks(ref)[0]
    s.append_annotation({"source_ref": ref, "source_version": chunk.source_version, "expected_annotation_revision": 0,
                         "idempotency_key": "revive-key-001", "annotation": {"summary": "x"}})
    (w.root / rel).unlink()
    d = s.refresh(budget=1000)                                  # a full pass notices the deletion
    assert d["pass_complete"] and db(w).execute("SELECT present FROM sources WHERE source_ref=?", (ref,)).fetchone()[0] == 0
    assert err(s.get_chunk, chunk.chunk_ref).code == "E_NOT_FOUND"
    (w.root / rel).write_bytes(original)
    s.refresh(budget=1000)
    view = s.get(ref)
    assert view.projection["source_version"] == chunk.source_version and view.annotation["revision"] == 1
    assert s.get_chunk(chunk.chunk_ref).text == chunk.text and view.projection["processing_state"] == "annotated"
    assert s.verify()["ok"]


def test_resolve_chunk_ref_distinguishes_bad_stale_and_missing(w):
    s = seeded(w)
    a, b = w.refs["fa_short"], w.refs["dup_a"]
    ca, cb = s.get_chunks(a)[0], s.get_chunks(b)[0]
    assert resolve_chunk_ref(s, a, ca.chunk_ref) == ca
    assert err(resolve_chunk_ref, s, a, cb.chunk_ref).code == "E_BAD_REF"
    assert err(resolve_chunk_ref, s, "nope", ca.chunk_ref).code == "E_BAD_REF"
    assert err(resolve_chunk_ref, s, a, "chk:xyz").code == "E_BAD_REF"
    assert err(resolve_chunk_ref, s, a, "chk:" + "e" * 32).code == "E_NOT_FOUND"
    (w.root / w.paths["fa_short"]).write_bytes((w.root / w.paths["fa_short"]).read_bytes().replace(b"\xd9\x81", b"\xd9\x82", 1))
    s.refresh(budget=1000)
    assert err(resolve_chunk_ref, s, a, ca.chunk_ref).code == "E_STALE_SOURCE"
    assert err(resolve_chunk_ref, s, b, ca.chunk_ref).code == "E_BAD_REF"        # stale AND someone else's: bad ref wins
    (w.root / w.paths["dup_a"]).unlink()
    assert err(resolve_chunk_ref, s, b, cb.chunk_ref).code == "E_NOT_FOUND"


# ================================================================= pending
PENDING_ITEM_KEYS = {"source_ref", "source_version", "artifact_id", "source_kind", "source_tier", "origin_channel",
                     "layers", "quality_flags", "coverage_warnings", "current_annotation_revision",
                     "expected_annotation_revision", "coverage", "existing_annotation", "page"}
PAGE_KEYS = {"chunks", "offset", "returned", "total", "truncated"}
CHUNK_KEYS = {"chunk_ref", "text_ref", "layer_id", "index", "start_char", "end_char", "chars", "text",
              "prev_chunk_ref", "next_chunk_ref", "annotated"}


def drain(s, limit=5, **kw):
    seen, cur, pages = [], None, []
    while True:
        pg = s.pending(cur, limit, **kw)
        pages.append(pg)
        seen += pg.items
        cur = pg.next_cursor
        if cur is None:
            return seen, pages


def full_annotation(item):
    return {"summary": "s", "chunk_annotations": [{"chunk_ref": c["chunk_ref"], "summary": "c"} for c in item["page"]["chunks"]]}


def submit(s, item, rev, key, ann=None):
    return s.append_annotation({"source_ref": item["source_ref"], "source_version": item["source_version"],
                                "expected_annotation_revision": rev, "idempotency_key": key,
                                "annotation": ann or full_annotation(item)})


def test_pending_offers_only_captures_and_holdings_with_text(w):
    s = open_store(w.root)
    pg = s.pending(None, 5)
    d = pg.discovery
    assert d["pass"] == 1 and d["scanned"] == 12 and d["projected"] == 9 and d["no_text"] == 3 and d["failed"] == 0
    assert d["pass_complete"] is True and isinstance(d["inventory_generation"], int)
    assert pg.total == 8 and pg.returned == 5 and pg.truncated is True and pg.next_cursor
    seen, _ = drain(open_store(w.root), limit=5)
    refs = [i["source_ref"] for i in seen]
    assert sorted(refs) == sorted([w.refs[k] for k in ("fa_short", "crlf", "escaped_headings", "long", "dup_a", "dup_b",
                                                       "voice_transcribed", "held_ok")])
    assert len(set(refs)) == 8
    for forbidden in ("canonical", "voice_plain", "held_nodoc", "held_ocr"):
        assert w.refs[forbidden] not in refs
    for it in seen:
        assert set(it) == PENDING_ITEM_KEYS and set(it["page"]) == PAGE_KEYS
        assert all(set(c) == CHUNK_KEYS for c in it["page"]["chunks"])
        assert it["current_annotation_revision"] == 0 and it["expected_annotation_revision"] == 0
        assert it["existing_annotation"] is None and it["coverage"]["annotated_chunks"] == 0
        assert json.dumps(it, ensure_ascii=False).count(str(w.root)) == 0
        assert it["artifact_id"] == C.artifact_id("zb-instance", it["source_ref"])
        text = "".join(c["text"] for c in it["page"]["chunks"])
        layer_chars = sum(la["chars"] for la in it["layers"])
        assert len(text) == layer_chars                         # whole artifact fits one page here, order preserved
    held = next(i for i in seen if i["source_ref"] == w.refs["held_ok"])
    assert held["source_kind"] == "ingested-file" and held["layers"][0]["text_ref"] == w.refs["held_ok_text"]
    assert held["layers"][0]["tier"] == "derivative" and held["layers"][0]["authority_level"] == 5
    assert held["origin_channel"] is None
    voice = next(i for i in seen if i["source_ref"] == w.refs["voice_transcribed"])
    assert voice["origin_channel"] == "filesystem" and "unreviewed_transcript" in voice["quality_flags"]
    # canonical and orphan derivatives are projected (indexable) but skip annotation
    skip = {i["projection"].source_ref: i for i in open_store(w.root).list_current(None, 100)}
    assert skip[w.refs["canonical"]]["annotation_policy"] == "skip" and skip[w.refs["canonical"]]["processing_state"] == "projected"
    assert skip[w.refs["fa_short"]]["annotation_policy"] == "annotate"


def test_pending_pages_a_long_artifact_by_max_input_chars_with_a_bound_cursor(w):
    s = open_store(w.root, max_input_chars=1000)
    long_ref = w.refs["long"]
    # drive pages until the long capture shows up
    cur, first = None, None
    while first is None:
        pg = s.pending(cur, 1)
        assert pg.returned == 1
        if pg.items[0]["source_ref"] == long_ref:
            first = pg
        cur = pg.next_cursor
        assert cur or first
    it = first.items[0]
    assert it["page"]["returned"] == 1 and it["page"]["offset"] == 0 and it["page"]["total"] == 2
    assert it["page"]["truncated"] is True and first.truncated is True
    c0 = it["page"]["chunks"][0]
    assert 0 < len(c0["text"]) <= 4000                       # a single chunk may exceed the cap: it is atomic evidence
    nxt = s.pending(first.next_cursor, 1)
    it2 = nxt.items[0]
    assert it2["source_ref"] == long_ref and it2["page"]["offset"] == 1 and it2["page"]["chunks"][0]["index"] == 1
    assert it2["page"]["truncated"] is False
    assert c0["text"] + it2["page"]["chunks"][0]["text"] == w.texts["long"]
    assert it2["page"]["chunks"][0]["prev_chunk_ref"] == c0["chunk_ref"]
    # the cursor binds source_ref + source_version + chunk offset: an edit makes it stale
    p = w.root / w.paths["long"]
    p.write_bytes(p.read_bytes().replace("نیل".encode(), "نیلوفر".encode(), 1))
    e = err(s.pending, first.next_cursor, 1)
    assert e.code == "E_STALE_CURSOR" and e.extra["source_ref"] == long_ref
    refs_now = [i["source_ref"] for i in drain(s, limit=1)[0]]               # restart works and serves the new version
    assert refs_now.count(long_ref) >= 1
    assert "نیلوفر" in "".join(c["text"] for i in drain(s, limit=1)[0] if i["source_ref"] == long_ref
                               for c in i["page"]["chunks"])


def test_pending_cursor_and_argument_validation(w):
    s = open_store(w.root)
    for bad in (0, 6, -1, True, "1", None, 1.5):
        assert err(s.pending, None, bad).code == "E_BAD_ARGUMENTS"
    assert err(s.pending, None, 1, retry_failed="yes").code == "E_BAD_ARGUMENTS"
    for cur in ("", "!!", C.encode_cursor("sources", a="x"), "a" * 3000,
                C.encode_cursor("pending", r="../x", v="a" * 64, o=0, t=""),
                C.encode_cursor("pending", r=w.refs["long"], v="a" * 64, o=-1, t=""),
                C.encode_cursor("pending", r=w.refs["long"], v="a" * 64)):
        assert err(s.pending, cur, 1).code == "E_BAD_CURSOR"
    ghost = C.encode_cursor("pending", r="cap:cap-20990101-000000-0000", v="a" * 64, o=0, t="")
    assert err(s.pending, ghost, 1).code == "E_STALE_CURSOR"
    wrong = C.encode_cursor("pending", r=w.refs["long"], v="a" * 64, o=0, t="")
    assert err(s.pending, wrong, 1).code == "E_STALE_CURSOR"


def test_annotated_sources_are_not_offered_again_but_partial_coverage_is(w):
    s = open_store(w.root)
    seen, _ = drain(s, limit=5)
    by = {i["source_ref"]: i for i in seen}
    long_ref = w.refs["long"]
    for n, (ref, it) in enumerate(by.items()):
        if ref == long_ref:
            continue
        r = submit(s, it, 0, f"annotate-all-{n:04d}")
        assert r.coverage["annotated_chunks"] == r.coverage["total_chunks"]
    pg = s.pending(None, 5)
    assert [i["source_ref"] for i in pg.items] == [long_ref] and pg.total == 1 and pg.truncated is False
    lit = by[long_ref]
    assert lit["coverage"] == {"annotated_chunks": 0, "total_chunks": 2} and len(lit["page"]["chunks"]) == 2
    first_only = {"summary": "half", "chunk_annotations": [{"chunk_ref": lit["page"]["chunks"][0]["chunk_ref"], "summary": "a"}]}
    r = submit(s, lit, 0, "annotate-half-01", first_only)
    assert r.coverage == {"annotated_chunks": 1, "total_chunks": 2}
    again = s.pending(None, 5)
    it = again.items[0]
    assert it["source_ref"] == long_ref and again.total == 1
    assert it["page"]["offset"] == 1 and [c["annotated"] for c in it["page"]["chunks"]] == [False]    # starts at the open chunk
    assert it["current_annotation_revision"] == 1 and it["expected_annotation_revision"] == 1
    assert it["existing_annotation"] == first_only and it["coverage"] == {"annotated_chunks": 1, "total_chunks": 2}
    second = {"chunk_annotations": [{"chunk_ref": it["page"]["chunks"][0]["chunk_ref"], "summary": "b"}]}
    submit(s, it, 1, "annotate-half-02", second)
    final = s.pending(None, 5)
    assert final.items == () and final.total == 0 and final.next_cursor is None and final.truncated is False
    assert s.status()["by_state"].get("annotated") == 8


def test_pending_rotates_partial_sources_to_the_back(w):
    s = open_store(w.root)
    seen, _ = drain(s, limit=5)
    long_it = next(i for i in seen if i["source_ref"] == w.refs["long"])
    half = {"chunk_annotations": [{"chunk_ref": long_it["page"]["chunks"][0]["chunk_ref"], "summary": "a"}]}
    submit(s, long_it, 0, "rotate-key-0001", half)
    order = [i["source_ref"] for i in drain(s, limit=5)[0]]
    assert order[-1] == w.refs["long"] and len(order) == 8              # not stuck at the head of the queue


def test_pending_pages_through_everything_with_cursors(w):
    s = open_store(w.root)
    seen, pages = drain(s, limit=2)
    assert len(seen) == 8 and len(pages) == 4 and [p.returned for p in pages] == [2, 2, 2, 2]
    assert all(p.total == 8 for p in pages) and pages[-1].next_cursor is None


# ============================================================== discovery
def test_discovery_is_bounded_resumable_across_restarts_and_finds_new_sources(w):
    totals = []
    for call in range(1, 30):
        s = open_store(w.root, discovery_budget=3)              # a "restart" before every call
        d = s.refresh()
        totals.append(d)
        if call == 1:
            assert d["scanned"] == 3 and d["pass_complete"] is False and s.status()["inventory"]["in_progress"] is True
            craft_capture(w, "cap-20991231-235959-ffff", "نوشته‌ای که وسط پیمایش ساخته شد\n")
        if d["pass_complete"]:
            break
    first = next(d for d in totals if d["pass_complete"])
    assert first["pass"] == 1 and first["scanned"] >= 12 and all(d["scanned_this_call"] <= 3 for d in totals)
    new_ref = "cap:cap-20991231-235959-ffff"
    # the new source is certainly known by the end of the NEXT pass
    for _ in range(30):
        s = open_store(w.root, discovery_budget=3)
        d = s.refresh()
        if d["pass_complete"] and d["pass"] == 2:
            break
    assert d["pass"] == 2 and s.get(new_ref).projection["processing_state"] == "projected"
    assert s.status()["inventory"]["pass"] == 3 and s.status()["inventory"]["in_progress"] is False
    assert s.status()["counts"]["present_sources"] == 13 and s.verify()["ok"]


def test_a_cached_snapshot_does_not_see_mid_pass_sources_until_the_next_pass(w):
    s = open_store(w.root, discovery_budget=4)
    d1 = s.refresh()
    assert d1["pass"] == 1 and not d1["pass_complete"]
    craft_capture(w, "cap-20991231-235959-eeee", "ساخته شده در میانه\n")
    new = "SELECT state FROM sources WHERE source_ref='cap:cap-20991231-235959-eeee'"
    done = d1
    while not done["pass_complete"]:
        done = s.refresh()
    assert done["pass"] == 1 and done["scanned"] == 12                       # pass 1 saw exactly the old snapshot
    assert db(w).execute(new).fetchone() is None                             # ...the new capture waits for pass 2
    while not (done := s.refresh())["pass_complete"]:
        assert done["pass"] == 2
    assert done["pass"] == 2 and done["scanned"] == 13
    assert db(w).execute(new).fetchone()[0] == "projected"


def test_steady_state_refresh_changes_nothing(w):
    s = seeded(w)
    marker = s.change_marker()
    for _ in range(3):
        d = s.refresh(budget=1000)
        assert d["failed"] == 0 and d["changed"] == 0
    assert s.change_marker() == marker
    assert s.status()["counts"]["projections"] == 12 and s.status()["counts"]["chunks"] == 10


def test_simultaneous_first_use_creates_the_store_exactly_once(tmp_path):
    for round_no in range(6):
        root = tmp_path / f"fresh-{round_no}"
        root.mkdir()
        barrier = threading.Barrier(8)
        errors = []

        def work(i):
            st = open_store(root, reader=StubReader(proj(REF_A, f"text {round_no}\n")))
            barrier.wait()
            try:
                st.refresh(budget=5)
                st.status()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        ts = [threading.Thread(target=work, args=(i,)) for i in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert not errors, errors
        fresh = open_store(root, reader=StubReader())
        assert fresh.verify()["ok"] and fresh.status()["counts"]["sources"] == 1
        assert sqlite3.connect(root / "_enrichment" / S.DB_NAME).execute(
            "SELECT COUNT(*) FROM inventory").fetchone()[0] == 1


def test_concurrent_pending_calls_do_not_duplicate_or_corrupt(w):
    barrier = threading.Barrier(4)
    out = []

    def work():
        st = open_store(w.root)
        barrier.wait()
        out.append(st.pending(None, 5))

    ts = [threading.Thread(target=work) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(out) == 4 and all(p.total == 8 for p in out)
    s = open_store(w.root)
    assert s.status()["counts"]["projections"] == 12 and s.verify()["ok"]


# ============================================================== failures
def test_unparseable_source_fails_visibly_and_the_scan_continues(w):
    cid = "cap-20261010-999999-abcd"
    d = w.root / "01-inbox/captures/2026/202610"
    d.mkdir(parents=True, exist_ok=True)
    bad = d / f"{cid}.md"
    bad.write_text(f"---\nid: {cid}\ntype: capture\nnever closed\n", encoding="utf-8")
    before = corpus_hashes(w.root)
    s = open_store(w.root)
    pg = s.pending(None, 5)
    assert pg.discovery["failed"] == 1 and pg.discovery["projected"] == 9 and pg.total == 8      # the rest was processed
    row = db(w).execute("SELECT * FROM failures").fetchone()
    assert row["source_ref"] == f"cap:{cid}" and row["attempts"] == 1 and row["transient"] == 0
    assert row["last_error_code"] == "E_VALIDATION" and row["next_retry_after"] is None
    assert f"cap:{cid}" in row["last_error_message"] and str(w.root) not in row["last_error_message"]
    assert json.loads(row["flags"]) == ["parse_warning"]
    assert db(w).execute("SELECT state FROM sources WHERE source_ref=?", (f"cap:{cid}",)).fetchone()[0] == "failed"
    assert s.status()["by_state"]["failed"] == 1 and s.status()["counts"]["failures"] == 1
    assert f"cap:{cid}" not in [i["source_ref"] for i in drain(s)[0]]
    assert err(s.get, f"cap:{cid}").code == "E_NOT_FOUND"
    assert corpus_hashes(w.root) == before                   # the broken file is never auto-repaired
    # a stable failure is NOT retried by ordinary passes, even after the file is fixed...
    craft_capture(w, cid, "اکنون سالم است\n")
    s.refresh(budget=1000)
    assert db(w).execute("SELECT attempts FROM failures").fetchone()[0] == 1
    # ...it needs an explicit retry
    d2 = s.refresh(budget=1000, retry_failed=True)
    assert d2["failed"] == 0 and db(w).execute("SELECT COUNT(*) FROM failures").fetchone()[0] == 0
    assert s.get(f"cap:{cid}").projection["processing_state"] == "projected"


def test_a_source_that_breaks_after_being_projected_becomes_stale_and_failed(w):
    s = seeded(w)
    ref, rel = w.refs["fa_short"], w.paths["fa_short"]
    chunk = s.get_chunks(ref)[0]
    (w.root / rel).write_bytes(b"---\nid: broken\n")
    s.refresh(budget=1000)
    assert err(s.get, ref).code == "E_NOT_FOUND"
    assert err(s.get_chunk, chunk.chunk_ref).code == "E_STALE_SOURCE"
    assert db(w).execute("SELECT current_version, state FROM sources WHERE source_ref=?", (ref,)).fetchone()[:] == (None, "failed")
    assert db(w).execute("SELECT status FROM projections WHERE source_ref=?", (ref,)).fetchone()[0] == "stale"
    assert ref not in [i["projection"].source_ref for i in s.list_current(None, 100)]
    assert s.verify()["ok"]
    with pytest.raises(C.EnrichmentError):
        s.reprocess(ref)


def test_transient_failures_retry_twice_with_spacing_then_wait_for_an_explicit_retry(w):
    now = [datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)]
    p = proj()
    rd = StubReader(p)
    rd.fail[REF_A] = C.EnrichmentError("E_UNAVAILABLE", f"{REF_A}: file cannot be read (OSError)")
    s = open_store(w.root, reader=rd, clock=lambda: now[0], retry_spacing_seconds=300)

    def attempts():
        r = db(w).execute("SELECT attempts, transient, next_retry_after FROM failures").fetchone()
        return None if r is None else (r["attempts"], r["transient"], r["next_retry_after"])

    s.refresh(budget=10)
    assert attempts() == (1, 1, "2026-10-10T12:05:00Z")
    s.refresh(budget=10)                                                  # too early: not retried
    assert attempts()[0] == 1 and rd.loads == 1
    now[0] += timedelta(seconds=301)
    s.refresh(budget=10)
    assert attempts()[0] == 2
    now[0] += timedelta(hours=3)
    s.refresh(budget=10)
    assert attempts()[0] == 2 and rd.loads == 2                           # max two automatic attempts
    del rd.fail[REF_A]
    s.refresh(budget=10)
    assert attempts()[0] == 2                                             # still waits for an explicit retry
    d = s.refresh(budget=10, retry_failed=True)
    assert attempts() is None and d["failed"] == 0 and s.get(REF_A).projection["processing_state"] == "projected"
    # a transient failure of a projected source keeps its projection (we cannot tell the text changed)
    rd.fail[REF_A] = C.EnrichmentError("E_UNAVAILABLE", "again")
    s.refresh(budget=10, retry_failed=True)
    assert db(w).execute("SELECT current_version IS NOT NULL FROM sources").fetchone()[0] == 1


def test_clock_accepts_datetimes_epoch_numbers_and_strings(w):
    assert open_store(w.root, clock=lambda: 0)._ts() == "1970-01-01T00:00:00Z"
    assert open_store(w.root, clock=lambda: 1_800_000_000.0)._ts() == "2027-01-15T08:00:00Z"
    assert open_store(w.root, clock=lambda: "2027-01-15T08:00:00Z")._ts() == "2027-01-15T08:00:00Z"
    assert open_store(w.root, clock=lambda: datetime(2027, 1, 15, 8, 0, 0))._ts() == "2027-01-15T08:00:00Z"       # naive = UTC
    aware = datetime(2027, 1, 15, 10, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    assert open_store(w.root, clock=lambda: aware)._ts() == "2027-01-15T08:00:00Z"


# ================================================================== crash
def test_exception_mid_transaction_rolls_back_and_the_store_stays_healthy(w, monkeypatch):
    s = open_store(w.root, reader=StubReader())
    base = proj(REF_B, "baseline\n")
    s.upsert_projection(base, chunks_of(base))
    marker = s.change_marker()
    p = proj(text=long_text())
    orig = SQLiteProjectionStore._insert_chunks

    def boom(cx, pr, chunks, ordinal):
        orig(cx, pr, chunks[:1], ordinal)
        raise RuntimeError("simulated crash after the first chunk row")

    monkeypatch.setattr(SQLiteProjectionStore, "_insert_chunks", staticmethod(boom))
    with pytest.raises(RuntimeError):
        s.upsert_projection(p, chunks_of(p))
    monkeypatch.undo()
    cx = db(w)
    assert cx.execute("SELECT COUNT(*) FROM projections WHERE source_ref=?", (REF_A,)).fetchone()[0] == 0
    assert cx.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 1 and cx.execute("SELECT COUNT(*) FROM layers").fetchone()[0] == 1
    assert s.change_marker() == marker and s.verify()["ok"]
    assert s.upsert_projection(p, chunks_of(p)).state == "created" and s.verify()["ok"]


KILL_SCRIPT = r"""
import os, signal, sys
sys.path[:0] = [{scripts!r}, {tests!r}]
from brain_enrichment.chunking import Chunker
from brain_enrichment import contract as C
from brain_enrichment.store import SQLiteProjectionStore
from support import long_text, make_projection
ref = "cap:cap-20260101-000000-aaaa"
p = make_projection(ref, [("user-text", ref, long_text(), None)])
chunks = Chunker().build(p, C.ChunkPolicy())
orig = SQLiteProjectionStore._insert_chunks
def kill(cx, pr, ch, ordinal):
    orig(cx, pr, ch[:1], ordinal)
    os.kill(os.getpid(), signal.SIGKILL)
SQLiteProjectionStore._insert_chunks = staticmethod(kill)
SQLiteProjectionStore({root!r}, reader=object()).upsert_projection(p, chunks)
"""


def test_a_killed_writer_leaves_a_healthy_store(w):
    s = open_store(w.root, reader=StubReader())
    base = proj(REF_B, "baseline\n")
    s.upsert_projection(base, chunks_of(base))
    script = KILL_SCRIPT.format(scripts=str(SCRIPTS), tests=str(HERE), root=str(w.root))
    res = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120)
    assert res.returncode == -signal.SIGKILL, res.stderr
    assert (w.root / "_enrichment" / (S.DB_NAME + "-wal")).exists()          # the kill really left a WAL behind
    rep = s.verify()
    assert rep["ok"], rep
    cx = db(w)
    assert cx.execute("SELECT COUNT(*) FROM projections").fetchone()[0] == 1          # only the baseline
    assert cx.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 1
    p = proj(text=long_text())
    assert s.upsert_projection(p, chunks_of(p)).state == "created"
    assert s.verify()["ok"] and s.status()["counts"]["chunks"] == 3


# ====================================================== read-only operations
def test_export_jsonl_is_complete_read_only_and_parseable(w):
    s = seeded(w)
    item = s.pending(None, 1).items[0]
    submit(s, item, 0, "export-key-0001")
    s.record_review({"source_ref": item["source_ref"], "annotation_revision": 1, "decision": "confirmed"}, {"actor": "sina"})
    marker, hashes = s.change_marker(), corpus_hashes(w.root)
    buf = io.StringIO()
    res = s.export_jsonl(buf)
    rows = [json.loads(line) for line in buf.getvalue().splitlines()]
    kinds = {}
    for r in rows:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    assert kinds == res["records"] and kinds["source"] == 12 and kinds["projection"] == 12 and kinds["chunk"] == 10
    assert kinds["annotation_revision"] == 1 and kinds["review"] == 1 and kinds["header"] == 1 and kinds["inventory"] == 1
    assert next(r for r in rows if r["kind"] == "review")["canonical_promotion"] == 0
    assert any("فیت" in r.get("text", "") for r in rows if r["kind"] == "layer")
    nt = io.StringIO()
    s.export_jsonl(nt, include_text=False)
    assert all("text" not in json.loads(line) for line in nt.getvalue().splitlines() if '"kind": "layer"' in line)
    assert s.change_marker() == marker and corpus_hashes(w.root) == hashes
    assert err(open_store(w.root.parent / "no-such-wiki").export_jsonl, io.StringIO()).code == "E_NOT_FOUND"


def test_backup_uses_the_sqlite_backup_api_and_the_snapshot_verifies(w, tmp_path):
    s = seeded(w)
    item = s.pending(None, 1).items[0]
    submit(s, item, 0, "backup-key-0001")
    dest = tmp_path / "snapshot.sqlite3"
    res = s.backup(dest)
    assert res["ok"] and res["bytes"] == dest.stat().st_size and res["sha256"] == sha(dest.read_bytes())
    assert dest.stat().st_mode & 0o777 == 0o600 and not (tmp_path / "snapshot.sqlite3.partial").exists()
    assert not (tmp_path / "snapshot.sqlite3-wal").exists()                  # a single self-contained file
    snap = sqlite3.connect(dest)
    assert snap.execute("PRAGMA integrity_check").fetchone()[0] == "ok" and snap.execute("PRAGMA user_version").fetchone()[0] == 1
    assert snap.execute("SELECT COUNT(*) FROM annotation_revisions").fetchone()[0] == 1
    snap.close()
    # the snapshot is a complete store: copy it into a fresh wiki and verify it there
    other = tmp_path / "restored-wiki"
    (other / "_enrichment").mkdir(parents=True, mode=0o700)
    (other / "_enrichment" / S.DB_NAME).write_bytes(dest.read_bytes())
    restored = open_store(other, reader=StubReader())
    rep = restored.verify()
    assert rep["ok"] and rep["checked"]["revisions"] == 1 and restored.status()["counts"]["chunks"] == 10
    # never overwrites, never targets the live store, needs an existing directory
    assert err(s.backup, dest).code == "E_BAD_ARGUMENTS"
    assert err(s.backup, w.root / "_enrichment" / S.DB_NAME).code == "E_BAD_ARGUMENTS"
    assert err(s.backup, tmp_path / "no-dir" / "x.sqlite3").code == "E_BAD_ARGUMENTS"
    assert err(open_store(tmp_path / "empty-wiki").backup, tmp_path / "y.sqlite3").code == "E_NOT_FOUND"


def test_status_verify_export_backup_are_safe_against_a_live_writer(w, tmp_path):
    p = proj(text=long_text())
    rd = StubReader(p)
    writer = open_store(w.root, reader=rd)
    writer.upsert_projection(p, chunks_of(p))
    stop = threading.Event()
    failures: list[BaseException] = []
    revisions = []

    def write_loop():
        try:
            for i in range(40):
                r = writer.append_annotation(request(p, i, f"live-writer-{i:04d}", ann={"summary": f"rev {i}"}))
                revisions.append(r.annotation_revision)
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)
        finally:
            stop.set()

    t = threading.Thread(target=write_loop)
    t.start()
    reader_side = open_store(w.root, reader=rd)
    n = 0
    while not stop.is_set() or n < 4:
        try:
            st = reader_side.status()
            assert st["state"] == "ready"
            assert reader_side.verify()["ok"]
            buf = io.StringIO()
            reader_side.export_jsonl(buf)
            assert buf.getvalue().count('"kind": "annotation_revision"') >= 0
            dest = tmp_path / f"live-{n}.sqlite3"
            reader_side.backup(dest)
            chk = sqlite3.connect(dest)
            assert chk.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            chk.close()
            reader_side.get(REF_A)
            reader_side.list_current(None, 5)
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)
            break
        n += 1
    t.join()
    assert not failures, failures
    assert revisions == list(range(1, 41)) and writer.verify()["ok"] and n >= 4


def test_list_current_pages_by_source_ref_and_marker_tracks_writes(w):
    s = seeded(w)
    first = s.list_current(None, 5)
    assert [i["projection"].source_ref for i in first] == sorted(i["projection"].source_ref for i in first)
    rest = s.list_current(first[-1]["projection"].source_ref, 100)
    assert len(first) + len(rest) == 12
    assert set(first[0]) == {"projection", "chunks", "annotation", "annotation_revision", "review_state",
                             "processing_state", "derived", "producer_reported", "received_at", "annotation_policy"}
    nt = next(i for i in first + rest if i["projection"].source_ref == w.refs["voice_plain"])
    assert nt["chunks"] == [] and nt["processing_state"] == "no_text"
    with_text = next(i for i in first + rest if i["projection"].source_ref == w.refs["long"])
    assert [c.text for c in with_text["chunks"]] == [with_text["projection"].layers[0].text[c.start_char:c.end_char]
                                                      for c in with_text["chunks"]]
    m0 = s.change_marker()
    s.pending(None, 1)
    assert s.change_marker() == m0
    assert err(s.list_current, None, 0).code == "E_BAD_ARGUMENTS"


def test_open_store_has_no_import_time_rooting(w, tmp_path):
    other = tmp_path / "other-root"
    other.mkdir()
    s = open_store(other)
    assert s.root == other.resolve() and s.status()["state"] == "missing"
    s2 = open_store(w.root)
    assert s2.root == w.root.resolve()
    assert open_store(other).refresh(budget=5)["scanned"] == 0
    assert (other / "_enrichment" / S.DB_NAME).exists() and not (w.root / "_enrichment").exists()


def test_held_file_flows_end_to_end_with_provenance(w):
    s = seeded(w)
    view = s.get(w.refs["held_ok"])
    assert view.projection["source_kind"] == "ingested-file" and view.projection["original_sha256"] == sha(b"letter bytes\n")
    chunk = s.get_chunks(w.refs["held_ok"])[0]
    assert chunk.text_ref == w.refs["held_ok_text"] and chunk.text == w.texts["held_ok_text"]
    # editing the derivative file (a new text_ref hash is NOT created: same path) moves the version
    der = w.root / w.paths["held_ok_derivative"]
    der.write_bytes(der.read_bytes() + "\nextra line\n".encode())
    s.refresh(budget=1000)
    assert s.get(w.refs["held_ok"]).projection["source_version"] != view.projection["source_version"]
    assert err(s.get_chunk, chunk.chunk_ref).code == "E_STALE_SOURCE"
    # a second holding of needs_ocr / preserved only never gets chunks
    assert s.get_chunks(w.refs["held_nodoc"]) == [] and s.get(w.refs["held_ocr"]).projection["processing_state"] == "no_text"


def test_metadata_only_changes_refresh_in_place_without_a_new_version(w):
    s = seeded(w)
    ref = w.refs["voice_transcribed"]
    before = s.get(ref)
    chunk = s.get_chunks(ref)[0]
    assert "unreviewed_transcript" in before.projection["quality_flags"] and "unreviewed_transcript" in chunk.quality_flags
    marker = s.change_marker()
    p = w.root / w.paths["voice_transcribed"]
    p.write_bytes(p.read_bytes().replace(b"transcription_reviewed: false", b"transcription_reviewed: true"))
    s.refresh(budget=1000)
    after = s.get(ref)
    assert after.projection["source_version"] == before.projection["source_version"]        # same text, same version
    assert "unreviewed_transcript" not in after.projection["quality_flags"]
    assert after.projection["layers"][0]["reviewed"] is True
    again = s.get_chunks(ref)[0]
    assert again.chunk_ref == chunk.chunk_ref and "unreviewed_transcript" not in again.quality_flags
    assert s.change_marker() > marker and s.verify()["ok"]
    marker = s.change_marker()
    s.refresh(budget=1000)
    assert s.change_marker() == marker


def test_a_changed_instance_id_keeps_the_source_and_its_history(w):
    s = seeded(w)
    ref = w.refs["fa_short"]
    old_id = s.get(ref).projection["artifact_id"]
    assert old_id == C.artifact_id("zb-instance", ref)
    (w.root / "00-system/registers/INSTANCE.json").write_text(json.dumps({"id": "renamed"}) + "\n", encoding="utf-8")
    s2 = open_store(w.root)
    s2.refresh(budget=1000)
    assert s2.get(ref).projection["artifact_id"] == C.artifact_id("renamed", ref) != old_id
    assert s2.status()["by_state"].get("failed") is None and s2.verify()["ok"]


def test_chunking_policy_change_replaces_unannotated_chunks_but_never_annotated_ones(w):
    s = seeded(w)
    ref = w.refs["long"]
    before = [c.chunk_ref for c in s.get_chunks(ref)]
    small = C.ChunkPolicy(soft_max=2000, hard_max=3000)
    s2 = open_store(w.root, policy=small)
    assert s2.refresh(budget=1000)["changed"] == 0
    after = s2.get_chunks(ref)
    assert [c.chunk_ref for c in after] != before and len(after) > len(before) and s2.verify()["ok"]
    assert all(c.text == "".join(after_c.text for after_c in after)[c.start_char:c.end_char] for c in after)
    # once a revision exists for the version, its chunks are frozen
    item = next(i for i in drain(s2, limit=5)[0] if i["source_ref"] == ref)
    submit(s2, item, 0, "policy-key-0001")
    s3 = open_store(w.root)                                  # default policy would re-chunk the annotated version
    d = s3.refresh(budget=1000)
    assert d.get("error") == "E_INTERNAL" and s3.verify()["ok"]
    assert [c.chunk_ref for c in s3.get_chunks(ref)] == [c.chunk_ref for c in after]       # nothing was rewritten
    assert s3.get(ref).projection["processing_state"] == "annotated"
