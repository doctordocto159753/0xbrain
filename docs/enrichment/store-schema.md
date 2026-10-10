# Enrichment store schema (WP-A)

`_enrichment/store-v1.sqlite3` implements CONTRACT-E1 section 6. This file is
the reference for the exact DDL, the state machine and the transaction
boundaries. The DDL block below is compared with `scripts/brain_enrichment/store.py::DDL`
by `tests/enrichment/test_store.py::test_documented_ddl_is_exactly_the_code_ddl`.

## Files, permissions, pragmas

| item | value |
|---|---|
| path | `<root>/_enrichment/store-v1.sqlite3` (+ `-wal`, `-shm` while open); never a symlink, always inside the injected root |
| directory mode | `0700` (set on creation and re-asserted); database file `0600`; no Markdown, only SQLite files |
| `PRAGMA user_version` | `1` (`contract.STORE_SCHEMA_VERSION`) |
| journal | `WAL`, `synchronous=FULL`, `foreign_keys=ON`, `busy_timeout=30000` ms |
| connections | one per operation (never shared between threads); read helpers use `PRAGMA query_only=ON` in a `BEGIN` snapshot |
| creation | lazy, on the first write; reads on a missing store answer "nothing stored" and create nothing |
| unknown schema | `user_version > 1`, or a non-empty database with `user_version = 0`, or missing tables -> `E_SCHEMA_UNSUPPORTED`; the file is never altered, dropped or migrated automatically. A file that is not SQLite -> `E_UNAVAILABLE`, left untouched. `status()` reports `state: "unsupported"` instead of raising. |

## DDL

<!-- DDL:begin -->
```sql
CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE sources (
  source_ref      TEXT PRIMARY KEY,
  artifact_id     TEXT NOT NULL,
  current_version TEXT,
  state           TEXT NOT NULL CHECK (state IN ('discovered','projected','annotated','failed','stale','no_text')),
  present         INTEGER NOT NULL DEFAULT 1 CHECK (present IN (0,1)),
  missing_since   TEXT,
  seen_pass       INTEGER NOT NULL DEFAULT 0,
  first_seen_at   TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);

CREATE TABLE projections (
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
);

CREATE UNIQUE INDEX projections_one_current ON projections(source_ref) WHERE status = 'current';

CREATE TABLE layers (
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
);

CREATE TABLE chunks (
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
);

CREATE TABLE annotation_revisions (
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
);

CREATE TRIGGER annotation_revisions_no_update BEFORE UPDATE ON annotation_revisions
BEGIN SELECT RAISE(ABORT, 'annotation_revisions is append-only'); END;

CREATE TRIGGER annotation_revisions_no_delete BEFORE DELETE ON annotation_revisions
BEGIN SELECT RAISE(ABORT, 'annotation_revisions is append-only'); END;

CREATE TABLE annotation_chunks (
  source_ref TEXT NOT NULL,
  revision   INTEGER NOT NULL,
  chunk_ref  TEXT NOT NULL,
  PRIMARY KEY (source_ref, revision, chunk_ref),
  FOREIGN KEY (source_ref, revision) REFERENCES annotation_revisions(source_ref, revision)
) WITHOUT ROWID;

CREATE INDEX annotation_chunks_by_chunk ON annotation_chunks(chunk_ref);

CREATE TABLE entities (
  entity_id      TEXT NOT NULL,
  source_ref     TEXT NOT NULL,
  first_revision INTEGER NOT NULL,
  name           TEXT NOT NULL,
  normal         TEXT NOT NULL,
  kind           TEXT,
  PRIMARY KEY (entity_id, source_ref),
  FOREIGN KEY (source_ref, first_revision) REFERENCES annotation_revisions(source_ref, revision)
) WITHOUT ROWID;

CREATE TABLE reviews (
  review_id           INTEGER PRIMARY KEY AUTOINCREMENT,
  source_ref          TEXT NOT NULL,
  annotation_revision INTEGER NOT NULL,
  decision            TEXT NOT NULL CHECK (decision IN ('confirmed','rejected')),
  reviewed_by         TEXT NOT NULL,
  reviewed_at         TEXT NOT NULL,
  note                TEXT,
  canonical_promotion INTEGER NOT NULL DEFAULT 0 CHECK (canonical_promotion = 0),
  FOREIGN KEY (source_ref, annotation_revision) REFERENCES annotation_revisions(source_ref, revision)
);

CREATE TRIGGER reviews_no_update BEFORE UPDATE ON reviews
BEGIN SELECT RAISE(ABORT, 'reviews is append-only'); END;

CREATE TRIGGER reviews_no_delete BEFORE DELETE ON reviews
BEGIN SELECT RAISE(ABORT, 'reviews is append-only'); END;

CREATE TABLE failures (
  source_ref         TEXT PRIMARY KEY REFERENCES sources(source_ref),
  attempts           INTEGER NOT NULL CHECK (attempts >= 1),
  last_error_code    TEXT NOT NULL,
  last_error_message TEXT NOT NULL,
  transient          INTEGER NOT NULL CHECK (transient IN (0,1)),
  flags              TEXT NOT NULL,
  first_failed_at    TEXT NOT NULL,
  last_failed_at     TEXT NOT NULL,
  next_retry_after   TEXT
);

CREATE TABLE inventory (
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
);
```
<!-- DDL:end -->

