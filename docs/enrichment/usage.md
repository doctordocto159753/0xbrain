# Using the enrichment layer (R1): Claude, Telegram (Hermes) and the owner

Normative contract: `CONTRACT-E1.md`. This page is the how-to. Everything the
enrichment layer adds is **candidate-tier navigation** (authority level 7). Only
the returned chunk text, with its refs, is quotable; a summary, tag, entity,
link or retrieval rank never is.

## 1. The eleven tools

`BRAIN_ENRICHMENT=off` (default): exactly the seven base tools, unchanged.
`BRAIN_ENRICHMENT=on`: the same seven plus four. `shadow` builds the sidecar
through the CLI only and exposes no tool.

| tool | who | what it is for |
|---|---|---|
| `brain_search`, `brain_read` | Claude, Hermes | base: find and read by ref (lexical/exact, whole record). Always available. |
| `brain_capture`, `brain_ingest_file`, `brain_propose`, `brain_reconcile_context`, `brain_status` | as before | base: unchanged. |
| `brain_retrieve` | Claude, Hermes | "what do we have about X": exact chunk text, with refs, from captures and the governed archive, with explicit fallback. |
| `brain_artifact_read` | Claude, Hermes | one source as an artifact (layers, flags, candidate metadata, coverage, chunk list), or one exact chunk. |
| `brain_enrichment_pending` | the periodic annotator (Hermes job) | technical work queue: small pages of sources that still need candidate metadata. Not a review queue. |
| `brain_enrichment_submit` | the annotator | record one candidate metadata revision. Needs `BRAIN_ENRICHMENT_WRITES=1`, else `E_DISABLED`. |

There is no tool to review, approve or promote anything. That is the local
human CLI (section 7), by design.

## 2. One ref, one text, one version, in both paths

Claude (remote connector) and Telegram (Hermes) address material with the same
public refs (`cap:`, `rec:`, `doc:`). A `source_ref` returned by any tool means
the same thing everywhere:

* `brain_read(text_ref)` returns the whole text of a layer;
* `brain_artifact_read(source_ref, chunk_ref)` returns a slice of that same
  text, `layer.text[start_char:end_char]`, byte for byte (CRLF, Persian,
  escaped headings included), with the `source_version` it belongs to;
* the layer's `text_ref` tells you which base ref carries that text (for an
  ingested file it is the `doc:` ref of the derivative, whose tier is
  `derivative`, level 5, never the original's);
* `source_version` moves when the text moves; an old `chk:` then answers
  `E_STALE_SOURCE` (changed) or `E_NOT_FOUND` (deleted). Stale text is never
  returned as current evidence. A `chk:` is valid only in the four extension
  tools, never in `brain_read` or `brain_propose`.

## 3. Answering "what do we have about X"

1. `brain_retrieve {"query": "<the owner's words>", "scope": "all"}`. Read
   `path`, `index.state`, `coverage` and `notes` first.
2. Answer **from `groups.*.results[].text`** and cite `source_ref`,
   `chunk_ref`, `tier` and `authority_level`. Say which family (captures are
   unreviewed intake; canonical is the governed side) each statement came from.
3. If a hit matters, `brain_artifact_read(source_ref, chunk_ref)` for the exact
   chunk and its neighbours (`prev_chunk_ref`, `next_chunk_ref`), or
   `brain_read` for the whole record.
4. Treat `annotation_*`, `inclusion_reason` and tags as hints about why it was
   returned. `rank` is attention only. `truncated: true` or `next_cursor` means
   there is more: say so, or page with the cursor (a changed index returns
   `E_STALE_CURSOR`: restart).
5. "Nothing found" means nothing found by these queries and this path.
   Reformulate (Persian/English variants, exact names) before concluding absence.

## 4. The annotator loop (Hermes job, periodic)

```
brain_enrichment_pending {limit}            -> items (+ next_cursor for long sources)
  for each item: read item.page.chunks[].text (exact), item.source_version,
                 item.expected_annotation_revision
brain_enrichment_submit {source_ref, source_version, expected_annotation_revision,
                         idempotency_key, annotation}
  -> {"ok": true, "persisted": true, annotation_revision, coverage, idempotent_replay}
```

* Report success only when the result has `ok: true` **and** `persisted: true`.
  Any error carries `persisted: false`: nothing was stored.
* Reuse the same `idempotency_key` on retry (the original receipt comes back
  with `idempotent_replay: true`); a different payload under the same key is
  `E_IDEMPOTENCY_CONFLICT`.
* Quotes (`evidence.quote`, `mention.text`) must be exact substrings of the
  chunk text you were given; the server records the offsets. A forged or
  paraphrased quote rejects the whole request (`E_VALIDATION`, with `reasons`).
* A long source is read page by page (`next_cursor`); partial chunk coverage is
  allowed and is reported as `coverage {annotated_chunks, total_chunks}`, never
  presented as a whole-artifact summary.
* `E_STALE_SOURCE`: the text changed; call `brain_enrichment_pending` again.
  `E_CONFLICT {current_revision}`: someone annotated meanwhile; re-read.

## 5. Correcting a wrong tag (candidate correction)

Nothing here edits the capture, the source record or an earlier revision.
Revisions are append-only; the newest one is the current one.

1. `brain_artifact_read {source_ref}`: note `artifact.current_annotation_revision`
   (N), `artifact.source_version` and the current `annotation`.
2. Build the corrected annotation (the fields you want to be current) and call
   `brain_enrichment_submit` with `expected_annotation_revision = N`, the same
   `source_version`, and a fresh `idempotency_key`. The result is revision N+1.
