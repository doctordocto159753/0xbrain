# CONTRACT-E1 — enrichment layer (R1)

Status: frozen by AGENT-0 at the freeze commit. Authority: this file +
`scripts/brain_enrichment/contract.py`, `config.py`, `tools_spec.py`,
`schemas/*.json`, `tests/enrichment/fixtures/*`. Where prose and code
disagree on a *value or hash*, the code and golden vectors win and this file
is amended in the same change. A contract change needs a version bump and a
shared fixture update (AGENT-0 only).

Base: `doctordocto159753/0xbrain` @ `a245b3b2620800b1d2d6cc4be0dba6e44fb5b103`.
Reading order for every agent: `AGENTS.md`, `CLAUDE.md`, `SYSTEM_DESIGN.md`,
this file.

## 0. What R1 is

An **additive sidecar** that makes existing material (captures, ingested
files, canonical records) findable and navigable: deterministic *projection*
and *chunks*, model-produced *candidate metadata*, a small derived *graph*,
and an *evidence-bearing retrieval* tool. The seven `brain_*` tools, refs,
capture/ingest paths, QMD and `_search/graph.db` are untouched.

Non-negotiable invariants (tests must prove them):

1. **Zero source mutation.** No enrichment code path writes under `_originals/`,
   `01-inbox/`, `02-sources/`, `03-objects/`…`07-genesis/`, `_captures/`,
   `_proposals/`, `00-system/`. `tests/enrichment/support.corpus_hashes` is the witness.
2. **Interpretation is not source.** Model metadata lives only in
   `_enrichment/store-v1.sqlite3`; it is candidate tier (authority level 7),
   never quote-able as evidence, never upgraded by fluency or repetition.
3. **Graceful absence.** Feature off, store missing, FTS5 missing, model down,
   index corrupt: the seven base tools behave exactly as before.
4. **No model call in core/search/read/retrieve.** `query_model_calls` is always 0.
5. **Server computes, caller supplies only candidate content.** authority,
   hashes, ids, source_kind, tier, actor are never accepted from a caller.
6. **Review is human-only and local.** No MCP tool approves, promotes or reviews.
7. **Append-only annotation revisions;** the index is rebuildable, the store is not.

## 1. Locations

| Path | Owner | Backup |
|---|---|---|
| `_enrichment/store-v1.sqlite3` | persistent store (projection manifests, chunks, revisions, reviews, inventory cursor, failures) | **yes**, via SQLite backup API |
| `_search/enrichment/search-v1.sqlite3` | derived FTS5 + graph (generations) | no (rebuildable) |
| `scripts/brain_enrichment/` | code | git |
| `docs/enrichment/` | docs | git |
| `harness/hermes/` | skill + job template (off by default) | git |

`_enrichment/` is git-ignored, listed in both validators' `IGNORED_PREFIXES`,
holds SQLite/JSON only (no runtime Markdown), is created with `0700`, and every
path is resolved relative to the injected root with symlink/escape checks.
The backend root is **injected** (constructor argument); no import-time
rooting, no monkeypatch of `wiki_capture` from production code.

## 2. Sources and layers (projection input)

A *source* is addressed by an existing public ref (`brain_surface.refs`). A
projection is an *envelope* (`SourceProjection`, schema `schemas/envelope.schema.json`)
over one or more text **layers**. Loader rules (SourceReader):

* Read files with newline preservation (`open(..., newline="")` /
  `wiki_capture.read_record_file`), decode UTF-8 strictly; undecodable ->
  `E_VALIDATION` recorded as `failed` with a `parse_warning`, never repaired.
  Use existing parsers (`wiki_capture.parse_record_text`, refs helpers). No re-render.
* Never include a path in any public object. `anchor_path` / `origin_path`
  are server-only.