`annotation_revisions` and `reviews` are append-only **by trigger**: any
`UPDATE`/`DELETE` aborts with `... is append-only`. `chunks.chunk_ref` is
constrained to be `'chk:' || substr(chunk_digest,1,32)` and `chunk_digest` is
`UNIQUE`, so a colliding ref can never silently point at other coordinates.
`reviews.canonical_promotion` is constrained to `0`: a review never promotes.

Chunk **text is not stored twice**: `chunks` holds coordinates only and the text
is `layers.text[start_char:end_char]` computed in Python (code points; SQLite's
`substr` is not used because NUL characters would truncate it). Layer texts of
superseded versions are retained for audit and are never served as current.

## Table roles

| table | role |
|---|---|
| `meta` | `created_at`, `store_schema`, `instance_id`, and `change_marker` (monotonic integer text) |
| `sources` | one row per `source_ref` ever seen: `current_version` pointer (NULL when there is no valid current projection), live `state`, `present`/`missing_since`, `seen_pass` (last discovery pass that listed it) |
| `projections` | `(source_ref, source_version)`; `status` is `current` (at most one per source, partial unique index) or `stale`; `processing_state` is the projection-time `projected`/`no_text` |
| `layers` | the full decoded layer texts of a projection, in order |
| `chunks` | `chunk-1` chunks of a projection (global `idx`, links within a layer) |
| `annotation_revisions` | one row per accepted submission; revision numbers are per `source_ref` (1..n over all versions); stores the validated annotation, the server-computed `derived` block, `producer_reported`, `request_digest`, `idempotency_key`, the original receipt |
| `annotation_chunks` | chunk refs named by `chunk_annotations` of a revision (drives coverage) |
| `entities` | entity ids from the `derived` blocks (answers `entity_exists`) |
| `reviews` | one row per human decision on `(source_ref, annotation_revision)`; the effective state is the latest row, history is kept |
| `failures` | per source: `attempts`, `last_error_code`, `last_error_message` (ref-level, no paths), `transient`, `flags` (`parse_warning`), `next_retry_after` |
| `inventory` | the single discovery cursor row: `pass_no`, opaque reader `cursor`, per-pass counters, `inventory_generation`, summary of the last completed pass |

## State machine

`sources.state` is derived (never set by callers):

```
 (listed) --> discovered
                 | upsert_projection (text)
                 v
              projected --append_annotation--> annotated
                 ^                                ^
                 |  new text while revisions      | append_annotation for the new version
                 |  exist for an older version    |
                 +------------------------------> stale
              upsert_projection (no layers) ----> no_text
 any state -- parse/validation failure ---------> failed   (stable: needs an explicit retry)
              transient failure ----------------> failed   (retried twice, with spacing)
 file removed -> present=0, current_version=NULL (revisions kept; revives when the file returns)
```

