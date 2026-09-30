# Remote MCP transport and authentication (Agent A)

Scope: transport and owner authentication only. Tool semantics are behind
`adapter.py`; Agent B supplies the `brain_*` surface, Agent H integrates.

## Chosen stack

MCP Python SDK **2.2.0** (MIT), low-level `Server.streamable_http_app`, plus
`owner_auth.py` (about 300 lines) implementing the SDK's
`OAuthAuthorizationServerProvider`. The SDK already provides protected-resource
and authorization-server metadata, dynamic client registration, `/authorize`,
`/token` with PKCE S256, `/revoke`, and bearer enforcement.

Run dependencies: `pip install -r requirements-remote.txt` (adds starlette,
uvicorn, httpx2, pydantic, PyJWT, anyio, sse-starlette transitively). No
Redis, database, broker, external IdP, or local model.

## Spike comparison (evidence gathered in this session)

| Option | Finding | Verdict |
|---|---|---|
| SDK v2 wrapping existing functions | Ships AS routes, DCR, PKCE, revoke, bearer middleware. Only the provider (state + owner consent) is missing. 11 tests pass with the official client. | **Adopted** |
| FastMCP 4.0.10 (Apache-2.0) | Same `mcp` SDK underneath (+ fastmcp-slim, authlib, cyclopts, key-value libs). Its built-in provider is `InMemoryOAuthProvider`, documented for testing; production providers proxy an external IdP (GitHub, Google, Auth0...) or only verify JWTs. Does not remove the owner-consent glue; adds an IdP dependency. | Rejected |
| Supergateway 4.0.0 (Node) | stdio to streamable-HTTP bridge. Auth flags (`--oauth2Bearer`, `--header`) are outbound only; no server-side authorization, no OAuth AS/DCR. Auth would become a second stack in front. Package declares no `license` field in package.json. | Rejected |
| Pocket ID | Not needed: the SDK-based owner consent removes the need for an external IdP. Revisit only if multi-user or passkey login becomes a requirement (it is not). | Not used |

## Auth flow

1. Claude calls `POST /mcp` without a token: `401` with
   `WWW-Authenticate: Bearer ... resource_metadata=<public>/.well-known/oauth-protected-resource/mcp`.
2. Claude reads the metadata, discovers the AS, registers via DCR
   (`/register`). Only redirect URIs in the allowlist are accepted
   (default: `https://claude.ai/api/mcp/auth_callback`,
   `https://claude.com/api/mcp/auth_callback`).
3. Claude opens `/authorize` (PKCE S256). The server redirects the owner's
   browser to `/owner/login?p=<one-time id>`, which shows the client name and
   return host and asks for `BRAIN_OWNER_SECRET`.
4. Correct secret: a one-time code (300 s) is minted and the browser returns to
   Claude. Wrong secret: 401; after 5 failures in 300 s the form answers 429.
5. `/token` issues an opaque access token (1 h, in memory, bound to
   `resource=<public>/mcp`) and a rotating refresh token (30 d).
6. Restart: clients and SHA-256 hashes of access and refresh tokens persist
   in `$BRAIN_STATE_DIR/oauth_state.json` (mode 0600, directory 0700), so a
   connected client continues without a new consent.

Unauthenticated callers reach only `/healthz` (`{"ok":true}`), OAuth
endpoints, and the login form. `tools/list` and status require a token.
At most 64 pending consents and 32 registered clients are kept.

## Tool surface (integrated)

`load_adapter()` returns exactly the seven semantic tools
(`brain_search`, `brain_read`, `brain_capture`, `brain_ingest_file`,
`brain_reconcile_context`, `brain_propose`, `brain_status`) from `scripts/brain_surface` over the one
`WikiBackend` (search: `search_lexical`; capture: `wiki_capture` + K9;
proposals/reconciliation/status: Agent E modules). `BRAIN_MCP_ADAPTER`
accepts only `semantic` (default) or `legacy`; `legacy` (the stdio `wiki_*`
tools) is development-only and additionally needs
`BRAIN_UNSAFE_REMOTE_LEGACY=1`. Arbitrary module imports are not supported.
`REMOTE_DENYLIST` removes review, path-based read/media and QMD tool names
from any adapter. Handlers are blocking; the transport runs them in worker
threads; a result `{"ok": false}` is returned with `isError`.

The server runs stateless streamable HTTP (no MCP session to lose on
restart), refuses to start unless `/mcp` is wrapped by the bearer
middleware, and sets `BRAIN_REMOTE_SESSION=1` for its process.

## Run

```bash
export BRAIN_PUBLIC_URL=https://brain.example.com   # exact public origin, TLS terminated in front
export BRAIN_OWNER_SECRET='<>=16 chars, from your secret store>'
export BRAIN_STATE_DIR=/var/lib/0xbrain/auth        # persistent OAuth state, outside the repo
python scripts/remote_mcp/server.py                 # binds 127.0.0.1:8787 (BRAIN_HOST/BRAIN_PORT)
```

Deployment (Docker + Caddy) sets all of this: `docs/INSTALL.md`. Connect
Claude with `https://brain.example.com/mcp`: `docs/CONNECT_CLAUDE.md`.
Security review: `docs/SECURITY.md`.

## Tests

- `tests/test_mcp_stdio_characterization.py`: pins the local stdio behavior.
- `tests/test_mcp_remote.py`: official client over real HTTP (OAuth flow,
  DCR allowlist, lockout, rotation/revocation/restart persistence, denylist,
  fail-closed adapter selection).
- `tests/test_remote_semantic.py`: the production surface end to end over
  HTTP on a real git wiki (exact seven tools, capture/search/read/reconcile/
  propose, K9 commits) plus PKCE, code reuse, open redirect, resource and
  scope binding, state-file privacy, bounded state, startup fail-closed,
  remote-session review refusal.
- Real official-Claude connection: `docs/CONNECT_CLAUDE.md` (release gate).

A regression guard exists for a fail-open trap: the low-level SDK app mounts
`/mcp` unauthenticated unless `token_verifier` is passed explicitly
(`test_unauthenticated_gets_nothing`).