| Source | `source_ref` (anchor) | `source_kind` / `source_tier` / level | Layers |
|---|---|---|---|
| text capture `01-inbox/captures/<cap>.md` | `cap:<id>` | `capture-text` / `capture` / 7 | `user-text` = the **User-supplied text** section only; `text_ref=cap:<id>`; `literal=true`; `reviewed=false`. Frontmatter, *Provenance events*, *Interpretation (candidate)*, *Machine description* are **never** chunked as user text. |
| media capture (voice/image/file/handwriting/mixed) | `cap:<id>` | `capture-media` / `capture` / 7 | `transcript` = *Literal transcript or extraction* section when non-empty (`reviewed = transcription_reviewed`, `text_ref=cap:<id>`); a `mixed` capture with User-supplied text also has `user-text`. No literal text at all -> no layers, state `no_text`, flag `media_only`. Description/interpretation sections are excluded in R1 (note it in `coverage_warnings`). Media bytes are never delivered. |
| ingested file (source record with `original_path`/`extracted_text_path`/`holdings_tier`) | `rec:<record id>` (the holding anchor) | `ingested-file` / `source-record`(anchor), layer tier `derivative` / level of the derivative (5) | one layer `derivative-text` = the **entire** derivative file (`02-sources/text/*.md`) as decoded text; `text_ref=doc:<hash>` (existing `refs.doc_ident`); `provenance_sha256` = the source record's `sha256` (SHA of the original bytes); `original_sha256` on the envelope = same. `storage_sha256` = hash of the derivative file on disk. Source record + derivative are **one** holding, not two inputs. `extraction_status=needs_ocr` -> flag `needs_ocr`; `preserved_only`/missing/empty derivative -> no layer, `no_text` + `metadata_only`. The source record's own text is never substituted for file content. |
| other source record (no file keys) | `rec:<id>` | `source-record` / `source-record` | no layer -> `no_text`, `metadata_only` |
| orphan derivative (no record points at it) | `doc:<hash>` | `derivative-text` / `derivative` / 5 | `derivative-text` layer as above |
| canonical record (zones `03-objects`..`06-relations`) | `rec:<id>` (or `doc:` without id) | `canonical-record` / `canonical-record` / existing level | `record-text` = whole file text (so offsets align with `brain_read` content); the leading front-matter block is its own chunk boundary |
| proposals, history (`prop:`, `hist:`) | — | not projected in R1 | read with the base tools |

* Duplicate captures (same text) are **two** artifacts (distinct `source_ref`).
  Duplicate uploads are already one holding (existing ingest dedupe).
* `origin_channel` is set only from server-side evidence (the capture's
  `capture_channel` front-matter field as recorded by the server). Never infer
  `telegram` from message text. The current remote channel value is `mcp`.
* Pending (annotation queue) covers **captures and ingested files only** in R1.
  Canonical records / orphan derivatives are projected and indexed (retrieval,
  graph) but carry `annotation_policy: skip` (state stays `projected`).
* Capture text is stored by the existing writer as `rstrip("\n")+"\n"`;
  projections bind to the **stored** text, and R1 makes no byte-equality claim
  about the user's original input.

## 3. Identity, hashing, versions (normative; see contract.py)

* `canonical_json`: `json.dumps(obj, sort_keys=True, separators=(",",":"), ensure_ascii=False, allow_nan=False)` UTF-8.
* `text_sha256` = SHA-256 of the exact UTF-8 bytes of the layer text.
  `layer.chars` = code points, `layer.bytes` = UTF-8 bytes.
* `artifact_id` = SHA-256(`INSTANCE.id` + `\0` + `source_ref`), 64 hex.
  `INSTANCE.id` from `00-system/registers/INSTANCE.json` (`"kit"` if absent).
* `source_version` = SHA-256(canonical_json({"v":1,"source_ref",layers:[{layer_id,text_ref,text_sha256,origin,provenance_sha256}...]})) in projection layer order.
  `projection_version` is deliberately excluded. `storage_sha256` is separate
  (on-disk file hash) and is never confused with a text hash or the original-file SHA.
* `chunk_digest` (64 hex) = SHA-256(canonical_json([source_ref, source_version, layer_id, start, end, chunker_version])).
  `chunk_ref` = `"chk:" + digest[:32]`. The store keeps the full digest and
  rejects a ref whose digest differs (collision guard).
* `entity_id` = `"ent:" + SHA-256("ent\0"+source_ref+"\0"+normal_form(name))[:24]` (per source).
* `normal_form`: index/entity/tag normalisation only (NFKC, casefold, ک/ی folding,
  digits to ASCII, harakat/tatweel dropped, ZWNJ->space, whitespace collapsed).
  Never applied to stored text, hashes, offsets or quotes.