* `compute_state`: no current version -> `failed` (if a failure row exists) else unchanged;
  a failure row -> `failed`; `no_text` projection -> `no_text`; a revision for the
  current version -> `annotated`; revisions only for older versions -> `stale`; else `projected`.
* **Review** is a separate axis per revision (`unreviewed|confirmed|rejected`, latest row
  of `reviews`). **Index freshness** is a third axis owned by the index (generation).
* **Coverage** of the current version = the number of its chunks named by `chunk_annotations`
  of any of its revisions (union); an artifact with exactly one chunk and any revision counts
  as fully covered. `pending` offers a source until covered chunks == total chunks.
* A stable failure after a good projection marks that projection `stale` and clears the pointer:
  stale text is never served as current. A transient failure keeps the projection.

## Transactions

Every mutation is exactly one `BEGIN IMMEDIATE ... COMMIT`; any exception rolls the whole
transaction back (`E_UNAVAILABLE` for SQLite busy/locked, `E_INTERNAL` for a rejected write).

| operation | transaction contents |
|---|---|
| schema creation | tables + `meta` rows + inventory row + `user_version=1`, re-checked under the lock |
| `upsert_projection` | validation first (outside, pure); then source row, supersede older `current` projection, projection + layers + chunks (or revive an existing stale version after verifying the same chunk set), clear the failure row, refresh `sources.state`, bump `change_marker` only if something changed |
| `append_annotation` | idempotency lookup `(source_ref, key)`: same `request_digest` -> original receipt with `idempotent_replay=true`, different -> `E_IDEMPOTENCY_CONFLICT`; **fresh `SourceReader.load` inside the transaction** (deleted -> mark missing, commit, `E_NOT_FOUND`; changed/unverifiable -> `E_STALE_SOURCE`); request version == stored current == fresh version; CAS `expected_annotation_revision == MAX(revision)` else `E_CONFLICT{current_revision}`; revision row (with its receipt), `annotation_chunks`, `entities`, state refresh, marker bump |
| `record_review` | refused (`E_REVIEW_FORBIDDEN`) when `BRAIN_REMOTE_SESSION` is set, or the context lacks a non-empty local `actor`, or has `remote`; insert one `reviews` row; marker bump |
| `record_failure` / `mark_missing` | failure upsert (`attempts+1`, `next_retry_after` only for transient), stale marking of the previous projection for stable failures; missing: projection stale, pointer cleared, failure row dropped |
| discovery page | each source is projected in its own `upsert_projection` transaction; after a page the cursor/counters/`seen_pass` are advanced in one compare-and-set transaction (`pass_no` and previous cursor must match, otherwise another worker already advanced and this one stops) |
| pass end | sources not listed during the pass (`seen_pass < pass_no`) are marked missing; `pass_no` increments, cursor resets |

The reader is consulted outside a transaction everywhere except `append_annotation`, where the
fresh version check must be atomic with the CAS.

## Discovery passes

`refresh(budget=100, retry_failed=False)` (also the first step of `pending`): optionally retries
`failures` rows (explicit), then continues the persisted pass: `reader.list_sources(cursor, n)`
pages, each source loaded -> chunked -> upserted (stable failures are skipped until
`retry_failed`; transient failures retry when due and fewer than 2 attempts were made). At most
`budget` sources are touched per call; the cursor survives restarts. A pass that completes ends the
call; the next call starts the next pass. Sources created during a pass are found by the next pass
at the latest (the reader snapshot is fixed for the pass unless the process restarted).

## Pending queue

Eligible: present, `state IN (projected, stale, annotated)`, `source_kind IN (capture-text,
capture-media, ingested-file)`, projection `projected` (not `no_text`), covered < total. Ordered
by `(time of the latest revision of the current version or '', source_ref)` so a partially
annotated source rotates to the back. A page is capped at `max_input_chars` (default 24000) and
`PAGE_MAX_CHUNKS`; the first chunk of a response is always returned (a chunk is atomic evidence
and may exceed a tiny cap). The cursor `{r, v, o, t}` binds source_ref, source_version and chunk
offset; a version change -> `E_STALE_CURSOR`. Canonical records, orphan derivatives and source
records without file are never queued (`annotation_policy: skip`).

