# Reconciliation packages (K5)

`brain_reconcile_context(seed_ref?, query?, depth, cursor?, sections?, expand?)`
assembles the evidence neighbourhood of a record, capture or query so Claude
(or a human) can judge whether new material conflicts with, refines, or
supersedes what exists. Implementation: `scripts/reconcile_context.py`
(Agent E), reusing the rebuildable graph index (`build_graph_index`), the
BFS neighbourhood of `context_pack`, the proposal queue, the capture parser
and the materials freeze state. Read-only; no model call; **no verdict**
(`verdict: null` always).

## Depth

- `focused`: the seed plus its direct (1-hop) neighbourhood, lexical hits of
  an optional query, all sections, one page.
- `deep`: multi-hop until exhausted (safety ceiling 8 hops), paged (50 items
  per page by default).

## Sections

`canonical_records`, `claims`, `relations`, `source_records`, `captures`,
`superseded` (status superseded/retired/deprecated/withdrawn),
`open_proposals`, `unresolved` (dangling links, open/disputed statuses),
`chronology` (genesis and handoff documents, flagged `related`).

Every item has `id`, `ref` (public; null for unresolved), `authority_level`,
`reason` (why it is included), `distance` (graph hops), `title`, `status`
and an `excerpt`, or `content` when listed in `expand`.

## Order, paging, truncation

- Deterministic order within a section: authority level, graph distance,
  id; sections in the fixed order above.
- `total` / `returned` / `offset` over the whole package; per-section
  `total`. `next_cursor` continues; it is bound to the arguments and to a
  fingerprint of the corpus, so a change yields `E_STALE_CURSOR` instead of
  silently skipping or repeating items.
- `truncated: true` + `truncation_reason` only when a safety bound cut
  something (`max_hops_safety_limit`, `content_byte_limit`); paging is not
  truncation. There is no product-level token or record cap.
- `materials_changed_since_freeze` reports drift of the materials register
  since the last reconcile freeze.