* `source_ref` is stable; `source_version`/`chunk_ref` move when text moves.
  Looking up an old `chk:` after the source changed -> `E_STALE_SOURCE`;
  after deletion -> `E_NOT_FOUND`. Stale text is never returned as current evidence.
* Golden vectors: `tests/enrichment/fixtures/golden_vectors.json`.

## 4. Chunking (`chunk-1`, normative)

Offsets: zero-based code points into the decoded layer text; `start_char`
inclusive, `end_char` exclusive. Invariant (tested for every chunk):
`chunk.text == layer.text[start_char:end_char]`. Chunks of a layer **partition**
`[0, len(text))`: no gap, no overlap, no stripping, no normalisation.

1. `len(text) <= SOFT_MAX (4000)` -> exactly one chunk `[0, len)`. Empty text -> no chunk.
2. Otherwise split the text into **blocks** at boundaries that start at: a heading line
   (`^#{1,6} `), the first line after a blank-line run (paragraph start), the start of a
   contiguous table (`|`-led lines, kept whole), a page/slide marker line
   (`^## Page \d+` / `^## Slide \d+`, as emitted by `to_md.py`), and the end of a leading
   YAML front-matter block (`---` … `---`). Line terminators are `\n`; `\r` stays part of
   the line (a `\r\n` pair is never split). Boundaries are offsets, blocks are never edited.
3. Greedy: append blocks while the chunk stays `<= SOFT_MAX`; cut before the block that
   would exceed it. A single block with `SOFT_MAX < len <= HARD_MAX (6000)` becomes its own chunk.
4. A block `> HARD_MAX` is split: at the last sentence boundary (`[.!?؟۔…]` or `\n`
   followed by whitespace/EOL) at or before `SOFT_MAX` from the piece start; if none,
   hard cut at `SOFT_MAX` (shifted +1 to keep `\r\n` together). Every piece `<= SOFT_MAX`.
5. A final chunk shorter than `MIN_TAIL (400)` merges into its predecessor when the sum
   is `<= HARD_MAX`.
6. No chunk exceeds `HARD_MAX`. `OVERLAP = 0`; use `prev_chunk_ref`/`next_chunk_ref`
   (within a layer; chunk `index` is global per artifact in layer order, first-to-last).
7. Chunks inherit the artifact `quality_flags`; if extraction is partial (`needs_ocr`,
   `extraction_partial`, `row_limit`) every chunk carries the flag and the artifact has a
   `coverage_warnings` entry (`to_md` caps XLSX at 200 rows; PDFs may need OCR).
8. Chunker output is deterministic: same layers + policy -> identical refs and offsets.
   A golden test fixes `long_text()` boundaries after WP-A lands (A produces
   `fixtures/golden_chunks.json`; AGENT-0 reviews).

## 5. Annotation (candidate metadata)

Schema: `schemas/annotation.schema.json` (inlined into the submit tool). Limits in
`contract.LIMITS`. Fields: `summary`, `topics`, `tags`, `categories`, `message_role`,
`entities[{name,kind,aliases,mention{chunk_ref,text}}]`, `links[{relation,target,evidence[{chunk_ref,quote}],note}]`,
`chunk_annotations[{chunk_ref,summary,topics,tags}]`, `uncertainty`, `producer_reported`.
At least one field is required; **partial annotations are valid** (one page's chunk
annotations without an artifact summary).

Validation (AnnotationService, deterministic, no model; all of these reject the whole
request and leave the previous revision untouched, error `E_VALIDATION` with
`reasons=[…]` referencing field + ref, never text dumps or paths):

* unknown key / bad enum / bound exceeded / `max_output_bytes` exceeded (`E_TOO_LARGE` for size);
* every `chunk_ref` resolves to a chunk of **this** `source_ref` at **this** `source_version`;
* `mention.text` and each `evidence.quote` are exact substrings of that chunk's text
  (quote min 8, max 500 code points; byte-exact, no normalisation); the server records
  `start_char/end_char` (first occurrence, plus `occurrences`);
