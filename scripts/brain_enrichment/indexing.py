"""Derived search index (CONTRACT-E1 section 7): SQLite FTS5 + graph tables in
generations. Rebuildable from the store; deleting the file loses nothing.

Generations
-----------
Every row of every table carries `gen_from` / `gen_to`. A reader fixes the
ACTIVE generation `g` once (one read transaction) and sees exactly the rows
with `gen_from <= g AND (gen_to IS NULL OR gen_to > g)`.

A build works on `g+1`: it retires the old rows of each dirty artifact
(`gen_to = g+1`, still visible at `g`) and inserts the new rows
(`gen_from = g+1`, invisible at `g`). Only the final one-row update of
`meta.active_generation` makes `g+1` visible. A crash anywhere before that
leaves `g` complete and readable; the next build discards the orphan rows.

Nothing in this module calls a model, the network or a subprocess.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import contract as C
from .contract import IndexReceipt
from .graph import GraphBuilder, TRAVERSE_KINDS, PUBLIC_REF_RE, derived_of, expand, usable_annotation

PAGE_SIZE = 50
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS generation(
  id INTEGER PRIMARY KEY, state TEXT NOT NULL, created_at TEXT, activated_at TEXT,
  store_marker INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS artifacts(
  id INTEGER PRIMARY KEY, artifact_id TEXT NOT NULL, source_ref TEXT NOT NULL,
  source_version TEXT NOT NULL, source_kind TEXT, source_tier TEXT, authority_level INTEGER,
  grp TEXT NOT NULL, layers TEXT, quality_flags TEXT, processing_state TEXT,
  annotation_revision INTEGER, review_state TEXT, annotated_chunks INTEGER, chunk_count INTEGER,
  fingerprint TEXT NOT NULL, gen_from INTEGER NOT NULL, gen_to INTEGER);
CREATE INDEX IF NOT EXISTS artifacts_aid ON artifacts(artifact_id);
CREATE INDEX IF NOT EXISTS artifacts_ref ON artifacts(source_ref);
CREATE TABLE IF NOT EXISTS chunk_rows(
  id INTEGER PRIMARY KEY, chunk_ref TEXT NOT NULL, artifact_id TEXT NOT NULL, source_ref TEXT NOT NULL,
  source_version TEXT NOT NULL, idx INTEGER, layer_id TEXT, start_char INTEGER, end_char INTEGER,
  gen_from INTEGER NOT NULL, gen_to INTEGER);
CREATE INDEX IF NOT EXISTS chunk_rows_ref ON chunk_rows(chunk_ref);
CREATE INDEX IF NOT EXISTS chunk_rows_aid ON chunk_rows(artifact_id);
CREATE VIRTUAL TABLE IF NOT EXISTS fts_chunks USING fts5(
  norm, chunk_ref UNINDEXED, tokenize='unicode61 remove_diacritics 2');
CREATE TABLE IF NOT EXISTS meta_rows(
  id INTEGER PRIMARY KEY, artifact_id TEXT NOT NULL, source_ref TEXT NOT NULL, chunk_ref TEXT,
  field TEXT NOT NULL, value TEXT, node_id TEXT, annotation_revision INTEGER,
  gen_from INTEGER NOT NULL, gen_to INTEGER);
CREATE INDEX IF NOT EXISTS meta_rows_aid ON meta_rows(artifact_id);
CREATE VIRTUAL TABLE IF NOT EXISTS fts_meta USING fts5(
  norm, artifact_id UNINDEXED, chunk_ref UNINDEXED, tokenize='unicode61 remove_diacritics 2');
CREATE TABLE IF NOT EXISTS nodes(
  node_id TEXT NOT NULL, kind TEXT NOT NULL, artifact_id TEXT, source_ref TEXT, label TEXT,
  normal TEXT, extra TEXT, gen_from INTEGER NOT NULL, gen_to INTEGER);
CREATE INDEX IF NOT EXISTS nodes_id ON nodes(node_id);
CREATE INDEX IF NOT EXISTS nodes_aid ON nodes(artifact_id);
CREATE TABLE IF NOT EXISTS edges(
  id INTEGER PRIMARY KEY, src TEXT NOT NULL, dst TEXT NOT NULL, edge_kind TEXT NOT NULL,
  label TEXT NOT NULL, artifact_id TEXT, source_ref TEXT, source_version TEXT,
  annotation_revision INTEGER, provenance TEXT, gen_from INTEGER NOT NULL, gen_to INTEGER);
CREATE INDEX IF NOT EXISTS edges_src ON edges(src);
CREATE INDEX IF NOT EXISTS edges_dst ON edges(dst);
CREATE INDEX IF NOT EXISTS edges_aid ON edges(artifact_id);
"""
ROW_TABLES = ("artifacts", "chunk_rows", "meta_rows", "nodes", "edges")
_FTS_OF = {"chunk_rows": "fts_chunks", "meta_rows": "fts_meta"}
FIELD_PRIORITY = {"entity": 0, "alias": 1, "topic": 2, "tag": 3, "chunk_tag": 4,
                  "chunk_summary": 5, "summary": 6}


