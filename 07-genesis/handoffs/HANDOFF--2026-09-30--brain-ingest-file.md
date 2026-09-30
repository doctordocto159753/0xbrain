---
id: wiki-handoff-2026-09-30-brain-ingest-file
type: handoff
title: "brain_ingest_file: exact-original held intake"
branch: claude/ecstatic-planck-50c8vo
commit: "5142f23 (base: PR 10 merge); head in the PR"
corpus_snapshot: wiki-corpus-empty
status: active
created: 2026-09-30
updated: 2026-09-30
schema_version: 1.0.0
---

# brain_ingest_file

## Changed

- New: `scripts/brain_surface/{uploads,ingest}.py`, `scripts/remote_mcp/upload.py`,
  `docs/INGEST.md`, `tests/test_ingest_file.py`, fixtures 10-11.
- Modified: surface/contract/backend (seventh tool, status), adapter (seven
  tools), server (upload route, staging dir), owner_auth (shared secret check),
  git_safety (per-write-class path policy), brain_review (`flush-ingest`),
  remote_smoke (`--ingest-file`, `--ingest-phrase`, `--read-ref`), docs.

## Decisions

- ATTACHMENT_TRANSPORT: UNVERIFIED (MCP tool calls carry only JSON arguments;
  Claude documents no attachment forwarding). Owner upload + upload_ref.
- Held intake only (pending-registration); registration stays human.
- Commit blocked only by validator errors the ingest introduced.

## Validation

pytest 563 passed / 1 opt-in skip; validators PASS; mutations 102/102;
Docker E2E: PDF and DOCX ingest over HTTPS, duplicate, restart, backup ->
destroy -> restore with byte-identical original.

## Unresolved

Official Claude connector test (release gate steps 1-16) still pending.

## Negative constraints

No URL/path/base64 ingestion; no OCR/model; never present a Claude
reconstruction as an original.

## Next exact operation

Run `docs/CONNECT_CLAUDE.md` release gate steps 1-16 on a public host.