* link `target`: `{source_ref}` must resolve through the public ref resolver;
  `{entity}` must name an entity in the same annotation (matched on `normal_form`);
  `{entity_id}` must exist in the store; a link to itself is rejected;
* `supports`/`contradicts` need evidence and are stored as `reading` edges
  (candidate readings; passing validation proves the quote exists, **not** the reading);
* caller never supplies path, authority, actor, canonical status, shell text;
  `producer_reported` is stored as a claim, never as verified identity;
* a summary is never accepted as evidence anywhere (no summary field is quote-checked).

`AnnotationService.submit` validates the raw request, then calls
`ProjectionStore.append_annotation` with the same request **plus** a server-computed
`derived` block (`{"entities":[{entity_id,name,normal,kind,aliases,mention{chunk_ref,start_char,end_char,occurrences}}],
"links":[{relation,edge_kind,src,dst,evidence[{chunk_ref,start_char,end_char,occurrences}]}]}`).
`derived` is excluded from `request_digest` (idempotency compares only caller-supplied content);
the store persists it and answers `entity_exists` from it.

Server stores per revision: `{revision, source_ref, source_version, annotation (validated, normalised copy),
request_digest, idempotency_key, producer_reported, received_at, coverage{annotated_chunks,total_chunks},
review_state='unreviewed'}`. `summary`/`topics`/`tags` apply to the whole artifact and are
inherited by chunks; `chunk_annotations` carry only additions/differences. An artifact summary
is stored with the coverage it was produced under; incomplete coverage is never presented as
a full-artifact summary.

## 6. Store (`_enrichment/store-v1.sqlite3`)

`PRAGMA user_version = 1`, WAL, `foreign_keys=ON`, one writer transaction per mutation,
`BEGIN IMMEDIATE`. Unknown (newer) version -> feature unavailable (`E_SCHEMA_UNSUPPORTED`);
never ALTER/DROP automatically. WP-A documents the exact DDL in `docs/enrichment/store-schema.md`.
Required semantics:

* **projections** keyed `(source_ref, source_version)`; the *current* version per source is
  a pointer; a new `source_version` supersedes and marks older revisions `stale`. Old
  revisions are retained for audit; their chunks are not served as current.
* **chunks** keyed by `chunk_ref` with `chunk_digest` UNIQUE-checked.
* **annotation_revisions** append-only, revision numbers per `source_ref` start at 1.
* **reviews** append-only, one row per human decision on `(source_ref, annotation_revision)`.
* **inventory**: a persistent discovery cursor with `generation`; a pass lists sources in a
  stable order; sources created during a pass are picked up by the next pass; when a pass ends
  a new one starts. Parse failures and no-text sources are recorded (`failed`/`no_text`), not skipped silently.
* **failures**: per source, `attempts`, `last_error_code`, `next_retry_after`; `retry_failed`
  is explicit; transient failures at most 2 attempts with spacing; stable failures need an
  explicit reprocess after a fix.
* **processing_state** per current version: `discovered -> projected -> annotated`, side states
  `failed`, `stale` (an annotation exists for an older version), `no_text`. Review is a
  separate axis per revision: `unreviewed|confirmed|rejected`. Index freshness is a third,
  separate axis (generation). An old indexed version never makes new text look annotated.
* **CAS/idempotency** (`append_annotation`): inside one transaction compare the *fresh*
  source version (re-load from disk through `SourceReader` before commit), then
  `expected_annotation_revision == current` else `E_CONFLICT{current_revision}`;
  stale version -> `E_STALE_SOURCE`; same `(source_ref, idempotency_key)` + same
  `request_digest` -> return the original receipt with `idempotent_replay=true`; different
  digest -> `E_IDEMPOTENCY_CONFLICT`. Two identical texts never merge identities.
* Concurrency tests are required even though R1 runs one worker.
* `export-jsonl` and `verify` are read-only; `backup(path)` uses the SQLite backup API.

## 7. Index and graph (`_search/enrichment/search-v1.sqlite3`)

Derived from the store only; deleting it loses nothing. Built in **generations**:
build into a new generation, then activate atomically (a pointer row / rename);
readers read the active generation and never a half-built one. Incremental refresh
(`IndexBuilder.refresh(batch_limit)`) processes dirty artifacts under a batch/time cap;
a full rebuild is a CLI action only, never inside a query.