def _vis(alias: str = "") -> str:
    p = alias + "." if alias else ""
    return f"({p}gen_from <= :g AND ({p}gen_to IS NULL OR {p}gen_to > :g))"


def fts5_available() -> bool:
    """Probe FTS5 with the real interpreter. Absence disables only the index
    path (state "unavailable"); it never raises to a caller."""
    try:
        con = sqlite3.connect(":memory:")
        try:
            con.execute("CREATE VIRTUAL TABLE t USING fts5(x, tokenize='unicode61 remove_diacritics 2')")
            return True
        finally:
            con.close()
    except sqlite3.Error:
        return False


class IndexUnavailable(Exception):
    """The index cannot serve reads; `state` is the public index.state."""

    def __init__(self, state: str, reason: str):
        super().__init__(reason)
        self.state, self.reason = state, reason


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def tokens_of(text: str) -> list[str]:
    """Query/FTS tokens of a text: normal form, then maximal alphanumeric runs."""
    return re.findall(r"[^\W_]+", C.normal_form(text))


def _sha(text: str) -> str:
    return C.sha256_text(text)


@contextmanager
def _txn(con):
    con.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        try:
            con.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    else:
        con.execute("COMMIT")


# ----------------------------------------------------------------- reader
class IndexReader:
    """A read view of ONE complete generation (a single read transaction)."""

    def __init__(self, con: sqlite3.Connection, generation: int):
        self.con = con
        self.generation = generation
        self._p = {"g": generation}
        self._by_ref: dict[str, dict | None] = {}
        self._by_id: dict[str, dict | None] = {}

    def close(self) -> None:
        try:
            self.con.execute("COMMIT")
        except sqlite3.Error:
            pass
        self.con.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- artifacts
    def _artifact(self, row) -> dict | None:
        if row is None:
            return None
        d = dict(row)
        d["layers"] = json.loads(d.get("layers") or "[]")
        d["quality_flags"] = json.loads(d.get("quality_flags") or "[]")
        return d

    def artifact_by_ref(self, source_ref: str) -> dict | None:
        if source_ref not in self._by_ref:
            row = self.con.execute(f"SELECT * FROM artifacts WHERE source_ref=:r AND {_vis()}",
                                   {**self._p, "r": source_ref}).fetchone()
            self._by_ref[source_ref] = self._artifact(row)
        return self._by_ref[source_ref]

    def artifact_by_id(self, artifact_id: str) -> dict | None:
        if artifact_id not in self._by_id:
            row = self.con.execute(f"SELECT * FROM artifacts WHERE artifact_id=:a AND {_vis()}",
                                   {**self._p, "a": artifact_id}).fetchone()
            self._by_id[artifact_id] = self._artifact(row)
        return self._by_id[artifact_id]

    def counts(self) -> dict:
        row = self.con.execute("SELECT value FROM meta WHERE key='counts'").fetchone()
        if row and row[0]:                                  # written in the activation transaction
            try:
                return json.loads(row[0])
            except ValueError:
                pass
        a = self.con.execute(f"SELECT COUNT(*) FROM artifacts WHERE {_vis()}", self._p).fetchone()[0]
        c = self.con.execute(f"SELECT COUNT(*) FROM chunk_rows WHERE {_vis()}", self._p).fetchone()[0]
        return {"artifacts": a, "chunks": c}

    def indexed_refs(self) -> set[str]:
        return {r[0] for r in self.con.execute(
            f"SELECT source_ref FROM artifacts WHERE {_vis()}", self._p)}

    # -- chunks
    def chunk_info(self, chunk_ref: str) -> dict | None:
        row = self.con.execute(f"SELECT * FROM chunk_rows WHERE chunk_ref=:c AND {_vis()}",
                               {**self._p, "c": chunk_ref}).fetchone()
        return dict(row) if row else None

    def first_chunk(self, artifact_id: str) -> str | None:
        row = self.con.execute(
            f"SELECT chunk_ref FROM chunk_rows WHERE artifact_id=:a AND {_vis()} ORDER BY idx LIMIT 1",
            {**self._p, "a": artifact_id}).fetchone()
        return row[0] if row else None

    def search_chunks(self, match: str, limit: int, group: str | None = None,
                      artifact_id: str | None = None) -> list[dict]:
        sql = (f"SELECT cr.chunk_ref, cr.artifact_id, cr.source_ref, cr.source_version, a.grp, "
               f"fts_chunks.norm AS norm, bm25(fts_chunks) AS score FROM fts_chunks "
               f"JOIN chunk_rows cr ON cr.id = fts_chunks.rowid "
               f"JOIN artifacts a ON a.artifact_id = cr.artifact_id AND {_vis('a')} "
               f"WHERE fts_chunks MATCH :m AND {_vis('cr')}")
        params = {**self._p, "m": match, "n": int(limit)}
        if group:
            sql += " AND a.grp = :grp"
            params["grp"] = group
        if artifact_id:
            sql += " AND cr.artifact_id = :aid"
            params["aid"] = artifact_id
        sql += " ORDER BY score, cr.chunk_ref LIMIT :n"
        try:
            return [dict(r) for r in self.con.execute(sql, params)]
        except sqlite3.OperationalError:
            return []                          # malformed MATCH can never reach here, but never raise

    def search_meta(self, match: str, limit: int, group: str | None = None) -> list[dict]:
        sql = (f"SELECT mr.artifact_id, mr.source_ref, mr.chunk_ref, mr.field, mr.node_id, "
               f"mr.annotation_revision, a.grp, fts_meta.norm AS norm, bm25(fts_meta) AS score "
               f"FROM fts_meta JOIN meta_rows mr ON mr.id = fts_meta.rowid "
               f"JOIN artifacts a ON a.artifact_id = mr.artifact_id AND {_vis('a')} "
               f"WHERE fts_meta MATCH :m AND {_vis('mr')}")
        params = {**self._p, "m": match, "n": int(limit)}
        if group:
            sql += " AND a.grp = :grp"
            params["grp"] = group
        sql += " ORDER BY score, mr.id LIMIT :n"
        try:
            return [dict(r) for r in self.con.execute(sql, params)]
        except sqlite3.OperationalError:
            return []

    # -- graph
    def _aliases(self, node: str) -> list[str]:
        if node.startswith("art:"):
            art = self.artifact_by_id(node[4:])
            return [node, art["source_ref"]] if art else [node]
        if PUBLIC_REF_RE.fullmatch(node):
            art = self.artifact_by_ref(node)
            return [node, "art:" + art["artifact_id"]] if art else [node]
        return [node]

    def canonical_node(self, node: str) -> str:
        """A public ref that is an indexed artifact's anchor IS that artifact
        node (identity of the same source, not a similarity merge)."""
        if PUBLIC_REF_RE.fullmatch(node):
            art = self.artifact_by_ref(node)
            if art:
                return "art:" + art["artifact_id"]
        return node

    def neighbors(self, node: str) -> list[dict]:
        names = self._aliases(node)
        q = ",".join("?" for _ in names)
        kinds = ",".join(f"'{k}'" for k in TRAVERSE_KINDS)
        sql = (f"SELECT src, dst, edge_kind, label, artifact_id, annotation_revision, provenance "
               f"FROM edges WHERE (src IN ({q}) OR dst IN ({q})) AND edge_kind IN ({kinds}) "
               f"AND gen_from <= ? AND (gen_to IS NULL OR gen_to > ?)")
        out, seen = [], set()
        for r in self.con.execute(sql, [*names, *names, self.generation, self.generation]):
            outgoing = r["src"] in names
            other = self.canonical_node(r["dst"] if outgoing else r["src"])
            if other == node or other in names:
                continue
            key = (other, r["label"], r["edge_kind"], outgoing)
            if key in seen:
                continue
            seen.add(key)
            try:
                prov = json.loads(r["provenance"] or "{}")
            except ValueError:
                prov = {}
            out.append({"other": other, "edge_kind": r["edge_kind"], "label": r["label"],
                        "direction": "out" if outgoing else "in", "owner": r["artifact_id"],
                        "annotation_revision": r["annotation_revision"],
                        "evidence": [e["chunk_ref"] for e in prov.get("evidence", []) if e.get("chunk_ref")],
                        "candidate": bool(prov.get("candidate"))})
        return out

    def expand(self, seeds, *, max_depth: int = C.GRAPH_MAX_DEPTH, max_nodes: int = C.GRAPH_MAX_NODES):
        """Bounded walk over this generation (see graph.expand): <= GRAPH_MAX_SEEDS
        seeds, depth <= 2, <= 40 nodes, hub guard, an inclusion reason per node."""
        return expand(self.neighbors, seeds, max_depth=max_depth, max_nodes=max_nodes)

    def entity_node(self, node_id: str) -> dict | None:
        row = self.con.execute(f"SELECT * FROM nodes WHERE node_id=:n AND kind='entity' AND {_vis()}",
                               {**self._p, "n": node_id}).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------- builder
