"""Persistent projection/annotation store (CONTRACT-E1 section 6).

`_enrichment/store-v1.sqlite3`: SQLite, WAL, foreign keys, `user_version = 1`,
one writer transaction per mutation (`BEGIN IMMEDIATE`). The store is the
only home of model-produced (candidate) metadata; the index under `_search/`
is derived from it and the corpus is never written. Unknown newer schema ->
`E_SCHEMA_UNSUPPORTED`, never ALTER/DROP. See docs/enrichment/store-schema.md
for the DDL, the state machine and the transaction boundaries.

Connections are opened per operation (no shared handle between threads) and
read helpers use `PRAGMA query_only=ON` snapshots, so `status`, `verify`,
`export_jsonl` and `backup` are safe against a live writer.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import threading
import time
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import contract as C
from . import projector
from .chunking import Chunker as _Chunker
from .source import SourceReader

DB_NAME = "store-v1.sqlite3"
BUSY_TIMEOUT_MS = 30_000
MAX_PENDING_LIMIT = 5
DISCOVERY_BUDGET = 100          # sources scanned/projected per pending() call
LIST_PAGE = 50
TRANSIENT_MAX_ATTEMPTS = 2
RETRY_SPACING_SECONDS = 300
ANNOTATE_KINDS = ("capture-text", "capture-media", "ingested-file")
MAX_ANNOTATION_BYTES = 1_048_576
SOURCE_REF_RE = re.compile(r"^(rec|doc|cap):[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

# ---------------------------------------------------------------------- DDL
DDL: tuple[str, ...] = (
    """CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
) WITHOUT ROWID""",
    """CREATE TABLE sources (
  source_ref      TEXT PRIMARY KEY,
  artifact_id     TEXT NOT NULL,
  current_version TEXT,
  state           TEXT NOT NULL CHECK (state IN ('discovered','projected','annotated','failed','stale','no_text')),
  present         INTEGER NOT NULL DEFAULT 1 CHECK (present IN (0,1)),
  missing_since   TEXT,
  seen_pass       INTEGER NOT NULL DEFAULT 0,
  first_seen_at   TEXT NOT NULL,
  updated_at      TEXT NOT NULL
)""",
    """CREATE TABLE projections (
  source_ref             TEXT NOT NULL REFERENCES sources(source_ref),
  source_version         TEXT NOT NULL CHECK (length(source_version) = 64),
  artifact_id            TEXT NOT NULL,
  source_kind            TEXT NOT NULL,
  source_tier            TEXT NOT NULL,
  source_authority_level INTEGER NOT NULL,
  storage_sha256         TEXT NOT NULL,
  original_sha256        TEXT,
  origin_channel         TEXT,
  quality_flags          TEXT NOT NULL,
  coverage_warnings      TEXT NOT NULL,
  processing_state       TEXT NOT NULL CHECK (processing_state IN ('projected','no_text')),
  projection_version     TEXT NOT NULL,
  schema_version         TEXT NOT NULL,
  status                 TEXT NOT NULL CHECK (status IN ('current','stale')),
  created_at             TEXT NOT NULL,
  superseded_at          TEXT,
  PRIMARY KEY (source_ref, source_version)
)""",
    "CREATE UNIQUE INDEX projections_one_current ON projections(source_ref) WHERE status = 'current'",
    """CREATE TABLE layers (
  source_ref        TEXT NOT NULL,
  source_version    TEXT NOT NULL,
  ordinal           INTEGER NOT NULL,
  layer_id          TEXT NOT NULL,
  text_ref          TEXT NOT NULL,
  text_sha256       TEXT NOT NULL CHECK (length(text_sha256) = 64),
  chars             INTEGER NOT NULL,
  bytes             INTEGER NOT NULL,
  literal           INTEGER NOT NULL,
  reviewed          INTEGER NOT NULL,
  origin            TEXT NOT NULL,
  provenance_sha256 TEXT,
  tier              TEXT NOT NULL,
  authority_level   INTEGER NOT NULL,
  text              TEXT NOT NULL,
  PRIMARY KEY (source_ref, source_version, ordinal),
  FOREIGN KEY (source_ref, source_version) REFERENCES projections(source_ref, source_version)
)""",
    """CREATE TABLE chunks (
  chunk_ref      TEXT PRIMARY KEY CHECK (chunk_ref = 'chk:' || substr(chunk_digest, 1, 32)),
  chunk_digest   TEXT NOT NULL UNIQUE CHECK (length(chunk_digest) = 64),
  source_ref     TEXT NOT NULL,
  source_version TEXT NOT NULL,
  layer_ordinal  INTEGER NOT NULL,
  layer_id       TEXT NOT NULL,
  text_ref       TEXT NOT NULL,
  idx            INTEGER NOT NULL,
  start_char     INTEGER NOT NULL CHECK (start_char >= 0),
  end_char       INTEGER NOT NULL CHECK (end_char >= start_char),
  prev_chunk_ref TEXT,
  next_chunk_ref TEXT,
  quality_flags  TEXT NOT NULL,
  heading_path   TEXT NOT NULL,
  UNIQUE (source_ref, source_version, idx),
  FOREIGN KEY (source_ref, source_version, layer_ordinal) REFERENCES layers(source_ref, source_version, ordinal)
)""",
    """CREATE TABLE annotation_revisions (
  source_ref        TEXT NOT NULL,
  revision          INTEGER NOT NULL CHECK (revision >= 1),
  source_version    TEXT NOT NULL,
  annotation        TEXT NOT NULL,
  derived           TEXT,
  producer_reported TEXT,
  request_digest    TEXT NOT NULL,
  idempotency_key   TEXT NOT NULL,
  receipt           TEXT NOT NULL,
  received_at       TEXT NOT NULL,
  PRIMARY KEY (source_ref, revision),
  UNIQUE (source_ref, idempotency_key),
  FOREIGN KEY (source_ref, source_version) REFERENCES projections(source_ref, source_version)
)""",
    """CREATE TRIGGER annotation_revisions_no_update BEFORE UPDATE ON annotation_revisions
BEGIN SELECT RAISE(ABORT, 'annotation_revisions is append-only'); END""",
    """CREATE TRIGGER annotation_revisions_no_delete BEFORE DELETE ON annotation_revisions
BEGIN SELECT RAISE(ABORT, 'annotation_revisions is append-only'); END""",
    """CREATE TABLE annotation_chunks (
  source_ref TEXT NOT NULL,
  revision   INTEGER NOT NULL,
  chunk_ref  TEXT NOT NULL,
  PRIMARY KEY (source_ref, revision, chunk_ref),
  FOREIGN KEY (source_ref, revision) REFERENCES annotation_revisions(source_ref, revision)
) WITHOUT ROWID""",
    "CREATE INDEX annotation_chunks_by_chunk ON annotation_chunks(chunk_ref)",
    """CREATE TABLE entities (
  entity_id      TEXT NOT NULL,
  source_ref     TEXT NOT NULL,
  first_revision INTEGER NOT NULL,
  name           TEXT NOT NULL,
  normal         TEXT NOT NULL,
  kind           TEXT,
  PRIMARY KEY (entity_id, source_ref),
  FOREIGN KEY (source_ref, first_revision) REFERENCES annotation_revisions(source_ref, revision)
) WITHOUT ROWID""",
    """CREATE TABLE reviews (
  review_id           INTEGER PRIMARY KEY AUTOINCREMENT,
  source_ref          TEXT NOT NULL,
  annotation_revision INTEGER NOT NULL,
  decision            TEXT NOT NULL CHECK (decision IN ('confirmed','rejected')),
  reviewed_by         TEXT NOT NULL,
  reviewed_at         TEXT NOT NULL,
  note                TEXT,
  canonical_promotion INTEGER NOT NULL DEFAULT 0 CHECK (canonical_promotion = 0),
  FOREIGN KEY (source_ref, annotation_revision) REFERENCES annotation_revisions(source_ref, revision)
)""",
    """CREATE TRIGGER reviews_no_update BEFORE UPDATE ON reviews
BEGIN SELECT RAISE(ABORT, 'reviews is append-only'); END""",
    """CREATE TRIGGER reviews_no_delete BEFORE DELETE ON reviews
