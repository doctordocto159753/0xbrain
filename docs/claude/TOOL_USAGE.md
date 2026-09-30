# Tool reference and usage patterns (K3)

Six tools, names and arguments frozen. Schemas: `scripts/brain_surface/contract.py`
(`TOOLS`). Every result is a JSON object with `ok`; failures carry
`error: {code, message}` and, for writes, `persisted: false`.

## What the server does and what Claude does

| Server (deterministic) | Claude |
|---|---|
| validates arguments, rejects paths, unknown arguments, bad refs | chooses queries, refs, kinds |
| lexical BM25 / exact search, grouped by scope | reformulates queries, judges relevance after reading |
| stores captures and proposals, reports `persisted` / `commit_state` | reports exactly that, never more |
| verifies quote-in-record for proposals, rejects with reasons | supplies quotes and the reading they support |
| returns graph neighborhoods with reasons, ordered and paged | decides conflict / supersession / relation type, as candidate |
| labels tiers and authority levels | keeps evidence classes apart in prose |

The server makes no semantic verdict and runs no model.

## brain_search(query, mode?, scope?, n?)

- `mode`: `lexical` (default, keyword, not semantic) or `exact` (verbatim substring).
- `scope`: `canonical` (default), `captures`, `all`. `all` returns
  `groups.canonical` and `groups.captures` separately; there is no merged rank.
- Result items: `ref`, `zone`, `tier`, `authority_level`, `title`, `snippet`,
  `relevance` (attention only). `notes` may say why hits were dropped or that
  zero hits do not prove absence.
- Pattern: exact term first for names/identifiers; lexical with variants for
  topics; then `brain_read`. Use `scope: captures` to find what the owner said
  recently but has not been reviewed.

## brain_read(ref)

- `ref` only (`rec:`, `cap:`, `prop:`, `hist:` + id). A path is rejected with
  `E_BAD_REF`. The response carries `authority_level`, `content`,
  `truncated` (+ reason). For captures, `media.inspectable: false` means the
  media exists and you cannot see it.
- Read before relying: a snippet or a rank is not the record.

## brain_capture(text, language_hint?)

- Verbatim owner words. Success = `ok && persisted`. Then report `ref`, and if
  `commit_state != "committed"`, the stated state. `duplicate_of` means
  identical text already exists: say so; nothing new was needed.
- Errors: `E_BAD_ARGUMENTS`, `E_TOO_LARGE` (split at natural boundaries),
  `E_UNAVAILABLE`, `E_NOT_PERSISTED`. All mean not stored.

## brain_reconcile_context(seed_ref?, query?, depth?, cursor?, sections?, expand?)

- Needs `seed_ref` and/or `query`. `focused`: direct neighborhood. `deep`:
  multi-hop.
- Sections: `canonical_records, claims, relations, source_records, captures,
  superseded, open_proposals, unresolved, chronology`. Each has `total`,
  `returned`, `offset`, `items`. Items have `id`, `ref`, `authority_level`,
  `reason`, optional `distance`, `title`, `status`. Ordering is deterministic:
  authority level, then graph distance, then id.
- Paging: when `truncated` is true, `truncation[]` names each section and why,
  and `next_cursor` continues. Pass it back with the same other arguments; a
  changed argument or a changed archive gives `E_STALE_CURSOR`: restart.
- `sections` narrows what is returned; `expand: [ref, ...]` adds those
  records' neighborhoods.
- `unresolved` items have `ref: null` (dangling links); everything else has a ref.
- `verdict` is always null. You judge; label it candidate.

## brain_propose(kind, structured_fields, evidence_refs)

Kinds and required fields come from `00-system/policies/proposal_schema.json`
(`intake-registration` is not offered remotely: it needs a filesystem path).

| kind | structured_fields |
|---|---|
| relation-edge | target_id, proposed_type, why |
| claim-amendment | claim_id, amendment, why |
| object-note | object_id, note, why |
| tier-change | target_id, from_tier, to_tier (pending-registration, registered, held), why |
| record-correction | target_id, field, old_value, new_value, why |
| link-repair | target_id, broken_link, repaired_link, why |
| retirement-request | target_id, why, superseded_by |

`evidence_refs`: `[{ref: "rec:...", quote: "<verbatim, >= 20 chars>"}]`. At
least one canonical ref. `cap:` refs may be added as supporting evidence.
Unknown fields are rejected. On rejection read `error.reasons`, repair, and
retry; `persisted` is false and nothing was queued.

Provisional relation: `relation-edge` with `proposed_type: "unclassified"` and a
`why` that describes the observed connection and its basis (relation before
type; candidate status; may-note strength).

## brain_status()

Snapshot id, counts, `pending_needs` (captures awaiting transcription /
description / interpretation), proposal counts, validator state, search state,
`uncommitted` paths. Missing information appears as null and in `missing`,
never as a guess. `degraded: true` means say so before relying on results.

## Error codes

`E_BAD_ARGUMENTS`, `E_UNKNOWN_ARGUMENT`, `E_BAD_REF`, `E_NOT_FOUND`,
`E_TOO_LARGE`, `E_BAD_CURSOR`, `E_STALE_CURSOR`, `E_PROPOSAL_REJECTED`,
`E_UNAVAILABLE`, `E_NOT_PERSISTED`, `E_BACKEND_SHAPE`, `E_UNKNOWN_TOOL`,
`E_INTERNAL`. Transport/auth failures happen before this layer (Agent A); when
a call cannot be made at all, treat it as "connector unavailable".

## Safety bounds (reported, never silent)

Search `n` <= 50 (noted when clamped). Capture <= 500 000 characters
(`E_TOO_LARGE`). Reconcile pages of 40 items per section (configurable;
`truncated` + `next_cursor`, so it is paging, not a cap). Read of very large
files reports `truncated`.