class IndexBuilder:
    """Builds and refreshes the derived index from a ProjectionStore.

    `store` needs `list_current(after, limit)` and `change_marker()`.
    `index_path` is injected (the caller owns the location). A single process
    is assumed to write (`_lock`); readers are any number."""

    def __init__(self, store, index_path, *, wiki_root=None, page_size: int = PAGE_SIZE):
        self.store = store
        self.index_path = Path(index_path)
        self.wiki_root = Path(wiki_root) if wiki_root else None
        self.page_size = page_size
        self.graph = GraphBuilder(self.wiki_root)
        self._lock = threading.Lock()
        self._lag_cache: tuple | None = None
        self._after_artifact = None        # test hook: called after each artifact commit

    # ------------------------------------------------------------ connections
    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.index_path, timeout=30, isolation_level=None)
        con.row_factory = sqlite3.Row
        return con

    def _open_checked(self) -> sqlite3.Connection:
        """Connect to an EXISTING index and verify schema; raise IndexUnavailable."""
        if not fts5_available():
            raise IndexUnavailable("unavailable", "FTS5 is not compiled into this SQLite")
        if not self.index_path.is_file():
            raise IndexUnavailable("missing", "index file does not exist")
        try:
            con = self._connect()
        except sqlite3.Error as exc:
            raise IndexUnavailable("unavailable", f"cannot open index: {type(exc).__name__}") from None
        try:
            row = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is None:
                con.close()
                raise IndexUnavailable("missing", "index has no schema marker")
            if row[0] != str(C.INDEX_SCHEMA_VERSION):
                con.close()
                raise IndexUnavailable("unavailable", f"unsupported index schema {row[0]!r}")
            return con
        except IndexUnavailable:
            raise
        except sqlite3.OperationalError as exc:
            con.close()
            if "no such table" in str(exc):
                raise IndexUnavailable("missing", "index is empty") from None
            raise IndexUnavailable("unavailable", "index is unreadable (corrupt)") from None
        except sqlite3.Error:
            con.close()
            raise IndexUnavailable("unavailable", "index is unreadable (corrupt)") from None

    @staticmethod
    def _meta(con, key, default=None):
        row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    @staticmethod
    def _set_meta(con, key, value) -> None:
        con.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, None if value is None else str(value)))

    def _active(self, con) -> int | None:
        v = self._meta(con, "active_generation")
        return int(v) if v not in (None, "") else None

    def open_reader(self) -> IndexReader:
        """One complete generation, fixed for the lifetime of the reader."""
        con = self._open_checked()
        try:
            con.execute("BEGIN")
            gen = self._active(con)
            if gen is None:
                con.execute("COMMIT")
                con.close()
                raise IndexUnavailable("missing", "no active generation")
            return IndexReader(con, gen)
        except IndexUnavailable:
            raise
        except sqlite3.Error:
            con.close()
            raise IndexUnavailable("unavailable", "index is unreadable (corrupt)") from None

    # --------------------------------------------------------------- status
    def status(self, store=None) -> dict:
        """{"state","generation","indexed_artifacts","store_artifacts","lag":
        {"pending_artifacts"},"fresh","reason"}. Never raises. `lagging` = an
        active generation that is behind the store. The lag count costs one scan of
        the store and is cached per (store change_marker, generation)."""
        store = store or self.store
        down = {"generation": None, "indexed_artifacts": 0, "store_artifacts": 0,
                "lag": {"pending_artifacts": 0}, "fresh": False}
        try:
            marker = store.change_marker()
        except Exception:  # noqa: BLE001 - a broken store cannot make the index unreadable
            marker = None
        try:
            with self.open_reader() as rd:
                gen, counts = rd.generation, rd.counts()
                key = (marker, gen, str(self.index_path))
                cached = self._lag_cache if (marker is not None and self._lag_cache
                                             and self._lag_cache[0] == key) else None
                if cached:
                    pending, total = cached[1], cached[2]
                else:
                    indexed = {r[0]: r[1] for r in rd.con.execute(
                        f"SELECT source_ref, fingerprint FROM artifacts WHERE {_vis()}", rd._p)}
                    stamp = self.graph.stamp()
                    pending, total, seen = 0, 0, set()
                    for ref, fp, _item in self._scan(store, stamp):
                        seen.add(ref)
                        total += 1
                        if indexed.get(ref) != fp:
                            pending += 1
                    pending += len(set(indexed) - seen)
                    self._lag_cache = (key, pending, total)
        except IndexUnavailable as exc:
            return {**down, "state": exc.state, "reason": exc.reason}
        except sqlite3.Error:
            return {**down, "state": "unavailable", "reason": "index is unreadable (corrupt)"}
        return {"state": "lagging" if pending else "ok", "generation": gen,
                "indexed_artifacts": counts["artifacts"], "store_artifacts": total,
                "lag": {"pending_artifacts": pending}, "fresh": pending == 0, "reason": None}

    # ----------------------------------------------------------------- scan
    def _iter_items(self, store=None):
        store = store or self.store
        after = None
        while True:
            page = store.list_current(after, self.page_size)
            if not page:
                return
            for item in page:
                yield item
            after = page[-1]["projection"].source_ref
            if len(page) < self.page_size:
                return

    def _all_refs(self, store=None) -> set[str]:
        return {it["projection"].source_ref for it in self._iter_items(store)}

    def _fingerprint(self, item: dict, graph_stamp: str = "") -> str:
        p = item["projection"]
        return self._fp(p.source_version, item.get("annotation_revision"), item.get("review_state"),
                        item.get("annotation_window"), [c.chunk_ref for c in item.get("chunks") or []],
                        p.source_kind, graph_stamp)

    def _fingerprint_light(self, row: dict, graph_stamp: str = "") -> str:
        """Same value as `_fingerprint` of the full item, from a text-free row."""
        return self._fp(row["source_version"], row.get("annotation_revision"), row.get("review_state"),
                        row.get("annotation_window"), row["chunk_refs"], row["source_kind"], graph_stamp)

    @staticmethod
    def _fp(sv, ar, rs, aw, chunk_refs, kind, graph_stamp) -> str:
        payload = {"v": C.INDEX_SCHEMA_VERSION, "sv": sv, "ar": ar, "rs": rs,
                   "aw": aw,                                 # composed revisions + their review states
                   "cr": _sha("\n".join(chunk_refs)),
                   "gs": graph_stamp if kind == "canonical-record" else ""}
        return _sha(C.canonical_json(payload).decode("utf-8"))

    def _scan(self, store=None, graph_stamp: str = ""):
        """Yield (source_ref, fingerprint, item_or_None) for every current artifact. With a
        store offering `list_current_light` this reads no text (item None: fetch it only if
        dirty); otherwise it falls back to full items."""
        store = store or self.store
        light = getattr(store, "list_current_light", None)
        if light is None:
            for item in self._iter_items(store):
                yield item["projection"].source_ref, self._fingerprint(item, graph_stamp), item
            return
        after = None
        while True:
            page = light(after, self.page_size)
            if not page:
                return
            for row in page:
                yield row["source_ref"], self._fingerprint_light(row, graph_stamp), None
            after = page[-1]["source_ref"]
            if len(page) < self.page_size:
                return

    # --------------------------------------------------------------- refresh
    def refresh(self, batch_limit: int = C.INDEX_REFRESH_BATCH, *, trust_marker: bool = False,
                max_seconds: float | None = None, blocking: bool = True) -> IndexReceipt:
        """Incremental: index at most `batch_limit` dirty artifacts (plus all
        removals) into a new generation and activate it atomically. Returns the
        remaining lag. `trust_marker=True` skips the scan when the store's
        change_marker equals the one recorded by the last clean refresh.

        Query-time use (`brain_retrieve`): `blocking=False` returns at once when
        another refresh (this process or another one, e.g. the CLI) is running,
        and `max_seconds` stops writing artifacts after that long; whatever was
        written is activated, the rest stays lag. Dirty detection reads no text
        when the store offers `list_current_light`."""
        return self._run(max(1, int(batch_limit)), full=False, trust_marker=trust_marker,
                         max_seconds=max_seconds, blocking=blocking)

    def rebuild_full(self) -> IndexReceipt:
        """Explicit full rebuild (CLI action). Never called by a query. A
        corrupt index file is replaced (it is derived data)."""
        return self._run(None, full=True, trust_marker=False)

    def _lock_path(self) -> Path:
        return self.index_path.with_name(self.index_path.name + ".lock")

    def _acquire_file_lock(self, blocking: bool):
        """Cross-process writer lock (POSIX advisory). Returns an open fd, None when
        locking is not supported here, or raises BlockingIOError when busy."""
        try:
            import fcntl  # noqa: PLC0415 - POSIX only; Windows runs single-process
        except ImportError:
            return None
        fd = os.open(self._lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _receipt_unavailable(self, exc: IndexUnavailable, pending: int | None = None) -> IndexReceipt:
        if pending is None:
            try:
                pending = sum(1 for _ in self._iter_items())
            except Exception:  # noqa: BLE001
                pending = 0
        return IndexReceipt(generation=0, activated=False, artifacts_indexed=0, artifacts_pending=pending,
                            lag={"pending_artifacts": pending, "state": exc.state, "reason": exc.reason})

    def _run(self, batch_limit, *, full: bool, trust_marker: bool, max_seconds: float | None = None,
             blocking: bool = True) -> IndexReceipt:
        busy = IndexReceipt(0, False, 0, 0, {"pending_artifacts": None,
                                             "skipped": "another index refresh is running"})
        if not self._lock.acquire(blocking=blocking):
            return busy
        try:
            if not fts5_available():
                return self._receipt_unavailable(IndexUnavailable("unavailable", "FTS5 is not available"))
            self.index_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                fd = self._acquire_file_lock(blocking)
            except BlockingIOError:
                return busy
            try:
                try:
                    con = self._open_or_create(full)
                except IndexUnavailable as exc:
                    return self._receipt_unavailable(exc)
                try:
                    deadline = None if max_seconds is None else time.monotonic() + max_seconds
                    return self._build(con, batch_limit, full, trust_marker, deadline)
                finally:
                    con.close()
            finally:
                if fd is not None:
                    os.close(fd)              # closing the descriptor releases the flock
        finally:
            self._lock.release()

    def _open_or_create(self, full: bool) -> sqlite3.Connection:
        try:
            return self._open_checked()
        except IndexUnavailable as exc:
            if exc.state == "unavailable" and not full and self.index_path.is_file():
                raise                                  # corrupt: only an explicit rebuild replaces it
            if exc.state == "unavailable" and full and self.index_path.is_file():
                for suffix in ("", "-wal", "-shm"):
                    try:
                        Path(str(self.index_path) + suffix).unlink()
                    except FileNotFoundError:
                        pass
        con = self._connect()
        try:
            try:
                con.execute("PRAGMA journal_mode=WAL")
            except sqlite3.Error:
                pass
            con.executescript(SCHEMA)
            if self._meta(con, "schema_version") is None:
                self._set_meta(con, "schema_version", C.INDEX_SCHEMA_VERSION)
            return con
        except sqlite3.Error:
            con.close()
            raise IndexUnavailable("unavailable", "cannot initialise index") from None

    def _cleanup_orphans(self, con, active: int | None) -> None:
        """Discard what an interrupted build left behind. Rows of a generation
        newer than the active one were never visible."""
        with _txn(con):
            for t in ROW_TABLES:
                fts = _FTS_OF.get(t)
                if active is None:
                    if fts:
                        con.execute(f"DELETE FROM {fts}")
                    con.execute(f"DELETE FROM {t}")
                    continue
                if fts:
                    con.execute(f"DELETE FROM {fts} WHERE rowid IN (SELECT id FROM {t} WHERE gen_from > ?)",
                                (active,))
                con.execute(f"DELETE FROM {t} WHERE gen_from > ?", (active,))
                con.execute(f"UPDATE {t} SET gen_to=NULL WHERE gen_to > ?", (active,))
            con.execute("DELETE FROM generation WHERE state='building'")
            self._set_meta(con, "building_generation", None)

    def _indexed_fingerprints(self, con, active: int | None) -> dict[str, tuple[str, str]]:
        if active is None:
            return {}
        return {r["source_ref"]: (r["artifact_id"], r["fingerprint"]) for r in con.execute(
            f"SELECT source_ref, artifact_id, fingerprint FROM artifacts WHERE {_vis()}", {"g": active})}

    def _build(self, con, batch_limit, full: bool, trust_marker: bool, deadline: float | None = None) -> IndexReceipt:
        active = self._active(con)
        self._cleanup_orphans(con, active)             # safe: this process holds the writer locks
        marker = self.store.change_marker()
        if (trust_marker and not full and active is not None
                and self._meta(con, "scanned_marker") == str(marker)):
            n = con.execute(f"SELECT COUNT(*) FROM artifacts WHERE {_vis()}", {"g": active}).fetchone()[0]
            return IndexReceipt(active, False, 0, 0, {"pending_artifacts": 0, "indexed_artifacts": n,
                                                      "skipped": "store marker unchanged"})
        indexed = self._indexed_fingerprints(con, active)
        # pass 1: every current ref (graph resolution needs the full set) + dirty detection.
        # Text is read only for the artifacts that will be written in this batch.
        refs, dirty, kept, fetch = set(), [], [], []
        self.graph.reset(())
        stamp = self.graph.stamp()
        for ref, fp, item in (self._scan(None, stamp) if not full else ()):
            refs.add(ref)
            if indexed.get(ref, (None, None))[1] != fp:
                dirty.append((ref, fp))
                if batch_limit is not None and len(kept) + len(fetch) < batch_limit:
                    (kept.append((item, fp)) if item is not None else fetch.append(ref))
        if full:
            for item in self._iter_items():
                refs.add(item["projection"].source_ref)
                dirty.append((item["projection"].source_ref, None))
        if fetch:
            # the item may have moved since the light scan: fingerprint what is actually written
            kept += [(it, self._fingerprint(it, stamp)) for it in self.store.get_current_items(fetch)]
        removed = [(ref, aid) for ref, (aid, _fp) in indexed.items() if ref not in refs]
        self.graph.reset(refs)
        pending_total = len(dirty) + len(removed)
        if not dirty and not removed and active is not None:
            self._set_meta(con, "scanned_marker", marker)
            n = len(indexed)
            self._lag_cache = ((marker, active, str(self.index_path)), 0, len(refs))
            return IndexReceipt(active, False, 0, 0, {"pending_artifacts": 0, "indexed_artifacts": n})

        gen = (active or 0) + 1
        with _txn(con):
            con.execute("INSERT OR REPLACE INTO generation(id,state,created_at,store_marker) VALUES(?,?,?,?)",
                        (gen, "building", _now(), marker))
            self._set_meta(con, "building_generation", gen)
        written = 0
        if full:
            with _txn(con):                             # full rebuild: retire everything, then re-add
                for t in ROW_TABLES:
                    con.execute(f"UPDATE {t} SET gen_to=? WHERE gen_to IS NULL AND gen_from <= ?",
                                (gen, active if active is not None else 0))
        for ref, aid in removed:
            with _txn(con):
                self._retire(con, aid, gen)
            self._hook()
        if batch_limit is None:                         # stream pass 2 for a full rebuild
            todo = ((item, self._fingerprint(item, stamp)) for item in self._iter_items())
        else:
            todo = iter(kept)
        cut_by_time = False
        for item, fp in todo:
            if deadline is not None and written and time.monotonic() >= deadline:
                cut_by_time = True                      # query-time cap: activate what is written
                break
            with _txn(con):
                self._write_artifact(con, gen, item, fp, retire=not full)
            written += 1
            self._hook()
        remaining = max(0, pending_total - written - len(removed))
        with _txn(con):                                 # activation: the single visible flip
            counts = {"artifacts": con.execute(f"SELECT COUNT(*) FROM artifacts WHERE {_vis()}", {"g": gen}).fetchone()[0],
                      "chunks": con.execute(f"SELECT COUNT(*) FROM chunk_rows WHERE {_vis()}", {"g": gen}).fetchone()[0]}
            self._set_meta(con, "counts", json.dumps(counts))
            self._set_meta(con, "active_generation", gen)
            self._set_meta(con, "scanned_marker", marker if remaining == 0 else None)
            con.execute("UPDATE generation SET state='retired' WHERE state='active'")
            con.execute("UPDATE generation SET state='active', activated_at=? WHERE id=?", (_now(), gen))
            self._set_meta(con, "building_generation", None)
        self._compact(con, gen)
        n = con.execute(f"SELECT COUNT(*) FROM artifacts WHERE {_vis()}", {"g": gen}).fetchone()[0]
        # the lag of (marker, gen) is known now: status() need not scan the store again
        self._lag_cache = ((marker, gen, str(self.index_path)), remaining, len(refs))
        lag = {"pending_artifacts": remaining, "indexed_artifacts": n, "removed_artifacts": len(removed)}
        if cut_by_time:
            lag["stopped"] = "time cap"
        return IndexReceipt(gen, True, written, remaining, lag)

    def _hook(self) -> None:
        if self._after_artifact is not None:
            self._after_artifact()

    def _retire(self, con, artifact_id: str, gen: int) -> None:
        for t in ROW_TABLES:
            con.execute(f"UPDATE {t} SET gen_to=? WHERE artifact_id=? AND gen_to IS NULL", (gen, artifact_id))

    def _compact(self, con, active: int) -> None:
        """Physically drop rows no generation >= active can see."""
        try:
            with _txn(con):
                for t in ROW_TABLES:
                    fts = _FTS_OF.get(t)
                    if fts:
                        con.execute(f"DELETE FROM {fts} WHERE rowid IN "
                                    f"(SELECT id FROM {t} WHERE gen_to IS NOT NULL AND gen_to <= ?)", (active,))
                    con.execute(f"DELETE FROM {t} WHERE gen_to IS NOT NULL AND gen_to <= ?", (active,))
                con.execute("DELETE FROM generation WHERE state='retired' AND id < ?", (active,))
        except sqlite3.Error:
            pass                                        # compaction is best effort; rows stay invisible

    # ------------------------------------------------------------ row writers
    def _write_artifact(self, con, gen: int, item: dict, fp: str, *, retire: bool = True) -> None:
        proj, chunks = item["projection"], list(item.get("chunks") or [])
        if retire:
            self._retire(con, proj.artifact_id, gen)
        ann = usable_annotation(item)
        chunk_refs = {c.chunk_ref for c in chunks}
        derived = derived_of(item) if ann is not None else {"entities": [], "links": []}
        annotated = set()
        if ann is not None:
            annotated = {c.get("chunk_ref") for c in (ann.get("chunk_annotations") or [])
                         if isinstance(c, dict)} & chunk_refs
        flags = list(proj.quality_flags)
        layers = [{"layer_id": la.layer_id, "text_ref": la.text_ref, "tier": la.tier,
                   "authority_level": la.authority_level} for la in proj.layers]
        grp = "captures" if proj.source_tier == "capture" else "canonical"
        con.execute(
            "INSERT INTO artifacts(artifact_id,source_ref,source_version,source_kind,source_tier,"
            "authority_level,grp,layers,quality_flags,processing_state,annotation_revision,review_state,"
            "annotated_chunks,chunk_count,fingerprint,gen_from) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (proj.artifact_id, proj.source_ref, proj.source_version, proj.source_kind, proj.source_tier,
             proj.source_authority_level, grp, json.dumps(layers, ensure_ascii=False),
             json.dumps(flags), item.get("processing_state") or proj.processing_state,
             item.get("effective_revision", item.get("annotation_revision")) if ann is not None else None,
             item.get("effective_review_state", item.get("review_state")),
             len(annotated), len(chunks), fp, gen))
        for c in chunks:
            cur = con.execute(
                "INSERT INTO chunk_rows(chunk_ref,artifact_id,source_ref,source_version,idx,layer_id,"
                "start_char,end_char,gen_from) VALUES(?,?,?,?,?,?,?,?,?)",
                (c.chunk_ref, proj.artifact_id, proj.source_ref, c.source_version, c.index, c.layer_id,
                 c.start_char, c.end_char, gen))
            con.execute("INSERT INTO fts_chunks(rowid, norm, chunk_ref) VALUES(?,?,?)",
                        (cur.lastrowid, C.normal_form(c.text), c.chunk_ref))
        if ann is not None:
            rev = item.get("effective_revision", item.get("annotation_revision"))
            for chunk_ref, fld, value, node_id in self._meta_values(ann, derived, chunk_refs):
                norm = C.normal_form(value)
                if not norm:
                    continue
                cur = con.execute(
                    "INSERT INTO meta_rows(artifact_id,source_ref,chunk_ref,field,value,node_id,"
                    "annotation_revision,gen_from) VALUES(?,?,?,?,?,?,?,?)",
                    (proj.artifact_id, proj.source_ref, chunk_ref, fld, value, node_id, rev, gen))
                con.execute("INSERT INTO fts_meta(rowid, norm, artifact_id, chunk_ref) VALUES(?,?,?,?)",
                            (cur.lastrowid, norm, proj.artifact_id, chunk_ref))
        frag = self.graph.build(item)
        for n in frag.nodes:
            con.execute("INSERT INTO nodes(node_id,kind,artifact_id,source_ref,label,normal,extra,gen_from) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (n["node_id"], n["kind"], n["artifact_id"], n["source_ref"], n["label"], n["normal"],
                         json.dumps(n["extra"], ensure_ascii=False), gen))
        for e in frag.edges:
            con.execute("INSERT INTO edges(src,dst,edge_kind,label,artifact_id,source_ref,source_version,"
                        "annotation_revision,provenance,gen_from) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (e["src"], e["dst"], e["edge_kind"], e["label"], e["artifact_id"], e["source_ref"],
                         e["source_version"], e["annotation_revision"],
                         json.dumps(e["provenance"], ensure_ascii=False, sort_keys=True), gen))

    @staticmethod
    def _meta_values(ann: dict, derived: dict, chunk_refs: set) -> list[tuple]:
        rows: list[tuple] = []
        seen: set = set()

        def add(chunk_ref, fld, value, node_id=None):
            if not isinstance(value, str) or not value.strip():
                return
            key = (chunk_ref, fld, C.normal_form(value), node_id)
            if key not in seen:
                seen.add(key)
                rows.append((chunk_ref, fld, value, node_id))

        add(None, "summary", ann.get("summary"))
        for t in ann.get("topics") or []:
            add(None, "topic", t)
        for t in ann.get("tags") or []:
            add(None, "tag", t)
        for e in derived.get("entities") or []:
            eid = e.get("entity_id")
            mention = (e.get("mention") or {}).get("chunk_ref")
            mention = mention if mention in chunk_refs else None
            add(mention, "entity", e.get("name"), eid)
            for a in e.get("aliases") or []:
                add(mention, "alias", a, eid)
        for ca in ann.get("chunk_annotations") or []:
            ref = ca.get("chunk_ref") if isinstance(ca, dict) else None
            if ref not in chunk_refs:
                continue
            add(ref, "chunk_summary", ca.get("summary"))
            for t in ca.get("tags") or []:
                add(ref, "chunk_tag", t)
            for t in ca.get("topics") or []:
                add(ref, "chunk_tag", t)
        return rows

