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
6. Restart: clients and SHA-256 hashes of refresh tokens persist in
   `$BRAIN_STATE_DIR/oauth_state.json` (mode 0600); access tokens do not.

Unauthenticated callers reach only `/healthz` (`{"ok":true}`), OAuth
endpoints, and the login form. `tools/list` and status require a token.

## Tool surface at this stage

`load_adapter()` returns the 12 existing stdio tools unchanged (business logic
reused). The transport removes `REMOTE_DENYLIST` (`wiki_mark_capture_reviewed`)
from any adapter, enforcing "remote MCP never performs human review".
Handlers are blocking; the transport runs them in worker threads. Swap the
surface with `BRAIN_MCP_ADAPTER=package.module:callable` returning
`list[ToolSpec]`.

## Run

```bash
export BRAIN_PUBLIC_URL=https://brain.example.com   # exact public origin, TLS terminated in front
export BRAIN_OWNER_SECRET='<>=16 chars, from your secret store>'
export BRAIN_STATE_DIR=/var/lib/0xbrain-oauth        # outside the repo
python scripts/remote_mcp/server.py                  # binds 127.0.0.1:8787
```

Behind a reverse proxy set `BRAIN_FORWARDED_ALLOW_IPS` to the proxy address and
forward `Host`. Register the connector in Claude with URL
`https://brain.example.com/mcp`.

## Real-Claude validation checklist (NOT yet proven)

No public HTTPS endpoint was available in this session, so no official Claude
connector test was run. To prove it:

1. Expose the server over public HTTPS (tunnel or host) and set `BRAIN_PUBLIC_URL` to that origin.
2. `curl -i -X POST $URL/mcp -H 'Accept: application/json, text/event-stream' -d '{}'` returns 401 with `resource_metadata`.
3. `curl $URL/.well-known/oauth-protected-resource/mcp` and `.../oauth-authorization-server` return JSON whose URLs equal `$URL`.
4. In Claude (web and mobile) add a custom connector with `$URL/mcp`; complete the owner login.
5. If the DCR step fails with `invalid_redirect_uri`, read the redirect URI from the server log/response and add it to `BRAIN_ALLOWED_REDIRECTS`; record it here.
6. Confirm tools list appears, one exact search and one text capture succeed, and `wiki_mark_capture_reviewed` is absent.
7. Revoke from the connector settings; confirm the next call is 401.

## Tests

- `tests/test_mcp_stdio_characterization.py` (13): pins current stdio behavior.
- `tests/test_mcp_remote.py` (11): official client over real HTTP; skipped if `mcp` is not installed.

A regression guard exists for a fail-open trap: the low-level SDK app mounts
`/mcp` unauthenticated unless `token_verifier` is passed explicitly
(`test_unauthenticated_gets_nothing`).
