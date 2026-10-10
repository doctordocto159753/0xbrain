# Retrieval, index and graph (WP-C)

Implements CONTRACT-E1 sections 7 and 8: `scripts/brain_enrichment/indexing.py`
(derived FTS5 index in generations), `graph.py` (nodes, edges, bounded
expansion), `retrieval.py` (`brain_retrieve`). Nothing here calls a model, the
network or a subprocess; `query_model_calls` is always `0`.

## What it guarantees

* The answer is **exact chunk text** read from the store at query time, with
  the tier and authority level of *that text* (a capture is 7, an ingested
  file reports its derivative: tier `derivative`, level 5, never the
  original's). A summary, tag, entity or edge is side metadata and is never
  returned as text or quote.
* **Direct text is never hidden by the graph.** The graph is seeded only by
  metadata/entity matches or explicit refs (never by degree), is bounded, and at
  least 50% of every window of `limit` results is reserved for direct
  candidates when enough exist.
* **Authority is never inferred from topology or rank.** `supports` /
  `contradicts` are *reading* edges: candidate readings with provenance, labelled
  in `inclusion_reason.matched` (`reading:supports`). Passing validation proves
  that a quote exists, not that the reading is right.
* **Every route is visible**: `inclusion_reason.paths` lists all of
  `direct_text`, `metadata`, `graph`, `fallback` that reached the chunk, `via`
  the refs/entity ids it travelled through, `matched` the query tokens (or edge
  labels) that matched. Never stored candidate text.
* **Stale text is never served as current**: each candidate is re-checked against
  the store's current chunk (and the optional `source_fresh` disk check);
  stale or deleted ones are dropped and counted in `coverage.stale_dropped`.

## Index (`_search/enrichment/search-v1.sqlite3`)

Derived from the store only. Deleting the file loses nothing
(`IndexBuilder.rebuild_full()` recreates it).

| table | content |
|---|---|
| `meta` | `schema_version`, `active_generation` (the pointer), `counts`, `scanned_marker` |
| `generation` | one row per generation (`building`/`active`/`retired`) |
| `artifacts` | one row per current source: ref, version, group (`captures`/`canonical`), per-layer tier/level, flags, annotation revision + review state, `fingerprint` |
| `chunk_rows` + `fts_chunks` | chunk_ref, offsets; FTS5 over `normal_form(chunk text)` (`unicode61 remove_diacritics 2`), rowid-joined |
| `meta_rows` + `fts_meta` | candidate metadata, one row per value: `summary`, `topic`, `tag`, `entity`, `alias`, `chunk_summary`, `chunk_tag`; FTS5 over the normal form; keyed by artifact (+ chunk for chunk-level and entity mentions) |
| `nodes`, `edges` | the graph: `art:<artifact_id>`, `chk:`, `ent:`, and public refs; edges carry `edge_kind`, `label`, `source_ref`, `source_version`, `annotation_revision`, `provenance` |

FTS5 is probed with the real interpreter (`fts5_available()`). Without it
`index.state` is `unavailable` and callers get the explicit fallback; no
exception reaches a caller.

### Generations and activation

Every row has `gen_from`/`gen_to`. A reader fixes the active generation `g`
once (one read transaction) and sees rows with
`gen_from <= g AND (gen_to IS NULL OR gen_to > g)`.

A build works on `g+1`: for each dirty artifact it sets `gen_to = g+1` on the old
rows (still visible at `g`) and inserts the new rows with `gen_from = g+1`
(invisible at `g`); each artifact is its own transaction. The **only** thing that
makes `g+1` visible is the final transaction that updates `meta.active_generation`
(together with the counts). A crash before it leaves `g` complete and readable; the
next build deletes the orphan rows (`gen_from > active`) and resets `gen_to > active`
before starting. After activation, rows no generation can see are physically
removed (best effort).

### Refresh

`IndexBuilder.refresh(batch_limit=25)`:

1. page through `store.list_current(after, 50)` (ordered by `source_ref`) and compute a
   per-artifact **fingerprint** = hash of `source_version`, annotation revision, review
   state, the ordered chunk refs (and the `graph.db` stamp for canonical records);
2. compare with the fingerprints of the active generation: changed or new artifacts are
   *dirty*, artifacts that left the store are *removed*;
3. index at most `batch_limit` dirty artifacts (all removals) into generation `g+1`,
   activate it, return `IndexReceipt(generation, activated, artifacts_indexed,
   artifacts_pending, lag={"pending_artifacts": n, ...})`. Repeat until pending is `0`.

`trust_marker=True` skips the scan when `store.change_marker()` equals the one
recorded by the last clean refresh (default off: correctness over the scan cost, and
the in-memory test double's marker does not move on every write). `status()` caches
the lag per `(change_marker, generation)`, so queries do not rescan the store.

`rebuild_full()` is explicit (CLI) and never reached from `retrieve`; it also replaces
a corrupt index file. A corrupt file is otherwise left untouched and reported as
`unavailable`.

### What is indexed

* chunk text: always, for the current `source_version` only;
* candidate metadata and semantic/reading edges: only from the **current, non-rejected**
  annotation revision bound to the **current** source version. A `rejected` review or an
  annotation for an older version produces no metadata rows and no candidate edges (the
  text stays searchable); the review state is part of the fingerprint, so the next
  refresh removes them.

## Graph (`graph.py`)

| edge_kind | labels | built from |
|---|---|---|
| structural | `contains` (art→chunk), `next_chunk`, `derived_from` (holding→derivative `text_ref`) | the projection |
| registered | `wikilink` (`[[id]]` in canonical records), `cites_ref` (a record/capture id verbatim in layer text that exists), bridged edges of `_search/graph.db` | existing corpus; `graph.db` is opened `mode=ro`, never written |
| semantic | `about`, `mentions`, `part_of_project`, `related_to` | annotation, candidate |
| reading | `supports`, `contradicts` | annotation + evidence, candidate reading |

* `cites_ref` tokens must contain `-`, be ≥5 characters and equal an id of a known
  current projection, a `graph.db` node, or (captures) resolve through
  `brain_surface.refs`. Unresolvable targets create no edge.
* Candidate edges use the store's server-computed `derived` block when the store
  returns it, otherwise the same block is recomputed from the validated annotation. The
  edge kind is always recomputed from the relation, never trusted.
* **Entities are per-source nodes** (`C.entity_id(source_ref, normal_form(name))`). Two
  sources naming "Kolan" give two nodes with no edge between them. They meet only at
  query time, through the shared normal form in `fts_meta`, and the response then
  lists them per source and says so in `notes`.
* `expand(neighbors, seeds, max_depth<=2, max_nodes<=40)`: breadth-first, deterministic
  order, every node carries `depth`, `seed`, `via` (the steps with `edge_kind:label` and
  direction) and `also_via` (other paths). **Hub guard**: a node with more than
  `FANOUT_CAP=10` traversable edges contributes only its first 10 in
  `(label, kind, id)` order (not by degree), and a non-seed node with more than
  `HUB_DEGREE=24` is reached but not walked through. Only registered, semantic and
  reading edges are traversed (structural edges would turn every artifact into a hub).

## Retrieval algorithm

1. **Validate**: `query` (1..1000), `scope` (`all|canonical|captures`), `limit` 1..50
   (default 10, *per group*), `budget_tokens` 500..32000 (default 6000), `cursor`; unknown
   keys → `E_UNKNOWN_ARGUMENT`. `scope=all` yields two groups (`canonical` = canonical
   records, source records, derivatives; `captures`); they are ranked separately, never
   merged, and the budget is shared by round-robin over the groups.
2. **Direct text**: FTS over `normal_form(query)` tokens (stop words removed): strict `AND`,
   then a relaxed `OR` (with Persian suffix-stem prefixes) that needs at least half of the
   content tokens. BM25 orders each list; strict hits precede relaxed ones.
3. **Metadata**: the same over `fts_meta` (entity, alias, topic, tag, chunk tag/summary, summary),
   ordered by match quality (row == query, all tokens, partial), then field priority
   (entity < alias < topic < tag < chunk < summary), then BM25. Explicit refs in the query
   (`cap:…`, `rec:…`, `doc:…`, `chk:…`, or a bare id) lead this list. An artifact-level match is
   shown through its best direct-text chunk, else its best in-artifact text match, else its
   first chunk, never through a chunk made up from a summary.
4. **Graph**: seeds (≤8) = explicit refs, then the entity node and owning artifact of
   metadata matches in rank order. `expand` as above. A reached artifact is shown through the
   evidence chunk of the edge when that chunk belongs to it, else as in step 3.
5. **Fusion**: the direct list is the RRF (`k=60`, weight 1 per list) of the text,
   metadata and fallback lists; final score = `w_direct/(k+rank_direct) + w_graph/(k+rank_graph)`
   with `contract.RRF_WEIGHTS`. Only **ranks** are used: BM25 and graph degree are never added.
   Ties: direct first, then rank, then chunk_ref. At most 3 chunks per source and group.
6. **Direct share**: in every window of `limit` results (`window` is stored in the cursor) at
   least `ceil(0.5 * window)` are direct candidates when enough exist.
7. **Page**: the ordered list is walked from the cursor offset; for each candidate the current
   chunk is read from the store (`get_chunk`), its `source_version` must equal the indexed one
   and `source_fresh(source_ref, source_version)` (if injected) must hold; otherwise it is
   dropped (`stale_dropped`). Hits are emitted round-robin over the groups until `limit` per
   group or the budget.
8. **Budget**: each hit costs `estimate_tokens(text) + 30` (the 30 covers refs and reason).
   A text is included whole only if `contract.fits_budget(text, remaining-30)` holds
   (token estimate **and** the 4 chars / 12 bytes per token ceilings). If the first hit of
   a page does not fit, or at least 110 tokens remain, a **prefix** of the exact text is
   returned with `truncated=true` and a shortened `span` (the rest is available through
   `brain_artifact_read` with the same `chunk_ref`); then the page ends. `estimated_tokens`
   is the sum of those estimates, an upper-bound style estimate and never a token count.
9. **Cursor**: `{query digest, scope, store change_marker, index generation, per-group offsets,
   window}`. Malformed or for another query/scope → `E_BAD_CURSOR`; a different generation or a
   changed store → `E_STALE_CURSOR` (restart; two generations are never stitched). Any store write
   (new capture, annotation, review) moves the marker and therefore invalidates open cursors.

### Fallbacks (never silent)

| situation | `path` | `index.state` |
|---|---|---|
| `config.retrieval` false | `fallback-lexical` / `fallback-exact` | what the index reports |
| index file missing / no active generation | same | `missing` |
| FTS5 absent, corrupt file, unsupported schema | same | `unavailable` |
| index lagging or backfill incomplete | `enrichment` | `lagging` |

Fallback calls `legacy_search(query, "lexical", scope, n)`; if it raises or returns nothing it
calls `legacy_search(query, "exact", scope, n)`; `path` says which result is shown and
`notes` why. Hits are `hit_level="source"` with `chunk_ref=null`, `span=null`, text = the legacy
snippet, `paths=["fallback"]`. Without an injected `legacy_search` the call fails with
`E_UNAVAILABLE` (never an empty success). A cursor cannot continue on a fallback path
(`E_STALE_CURSOR`).

**Lagging / incomplete backfill.** `lag.pending_artifacts` = dirty artifacts in the store +
(sources on disk minus sources in the store, when `wiki_root` is given). While it is > 0 the base
search is also asked, and its hits for sources **without a current indexed projection** are added
as source-level candidates to the direct list (counted in `coverage.source_level_fallback_hits`,
`state="lagging"`). Sources the index already covers are not duplicated. When pending is 0 the base
search is not called.

## Parameters

| name | value | where |
|---|---|---|
| `RRF_K`, `RRF_WEIGHTS` | 60, direct 1.0 / graph 1.0 | `contract.py` |
| `DIRECT_MIN_SHARE` | 0.5 | `contract.py` |
| `GRAPH_MAX_SEEDS/DEPTH/NODES` | 8 / 2 / 40 | `contract.py` |
| `FANOUT_CAP`, `HUB_DEGREE` | 10, 24 | `graph.py` |
| `TEXT_POOL`, `META_POOL`, `PER_SOURCE_CAP` | 120, 60, 3 | `retrieval.py` |
| `HIT_OVERHEAD_TOKENS`, `MIN_PARTIAL_TOKENS` | 30, 80 | `retrieval.py` |
| `INDEX_REFRESH_BATCH` | 25 | `contract.py` |

## Limits (honest list)

* Lexical, not semantic: no embeddings or models. A query without word overlap with the text or
  the candidate metadata finds nothing (the benchmark keeps such queries on purpose).
* Candidate metadata is only as good as the annotations; canonical records and orphan derivatives
  carry no annotation in R1, so they are found only by text, refs and registered edges.
* Neighbour chunks are not inlined (refs only): inlining their text would exceed the Hit shape of
  section 8; read them with `brain_artifact_read`.
* The lag count and the "sources on disk" count are exact for the store but approximate for the disk
  (files the projector deliberately skips would keep the state at `lagging`; the effect is extra base
  search calls, never a silent gap).
* While `lagging`, the base search is re-queried for every page; if its answer changes between pages
  the offsets can shift (the cursor guards the index generation and the store, not the base search).
* A hit's `rank` is its position in the ordered group list (gaps appear where stale candidates were
  dropped).
* `change_marker` is the only freshness token: one unrelated write invalidates open cursors.

## Benchmark

`python tests/enrichment/bench_retrieval.py [--misses] [--json]` runs
`tests/enrichment/fixtures/retrieval_queries.json` (18 documents, 26 fa/en queries, 1 negative)
for `legacy-exact` (the normalisation-aware substring search of `search_lexical.exact_search`),
`r1-direct` (R1 without annotations) and `r1` (R1 with hand-written annotations). The documents,
queries and annotations share an author and the annotations are stand-ins for annotator output:
the numbers characterise the mechanism, not recall on a real corpus. QMD BM25 (the other legacy
route) is not part of it because `qmd` is not installed in the test environment.

Reference run (this repository, Python 3.13, SQLite 3.45):

| system | Recall@10 | MRR | negative false positives |
|---|---|---|---|
| legacy-exact | 0.320 | 0.360 | 0 |
| r1-direct | 0.800 | 0.840 | 0 |
| r1 | 0.940 | 0.960 | 0 |

R1 still misses `oral history` (cross-language, canonical records are not annotated in R1) and
half of `what is my exercise routine` (no lexical overlap). Run with `--misses` for every miss.
