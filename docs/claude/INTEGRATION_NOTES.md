# Agent B: integration notes and contract requests

Base: `9395ed9f26729910f5ccc868c8598fad54811e19`, branch `agent/b-claude-surface`.

## What is where

| Path | Content |
|---|---|
| `scripts/brain_surface/contract.py` | frozen K3 schemas (`TOOLS`), ref grammar, enums, authority constants; proposal kinds derived from `proposal_schema.json` |
| `scripts/brain_surface/surface.py` | `BrainSurface(backend).call(name, args)`; validation, envelopes, K5 paging |
| `scripts/brain_surface/backend.py` | `Backend` interface + `FakeBackend` (used by A/H for protocol tests without C/D/E) |
| `scripts/brain_surface/local_backend.py` | adapter over existing `wiki_capture`, `wiki_mcp_server`, `evidence_audit`, `context_pack`, `build_graph_index` |
| `docs/claude/*` | standing instructions, tool usage, connector checklist, source map, fixtures |
| `tests/test_brain_*.py` | 63 tests |

Agent A: register `TOOLS` and route `tools/call` to `BrainSurface.call`; its
return value is the JSON object to serialize as the tool result text (never
raises). Inject a backend: `LocalBackend()` today, C/D/E-backed later.
No existing file was modified.

## Ownership deviation (flagging, not a Contract Change)

The Agent 0 table gives B docs only. The Agent B brief says implement the K3
wrappers with fakes/adapters. B therefore added a new package under
`scripts/brain_surface/` (separate from A's server directory, imports only).
If A prefers to host it under `scripts/brain_mcp/`, it is a `git mv`.

## Final schemas

See `TOOLS` in `contract.py` (also rendered in `TOOL_USAGE.md`). K3 argument
names and enums are exact and asserted by `test_tool_names_and_args_are_exactly_k3`.
`additionalProperties: false` everywhere, so `path`, `channel`, `actor` cannot
be passed.

## Adapter assumptions

1. Authority levels are 1-indexed so that "model output = level 7" (Agent 0 /
   SYSTEM_DESIGN 2.2) holds: canonical records and source records 4, notes 6,
   captures/proposals 7, genesis 4, handoffs 7. Mapping is one dict
   (`LEVEL_BY_ZONE`); E should confirm.
2. `commit_state` is `not_attempted` from `LocalBackend` (K9 commit path is A/E's).
   `uncommitted` in status comes from `git status` over `01-inbox/captures`
   and `_proposals`.
3. `pending_needs` in `LocalBackend.status` is inferred (non-text captures
   without complete transcription -> `needs_transcription`/`needs_description`,
   plus any `quality_flags` starting `needs_`). Agent D's K4 states replace this.
4. Lexical search for canonical requires `qmd` (Agent C's collection config)
   or an injected `search_fn`; without either, `brain_search lexical` returns
   `E_UNAVAILABLE`. Exact mode and captures need nothing. Captures "lexical"
   is casefolded all-terms substring, not BM25 (no capture index exists).
5. Reconcile graph comes from `_search/graph.db` (rebuilt via
   `build_graph_index` when missing or older than a record). `superseded` is
   classified from `status`/`relation_status` containing "supersed"/"retired".
   `chronology` = `07-genesis/*.md` and `_captures/HANDOFF*.md` mentioning an
   included id or the query. These heuristics are adapter-level; a richer
   builder from C/E can replace the backend without touching the surface.
6. Pagination is stateless: the cursor encodes page number + a hash of
   parameters and snapshot; a changed archive gives `E_STALE_CURSOR`.
   Page size (40 per section) and deep hops (3) are constructor settings.
7. Persian text: capture is byte-verbatim (tested with Persian); search does not
   normalise ZWNJ/Arabic-Indic digits/ye-kaf variants. Claude is told to try
   variants. A normalisation layer belongs to C.

## Contract requests (none blocking)

- CCR-1 (E, additive): expose `evidence_audit._check_passage` and the
  per-kind required-field check as a public function; B currently imports the
  private name and reimplements required-field checks in `surface.py`.
- CCR-2 (E, additive): schema support for `supporting_captures` /
  `additional_passages` keys written by `brain_propose` (ignored by the
  current audit, so harmless, but should be blessed) and a decision on whether
  captures may ever be primary evidence (B: no).
- CCR-3 (A/K3 clarification): `intake-registration` is not exposed by
  `brain_propose` because it requires a filesystem path and a hash of an
  original. Confirm this reading of K3's "no arbitrary paths".
- CCR-4 (K3 clarification): `brain_read` has no range argument, so a record
  larger than the read bound is reported `truncated` without a way to fetch the
  rest. If large records occur, add an optional `offset`; not added
  unilaterally.
- Governance text changes for `CLAUDE.md` / `AGENTS.md` (B proposes, E merges):
  `PROPOSED_GOVERNANCE_EDITS.md`.

## Not verified here

No live official-Claude connection exists in this sandbox; `qmd` is not
installed; a live model's adherence to the standing instructions is untested
(fixtures test tools and reference behavior only). Hand off to H:
`CONNECTOR_GUIDE.md` steps 2-5 and the manual half of `fixtures/README.md`.
