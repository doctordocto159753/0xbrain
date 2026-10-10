# Request b-derived-offsets (WP-B to AGENT-0): clarify CONTRACT-E1 section 5 wording

No code change is requested; `annotation.py` already behaves as below. Please
amend the prose of section 5 (and bump nothing, it only fixes ambiguity) so WP-A
(store persistence of `derived`), WP-C (graph/indexing) and WP-D (surface) read
the same thing.

## 1. Offsets in `derived`

`derived.entities[].mention` and `derived.links[].evidence[]` carry
`{chunk_ref, start_char, end_char, occurrences}` where

* `start_char`/`end_char` are zero-based **code-point offsets into the decoded
  layer text** (`chunk.start_char` + offset inside the chunk), the same
  coordinate system as `Chunk.start_char`, so `layer.text[start_char:end_char]`
  equals the mention/quote. They are not chunk-relative.
* the span is the **first** occurrence inside that chunk; `occurrences` counts
  non-overlapping occurrences of the exact string inside that chunk.

## 2. Extra informational key

`derived.validated_at` (ISO-8601 UTC string from the injected clock) is added.
`derived` is excluded from `request_digest`, so it cannot affect idempotency.
Consumers may ignore it.

## 3. `derived.links[]` endpoints

`src` is the annotated artifact's public `source_ref`. `dst` is one of: the
target `source_ref` (`rec:`/`doc:`/`cap:`), or an `ent:` id (a same-annotation
`{entity}` target is resolved to its server-computed `entity_id`; an
`{entity_id}` target is passed through after `store.entity_exists`). Graph code
maps `src` to `art:<artifact_id>`.
`edge_kind` is `reading` for `supports`/`contradicts`, otherwise `semantic`.

## 4. Error shape of E_VALIDATION

`reasons` is a list (capped at 50, `reasons_omitted` gives the rest) of
`{field, code[, ref]}`. `field` is a path such as
`annotation.links[0].evidence[0].quote`; `code` is a fixed machine code
(`not_an_exact_substring_of_chunk`, `unknown_chunk_ref`, `chunk_of_another_source`,
`unknown_entity_id`, `server_owned_field`, `unknown_key`, `bad_enum`,
`too_many_items`, ...); `ref` is a `chk:`/`ent:`/source ref, never text.
A chunk of a superseded version raises `E_STALE_SOURCE` (not a reason).
A chunk of another source is an `E_VALIDATION` reason with code
`chunk_of_another_source` (the contract allowed "E_VALIDATION/E_BAD_REF").

## 5. Store expectations (for WP-A)

`get_chunks(source_ref, source_version)` must raise `E_STALE_SOURCE` for a
non-current version and `E_NOT_FOUND` for an unknown source;
`get_chunk(chunk_ref)` must raise `E_NOT_FOUND` (unknown) or `E_STALE_SOURCE`
(superseded version), and may return a chunk of another source (the service
turns that into a reason). `append_annotation` receives the validated request
plus `derived` and must persist `derived` so `entity_exists` can answer.
