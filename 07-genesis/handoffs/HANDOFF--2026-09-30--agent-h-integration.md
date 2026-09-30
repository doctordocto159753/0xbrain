---
id: wiki-handoff-2026-09-30-agent-h-integration
type: handoff
title: "Agent H: integration of A-G, defect repair, hardening, release qualification"
branch: claude/ecstatic-planck-50c8vo
commit: "9395ed9 (base); integration head in the PR"
corpus_snapshot: wiki-corpus-empty
status: active
created: 2026-09-30
updated: 2026-09-30
schema_version: 1.0.0
---

# Agent H: integration and release qualification

## Changed

- Merged the heads of PRs 3-9 (F, D, E, C, B, A, G) as traceable merge
  commits.
- Fixed capture D1/D2/D3 (`scripts/capture/wiki_capture.py`, tests).
- Wired the K3 surface to C/D/E through one backend and one public ref
  boundary (`scripts/brain_surface/`).
- Made the remote MCP fail closed to six tools (`scripts/remote_mcp/`) and
  hardened auth.
- Integrated deployment with the real runtime (Dockerfile, compose,
  entrypoint, install, backup/restore/upgrade).
- Consolidated documentation under `docs/`.
- Added the final reports: `FINAL_INTEGRATION_REPORT.md`,
  `FINAL_TEST_REPORT.md`, `FINAL_ARCHITECTURE.md`, `RELEASE_NOTES.md`.

## Decisions

- Merge conflict authority as briefed: C for search, D for capture, E for
  governance, B for the surface, A for transport, G for deployment.
- `doc:` ref kind for id-less canonical-scope files.
- `evidence_refs` items are `{ref, quote?}`, mapped onto E's schema without
  expanding it.
- `truncated` means safety bounds only; `next_cursor` means paging.
- Access-token hashes persist; HTTP is stateless.
- Rollback reverts, it never rewinds history.
- `promote` requires `--paths` for non-content files.
- `.mcp.json` registers no server by default.

## Validation

`FINAL_TEST_REPORT.md`:

- pytest 517 passed, 1 opt-in skip (run separately); 0 xfail;
- mutations 102/102; validators PASS; baseline gate clean;
- Docker install, smoke, restart, backup/restore, upgrade/conflict/rollback
  all PASS;
- lexical eval ladder+exact hit@5 67/68;
- no-model proof PASS.

## Unresolved

- The official Claude custom-connector test on a public host was not run
  (no public DNS/HTTPS host). Verdict: CONDITIONAL PASS.
- CCR-4 (`brain_read` offset) deferred.

## Negative constraints

- Do not expose QMD's MCP server, `wiki_*` tools, or review/promote remotely.
- Do not add a local semantic model to fix lexical limits.
- Do not merge to main before the official-Claude gate is recorded.

## Next exact operation

On a public VPS with DNS, run `docs/CONNECT_CLAUDE.md` "Release gate"
steps 1-11 and record the results in the PR. If step 3 reports
`invalid_redirect_uri`, add only that exact URI.