BEGIN SELECT RAISE(ABORT, 'reviews is append-only'); END""",
    """CREATE TABLE failures (
  source_ref         TEXT PRIMARY KEY REFERENCES sources(source_ref),
  attempts           INTEGER NOT NULL CHECK (attempts >= 1),
  last_error_code    TEXT NOT NULL,
  last_error_message TEXT NOT NULL,
  transient          INTEGER NOT NULL CHECK (transient IN (0,1)),
  flags              TEXT NOT NULL,
  first_failed_at    TEXT NOT NULL,
  last_failed_at     TEXT NOT NULL,
  next_retry_after   TEXT
)""",
    """CREATE TABLE inventory (
  id                     INTEGER PRIMARY KEY CHECK (id = 1),
  pass_no                INTEGER NOT NULL,
  cursor                 TEXT,
  started_at             TEXT NOT NULL,
  scanned                INTEGER NOT NULL DEFAULT 0,
  projected              INTEGER NOT NULL DEFAULT 0,
  changed                INTEGER NOT NULL DEFAULT 0,
  failed                 INTEGER NOT NULL DEFAULT 0,
  no_text                INTEGER NOT NULL DEFAULT 0,
  inventory_generation   INTEGER,
  last_pass_no           INTEGER,
  last_pass_completed_at TEXT,
  last_pass_summary      TEXT
)""",
)
REQUIRED_TABLES = ("meta", "sources", "projections", "layers", "chunks", "annotation_revisions",
                   "annotation_chunks", "entities", "reviews", "failures", "inventory")


class _StoreMissing(Exception):
    """The store file does not exist (reads treat this as 'nothing stored')."""


def _j(obj) -> str:
    return C.canonical_json(obj).decode("utf-8")


def _unj(text):
    return None if text is None else json.loads(text)


def annotation_policy(source_kind: str) -> str:
    """`annotate` for captures and ingested files, `skip` for everything else."""
    return "annotate" if source_kind in ANNOTATE_KINDS else "skip"


def _err(code: str, message: str, **extra) -> C.EnrichmentError:
    return C.EnrichmentError(code, message, **extra)


# -------------------------------------------------------------------- store
class SQLiteProjectionStore:
    """Implements contract.ProjectionStore (+ read helpers) on SQLite."""

    def __init__(self, root, *, reader=None, max_input_chars: int = 24000, clock=None,
                 chunker=None, policy: C.ChunkPolicy | None = None,
                 discovery_budget: int = DISCOVERY_BUDGET,
                 retry_spacing_seconds: int = RETRY_SPACING_SECONDS):
        self.root = Path(root).resolve()
        self.reader = reader if reader is not None else SourceReader(self.root)
        if not isinstance(max_input_chars, int) or isinstance(max_input_chars, bool) or max_input_chars < 1000:
            raise _err("E_BAD_ARGUMENTS", "max_input_chars must be an integer >= 1000")
        self.max_input_chars = max_input_chars
        self.chunker = chunker if chunker is not None else _Chunker()
        self.policy = policy or C.ChunkPolicy()
        self.discovery_budget = int(discovery_budget)
        self.retry_spacing = timedelta(seconds=retry_spacing_seconds)
        self._clock = clock
        self._dir = self.root / C.ENRICHMENT_DIR
        self._db = self._dir / DB_NAME
        self._refresh_lock = threading.Lock()
        self._instance: str | None = None

    # ------------------------------------------------------------ plumbing
    def _now(self) -> datetime:
        raw = self._clock() if self._clock is not None else datetime.now(timezone.utc)
        if isinstance(raw, (int, float)):
            return datetime.fromtimestamp(raw, timezone.utc)
        if isinstance(raw, str):
            raw = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return raw.replace(tzinfo=timezone.utc) if raw.tzinfo is None else raw.astimezone(timezone.utc)

    @staticmethod
    def _fmt(dt: datetime) -> str:
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _ts(self) -> str:
        return self._fmt(self._now())

    def _instance_id(self) -> str:
        if self._instance is None:
            self._instance = C.instance_id(self.root)
        return self._instance

    def _ensure_dir(self) -> None:
        if self._dir.is_symlink():
            raise _err("E_UNAVAILABLE", "the enrichment directory is a symlink; refusing to use it")
        try:
            self._dir.mkdir(mode=0o700, exist_ok=True)
            os.chmod(self._dir, 0o700)
            self._dir.resolve().relative_to(self.root)
        except ValueError:
            raise _err("E_UNAVAILABLE", "the enrichment directory resolves outside the wiki root") from None
        except OSError as exc:
            raise _err("E_UNAVAILABLE", f"the enrichment directory cannot be created ({exc.__class__.__name__})") from None

    def _open(self, *, create: bool, readonly: bool = False) -> sqlite3.Connection:
        if self._dir.is_symlink() or self._db.is_symlink():
            raise _err("E_UNAVAILABLE", "the enrichment store path is a symlink; refusing to use it")
        if not self._db.exists():
            if not create:
                raise _StoreMissing()
            self._ensure_dir()
        try:
            cx = sqlite3.connect(str(self._db), timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
            cx.row_factory = sqlite3.Row
            cx.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            # one snapshot: a concurrent creator must never look "half made" (version 0 + objects)
            cx.execute("BEGIN")
            version = cx.execute("PRAGMA user_version").fetchone()[0]
            n_objects = cx.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]
            have = {r[0] for r in cx.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            cx.execute("COMMIT")
            if version > C.STORE_SCHEMA_VERSION:
                raise _err("E_SCHEMA_UNSUPPORTED",
                           f"the enrichment store has schema version {version}; this code supports "
                           f"{C.STORE_SCHEMA_VERSION}", found=version, supported=C.STORE_SCHEMA_VERSION)
            if version == 0:
                if n_objects:
                    raise _err("E_SCHEMA_UNSUPPORTED",
                               "the enrichment store has no schema version but contains objects; it is left untouched",
                               found=0, supported=C.STORE_SCHEMA_VERSION)
                if not create:
                    cx.close()
                    raise _StoreMissing()
                self._init_schema(cx)
            else:
                missing = [t for t in REQUIRED_TABLES if t not in have]
                if missing:
                    raise _err("E_SCHEMA_UNSUPPORTED",
                               f"the enrichment store lacks required tables {missing}; it is left untouched",
                               found=version, supported=C.STORE_SCHEMA_VERSION)
            if readonly:
                cx.execute("PRAGMA query_only=ON")
            cx.execute("PRAGMA foreign_keys=ON")
            if not readonly:
                cx.execute("PRAGMA synchronous=FULL")
            return cx
        except sqlite3.DatabaseError as exc:
            with contextlib.suppress(Exception):
                cx.close()
            raise _err("E_UNAVAILABLE", f"the enrichment store cannot be opened ({exc.__class__.__name__})") from None
        except BaseException:
            with contextlib.suppress(Exception):
                cx.close()
            raise

    def _init_schema(self, cx: sqlite3.Connection) -> None:
        for attempt in range(100):          # switching to WAL can report BUSY without waiting
            try:
                cx.execute("PRAGMA journal_mode=WAL")
                break
            except sqlite3.OperationalError:
                if attempt == 99:
                    raise
                time.sleep(0.05)
        cx.execute("BEGIN IMMEDIATE")
        try:
            version = cx.execute("PRAGMA user_version").fetchone()[0]
            if version == 0 and cx.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 0:
                for stmt in DDL:
                    cx.execute(stmt)
                now = self._ts()
                cx.executemany("INSERT INTO meta(key, value) VALUES (?, ?)", [
                    ("created_at", now), ("change_marker", "0"),
                    ("store_schema", str(C.STORE_SCHEMA_VERSION)), ("instance_id", self._instance_id())])
                cx.execute("INSERT INTO inventory(id, pass_no, started_at) VALUES (1, 1, ?)", (now,))
                cx.execute(f"PRAGMA user_version={C.STORE_SCHEMA_VERSION}")
            cx.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(Exception):
                cx.execute("ROLLBACK")
            raise
        with contextlib.suppress(OSError):
            os.chmod(self._db, 0o600)

    @contextlib.contextmanager
    def _write(self):
        """One writer transaction: BEGIN IMMEDIATE ... COMMIT, ROLLBACK on any error."""
        cx = self._open(create=True)
        try:
            try:
                cx.execute("BEGIN IMMEDIATE")
                yield cx
                cx.execute("COMMIT")
            except BaseException as exc:
                with contextlib.suppress(Exception):
                    if cx.in_transaction:
                        cx.execute("ROLLBACK")
                if isinstance(exc, sqlite3.OperationalError):
                    raise _err("E_UNAVAILABLE", f"the enrichment store is busy or unavailable ({exc})") from None
                if isinstance(exc, sqlite3.IntegrityError):
                    raise _err("E_INTERNAL", f"the enrichment store rejected a write ({exc})") from None
                raise
        finally:
            cx.close()

    @contextlib.contextmanager
    def _read(self):
        """A consistent read-only snapshot (query_only). Raises _StoreMissing when absent."""
        cx = self._open(create=False, readonly=True)
        try:
            cx.execute("BEGIN")
            yield cx
        except sqlite3.OperationalError as exc:
            raise _err("E_UNAVAILABLE", f"the enrichment store is busy or unavailable ({exc})") from None
        finally:
            with contextlib.suppress(Exception):
                if cx.in_transaction:
                    cx.execute("ROLLBACK")
            cx.close()

    @staticmethod
    def _bump(cx: sqlite3.Connection) -> None:
        cx.execute("UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key='change_marker'")

    # ----------------------------------------------------------- validation
    @staticmethod
    def _validate(projection: C.SourceProjection, chunks: list[C.Chunk]) -> None:
        ref = projection.source_ref

        def bad(reason: str):
            return _err("E_VALIDATION", f"{ref}: {reason}", source_ref=ref, stage="store")

        if not isinstance(ref, str) or not SOURCE_REF_RE.match(ref):
            raise _err("E_VALIDATION", "source_ref is not a valid public ref", stage="store")
        if not C.SHA256_RE.match(projection.source_version or "") or not C.SHA256_RE.match(projection.artifact_id or ""):
            raise bad("source_version and artifact_id must be 64 hex characters")
        if projection.source_kind not in C.SOURCE_KINDS or projection.source_tier not in C.SOURCE_TIERS:
            raise bad("unknown source_kind or source_tier")
        if not C.SHA256_RE.match(projection.storage_sha256 or ""):
            raise bad("storage_sha256 must be 64 hex characters")
        seen_ids = set()
        for la in projection.layers:
            if la.layer_id not in C.LAYER_IDS:
                raise bad(f"unknown layer_id {la.layer_id!r}")
            if la.layer_id in seen_ids:
                raise bad(f"duplicate layer_id {la.layer_id!r}")
            seen_ids.add(la.layer_id)
            if not SOURCE_REF_RE.match(la.text_ref or ""):
                raise bad(f"layer {la.layer_id}: text_ref is not a public ref")
            if la.text_sha256 != C.sha256_text(la.text) or la.chars != len(la.text) \
                    or la.bytes != len(la.text.encode("utf-8")):
                raise bad(f"layer {la.layer_id}: text hash/length does not match its text")
        expect_version = C.source_version(ref, [la.version_dict() for la in projection.layers])
        if expect_version != projection.source_version:
            raise bad("source_version does not match the layers")
        by_layer = {la.layer_id: la for la in projection.layers}
        per: dict[str, list[C.Chunk]] = {}
        refs = set()
        for i, ch in enumerate(chunks):
            la = by_layer.get(ch.layer_id)
            if la is None or ch.source_ref != ref or ch.source_version != projection.source_version:
                raise bad(f"chunk {i} does not belong to this projection")
            if ch.index != i:
                raise bad(f"chunk indexes must run 0..n-1 in layer order (chunk {i} has index {ch.index})")
            if ch.text_ref != la.text_ref:
                raise bad(f"chunk {i}: text_ref differs from its layer")
            if not (0 <= ch.start_char <= ch.end_char <= len(la.text)) or ch.text != la.text[ch.start_char:ch.end_char]:
                raise bad(f"chunk {i}: text is not layer.text[start_char:end_char]")
            digest = C.chunk_digest(ref, projection.source_version, ch.layer_id, ch.start_char, ch.end_char)
            if ch.chunk_digest != digest or ch.chunk_ref != C.chunk_ref(digest) or not C.CHUNK_REF_RE.match(ch.chunk_ref):
                raise bad(f"chunk {i}: chunk_ref/chunk_digest do not match its coordinates")
            if ch.chunk_ref in refs:
                raise bad(f"chunk {i}: duplicate chunk_ref")
            refs.add(ch.chunk_ref)
            per.setdefault(ch.layer_id, []).append(ch)
        order = {la.layer_id: n for n, la in enumerate(projection.layers)}
        if [c.layer_id for c in chunks] != sorted((c.layer_id for c in chunks), key=lambda x: order[x]):
            raise bad("chunks must be ordered by layer")
        for la in projection.layers:
            mine = per.get(la.layer_id, [])
            if not la.text:
                if len(mine) > 1 or any(c.end_char for c in mine):
                    raise bad(f"layer {la.layer_id}: an empty layer can have at most one empty chunk")
                continue
            pos = 0
            for c in mine:
                if c.start_char != pos:
                    raise bad(f"layer {la.layer_id}: chunks leave a gap or overlap at {pos}")
                pos = c.end_char
            if pos != len(la.text):
                raise bad(f"layer {la.layer_id}: chunks do not cover the whole text")
        for ch in chunks:
            for nb in (ch.prev_chunk_ref, ch.next_chunk_ref):
                if nb is not None and nb not in refs:
                    raise bad("prev/next chunk link points outside this projection")

    # ------------------------------------------------------- row -> object
    @staticmethod
    def _layers(cx, ref: str, version: str) -> list[sqlite3.Row]:
        return cx.execute("SELECT * FROM layers WHERE source_ref=? AND source_version=? ORDER BY ordinal",
                          (ref, version)).fetchall()

    def _projection(self, cx, ref: str, version: str, *, state: str | None = None) -> C.SourceProjection:
        p = cx.execute("SELECT * FROM projections WHERE source_ref=? AND source_version=?",
                       (ref, version)).fetchone()
        layers = tuple(C.Layer(
            layer_id=r["layer_id"], text_ref=r["text_ref"], text_sha256=r["text_sha256"], chars=r["chars"],
            bytes=r["bytes"], literal=bool(r["literal"]), reviewed=bool(r["reviewed"]), text=r["text"],
            origin=r["origin"], provenance_sha256=r["provenance_sha256"], tier=r["tier"],
            authority_level=r["authority_level"]) for r in self._layers(cx, ref, version))
        return C.SourceProjection(
            artifact_id=p["artifact_id"], source_ref=ref, source_version=version, source_kind=p["source_kind"],
            source_tier=p["source_tier"], source_authority_level=p["source_authority_level"], layers=layers,
            storage_sha256=p["storage_sha256"], quality_flags=tuple(json.loads(p["quality_flags"])),
            coverage_warnings=tuple(json.loads(p["coverage_warnings"])),
            processing_state=state or p["processing_state"], projection_version=p["projection_version"],
            schema_version=p["schema_version"], original_sha256=p["original_sha256"],
            origin_channel=p["origin_channel"])

    @staticmethod
    def _chunks(cx, ref: str, version: str, layer_texts: dict[int, str]) -> list[C.Chunk]:
        out = []
        for r in cx.execute("SELECT * FROM chunks WHERE source_ref=? AND source_version=? ORDER BY idx",
                            (ref, version)):
            out.append(C.Chunk(
                chunk_ref=r["chunk_ref"], chunk_digest=r["chunk_digest"], source_ref=ref, source_version=version,
                text_ref=r["text_ref"], layer_id=r["layer_id"], index=r["idx"], start_char=r["start_char"],
                end_char=r["end_char"], text=layer_texts[r["layer_ordinal"]][r["start_char"]:r["end_char"]],
                prev_chunk_ref=r["prev_chunk_ref"], next_chunk_ref=r["next_chunk_ref"],
                quality_flags=tuple(json.loads(r["quality_flags"])),
                heading_path=tuple(json.loads(r["heading_path"]))))
        return out

    def _layer_texts(self, cx, ref: str, version: str) -> dict[int, str]:
        return {r["ordinal"]: r["text"] for r in self._layers(cx, ref, version)}

    # ------------------------------------------------------ annotation info
    @staticmethod
    def _revision_info(cx, ref: str, version: str):
        """(latest revision row for `version` or None, max revision overall)."""
        row = cx.execute("SELECT * FROM annotation_revisions WHERE source_ref=? AND source_version=? "
                         "ORDER BY revision DESC LIMIT 1", (ref, version)).fetchone()
        overall = cx.execute("SELECT COALESCE(MAX(revision), 0) FROM annotation_revisions WHERE source_ref=?",
                             (ref,)).fetchone()[0]
        return row, overall

    @staticmethod
    def _covered(cx, ref: str, version: str) -> tuple[set[str], int]:
        """Chunk refs annotated under `version` (union over its revisions) and total chunks.
        A one-chunk artifact with any revision counts as covered."""
        chunk_refs = [r[0] for r in cx.execute(
            "SELECT chunk_ref FROM chunks WHERE source_ref=? AND source_version=? ORDER BY idx", (ref, version))]
        covered = {r[0] for r in cx.execute(
            "SELECT DISTINCT ac.chunk_ref FROM annotation_chunks ac "
            "JOIN annotation_revisions ar ON ar.source_ref=ac.source_ref AND ar.revision=ac.revision "
            "JOIN chunks c ON c.chunk_ref=ac.chunk_ref AND c.source_version=ar.source_version "
            "WHERE ac.source_ref=? AND ar.source_version=?", (ref, version))}
        if len(chunk_refs) == 1 and cx.execute(
                "SELECT 1 FROM annotation_revisions WHERE source_ref=? AND source_version=? LIMIT 1",
                (ref, version)).fetchone():
            covered = set(chunk_refs)
        return covered, len(chunk_refs)

    def _coverage(self, cx, ref: str, version: str) -> dict:
        covered, total = self._covered(cx, ref, version)
        return {"annotated_chunks": len(covered), "total_chunks": total}

    @staticmethod
    def _review_state(cx, ref: str, revision: int | None) -> str:
        if revision is None:
            return "unreviewed"
        row = cx.execute("SELECT decision FROM reviews WHERE source_ref=? AND annotation_revision=? "
                         "ORDER BY review_id DESC LIMIT 1", (ref, revision)).fetchone()
        return row[0] if row else "unreviewed"

    def _annotation_dict(self, cx, row) -> dict:
        cov = self._coverage(cx, row["source_ref"], row["source_version"])
        return {"revision": row["revision"], "source_version": row["source_version"],
                "annotation": json.loads(row["annotation"]),
                "producer_reported": _unj(row["producer_reported"]), "received_at": row["received_at"],
                "coverage": cov, "review_state": self._review_state(cx, row["source_ref"], row["revision"])}

    def _compute_state(self, cx, ref: str) -> str:
        src = cx.execute("SELECT * FROM sources WHERE source_ref=?", (ref,)).fetchone()
        if src["current_version"] is None:
            return "failed" if cx.execute("SELECT 1 FROM failures WHERE source_ref=?", (ref,)).fetchone() \
                else src["state"]
        if cx.execute("SELECT 1 FROM failures WHERE source_ref=?", (ref,)).fetchone():
            return "failed"
        p = cx.execute("SELECT processing_state FROM projections WHERE source_ref=? AND source_version=?",
                       (ref, src["current_version"])).fetchone()
        if p["processing_state"] == "no_text":
            return "no_text"
        if cx.execute("SELECT 1 FROM annotation_revisions WHERE source_ref=? AND source_version=? LIMIT 1",
                      (ref, src["current_version"])).fetchone():
            return "annotated"
        if cx.execute("SELECT 1 FROM annotation_revisions WHERE source_ref=? LIMIT 1", (ref,)).fetchone():
            return "stale"
        return "projected"

    def _refresh_state(self, cx, ref: str, now: str) -> str:
        state = self._compute_state(cx, ref)
        cx.execute("UPDATE sources SET state=?, updated_at=? WHERE source_ref=? AND state != ?",
                   (state, now, ref, state))
        return state

    def _pass_no(self, cx) -> int:
        return cx.execute("SELECT pass_no FROM inventory WHERE id=1").fetchone()[0]

    # ============================================================ projection
    def upsert_projection(self, projection: C.SourceProjection, chunks: list[C.Chunk]) -> C.ProjectionReceipt:
        chunks = list(chunks)
        self._validate(projection, chunks)
        now = self._ts()
        with self._write() as cx:
            return self._upsert(cx, projection, chunks, now)

    def _upsert(self, cx, p: C.SourceProjection, chunks: list[C.Chunk], now: str) -> C.ProjectionReceipt:
        ref, ver = p.source_ref, p.source_version
        state_text = "projected" if p.layers else "no_text"
        src = cx.execute("SELECT * FROM sources WHERE source_ref=?", (ref,)).fetchone()
        if src is None:
            cx.execute("INSERT INTO sources(source_ref, artifact_id, current_version, state, present, seen_pass,"
                       " first_seen_at, updated_at) VALUES (?, ?, NULL, 'discovered', 1, ?, ?, ?)",
                       (ref, p.artifact_id, self._pass_no(cx), now, now))
            src = cx.execute("SELECT * FROM sources WHERE source_ref=?", (ref,)).fetchone()
        elif src["artifact_id"] != p.artifact_id:
            # INSTANCE.id changed (e.g. the kit was instantiated after the store was created): the
            # source keeps its row and history; projections retain the artifact_id they were made under.
            cx.execute("UPDATE sources SET artifact_id=? WHERE source_ref=?", (p.artifact_id, ref))
            self._bump(cx)
        prev = src["current_version"]
        had_any = cx.execute("SELECT 1 FROM projections WHERE source_ref=? LIMIT 1", (ref,)).fetchone() is not None
        existing = cx.execute("SELECT status FROM projections WHERE source_ref=? AND source_version=?",
                              (ref, ver)).fetchone()
        changed = False
        if existing is not None and prev == ver and src["present"]:
            changed = self._refresh_meta(cx, p, state_text)
            changed = self._sync_chunks(cx, p, chunks) or changed
            kind = "unchanged"
        else:
            cx.execute("UPDATE projections SET status='stale', superseded_at=? WHERE source_ref=? "
                       "AND status='current' AND source_version != ?", (now, ref, ver))
            if existing is None:
                self._insert_projection(cx, p, chunks, state_text, now)
            else:
                cx.execute("UPDATE projections SET status='current', superseded_at=NULL WHERE source_ref=? "
                           "AND source_version=?", (ref, ver))
                self._refresh_meta(cx, p, state_text)
                self._sync_chunks(cx, p, chunks)
            cx.execute("UPDATE sources SET current_version=?, present=1, missing_since=NULL, seen_pass=? "
                       "WHERE source_ref=?", (ver, self._pass_no(cx), ref))
            kind = "created" if not had_any else "new_version"
            changed = True
        cx.execute("DELETE FROM failures WHERE source_ref=?", (ref,))
        new_state = self._refresh_state(cx, ref, now)
        if changed or new_state != src["state"]:
            self._bump(cx)
        state = "no_text" if not p.layers else kind
        return C.ProjectionReceipt(ref, p.artifact_id, ver, len(chunks), state, prev)

    @staticmethod
    def _insert_projection(cx, p: C.SourceProjection, chunks: list[C.Chunk], state_text: str, now: str) -> None:
        ref, ver = p.source_ref, p.source_version
        cx.execute(
            "INSERT INTO projections(source_ref, source_version, artifact_id, source_kind, source_tier,"
            " source_authority_level, storage_sha256, original_sha256, origin_channel, quality_flags,"
            " coverage_warnings, processing_state, projection_version, schema_version, status, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'current', ?)",
            (ref, ver, p.artifact_id, p.source_kind, p.source_tier, p.source_authority_level, p.storage_sha256,
             p.original_sha256, p.origin_channel, _j(list(p.quality_flags)), _j(list(p.coverage_warnings)),
             state_text, p.projection_version, p.schema_version, now))
        ordinal = {}
        for n, la in enumerate(p.layers):
            ordinal[la.layer_id] = n
            cx.execute(
                "INSERT INTO layers(source_ref, source_version, ordinal, layer_id, text_ref, text_sha256, chars,"
                " bytes, literal, reviewed, origin, provenance_sha256, tier, authority_level, text)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ref, ver, n, la.layer_id, la.text_ref, la.text_sha256, la.chars, la.bytes, int(la.literal),
                 int(la.reviewed), la.origin, la.provenance_sha256, la.tier, la.authority_level, la.text))
        SQLiteProjectionStore._insert_chunks(cx, p, chunks, ordinal)

    @staticmethod
    def _insert_chunks(cx, p: C.SourceProjection, chunks: list[C.Chunk], ordinal: dict[str, int]) -> None:
        for ch in chunks:
            clash = cx.execute("SELECT chunk_digest FROM chunks WHERE chunk_ref=?", (ch.chunk_ref,)).fetchone()
            if clash is not None:
                raise _err("E_INTERNAL", f"chunk_ref collision for {ch.chunk_ref}: refusing to overwrite",
                           source_ref=p.source_ref)
            cx.execute(
                "INSERT INTO chunks(chunk_ref, chunk_digest, source_ref, source_version, layer_ordinal, layer_id,"
                " text_ref, idx, start_char, end_char, prev_chunk_ref, next_chunk_ref, quality_flags, heading_path)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ch.chunk_ref, ch.chunk_digest, p.source_ref, p.source_version, ordinal[ch.layer_id], ch.layer_id,
                 ch.text_ref, ch.index, ch.start_char, ch.end_char, ch.prev_chunk_ref, ch.next_chunk_ref,
                 _j(list(ch.quality_flags)), _j(list(ch.heading_path))))

    def _sync_chunks(self, cx, p: C.SourceProjection, chunks: list[C.Chunk]) -> bool:
        """Same source_version: the chunk set must be identical (flags and heading paths, which
        are not part of the digest, are refreshed). A different set (a chunker or policy change)
        is only replaced while no annotation revision references the version. Returns True when
        any stored chunk row changed."""
        ref, ver = p.source_ref, p.source_version
        stored = {r["chunk_ref"]: r for r in cx.execute(
            "SELECT chunk_ref, quality_flags, heading_path, prev_chunk_ref, next_chunk_ref FROM chunks "
            "WHERE source_ref=? AND source_version=?", (ref, ver))}
        if set(stored) == {c.chunk_ref for c in chunks}:
            changed = False
            for c in chunks:
                row = stored[c.chunk_ref]
                new = (_j(list(c.quality_flags)), _j(list(c.heading_path)), c.prev_chunk_ref, c.next_chunk_ref)
                if tuple(row)[1:] != new:
                    cx.execute("UPDATE chunks SET quality_flags=?, heading_path=?, prev_chunk_ref=?, next_chunk_ref=? "
                               "WHERE chunk_ref=?", (*new, c.chunk_ref))
                    changed = True
            return changed
        if cx.execute("SELECT 1 FROM annotation_revisions WHERE source_ref=? AND source_version=? LIMIT 1",
                      (ref, ver)).fetchone():
            raise _err("E_INTERNAL", f"{ref}: chunking changed for an annotated, unchanged source_version; "
                       "refusing to rewrite chunks (needs an explicit chunker migration)", source_ref=ref, stage="store")
        cx.execute("DELETE FROM chunks WHERE source_ref=? AND source_version=?", (ref, ver))
        ordinal = {la.layer_id: n for n, la in enumerate(p.layers)}
        self._insert_chunks(cx, p, chunks, ordinal)
        return True

    @staticmethod
    def _refresh_meta(cx, p: C.SourceProjection, state_text: str) -> bool:
        ref, ver = p.source_ref, p.source_version
        old = cx.execute("SELECT * FROM projections WHERE source_ref=? AND source_version=?", (ref, ver)).fetchone()
        new = (p.artifact_id, p.source_kind, p.source_tier, p.source_authority_level, p.storage_sha256,
               p.original_sha256, p.origin_channel, _j(list(p.quality_flags)), _j(list(p.coverage_warnings)),
               state_text, p.projection_version, p.schema_version)
        cur = (old["artifact_id"], old["source_kind"], old["source_tier"], old["source_authority_level"],
               old["storage_sha256"], old["original_sha256"], old["origin_channel"], old["quality_flags"],
               old["coverage_warnings"], old["processing_state"], old["projection_version"], old["schema_version"])
        changed = new != cur
        if changed:
            cx.execute("UPDATE projections SET artifact_id=?, source_kind=?, source_tier=?, source_authority_level=?,"
                       " storage_sha256=?, original_sha256=?, origin_channel=?, quality_flags=?, coverage_warnings=?,"
                       " processing_state=?, projection_version=?, schema_version=? WHERE source_ref=? AND source_version=?",
                       (*new, ref, ver))
        for n, la in enumerate(p.layers):
            row = cx.execute("SELECT literal, reviewed, tier, authority_level FROM layers WHERE source_ref=? "
                             "AND source_version=? AND ordinal=?", (ref, ver, n)).fetchone()
            vals = (int(la.literal), int(la.reviewed), la.tier, la.authority_level)
            if tuple(row) != vals:
                cx.execute("UPDATE layers SET literal=?, reviewed=?, tier=?, authority_level=? WHERE source_ref=? "
                           "AND source_version=? AND ordinal=?", (*vals, ref, ver, n))
                changed = True
        return changed

    # ------------------------------------------------- failures / missing
    def record_failure(self, source_ref: str, error: Exception, *, transient: bool | None = None) -> None:
        """Record that `source_ref` could not be projected. Stable failures (parse/validation)
        mark the previous projection stale; the source is never dropped or repaired."""
        code = error.code if isinstance(error, C.EnrichmentError) else "E_INTERNAL"
        message = (error.message if isinstance(error, C.EnrichmentError) else str(error))
        message = " ".join(str(message).replace(str(self.root), ".").split())[:300]
        if transient is None:
            transient = projector.is_transient(code)
        flags = ["parse_warning"] if "parse_warning" in message else []
        message = message.replace(" (parse_warning)", "")
        now_dt = self._now()
        now = self._fmt(now_dt)
        retry = self._fmt(now_dt + self.retry_spacing) if transient else None
        with self._write() as cx:
            src = cx.execute("SELECT * FROM sources WHERE source_ref=?", (source_ref,)).fetchone()
            if src is None:
                cx.execute("INSERT INTO sources(source_ref, artifact_id, current_version, state, present, seen_pass,"
                           " first_seen_at, updated_at) VALUES (?, ?, NULL, 'failed', 1, ?, ?, ?)",
                           (source_ref, C.artifact_id(self._instance_id(), source_ref), self._pass_no(cx), now, now))
            elif not transient and src["current_version"] is not None:
                cx.execute("UPDATE projections SET status='stale', superseded_at=? WHERE source_ref=? "
                           "AND status='current'", (now, source_ref))
                cx.execute("UPDATE sources SET current_version=NULL WHERE source_ref=?", (source_ref,))
                self._bump(cx)
            cx.execute("UPDATE sources SET present=1, missing_since=NULL, state='failed', updated_at=?, seen_pass=? "
                       "WHERE source_ref=?", (now, self._pass_no(cx), source_ref))
            old = cx.execute("SELECT attempts, first_failed_at FROM failures WHERE source_ref=?",
                             (source_ref,)).fetchone()
            cx.execute("INSERT INTO failures(source_ref, attempts, last_error_code, last_error_message, transient,"
                       " flags, first_failed_at, last_failed_at, next_retry_after) VALUES (?,?,?,?,?,?,?,?,?) "
                       "ON CONFLICT(source_ref) DO UPDATE SET attempts=excluded.attempts,"
                       " last_error_code=excluded.last_error_code, last_error_message=excluded.last_error_message,"
                       " transient=excluded.transient, flags=excluded.flags, last_failed_at=excluded.last_failed_at,"
                       " next_retry_after=excluded.next_retry_after",
                       (source_ref, (old["attempts"] if old else 0) + 1, code, message, int(transient), _j(flags),
                        old["first_failed_at"] if old else now, now, retry))
            self._bump(cx)

    def mark_missing(self, source_ref: str) -> bool:
        """The source no longer exists on disk: its projection becomes stale, its chunks
        stop being served, revisions stay for audit. Returns True when something changed."""
        now = self._ts()
        with self._write() as cx:
            return self._mark_missing(cx, source_ref, now)

    def _mark_missing(self, cx, source_ref: str, now: str) -> bool:
        src = cx.execute("SELECT * FROM sources WHERE source_ref=?", (source_ref,)).fetchone()
        if src is None or not src["present"]:
            return False
        cx.execute("UPDATE projections SET status='stale', superseded_at=? WHERE source_ref=? AND status='current'",
                   (now, source_ref))
        cx.execute("UPDATE sources SET present=0, missing_since=?, current_version=NULL, updated_at=? "
                   "WHERE source_ref=?", (now, now, source_ref))
        cx.execute("DELETE FROM failures WHERE source_ref=?", (source_ref,))
        self._bump(cx)
        return True

    # ============================================================ discovery
    def _failure(self, cx, ref: str):
        return cx.execute("SELECT * FROM failures WHERE source_ref=?", (ref,)).fetchone()

    def _retry_due(self, row, now: datetime) -> bool:
        if not row["transient"] or row["attempts"] >= TRANSIENT_MAX_ATTEMPTS:
            return False
        return row["next_retry_after"] is None or row["next_retry_after"] <= self._fmt(now)

    def reprocess(self, source_ref: str):
        """Explicit reprocess of one source (ignores failure gating)."""
        outcome, res = projector.project_source(self, self.reader, self.chunker, self.policy, source_ref)
        if outcome == "failed":
            raise res
        if outcome == "gone":
            raise res
        return res

    def refresh(self, budget: int | None = None, retry_failed: bool = False) -> dict:
        """Bounded deterministic discovery/projection pass step; resumable across restarts.
        Returns the `discovery` block of the pending page."""
        budget = self.discovery_budget if budget is None else int(budget)
        with self._refresh_lock:
            return self._refresh(budget, retry_failed)

    def _inventory(self) -> sqlite3.Row:
        with self._write() as cx:
            return cx.execute("SELECT * FROM inventory WHERE id=1").fetchone()

    def _refresh(self, budget: int, retry_failed: bool) -> dict:
        inv = self._inventory()
        pass_no, cursor = inv["pass_no"], inv["cursor"]
        counters = {"scanned": inv["scanned"], "projected": inv["projected"], "changed": inv["changed"],
                    "failed": inv["failed"], "no_text": inv["no_text"]}
        generation = inv["inventory_generation"]
        processed: set[str] = set()
        remaining = max(0, budget)
        error = None
        complete = False
        call = {"scanned": 0}

        def tally(outcome: str, res) -> None:
            if outcome == "projected":
                counters["projected"] += 1
                if res.state in ("created", "new_version"):
                    counters["changed"] += 1
            elif outcome == "no_text":
                counters["no_text"] += 1
                if res.state in ("created", "new_version"):
                    counters["changed"] += 1
            elif outcome == "failed":
                counters["failed"] += 1

        try:
            if retry_failed:
                with self._read() as cx:
                    todo = [r["source_ref"] for r in cx.execute(
                        "SELECT source_ref FROM failures ORDER BY source_ref LIMIT ?", (remaining,))]
                for ref in todo:
                    outcome, res = projector.project_source(self, self.reader, self.chunker, self.policy, ref)
                    processed.add(ref)
                    remaining -= 1
                    call["scanned"] += 1
                    tally(outcome, res)
            while remaining > 0:
                page = self.reader.list_sources(cursor, min(remaining, LIST_PAGE))
                generation = page.inventory_generation
                seen = []
                for ref in page.refs:
                    seen.append(ref)
                    remaining -= 1
                    counters["scanned"] += 1
                    call["scanned"] += 1
                    if ref in processed:
                        continue
                    with self._read() as cx:
                        frow = self._failure(cx, ref)
                    if frow is not None and not self._retry_due(frow, self._now()):
                        counters["failed"] += 1
                        continue
                    outcome, res = projector.project_source(self, self.reader, self.chunker, self.policy, ref)
                    tally(outcome, res)
                cursor_next = page.next_cursor
                ok = self._advance(pass_no, inv_cursor=cursor, new_cursor=cursor_next, counters=counters,
                                   generation=generation, seen=seen)
                if not ok:                       # another worker moved the cursor: stop quietly
                    break
                cursor = cursor_next
                if cursor is None:
                    complete = True
                    break
        except C.EnrichmentError as exc:
            if exc.code in ("E_SCHEMA_UNSUPPORTED",):
                raise
            error = exc.code
        if complete:
            self._finish_pass(pass_no, counters, generation)
        out = {"pass": pass_no, "inventory_generation": generation, "scanned": counters["scanned"],
               "projected": counters["projected"], "failed": counters["failed"], "no_text": counters["no_text"],
               "changed": counters["changed"], "pass_complete": complete, "scanned_this_call": call["scanned"]}
        if error:
            out["error"] = error
        return out

    def _advance(self, pass_no: int, *, inv_cursor, new_cursor, counters: dict, generation, seen: list[str]) -> bool:
        with self._write() as cx:
            n = cx.execute(
                "UPDATE inventory SET cursor=?, scanned=?, projected=?, changed=?, failed=?, no_text=?,"
                " inventory_generation=? WHERE id=1 AND pass_no=? AND IFNULL(cursor,'') = IFNULL(?,'')",
                (new_cursor, counters["scanned"], counters["projected"], counters["changed"], counters["failed"],
                 counters["no_text"], generation, pass_no, inv_cursor)).rowcount
            if not n:
                return False
            cx.executemany("UPDATE sources SET seen_pass=? WHERE source_ref=?", [(pass_no, r) for r in seen])
            return True

    def _finish_pass(self, pass_no: int, counters: dict, generation) -> None:
        now = self._ts()
        with self._write() as cx:
            cur = self._pass_no(cx)
            if cur != pass_no:
                return
            gone = [r[0] for r in cx.execute("SELECT source_ref FROM sources WHERE present=1 AND seen_pass < ?",
                                             (pass_no,))]
            for ref in gone:
                self._mark_missing(cx, ref, now)
            summary = dict(counters, marked_missing=len(gone), inventory_generation=generation)
            cx.execute("UPDATE inventory SET pass_no=?, cursor=NULL, started_at=?, scanned=0, projected=0, changed=0,"
                       " failed=0, no_text=0, last_pass_no=?, last_pass_completed_at=?, last_pass_summary=? WHERE id=1",
                       (pass_no + 1, now, pass_no, now, _j(summary)))

    # ================================================================ pending
    def _queue(self, cx) -> list[dict]:
        """Sources that still need annotation, ordered by (last revision time or '', source_ref)."""
        rows = cx.execute(
            "SELECT s.source_ref, s.current_version AS ver, p.source_kind FROM sources s JOIN projections p "
            "ON p.source_ref=s.source_ref AND p.source_version=s.current_version "
            "WHERE s.present=1 AND s.state IN ('projected','stale','annotated') AND p.processing_state='projected' "
            "AND p.source_kind IN ('capture-text','capture-media','ingested-file')").fetchall()
        totals = {r[0]: r[1] for r in cx.execute(
            "SELECT c.source_ref, COUNT(*) FROM chunks c JOIN sources s ON s.source_ref=c.source_ref "
            "AND s.current_version=c.source_version GROUP BY c.source_ref")}
        cov = {r[0]: r[1] for r in cx.execute(
            "SELECT ac.source_ref, COUNT(DISTINCT ac.chunk_ref) FROM annotation_chunks ac "
            "JOIN sources s ON s.source_ref=ac.source_ref "
            "JOIN annotation_revisions ar ON ar.source_ref=ac.source_ref AND ar.revision=ac.revision "
            "AND ar.source_version=s.current_version "
            "JOIN chunks c ON c.chunk_ref=ac.chunk_ref AND c.source_version=s.current_version "
            "GROUP BY ac.source_ref")}
        last = {r[0]: r[1] for r in cx.execute(
            "SELECT ar.source_ref, MAX(ar.received_at) FROM annotation_revisions ar JOIN sources s "
            "ON s.source_ref=ar.source_ref AND s.current_version=ar.source_version GROUP BY ar.source_ref")}
        out = []
        for r in rows:
            ref = r["source_ref"]
            total = totals.get(ref, 0)
            if total == 0:
                continue
            has_rev = ref in last
            covered = total if (total == 1 and has_rev) else cov.get(ref, 0)
            if covered >= total:
                continue
            out.append({"source_ref": ref, "source_version": r["ver"], "t": last.get(ref, ""),
                        "total": total, "covered": covered})
        out.sort(key=lambda d: (d["t"], d["source_ref"]))
        return out

    def pending(self, cursor: str | None, limit: int, retry_failed: bool = False) -> C.PendingPage:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_PENDING_LIMIT:
            raise _err("E_BAD_ARGUMENTS", f"limit must be an integer in 1..{MAX_PENDING_LIMIT}")
        if not isinstance(retry_failed, bool):
            raise _err("E_BAD_ARGUMENTS", "retry_failed must be a boolean")
        cur = None
        if cursor is not None:
            cur = C.decode_cursor(cursor, "pending")
            if not (isinstance(cur.get("r"), str) and SOURCE_REF_RE.match(cur["r"]) and isinstance(cur.get("v"), str)
                    and isinstance(cur.get("o"), int) and not isinstance(cur.get("o"), bool) and cur["o"] >= 0
                    and isinstance(cur.get("t"), str)):
                raise _err("E_BAD_CURSOR", "cursor is malformed or for a different call")
        discovery = self.refresh(retry_failed=retry_failed)
        with self._read() as cx:
            if cur is not None:
                row = cx.execute("SELECT current_version, present FROM sources WHERE source_ref=?",
                                 (cur["r"],)).fetchone()
                if row is None or not row["present"] or row["current_version"] != cur["v"]:
                    raise _err("E_STALE_CURSOR", "the source changed since this cursor was issued; restart without a cursor",
                               source_ref=cur["r"])
            queue = self._queue(cx)
            total = len(queue)
            if cur is not None:
                head = [q for q in queue if q["source_ref"] == cur["r"]]
                tail = [q for q in queue if q["source_ref"] != cur["r"] and (q["t"], q["source_ref"]) >= (cur["t"], cur["r"])]
                ordered = head + tail
            else:
                ordered = queue
            items, next_cursor = self._assemble(cx, ordered, limit, cur)
        return C.PendingPage(tuple(items), total, len(items), next_cursor is not None, next_cursor, discovery)

    def _assemble(self, cx, ordered: list[dict], limit: int, cur: dict | None):
        budget = self.max_input_chars
        items: list[dict] = []
        next_cursor = None
        for idx, q in enumerate(ordered):
            if len(items) >= limit:
                next_cursor = C.encode_cursor("pending", r=q["source_ref"], v=q["source_version"], o=0, t=q["t"])
                break
            ref, ver = q["source_ref"], q["source_version"]
            texts = self._layer_texts(cx, ref, ver)
            chunks = self._chunks(cx, ref, ver, texts)
            covered, total = self._covered(cx, ref, ver)
            first_open = next((c.index for c in chunks if c.chunk_ref not in covered), 0)
            start = cur["o"] if (cur is not None and ref == cur["r"] and cur["o"] < len(chunks)) else first_open
            page, used = [], 0
            for c in chunks[start:]:
                if len(page) >= C.PAGE_MAX_CHUNKS:
                    break
                size = len(c.text)
                if (items or page) and used + size > budget:
                    break
                page.append(c)
                used += size
            if not page:
                next_cursor = C.encode_cursor("pending", r=ref, v=ver, o=start, t=q["t"])
                break
            budget -= used
            end = start + len(page)
            proj = self._projection(cx, ref, ver)
            rev, overall = self._revision_info(cx, ref, ver)
            items.append({
                "source_ref": ref, "source_version": ver, "artifact_id": proj.artifact_id,
                "source_kind": proj.source_kind, "source_tier": proj.source_tier,
                "origin_channel": proj.origin_channel, "layers": [la.to_public() for la in proj.layers],
                "quality_flags": list(proj.quality_flags), "coverage_warnings": list(proj.coverage_warnings),
                "current_annotation_revision": rev["revision"] if rev else 0,
                "expected_annotation_revision": overall,
                "coverage": {"annotated_chunks": len(covered), "total_chunks": total},
                "existing_annotation": json.loads(rev["annotation"]) if rev else None,
                "page": {"chunks": [{
                    "chunk_ref": c.chunk_ref, "text_ref": c.text_ref, "layer_id": c.layer_id, "index": c.index,
                    "start_char": c.start_char, "end_char": c.end_char, "chars": c.chars, "text": c.text,
                    "prev_chunk_ref": c.prev_chunk_ref, "next_chunk_ref": c.next_chunk_ref,
                    "annotated": c.chunk_ref in covered} for c in page],
                    "offset": start, "returned": len(page), "total": len(chunks), "truncated": end < len(chunks)}})
            if end < len(chunks):
                next_cursor = C.encode_cursor("pending", r=ref, v=ver, o=end, t=q["t"])
                break
        return items, next_cursor

    # ================================================================== reads
    def _source_row(self, cx, ref: str):
        if not isinstance(ref, str) or not SOURCE_REF_RE.match(ref):
            raise _err("E_BAD_REF", "source_ref is not a valid public ref")
        return cx.execute("SELECT * FROM sources WHERE source_ref=?", (ref,)).fetchone()

    def _fresh(self, ref: str, stored_version: str | None) -> str:
        """'current' | 'stale' | 'missing' | 'unverifiable' (one fresh load through the reader)."""
        try:
            fresh = self.reader.load(ref)
        except C.EnrichmentError as exc:
            return "missing" if exc.code == "E_NOT_FOUND" else "unverifiable"
        except Exception:  # noqa: BLE001
            return "unverifiable"
        return "current" if fresh.source_version == stored_version else "stale"

    def get(self, source_ref: str, source_version: str | None = None) -> C.ArtifactView:
        try:
            with self._read() as cx:
                src = self._source_row(cx, source_ref)
                if src is None or not src["present"]:
                    raise _err("E_NOT_FOUND", f"{source_ref}: no projection exists" if src is None
                               else f"{source_ref}: the source no longer exists", source_ref=source_ref)
                ver = src["current_version"]
                if ver is None:
                    raise _err("E_NOT_FOUND", f"{source_ref}: no current projection (state {src['state']})",
                               source_ref=source_ref)
                if source_version is not None and source_version != ver:
                    known = cx.execute("SELECT 1 FROM projections WHERE source_ref=? AND source_version=?",
                                       (source_ref, source_version)).fetchone()
                    if known:
                        raise _err("E_STALE_SOURCE", f"{source_ref}: source_version is not current",
                                   source_ref=source_ref)
                    raise _err("E_NOT_FOUND", f"{source_ref}: unknown source_version", source_ref=source_ref)
                proj = self._projection(cx, source_ref, ver, state=src["state"])
                rev, overall = self._revision_info(cx, source_ref, ver)
                chunk_count = cx.execute("SELECT COUNT(*) FROM chunks WHERE source_ref=? AND source_version=?",
                                         (source_ref, ver)).fetchone()[0]
                annotation = self._annotation_dict(cx, rev) if rev else None
                coverage = self._coverage(cx, source_ref, ver)
                review = {"state": self._review_state(cx, source_ref, rev["revision"] if rev else None),
                          "revision": rev["revision"] if rev else None}
                old_only = rev is None and overall > 0
        except _StoreMissing:
            raise _err("E_NOT_FOUND", f"{source_ref}: no projection exists", source_ref=source_ref) from None
        fresh = self._fresh(source_ref, ver)
        if fresh == "missing":
            self.mark_missing(source_ref)
            raise _err("E_NOT_FOUND", f"{source_ref}: the source no longer exists", source_ref=source_ref)
        public = proj.to_public()
        public["current_annotation_revision"] = rev["revision"] if rev else None
        return C.ArtifactView(projection=public, annotation=annotation, coverage=coverage, review=review,
                              chunk_count=chunk_count, stale=bool(old_only or fresh != "current"))

    def get_chunks(self, source_ref: str, source_version: str | None = None) -> list[C.Chunk]:
        try:
            with self._read() as cx:
                src = self._source_row(cx, source_ref)
                if src is None or not src["present"] or src["current_version"] is None:
                    raise _err("E_NOT_FOUND", f"{source_ref}: no current projection", source_ref=source_ref)
                ver = src["current_version"]
                if source_version is not None and source_version != ver:
                    raise _err("E_STALE_SOURCE", f"{source_ref}: source_version is not current", source_ref=source_ref)
                chunks = self._chunks(cx, source_ref, ver, self._layer_texts(cx, source_ref, ver))
        except _StoreMissing:
            raise _err("E_NOT_FOUND", f"{source_ref}: no projection exists", source_ref=source_ref) from None
        fresh = self._fresh(source_ref, ver)
        if fresh == "missing":
            self.mark_missing(source_ref)
            raise _err("E_NOT_FOUND", f"{source_ref}: the source no longer exists", source_ref=source_ref)
        if fresh != "current":
            raise _err("E_STALE_SOURCE", f"{source_ref}: the source changed since it was projected",
                       source_ref=source_ref)
        return chunks

    def get_chunk(self, chunk_ref: str) -> C.Chunk:
        if not isinstance(chunk_ref, str) or not C.CHUNK_REF_RE.match(chunk_ref):
            raise _err("E_BAD_REF", "chunk_ref must look like chk:<32 hex>")
        try:
            with self._read() as cx:
                row = cx.execute("SELECT * FROM chunks WHERE chunk_ref=?", (chunk_ref,)).fetchone()
                if row is None:
                    raise _err("E_NOT_FOUND", f"no such chunk {chunk_ref}")
                ref, ver = row["source_ref"], row["source_version"]
                if C.chunk_digest(ref, ver, row["layer_id"], row["start_char"], row["end_char"]) != row["chunk_digest"]:
                    raise _err("E_INTERNAL", f"{chunk_ref}: stored digest does not match its coordinates")
                src = cx.execute("SELECT * FROM sources WHERE source_ref=?", (ref,)).fetchone()
                current = src["current_version"] if src is not None and src["present"] else None
                texts = self._layer_texts(cx, ref, ver)
                chunk = next(c for c in self._chunks(cx, ref, ver, texts) if c.chunk_ref == chunk_ref)
        except _StoreMissing:
            raise _err("E_NOT_FOUND", f"no such chunk {chunk_ref}") from None
        fresh = self._fresh(ref, current if current is not None else ver)
        if fresh == "missing":
            self.mark_missing(ref)
            raise _err("E_NOT_FOUND", f"{ref}: the source no longer exists", source_ref=ref)
        if current is None:
            raise _err("E_NOT_FOUND" if src is not None and not src["present"] else "E_STALE_SOURCE",
                       f"{ref}: the source has no current projection", source_ref=ref)
        if ver != current or fresh != "current":
            raise _err("E_STALE_SOURCE", f"{chunk_ref}: the source changed; this chunk is superseded", source_ref=ref)
        return chunk

    def entity_exists(self, entity_id: str) -> bool:
        if not isinstance(entity_id, str) or not C.ENTITY_ID_RE.match(entity_id):
            return False
        try:
            with self._read() as cx:
                return cx.execute("SELECT 1 FROM entities e JOIN sources s ON s.source_ref=e.source_ref "
                                  "WHERE e.entity_id=? AND s.present=1 LIMIT 1", (entity_id,)).fetchone() is not None
        except _StoreMissing:
            return False

    def list_current(self, after: str | None, limit: int) -> list[dict]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise _err("E_BAD_ARGUMENTS", "limit must be an integer in 1..1000")
        try:
            with self._read() as cx:
                rows = cx.execute("SELECT * FROM sources WHERE present=1 AND current_version IS NOT NULL "
                                  "AND (? IS NULL OR source_ref > ?) ORDER BY source_ref LIMIT ?",
                                  (after, after, limit)).fetchall()
                out = []
                for s in rows:
                    ref, ver = s["source_ref"], s["current_version"]
                    proj = self._projection(cx, ref, ver, state=s["state"])
                    chunks = self._chunks(cx, ref, ver, self._layer_texts(cx, ref, ver))
                    rev, _ = self._revision_info(cx, ref, ver)
                    ann = self._annotation_dict(cx, rev) if rev else None
                    out.append({"projection": proj, "chunks": chunks,
                                "annotation": ann["annotation"] if ann else None,
                                "annotation_revision": rev["revision"] if rev else None,
                                "review_state": self._review_state(cx, ref, rev["revision"] if rev else None),
                                "processing_state": s["state"],
                                "derived": _unj(rev["derived"]) if rev else None,
                                "producer_reported": ann["producer_reported"] if ann else None,
                                "received_at": rev["received_at"] if rev else None,
                                "annotation_policy": annotation_policy(proj.source_kind)})
                return out
        except _StoreMissing:
            return []

    def change_marker(self) -> int:
        try:
            with self._read() as cx:
                return int(cx.execute("SELECT value FROM meta WHERE key='change_marker'").fetchone()[0])
        except _StoreMissing:
            return 0

    # ============================================================ annotation
    _REQUEST_KEYS = {"source_ref", "source_version", "expected_annotation_revision", "idempotency_key",
                     "annotation", "derived"}

    def _check_request(self, request: dict) -> None:
        if not isinstance(request, dict):
            raise _err("E_BAD_ARGUMENTS", "request must be an object")
        extra = sorted(set(request) - self._REQUEST_KEYS)
        if extra:
            raise _err("E_BAD_ARGUMENTS", f"unknown request fields: {extra}")
        ref = request.get("source_ref")
        if not isinstance(ref, str) or not SOURCE_REF_RE.match(ref):
            raise _err("E_BAD_REF", "source_ref is not a valid public ref")
        if not isinstance(request.get("source_version"), str) or not C.SHA256_RE.match(request["source_version"]):
            raise _err("E_BAD_ARGUMENTS", "source_version must be 64 hex characters")
        exp = request.get("expected_annotation_revision")
        if not isinstance(exp, int) or isinstance(exp, bool) or exp < 0:
            raise _err("E_BAD_ARGUMENTS", "expected_annotation_revision must be an integer >= 0")
        if not isinstance(request.get("idempotency_key"), str) or not C.IDEMPOTENCY_KEY_RE.match(request["idempotency_key"]):
            raise _err("E_BAD_ARGUMENTS", "idempotency_key is malformed")
        ann = request.get("annotation")
        if not isinstance(ann, dict) or not ann:
            raise _err("E_BAD_ARGUMENTS", "annotation must be a non-empty object")
        derived = request.get("derived")
        if derived is not None:
            if not isinstance(derived, dict):
                raise _err("E_BAD_ARGUMENTS", "derived must be an object")
            for i, ent in enumerate(derived.get("entities") or []):
                if not isinstance(ent, dict) or not isinstance(ent.get("entity_id"), str) \
                        or not C.ENTITY_ID_RE.match(ent["entity_id"]):
                    raise _err("E_VALIDATION", f"derived.entities[{i}].entity_id is invalid")
        try:
            size = len(C.canonical_json(request))
        except (TypeError, ValueError):
            raise _err("E_BAD_ARGUMENTS", "request is not JSON-serialisable") from None
        if size > MAX_ANNOTATION_BYTES:
            raise _err("E_TOO_LARGE", f"request exceeds {MAX_ANNOTATION_BYTES} bytes")

    def append_annotation(self, request: dict) -> C.AnnotationReceipt:
        self._check_request(request)
        digest = C.request_digest(request)
        ref, key = request["source_ref"], request["idempotency_key"]
        now = self._ts()
        raise_after: C.EnrichmentError | None = None
        with self._write() as cx:
            prior = cx.execute("SELECT request_digest, receipt FROM annotation_revisions "
                               "WHERE source_ref=? AND idempotency_key=?", (ref, key)).fetchone()
            if prior is not None:
                if prior["request_digest"] != digest:
                    raise _err("E_IDEMPOTENCY_CONFLICT", "idempotency_key was already used with a different payload",
                               source_ref=ref)
                rec = json.loads(prior["receipt"])
                return C.AnnotationReceipt(rec["source_ref"], rec["source_version"], rec["annotation_revision"],
                                           True, rec["coverage"], rec["processing_state"], rec.get("index_dirty", True))
            src = cx.execute("SELECT * FROM sources WHERE source_ref=?", (ref,)).fetchone()
            if src is None or not src["present"]:
                raise _err("E_NOT_FOUND", f"{ref}: no projection exists" if src is None
                           else f"{ref}: the source no longer exists", source_ref=ref)
            cur_ver = src["current_version"]
            if cur_ver is None:
                raise _err("E_STALE_SOURCE", f"{ref}: the source has no current projection (state {src['state']})",
                           source_ref=ref)
            try:
                fresh = self.reader.load(ref)
            except C.EnrichmentError as exc:
                if exc.code == "E_NOT_FOUND":
                    self._mark_missing(cx, ref, now)
                    raise_after = _err("E_NOT_FOUND", f"{ref}: the source no longer exists", source_ref=ref)
                else:
                    raise _err("E_STALE_SOURCE", f"{ref}: the source cannot be verified against disk",
                               source_ref=ref) from None
            if raise_after is None:
                if request["source_version"] != cur_ver or fresh.source_version != cur_ver:
                    raise _err("E_STALE_SOURCE", f"{ref}: source_version is not current; re-read the source",
                               source_ref=ref)
                current = cx.execute("SELECT COALESCE(MAX(revision), 0) FROM annotation_revisions WHERE source_ref=?",
                                     (ref,)).fetchone()[0]
                if request["expected_annotation_revision"] != current:
                    raise _err("E_CONFLICT", f"{ref}: the annotation revision moved", source_ref=ref,
                               current_revision=current)
                revision = current + 1
                ann = request["annotation"]
                producer = ann.get("producer_reported") if isinstance(ann.get("producer_reported"), dict) else None
                derived = request.get("derived")
                named = sorted({c.get("chunk_ref") for c in (ann.get("chunk_annotations") or [])
                                if isinstance(c, dict) and isinstance(c.get("chunk_ref"), str)})
                prior_cov, total = self._covered(cx, ref, cur_ver)
                in_version = {r[0] for r in cx.execute(
                    "SELECT chunk_ref FROM chunks WHERE source_ref=? AND source_version=?", (ref, cur_ver))}
                covered = in_version if total == 1 else (prior_cov | (set(named) & in_version))
                coverage = {"annotated_chunks": len(covered), "total_chunks": total}
                predicted = "failed" if cx.execute("SELECT 1 FROM failures WHERE source_ref=?", (ref,)).fetchone() \
                    else ("no_text" if cx.execute("SELECT processing_state FROM projections WHERE source_ref=? "
                                                  "AND source_version=?", (ref, cur_ver)).fetchone()[0] == "no_text"
                          else "annotated")
                receipt = {"source_ref": ref, "source_version": cur_ver, "annotation_revision": revision,
                           "coverage": coverage, "processing_state": predicted, "index_dirty": True}
                cx.execute(
                    "INSERT INTO annotation_revisions(source_ref, revision, source_version, annotation, derived,"
                    " producer_reported, request_digest, idempotency_key, receipt, received_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (ref, revision, cur_ver, _j(ann), None if derived is None else _j(derived),
                     None if producer is None else _j(producer), digest, key, _j(receipt), now))
                cx.executemany("INSERT OR IGNORE INTO annotation_chunks(source_ref, revision, chunk_ref) VALUES (?,?,?)",
                               [(ref, revision, c) for c in named])
                for ent in (derived or {}).get("entities") or []:
                    cx.execute("INSERT OR IGNORE INTO entities(entity_id, source_ref, first_revision, name, normal, kind)"
                               " VALUES (?,?,?,?,?,?)",
                               (ent["entity_id"], ref, revision, str(ent.get("name", ""))[:200],
                                str(ent.get("normal", ""))[:200], ent.get("kind") if isinstance(ent.get("kind"), str) else None))
                state = self._refresh_state(cx, ref, now)
                self._bump(cx)
                return C.AnnotationReceipt(ref, cur_ver, revision, False, coverage, state, True)
        raise raise_after

    # ================================================================== review
    def record_review(self, request: dict, human_context: dict) -> C.ReviewReceipt:
        if os.environ.get("BRAIN_REMOTE_SESSION"):
            raise _err("E_REVIEW_FORBIDDEN", "review is a local human action and is refused in a remote session")
        actor = human_context.get("actor") if isinstance(human_context, dict) else None
        if (not isinstance(human_context, dict) or human_context.get("remote")
                or not isinstance(actor, str) or not actor.strip() or len(actor) > 80
                or any(ord(ch) < 32 for ch in actor)):
            raise _err("E_REVIEW_FORBIDDEN", "review needs a resolved local human actor (not a remote context)")
        if not isinstance(request, dict) or set(request) - {"source_ref", "annotation_revision", "decision", "note"}:
            raise _err("E_BAD_ARGUMENTS", "review request has unknown or missing fields")
        ref, revision, decision = request.get("source_ref"), request.get("annotation_revision"), request.get("decision")
        note = request.get("note")
        if not isinstance(ref, str) or not SOURCE_REF_RE.match(ref):
            raise _err("E_BAD_REF", "source_ref is not a valid public ref")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise _err("E_BAD_ARGUMENTS", "annotation_revision must be an integer >= 1")
        if decision not in ("confirmed", "rejected"):
            raise _err("E_BAD_ARGUMENTS", "decision must be 'confirmed' or 'rejected'")
        if note is not None and (not isinstance(note, str) or len(note) > 500):
            raise _err("E_BAD_ARGUMENTS", "note must be a string of at most 500 characters")
        now = self._ts()
        with self._write() as cx:
            if cx.execute("SELECT 1 FROM annotation_revisions WHERE source_ref=? AND revision=?",
                          (ref, revision)).fetchone() is None:
                raise _err("E_NOT_FOUND", f"{ref}: annotation revision {revision} does not exist", source_ref=ref)
            cx.execute("INSERT INTO reviews(source_ref, annotation_revision, decision, reviewed_by, reviewed_at, note,"
                       " canonical_promotion) VALUES (?,?,?,?,?,?,0)", (ref, revision, decision, actor.strip(), now, note))
            self._bump(cx)
        return C.ReviewReceipt(ref, revision, decision, actor.strip(), now, False)

    # ============================================================ operations
    def status(self) -> dict:
        """Counts and state, read-only. Never raises for a missing or newer-schema store."""
        try:
            with self._read() as cx:
                one = lambda sql: cx.execute(sql).fetchone()[0]  # noqa: E731
                by_state = {r[0]: r[1] for r in cx.execute("SELECT state, COUNT(*) FROM sources WHERE present=1 GROUP BY state")}
                inv = cx.execute("SELECT * FROM inventory WHERE id=1").fetchone()
                return {"exists": True, "state": "ready", "schema_version": C.STORE_SCHEMA_VERSION,
                        "counts": {"sources": one("SELECT COUNT(*) FROM sources"),
                                   "present_sources": one("SELECT COUNT(*) FROM sources WHERE present=1"),
                                   "missing_sources": one("SELECT COUNT(*) FROM sources WHERE present=0"),
                                   "projections": one("SELECT COUNT(*) FROM projections"),
                                   "current_projections": one("SELECT COUNT(*) FROM projections WHERE status='current'"),
                                   "chunks": one("SELECT COUNT(*) FROM chunks"),
                                   "annotation_revisions": one("SELECT COUNT(*) FROM annotation_revisions"),
                                   "reviews": one("SELECT COUNT(*) FROM reviews"),
                                   "failures": one("SELECT COUNT(*) FROM failures"),
                                   "entities": one("SELECT COUNT(*) FROM entities")},
                        "by_state": by_state, "pending_sources": len(self._queue(cx)),
                        "inventory": {"pass": inv["pass_no"], "in_progress": inv["cursor"] is not None,
                                      "scanned": inv["scanned"], "projected": inv["projected"],
                                      "failed": inv["failed"], "no_text": inv["no_text"],
                                      "inventory_generation": inv["inventory_generation"],
                                      "last_pass": inv["last_pass_no"],
                                      "last_pass_completed_at": inv["last_pass_completed_at"]},
                        "change_marker": int(cx.execute("SELECT value FROM meta WHERE key='change_marker'").fetchone()[0])}
        except _StoreMissing:
            return {"exists": False, "state": "missing", "schema_version": None, "counts": {}, "by_state": {}}
        except C.EnrichmentError as exc:
            if exc.code == "E_SCHEMA_UNSUPPORTED":
                return {"exists": True, "state": "unsupported", "schema_version": exc.extra.get("found"),
                        "message": exc.message, "counts": {}, "by_state": {}}
            raise

    def verify(self, max_problems: int = 100) -> dict:
        """Read-only integrity check: SQLite integrity, hashes, offsets, digests, partitions, orphans."""
        problems: list[dict] = []
        total = 0

        def add(code: str, ref: str | None, detail: str) -> None:
            nonlocal total
            total += 1
            if len(problems) < max_problems:
                problems.append({"code": code, "ref": ref, "detail": detail})

        try:
            with self._read() as cx:
                found = [r[0] for r in cx.execute("PRAGMA integrity_check").fetchall()]
                if found != ["ok"]:
                    add("sqlite_integrity", None, "; ".join(str(x) for x in found)[:300])
                for r in cx.execute("PRAGMA foreign_key_check"):
                    add("foreign_key", r[0], f"row {r[1]} violates a foreign key to {r[2]}")
                checked = {"projections": 0, "chunks": 0, "revisions": 0}
                for p in cx.execute("SELECT * FROM projections ORDER BY source_ref, source_version"):
                    checked["projections"] += 1
                    ref, ver = p["source_ref"], p["source_version"]
                    layers = self._layers(cx, ref, ver)
                    for la in layers:
                        if C.sha256_text(la["text"]) != la["text_sha256"]:
                            add("layer_hash", ref, f"layer {la['layer_id']} text does not match text_sha256")
                        if len(la["text"]) != la["chars"] or len(la["text"].encode("utf-8")) != la["bytes"]:
                            add("layer_size", ref, f"layer {la['layer_id']} chars/bytes mismatch")
                    expect = C.source_version(ref, [{"layer_id": la["layer_id"], "text_ref": la["text_ref"],
                                                     "text_sha256": la["text_sha256"], "origin": la["origin"],
                                                     "provenance_sha256": la["provenance_sha256"]} for la in layers])
                    if expect != ver:
                        add("source_version", ref, f"stored version {ver[:12]} does not match its layers")
                    texts = {la["ordinal"]: la["text"] for la in layers}
                    chunk_rows = cx.execute("SELECT * FROM chunks WHERE source_ref=? AND source_version=? ORDER BY idx",
                                            (ref, ver)).fetchall()
                    refs = {c["chunk_ref"] for c in chunk_rows}
                    pos: dict[int, int] = {}
                    for n, c in enumerate(chunk_rows):
                        checked["chunks"] += 1
                        if c["idx"] != n:
                            add("chunk_index", ref, f"chunk indexes are not 0..n-1 at {n}")
                        if C.chunk_digest(ref, ver, c["layer_id"], c["start_char"], c["end_char"]) != c["chunk_digest"]:
                            add("chunk_digest", c["chunk_ref"], "digest does not match coordinates")
                        t = texts.get(c["layer_ordinal"])
                        if t is None or c["end_char"] > len(t):
                            add("chunk_offsets", c["chunk_ref"], "offsets fall outside the layer text")
                            continue
                        expected_start = pos.get(c["layer_ordinal"], 0)
                        if c["start_char"] != expected_start:
                            add("chunk_partition", c["chunk_ref"], f"gap or overlap at {expected_start}")
                        pos[c["layer_ordinal"]] = c["end_char"]
                        for nb in (c["prev_chunk_ref"], c["next_chunk_ref"]):
                            if nb is not None and nb not in refs:
                                add("chunk_link", c["chunk_ref"], "prev/next link points outside the projection")
                    for ordinal, t in texts.items():
                        if t and pos.get(ordinal, 0) != len(t):
                            add("chunk_partition", ref, f"layer {ordinal} is not fully covered by chunks")
                for r in cx.execute("SELECT s.source_ref FROM sources s LEFT JOIN projections p ON p.source_ref=s.source_ref "
                                    "AND p.source_version=s.current_version AND p.status='current' "
                                    "WHERE s.current_version IS NOT NULL AND p.source_ref IS NULL"):
                    add("dangling_current", r[0], "current_version has no current projection")
                for r in cx.execute("SELECT source_ref, COUNT(*) FROM projections WHERE status='current' "
                                    "GROUP BY source_ref HAVING COUNT(*) > 1"):
                    add("multiple_current", r[0], "more than one current projection")
                for r in cx.execute("SELECT p.source_ref FROM projections p JOIN sources s ON s.source_ref=p.source_ref "
                                    "WHERE p.status='current' AND IFNULL(s.current_version,'') != p.source_version"):
                    add("current_pointer", r[0], "current projection is not the source's pointer")
                for r in cx.execute("SELECT source_ref, MAX(revision), COUNT(*) FROM annotation_revisions GROUP BY source_ref"):
                    checked["revisions"] += r[2]
                    if r[1] != r[2]:
                        add("revision_gap", r[0], "annotation revisions are not 1..n")
                for r in cx.execute("SELECT c.chunk_ref FROM chunks c LEFT JOIN layers l ON l.source_ref=c.source_ref "
                                    "AND l.source_version=c.source_version AND l.ordinal=c.layer_ordinal WHERE l.source_ref IS NULL"):
                    add("orphan_chunk", r[0], "chunk has no layer")
                for r in cx.execute("SELECT f.source_ref FROM failures f LEFT JOIN sources s ON s.source_ref=f.source_ref "
                                    "WHERE s.source_ref IS NULL"):
                    add("orphan_failure", r[0], "failure row has no source")
                return {"ok": total == 0, "exists": True, "problems": problems, "problem_count": total,
                        "checked": checked}
        except _StoreMissing:
            return {"ok": True, "exists": False, "problems": [], "problem_count": 0, "checked": {}}

    def export_jsonl(self, fp, *, include_text: bool = True) -> dict:
        """Write every table as JSON lines (one consistent read snapshot). Read-only."""
        counts: dict[str, int] = {}

        def emit(kind: str, obj: dict) -> None:
            counts[kind] = counts.get(kind, 0) + 1
            fp.write(json.dumps({"kind": kind, **obj}, ensure_ascii=False, sort_keys=True) + "\n")

        try:
            with self._read() as cx:
                emit("header", {"schema_version": C.STORE_SCHEMA_VERSION, "include_text": include_text,
                                **{r["key"]: r["value"] for r in cx.execute("SELECT * FROM meta")}})
                for r in cx.execute("SELECT * FROM sources ORDER BY source_ref"):
                    emit("source", dict(r))
                for r in cx.execute("SELECT * FROM projections ORDER BY source_ref, source_version"):
                    d = dict(r)
                    d["quality_flags"] = json.loads(d["quality_flags"])
                    d["coverage_warnings"] = json.loads(d["coverage_warnings"])
                    emit("projection", d)
                for r in cx.execute("SELECT * FROM layers ORDER BY source_ref, source_version, ordinal"):
                    d = dict(r)
                    if not include_text:
                        d.pop("text")
                    emit("layer", d)
                for r in cx.execute("SELECT * FROM chunks ORDER BY source_ref, source_version, idx"):
                    d = dict(r)
                    d["quality_flags"] = json.loads(d["quality_flags"])
                    d["heading_path"] = json.loads(d["heading_path"])
                    emit("chunk", d)
                for r in cx.execute("SELECT * FROM annotation_revisions ORDER BY source_ref, revision"):
                    d = dict(r)
                    for k in ("annotation", "derived", "producer_reported", "receipt"):
                        d[k] = _unj(d[k]) if d[k] else None
                    emit("annotation_revision", d)
                for r in cx.execute("SELECT * FROM reviews ORDER BY review_id"):
                    emit("review", dict(r))
                for r in cx.execute("SELECT * FROM entities ORDER BY source_ref, entity_id"):
                    emit("entity", dict(r))
                for r in cx.execute("SELECT * FROM failures ORDER BY source_ref"):
                    d = dict(r)
                    d["flags"] = json.loads(d["flags"])
                    emit("failure", d)
                inv = dict(cx.execute("SELECT * FROM inventory WHERE id=1").fetchone())
                inv["last_pass_summary"] = _unj(inv["last_pass_summary"])
                emit("inventory", inv)
        except _StoreMissing:
            raise _err("E_NOT_FOUND", "the enrichment store does not exist") from None
        return {"ok": True, "records": counts}

    def backup(self, dest_path) -> dict:
        """Consistent snapshot through SQLite's online backup API into a NEW file
        (never overwrites; a plain file copy of a WAL database would be torn)."""
        dest = Path(dest_path)
        if dest.exists() or dest.is_symlink():
            raise _err("E_BAD_ARGUMENTS", "backup destination already exists; refusing to overwrite")
        if dest.resolve() == self._db.resolve():
            raise _err("E_BAD_ARGUMENTS", "backup destination is the live store")
        if not dest.parent.is_dir():
            raise _err("E_BAD_ARGUMENTS", "backup destination directory does not exist")
        tmp = dest.with_name(dest.name + ".partial")
        if tmp.exists():
            raise _err("E_BAD_ARGUMENTS", "a partial backup file is in the way; remove it first")
        try:
            src = self._open(create=False, readonly=True)
        except _StoreMissing:
            raise _err("E_NOT_FOUND", "the enrichment store does not exist") from None
        try:
            dst = sqlite3.connect(str(tmp), isolation_level=None)
            try:
                src.backup(dst)
                dst.execute("PRAGMA journal_mode=DELETE")
                ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
                version = dst.execute("PRAGMA user_version").fetchone()[0]
            finally:
                dst.close()
            if ok != "ok" or version != C.STORE_SCHEMA_VERSION:
                raise _err("E_INTERNAL", "backup snapshot failed its own integrity check")
            os.chmod(tmp, 0o600)
            fd = os.open(tmp, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, dest)
        except BaseException:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise
        finally:
            src.close()
        h = hashlib.sha256(dest.read_bytes()).hexdigest()
        return {"ok": True, "bytes": dest.stat().st_size, "sha256": h, "schema_version": C.STORE_SCHEMA_VERSION}


def open_store(root, *, reader=None, max_input_chars: int = 24000, clock=None, **kwargs) -> SQLiteProjectionStore:
    """Construct the store for the wiki at `root` (no import-time rooting). The database file is
    created lazily on the first write; `reader` defaults to `SourceReader(root)`."""
    return SQLiteProjectionStore(root, reader=reader, max_input_chars=max_input_chars, clock=clock, **kwargs)