Tables (informative): `fts_chunks` (FTS5: `normal_form(text)`, unicode61 remove_diacritics, with
stored `chunk_ref`), `fts_meta` (FTS5 over normal-formed summary/topics/tags/entity names/aliases per
artifact+chunk), `nodes`, `edges`, `generation`. FTS5 is probed at runtime with the real interpreter;
absence disables only the new tools' index path (`index.state="unavailable"`, fallback).

Nodes: `art:<artifact_id>`, `chk:…`, `ent:…`, and existing public refs as destinations (`rec:`/`cap:`/`doc:`).
Tags are indexed but are not nodes.

Edges carry `edge_kind`, `label`, `src`, `dst`, `source_ref`, `source_version`, `annotation_revision|null`, `provenance`:

| edge_kind | labels | origin |
|---|---|---|
| `structural` | `contains` (art->chunk), `next_chunk`, `derived_from` (holding -> derivative text_ref) | deterministic projection |
| `registered` | `wikilink` (`[[id]]` in canonical records), `cites_ref` (an explicit existing record/capture id occurring verbatim in layer text, resolved through refs), bridged edges read from `_search/graph.db` (read-only helper; table/build unchanged) | existing corpus |
| `semantic` | `about`, `mentions`, `part_of_project`, `related_to` | annotation, candidate |
| `reading` | `supports`, `contradicts` | annotation + evidence span, candidate reading |

Similar names never merge entities: nodes are per source; cross-source joining happens at
query time through shared `normal_form` keys/aliases and surfaces as an `inclusion_reason`, or
as an explicit *candidate* same-as suggestion, never as a fused node. Authority is never
inferred from graph topology.

## 8. Retrieval (`brain_retrieve`, normative)

Request: `{query, scope (default all), limit (default 10, max 50), budget_tokens (default 6000, 500..32000), cursor}`.

1. Validate; scope `all` = the two families *captures* and *canonical* (canonical = canonical records,
   source records, derivatives), not all private files. Groups stay separate; no merged ranking.
2. **Direct path** (works with no annotation, no graph): FTS5 chunk text match over `normal_form(query)`;
   plus **metadata path**: FTS over summary/topics/tags/entity names/aliases and explicit refs in the query.
3. **Graph path**: seeds (<= `GRAPH_MAX_SEEDS`=8) come from metadata/entity matches or explicit refs;
   traverse <= `GRAPH_MAX_DEPTH`=2, expand <= `GRAPH_MAX_NODES`=40 nodes. Query starts at a named
   project/concept, never at the densest hub; degree/density is not a relevance signal; a hub guard caps
   per-node fan-out.
4. Fuse direct+metadata and graph lists with **RRF**, `k = RRF_K = 60`, weights from
   `RRF_WEIGHTS` (config + fixture pinned). Raw BM25 scores are never added to graph degree.
5. Reserve >= `DIRECT_MIN_SHARE`=50% of initial capacity for direct candidates when enough exist;
   graph cannot be the only route to a source.
6. Re-check every candidate against the **current** `source_version` on disk; drop stale/deleted and
   report `coverage.stale_dropped`.
7. Return **exact chunk text** (and neighbours when budget allows) in budget; summaries are side metadata only.
   Budget uses `contract.estimate_tokens` AND the char/byte ceilings (`fits_budget`); the response says
   `estimated_tokens` and never calls it an exact token count. Dedupe never hides that several
   routes reached the same source (`inclusion_reason.paths`).
8. Partial index (backfill incomplete / lag): candidates of the legacy search (QMD lexical -> exact,
   explicit, per-source `hit_level="source"`, no invented `chunk_ref`) are added for sources without a
   current projection; `coverage` reports the gap.
9. Index missing/corrupt/FTS5 absent/`BRAIN_ENRICHMENT_RETRIEVAL=0`: explicit fallback to the base lexical
   search, then exact if lexical is unavailable or empty; `path` says which one ran; never silent.
10. `cursor` binds `{query digest, scope, source-freshness token, index generation, offset}`; a generation
    change -> `E_STALE_CURSOR` (restart), results of two generations are never stitched.

