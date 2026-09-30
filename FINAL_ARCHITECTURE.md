# 0xBrain final architecture (release candidate 1.0.0-rc1)

The Living Wiki kit (governance, validators, capture core, proposal audit,
exports, skills) is the base. The release adds a remote, single-owner,
model-free MCP surface on top of it by reuse, wrapping and configuration;
nothing in the base was rewritten except where a defect required it.

```
official Claude (web / desktop / mobile)
        |  HTTPS, OAuth 2.1 (DCR + PKCE S256 + owner consent), bearer on /mcp
        v
Caddy (TLS, ACME)  --->  brain container  (python scripts/remote_mcp/server.py)
                          |
                          |  MCP SDK 2.2.0 low-level Server, stateless streamable HTTP
                          |  owner_auth.py: single-owner OAuth provider (state: /state/auth)
                          |  adapter.py: fail-closed -> exactly six brain_* tools
                          v
                     brain_surface/
                       contract.py   frozen K3 schemas
                       refs.py       public ref grammar  rec: doc: cap: prop: hist:
                       surface.py    argument shapes, envelopes, authority labels
                       backend.py    WikiBackend (the only production backend)
                          |
       +------------------+--------------------+----------------------+
       v                  v                    v                      v
  search_lexical     wiki_capture          brain_proposals        reconcile_context
  (Agent C)          (Agent D)             evidence_audit          (Agent E, K5)
  QMD BM25 + exact   verbatim capture      (Agent E, K3 propose)   graph + neighbourhood
  model-free guard   state machine         one validator           paged, no verdict
       |                  |                    |
       |                  +---------+----------+
       |                            v
       |                      git_safety (Agent E, K9)
       |                      lock -> durable write -> validate -> pathspec commit
       v                            v
  /state/qmd (rebuildable)    /wiki = the Git repository (code + archive)
                              01-inbox/captures/, _proposals/  <- only remote writes

owner shell (human context)
  scripts/review.sh -> brain_review.py: accept / reject / edit / promote
  promote = full validation -> separate canonical commit (never remote)
```

## Components and ownership

| Layer | Module | Source of truth |
|---|---|---|
| transport + auth | `scripts/remote_mcp/{server,owner_auth,adapter}.py` | Agent A, hardened in H |
| semantic surface | `scripts/brain_surface/{contract,surface}.py` | Agent B |
| public refs | `scripts/brain_surface/refs.py` | H (single boundary) |
| backend wiring | `scripts/brain_surface/backend.py` | H (replaces B's LocalBackend/FakeBackend) |
| search | `scripts/search_lexical.py`, `configure/refresh/search-wiki.sh`, `00-system/configuration/qmd-collections.json` | Agent C |
| capture | `scripts/capture/wiki_capture.py` | Agent D (+ H defect fixes) |
| proposals, review, K9, K5 | `evidence_audit.py`, `brain_proposals.py`, `brain_review.py`, `git_safety.py`, `reconcile_context.py` | Agent E |
| conversion, exports | `scripts/file-to-md/to_md.py`, `export_interchange.py`, `export_public.py` | Agent F |
| deployment | `Dockerfile`, `compose.yaml`, `Caddyfile`, `install.sh`, `deploy/`, backup/restore/upgrade scripts | Agent G (+ H runtime integration) |

## Contracts

- **K3 remote surface:** `brain_search(query, mode, scope, n)`,
  `brain_read(ref)`, `brain_capture(text, language_hint)`,
  `brain_reconcile_context(seed_ref, query, depth, cursor, sections, expand)`,
  `brain_propose(kind, structured_fields, evidence_refs)`, `brain_status()`.
  No path, actor or channel argument; no review/accept/promote; no media.
- **Refs:** `rec:<id>` (record id in a canonical-scope zone), `doc:<20 hex>`
  (id-less canonical-scope file, e.g. derivative), `cap:<capture-id>`,
  `prop:<proposal-id>`, `hist:<doc-stem>`. Identifier alphabet
  `[A-Za-z0-9_-]`, so traversal and paths cannot be expressed; every
  resolution is re-checked to stay inside its zone.
- **K5:** frozen section names, per-item `id/ref/authority_level/reason`,
  deterministic order (authority, distance, id), cursor bound to arguments
  and corpus fingerprint, `truncated` only for safety bounds.
- **K9:** remote writes only `01-inbox/captures/` and `_proposals/`; each
  write is validated and committed alone to the checked-out branch under a
  lock; on commit failure the data stays and `brain_status` reports it;
  canonical promotion is a separate human CLI commit after full validation.
- **Authority levels** (one map, `evidence_audit.AUTHORITY_LEVEL_BY_PREFIX`):
  canonical records/source records 4, derivatives 5, notes 6, captures and
  proposals 7.

## Runtime services and state

| Service | Image | State |
|---|---|---|
| `brain` | `Dockerfile` (Python 3.12, Git, Node 22 + QMD 2.8.3, MCP SDK 2.2.0, conversion libs) | `/wiki` (bind mount: repository), `/state/auth` (OAuth), `/state/qmd` (index), `/state/home` |
| `caddy` | `caddy:2-alpine` | certificates |

No database, cache, queue, vector store, external IdP or model service.
The brain container is read-only, drops all capabilities, runs as the
repository owner, and exposes 8080 only to Caddy.

## Model-free boundary

`search_lexical.assert_model_free` refuses `qmd embed`, `vsearch`, `pull`,
`mcp`, bare `qmd query` and any query without `--no-rerank`. QMD's own MCP
server is never exposed (and no longer registered by default in
`.mcp.json`). No embedding, reranker, OCR, STT, vision or LLM package is in
any requirements file used by the image.

## Local surfaces kept for backward compatibility

`scripts/wiki_mcp_server.py` (stdio `wiki_*` tools) for a filesystem-attached
harness; `/wiki-*` skills; Windows `*.ps1` helpers; `context_pack.py`. They are
not part of the remote surface; the remote legacy adapter exists only behind
`BRAIN_MCP_ADAPTER=legacy` + `BRAIN_UNSAFE_REMOTE_LEGACY=1`.