## Operations

* `status()` counts and state (never raises for a missing/newer store).
* `verify()` SQLite `integrity_check`, `foreign_key_check`, layer hash/size, `source_version`
  recomputation, chunk digest/offset/partition/link checks, one current projection per source,
  revision numbering, orphans. Read-only.
* `export_jsonl(fp, include_text=True)` every table as JSON lines from one read snapshot. Read-only.
* `backup(dest)` SQLite online backup API into a **new** file (`.partial` then atomic rename,
  `journal_mode=DELETE`, `0600`, integrity-checked); never overwrites.

## Python API (WP-A surface used by B, C, D, E)

```python
from brain_enrichment.source import SourceReader          # contract.SourceReader
from brain_enrichment.chunking import Chunker              # contract.Chunker (chunk-1)
from brain_enrichment.store import open_store, SQLiteProjectionStore, annotation_policy
from brain_enrichment.refs import resolve_chunk_ref, chunk_neighbours
from brain_enrichment import projector                    # project_source(store, reader, chunker, policy, ref)

reader = SourceReader(root)                                # root is injected; no import-time rooting
store  = open_store(root, reader=None, max_input_chars=24000, clock=None,
                    chunker=None, policy=None, discovery_budget=100, retry_spacing_seconds=300)
```

`SQLiteProjectionStore` implements `contract.ProjectionStore` (`upsert_projection`, `pending`, `get`,
`append_annotation`, `record_review`, `get_chunks`, `get_chunk`, `entity_exists`, `list_current`,
`change_marker`) plus:

| method | notes |
|---|---|
| `refresh(budget=None, retry_failed=False) -> dict` | one bounded discovery/projection step; the `discovery` block of `pending` (`pass, inventory_generation, scanned, projected, failed, no_text, changed, pass_complete, scanned_this_call[, error]`) |
| `reprocess(source_ref)` | explicit reprocess of one source; raises the recorded error on failure |
| `record_failure(ref, error, transient=None)` / `mark_missing(ref)` | used by `projector.project_source`; callable by the CLI |
| `status()`, `verify()`, `export_jsonl(fp, include_text=True)`, `backup(dest_path)` | read-only operations (see above) |
| `annotation_policy(source_kind)` | `annotate` for `capture-text`/`capture-media`/`ingested-file`, else `skip` |

Rules callers must know:

* **The reader is the authority on what exists on disk.** `get`, `get_chunks`, `get_chunk` and
  `append_annotation` re-load the source through `reader` (deleted -> `E_NOT_FOUND`, changed ->
  `E_STALE_SOURCE`); discovery marks sources the reader no longer lists as missing at the end of a pass.
  Tests that store synthetic projections must pass a `reader=` that serves them (a ~15 line stub).
  `list_current` and `change_marker` are store-only (no disk access).
* `pending(cursor, limit<=5, retry_failed)` first runs `refresh()`; items follow CONTRACT-E1 section 9;
  `expected_annotation_revision` is the latest revision of the source over all versions (the CAS value),
  `current_annotation_revision` the latest one for the current version (0 if none).
* `append_annotation(request)` takes `{source_ref, source_version, expected_annotation_revision,
  idempotency_key, annotation, derived?}`; no other keys. Errors: `E_STALE_SOURCE`, `E_CONFLICT`
  (`current_revision`), `E_IDEMPOTENCY_CONFLICT`, `E_NOT_FOUND`, `E_TOO_LARGE` (1 MiB request bound).
* `list_current` items: `projection, chunks, annotation, annotation_revision, review_state,
  processing_state, derived, producer_reported, received_at, annotation_policy`. Rejected revisions are
  still returned (`review_state == "rejected"`); filtering is the index's decision.
* `E_STALE_SOURCE` raised for a chunk carries `source_ref` in `extra`; `resolve_chunk_ref` uses it to
  report `E_BAD_REF` when the chunk belongs to another source.