Envelope (`brain_retrieve` success):

```
{"ok":true,"query","scope","limit",
 "path":"enrichment"|"fallback-lexical"|"fallback-exact",
 "query_model_calls":0,"uses_candidate_annotations":bool,
 "budget":{"budget_tokens","estimated_tokens","truncated"},
 "index":{"generation":int|null,"state":"ok"|"lagging"|"missing"|"unavailable","lag":{"pending_artifacts":int},"fresh":bool},
 "coverage":{"indexed_artifacts","total_artifacts","source_level_fallback_hits","stale_dropped"},
 "groups":{"canonical":{"count","results":[Hit]},"captures":{"count","results":[Hit]}},   // only the requested families
 "notes":[str],"truncated":bool,"truncation_reason":str|null,"next_cursor":str|null,
 "authority_note":str}
Hit = {"source_ref","text_ref","chunk_ref"|null,"layer_id"|null,"span":{"start_char","end_char"}|null,
       "text":str,"hit_level":"chunk"|"source","tier","authority_level","annotation_revision":int|null,
       "annotation_coverage":{"annotated_chunks","total_chunks"}|null,
       "inclusion_reason":{"paths":["direct_text"|"metadata"|"graph"|"fallback"],"via":[ref],"matched":[str]},
       "rank":int,"flags":[quality flags],"truncated":bool,"neighbors":{"prev_chunk_ref","next_chunk_ref"}|null}
```

For an ingested file the hit reports the **derivative's** tier/level (5), never the original's;
the original's SHA is available on the artifact, and extraction warnings ride with the hit.

## 9. Extension tools (names/schemas frozen in `tools_spec.py`)

`BRAIN_ENRICHMENT=off`: exactly the seven base tools (baseline tests unchanged).
`on`: the same seven + the four below. The base `brain_surface.contract.TOOLS` is **not** edited.
Unknown arguments -> `E_UNKNOWN_ARGUMENT` (same dispatch rule as `BrainSurface.call`). Write tool
(`brain_enrichment_submit`) additionally needs `BRAIN_ENRICHMENT_WRITES=1` else `E_DISABLED`.
Errors never include paths, secrets or tracebacks; they carry the ref/stage.

* `brain_enrichment_pending {limit<=5 (default 1), cursor, retry_failed}` -> read-only for corpus/annotations;
  first runs a bounded deterministic discovery/projection refresh (inventory cursor in the store), then
  returns items:
  `{"ok":true,"items":[{source_ref,source_version,artifact_id,source_kind,source_tier,origin_channel,layers[public],quality_flags,coverage_warnings,
  "current_annotation_revision":int,"expected_annotation_revision":int,"coverage":{annotated_chunks,total_chunks},
  "existing_annotation":obj|null,"page":{"chunks":[{chunk_ref,text_ref,layer_id,index,start_char,end_char,chars,text,prev_chunk_ref,next_chunk_ref,"annotated":bool}],"offset","returned","total","truncated"}}],
  "total","returned","truncated","next_cursor","discovery":{pass,inventory_generation,scanned,projected,failed,no_text},"authority_note"}`.
  A page of text is capped at `max_input_chars`; a long artifact continues via `next_cursor` (cursor
  carries source_ref + source_version + chunk offset; version change -> `E_STALE_CURSOR`).
  Already-current annotated sources are not offered again (unless coverage is partial).
* `brain_enrichment_submit` -> `{"ok":true,"persisted":true,source_ref,source_version,annotation_revision,idempotent_replay,coverage,processing_state,index_dirty:true,authority_note}`.
* `brain_artifact_read {source_ref, chunk_ref?, cursor?}`:
  without `chunk_ref`: `{"ok":true,"artifact":{envelope public + current_annotation_revision},"annotation":obj|null,"coverage","review":{state,revision},"chunks":{items:[chunk meta without text],total,returned,truncated,next_cursor},"authority_note"}`;
  with `chunk_ref`: `{"ok":true,"chunk":{…with text},"layer":{public},"source":{source_ref,source_version,tier,authority_level},"annotation_for_chunk":obj|null,"authority_note"}`.
  A `chk:` is valid only here and in `brain_retrieve` output; `brain_read`/`brain_propose`
  grammar is unchanged. A chunk of a different `source_ref` -> `E_BAD_REF`.
