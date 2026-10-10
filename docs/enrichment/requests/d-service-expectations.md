# Request d-service-expectations: what WP-D relies on from A, B and C

No contract change is requested; these are behaviours the surface and CLI were
written against (and tested with `tests/enrichment/test_surface.py::RichStore`).
Where a landed implementation differs, tell WP-D / AGENT-0 rather than editing
the surface.

## WP-A (store / source)

1. `ProjectionStore.get()` fills `ArtifactView.projection["current_annotation_revision"]`
   (0 when none) and `ArtifactView.review = {"state", "revision"}` for the current
   annotation revision; `stale=True` / `processing_state="stale"` when annotations
   exist only for an older `source_version`. `review` and the CLI `status` use this.
2. `get_chunk(chunk_ref)` raises `E_STALE_SOURCE` for a chunk of a superseded
   version, `E_NOT_FOUND` otherwise; the returned `Chunk.text` is the exact layer
   slice (the surface cross-checks it against the live `SourceReader` when present
   and answers `E_INTERNAL` on a mismatch).
3. `pending(cursor, limit, retry_failed)` returns contract section 9 items, performs the bounded
   discovery refresh, and applies `config.max_input_chars` / `PAGE_MAX_CHUNKS`
   itself (the factory receives `config`); `PendingPage.discovery` carries
   `{pass, inventory_generation, scanned, projected, failed, no_text}`. The CLI
   `scan` loops `pending(None, 1, retry_failed)` and stops when `discovery.pass`
   changes (pass complete), when nothing moved (idle), or on its round/time budget.
4. `record_review(request, human_context)` accepts `request = {source_ref,
   annotation_revision, decision, note}`, refuses without `human_context["actor"]`
   or with `remote=True`, and should itself re-check that `annotation_revision` is
   still current (the CLI checks first; the store closes the race window).
   `human_context` also carries `os_user`, `tty`, `confirmation`, `via`.
5. `list_current(after, limit)` returns items ordered by `source_ref`, each with
   `review_state` and `processing_state` (the CLI pages it for `status`, `verify`,
   `pending`, `export`).
6. Optional, used when present: `store.verify() -> {"ok": bool, ...}` (merged into
   `cli verify`), `store.status() -> dict` (shown as `store.reported`).
   Full-history `export_jsonl` is not required: the CLI exports current state.

## WP-C (index / retrieval)

1. Optional `IndexBuilder.status() -> {"generation", "state", "lag": {"pending_artifacts"}, "fresh"}`
   so `cli status` can show index generation and lag; without it the CLI shows
   file presence and size only.
2. Optional `IndexBuilder.rebuild() -> IndexReceipt` for `cli index rebuild`
   (a full rebuild is a CLI action only). Without it the command answers
   `E_UNAVAILABLE` and deletes nothing.
3. `RetrievalEngine.retrieve(request)` raises `EnrichmentError("E_UNAVAILABLE" |
   "E_SCHEMA_UNSUPPORTED")` (or a `sqlite3.Error`/`OSError`) when the index cannot
   serve; the surface then falls back explicitly to the base search. Other
   `EnrichmentError`s (`E_STALE_CURSOR`, `E_BAD_CURSOR`) propagate unchanged.

## WP-B (annotation)

`AnnotationService.submit(request)` receives exactly the five caller fields
(`source_ref, source_version, expected_annotation_revision, idempotency_key,
annotation`) after the surface checked their types/patterns and that the
annotation is an object of at most `max_output_bytes`; schema, quote, chunk and
link validation stay entirely in B.
