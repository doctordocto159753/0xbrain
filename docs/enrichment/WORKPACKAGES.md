# R1 work packages (ownership and acceptance)

Branch base: freeze commit of `claude/nice-fermat-lhs7lq`. Agents work on local
branches `feature/enrichment-{a-projection,b-annotation,c-retrieval,d-surface,e-upgrade-qa}`
in separate worktrees; AGENT-0 merges them into `integration/enrichment-r1` and
then into the session branch. Order: contract -> A -> B -> C -> D -> E -> wiring/docs.

| WP | Exclusive new files | Depends on |
|---|---|---|
| A projection/store | `scripts/brain_enrichment/{source,chunking,store}.py`, `refs.py` (chunk-ref resolver), `tests/enrichment/test_projection.py`, `test_store.py`, `test_chunking.py`, `docs/enrichment/store-schema.md` | CONTRACT-E1 |
| B annotation/Hermes | `annotation.py`, `harness/hermes/**`, `tests/enrichment/test_annotation.py`, `test_harness_contract.py`, `docs/enrichment/hermes.md` | store interface (use `FakeProjectionStore` until A lands) |
| C graph/retrieval | `graph.py`, `retrieval.py`, `indexing.py`, `tests/enrichment/test_graph.py`, `test_retrieval.py`, `test_indexing.py`, `docs/enrichment/retrieval.md` | projection + annotation fixtures |
| D extension/CLI | `surface.py`, `cli.py`, `review.py`, `tests/enrichment/test_surface.py`, `test_review.py`, `test_cli.py`, `docs/enrichment/usage.md` | services of A/B/C (fakes first) |
| E upgrade/QA | `scripts/upgrade-enrichment.py`, `scripts/enrichment-backup.py`, `tests/enrichment/test_upgrade.py`, `docs/enrichment/upgrade.md` | manifest + fixtures |

Acceptance (summary; full text in the plan):

* A: zero source mutation; offsets/`chunk.text == layer.text[s:e]` for CRLF/Persian/escaped headings;
  changed/deleted source -> stale; duplicate captures stay two artifacts; crash mid-transaction leaves a healthy
  store; future schema fails only the feature; resumable bounded scan; read-only `status/verify/export-jsonl`;
  SQLite-backup-API snapshot.
* B: provider down -> capture/ingest unaffected; retry creates no duplicate input/annotation; unconfirmed
  transcript never auto-captured; validator rejects forged quotes/spans/refs and server-owned fields;
  CAS + idempotency through the store interface; Hermes skill + job template ship disabled.
* C: "فیت پلن" capture found without a ref; fa/en alias found; graph never hides text; supports/contradicts
  only as candidate; summary never a quote; atomic generation activation; bounded refresh; explicit fallback.
* D: off = 7 tools, on = 7+4; unknown params rejected; review refused in remote context; one ref gives the
  same text and version in Claude and Telegram paths; no MCP tool can approve/promote.
* E: preflight (read-only, redacted), bounded code merge manifest, backup incl. dirty writes + enrichment store,
  rollback of code+image only (data kept), no global compose down / recursive chown, no-touch assertions.