* `brain_retrieve` as section 8.

For old `brain_propose` evidence: pass `text_ref` + a real verbatim quote of that text; the
`source_ref` of a source record counts as a quote carrier only if the quote is literally in that
record. A model summary or a `chk:` is never evidence.

## 10. Configuration

`BRAIN_ENRICHMENT=off|shadow|on` (default off), `…_WRITES=0|1`, `…_RETRIEVAL=0|1`, `…_BATCH_SIZE`
(1..5), `…_MAX_INPUT_CHARS` (default 24000), `…_MAX_OUTPUT_BYTES` (default 65536).
`EnrichmentConfig.from_env` fails safe (invalid -> default + warning). Shadow builds sidecar via CLI
only; it exposes no tool. No model API key appears in server env, ever.

## 11. Review and authority

* Review decisions are rows in the store written only by the local CLI
  (`ProjectionStore.record_review(request, human_context)`); it refuses when `BRAIN_REMOTE_SESSION`
  is set or when `human_context` lacks a resolved local actor. `canonical_promotion` is always `false`;
  `confirmed` means "the user checked this interpretation", nothing more.
* `confirmed` never raises tier/authority of the source or of the model; `rejected` hides that revision
  from `uses_candidate_annotations` retrieval but keeps it for audit.
* Promotion of canonical content stays `brain_review.py` (human CLI). A capture-only candidate does not
  become a proposal with fabricated evidence (author-confirmation path is R1.1/G2).

## 12. Error codes

`E_BAD_ARGUMENTS`, `E_UNKNOWN_ARGUMENT`, `E_BAD_REF`, `E_NOT_FOUND`, `E_STALE_SOURCE`,
`E_CONFLICT`, `E_IDEMPOTENCY_CONFLICT`, `E_STALE_CURSOR`, `E_BAD_CURSOR`, `E_TOO_LARGE`,
`E_VALIDATION`, `E_UNAVAILABLE`, `E_DISABLED`, `E_REVIEW_FORBIDDEN`, `E_SCHEMA_UNSUPPORTED`,
`E_INTERNAL` (`contract.ERROR_CODES`, `EnrichmentError`). Tool envelope: `{"ok":false,"error":{code,message,…}}`
and, for the write tool, `"persisted": false` when nothing was stored.

## 13. Ownership and rules of engagement

* Exclusive file ownership per work package is in the WP table of the work-package plan; shared files
  (`.gitignore`, `.env.example`, `remote_mcp/adapter.py`, package `__init__` wiring, validators,
  `AGENTS.md`, `CLAUDE.md`, `SYSTEM_DESIGN.md`, CI workflow, `contract.py`, `config.py`,
  `tools_spec.py`, `schemas/`, `tests/enrichment/{conftest,support}.py`, fixtures) belong to AGENT-0.
  Need a change? Write `docs/enrichment/requests/<wp>-<topic>.md` with the proposed patch; do not edit.
* Real corpus, `_originals/`, auth state, Compose overrides, VPS: out of bounds. Tests use
  `enrichment_wiki` (throwaway git wiki).
* No new service, port, volume, daemon, model dependency or API key. Stdlib + packages already in
  `requirements*.txt`. Nothing under `scripts/brain_enrichment/` imports a model SDK.
* Never skip/disable a test or add a validator baseline exception to get green. Report PASS/FAIL/SKIP
  with reasons.
* Baseline at freeze (python 3.13, no `qmd`): 541 passed, 20 skipped, 3 failed
  (`test_brain_fixtures[01-retrieve-prior-context]`, `[05-provisional-relation]`,
  `test_remote_semantic::…end_to_end`: all need the `qmd` binary). New code must not add failures.

## 14. Deliberately deferred (not R1)

Embeddings / LightRAG (R2, gated by benchmark); Telegram file/voice bridge repair (R1.1 / WP-F);
UI-based human review (WP-G); personal-statement author confirmation (WP-G2); proposal/history
backfill; multi-worker leases; storage engine changes.