3. Read it back with `brain_artifact_read` and check the tag is as intended;
   do not assume fields of revision N are merged in unless you see them.
4. The wrong revision stays for audit. The owner may mark it `rejected` with the
   CLI (section 7); a rejected revision is hidden from candidate-annotation
   retrieval but kept.

## 6. What `confirmed` means

`confirmed` is a decision row written by the owner with the local CLI:
"I checked this interpretation (revision N of source S)". It does **not**
promote anything, raise the tier or authority of the source or of the model,
change the corpus, or turn the annotation into evidence. Every receipt prints
`canonical_promotion=false`. `rejected` hides that revision from
`uses_candidate_annotations` retrieval. Promotion of canonical content remains
`scripts/brain_review.py`, a separate human step.

## 7. Fallback: `brain_search` / `brain_read` always work

The enrichment layer is additive. If the store or index is missing, corrupt,
newer than the code (`E_SCHEMA_UNSUPPORTED`), FTS5 is absent, or
`BRAIN_ENRICHMENT_RETRIEVAL=0`:

* `brain_retrieve` answers anyway, **explicitly**: `path` is `fallback-lexical`
  (QMD BM25) or `fallback-exact`, `index.state` is `missing`/`unavailable`,
  `notes[0]` says why, hits are source-level (`hit_level: "source"`,
  `chunk_ref: null`, `text` is a snippet). Open the hit with `brain_read`
  before relying on it. A cursor from the enrichment path is not honoured here
  (`E_STALE_CURSOR`: restart).
* The other three tools return `E_UNAVAILABLE` / `E_SCHEMA_UNSUPPORTED` for that
  call only. The seven base tools are never affected; use them directly:
  `brain_search` (lexical, then `mode: "exact"`) then `brain_read`.
* Partial index: a fresh capture not yet indexed is still found through the
  source-level fallback hits that `coverage.source_level_fallback_hits` counts.

## 8. The local CLI (owner / operator)

```
cd scripts && python -m brain_enrichment.cli [--root DIR] [--json] COMMAND
python scripts/brain_enrichment/cli.py [--root DIR] [--json] COMMAND      (same thing)
```

| command | effect |
|---|---|
| `status` | enrichment state **next to** the legacy capture workflow: processing_state counts, backlog (awaiting_annotation / failed / no_text), chunk coverage, review states, store schema, index generation and lag, sidecar bytes. `legacy_capture_workflow` (received / needs-review / reviewed ...) is the old human intake queue, **not** the enrichment backlog. Read-only. |
| `inspect REF [--chunk CHK] [--cursor C]` | the `brain_artifact_read` view. Read-only. |
| `pending [--limit N] [--after REF] [--failed]` | read-only list of sources awaiting annotation (does not refresh discovery; the MCP tool does). |
| `verify [--no-freshness]` | chunk partition / digests / links / annotation refs / source_version recomputation; re-reads sources and lists stale or missing ones as warnings. Exit 1 on corruption. Read-only. |
| `export [--out FILE] [--with-text]` | JSONL of the sidecar (current state). `--out` inside the wiki must be under `_enrichment/exports/`. Read-only. |
| `scan [--max-rounds N] [--time-budget S] [--retry-failed]` | bounded, resumable projection pass; writes only `_enrichment/`. Needs `BRAIN_ENRICHMENT=shadow` or `on`. Re-run until `complete: true`. |
| `index refresh\|rebuild [--batch-limit N] [--max-batches N]` | derived search index; writes only `_search/enrichment/`. Same mode gate. |
| `review REF --revision N --decision confirmed\|rejected --note TEXT --actor NAME [--yes]` | the human decision. See below. |

`review` refuses (exit 3, nothing written) when `BRAIN_REMOTE_SESSION` is set,
when the actor is empty or a machine identity (claude, hermes, mcp ...), when
there is no terminal and `--yes` was not given, or when `--yes` is used inside
a known agent harness without a terminal. The recorded actor is
`<name>@<os-user>`. You type `REF@N` to confirm unless `--yes`. If the
annotation moved since you looked (`E_CONFLICT`, exit 4, with
`current_revision`) or the source changed (`E_STALE_SOURCE`), nothing is
recorded. One audit line per attempt goes to `_enrichment/review-audit.jsonl`.
There is no approve or promote command.

Exit codes: `0` ok; `1` finished with findings (verify errors; scan/index not
finished); `2` usage; `3` refused or unavailable; `4` conflict or stale; `5`
not found / bad ref; `70` internal. `--json` prints one JSON object (errors too).

## 9. Configuration

`BRAIN_ENRICHMENT=off|shadow|on` (default `off`), `BRAIN_ENRICHMENT_WRITES=0|1`,
`BRAIN_ENRICHMENT_RETRIEVAL=0|1`, `BRAIN_ENRICHMENT_BATCH_SIZE` (1..5; the
default `limit` of `brain_enrichment_pending`), `BRAIN_ENRICHMENT_MAX_INPUT_CHARS`,
`BRAIN_ENRICHMENT_MAX_OUTPUT_BYTES` (also the `E_TOO_LARGE` limit on an
annotation). Invalid values fall back to the safe default and are listed in
`status.enrichment.config_warnings`. No model API key is ever part of the server
environment.

## 10. What never happens

* no write under `_originals/`, the corpus zones, `_captures/`, `_proposals/`
  or `00-system/` by any tool or CLI command here;
* no model call at query time (`query_model_calls` is always 0);
* no path, secret or traceback in any error; `anchor_path`/`origin_path` never leave the server;
* no tool that reviews, approves or promotes; no `chk:` accepted as evidence
  by `brain_propose`.
