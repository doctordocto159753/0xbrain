# Security and authentication

Single owner (K6). Transport + auth: MCP Python SDK **2.2.0**
(`requirements-remote.txt`, exact pin) low-level streamable-HTTP server plus
`scripts/remote_mcp/owner_auth.py`, an SDK `OAuthAuthorizationServerProvider`
with one owner-consent step. No external IdP, no FastMCP, no Supergateway,
no Pocket ID, no database.

## What is reachable

| Route | Auth | Content |
|---|---|---|
| `POST/GET /mcp` | bearer token, scope `brain`, resource `<BRAIN_PUBLIC_URL>/mcp` | the six `brain_*` tools only |
| `/healthz` | none | `{"ok":true}` (liveness only) |
| `/.well-known/oauth-protected-resource/mcp`, `/.well-known/oauth-authorization-server` | none | OAuth discovery |
| `/register`, `/authorize`, `/token`, `/revoke` | OAuth | SDK handlers |
| `/owner/login` | pending-consent id + owner secret | consent form |

Everything else is 404/405. `tools/list` and `tools/call` (including
`brain_status`) without a valid token get 401.

## Properties verified by tests (`tests/test_remote_semantic.py`, `tests/test_mcp_remote.py`)

- Fail closed: the server refuses to start unless `/mcp` is wrapped by the
  bearer middleware (`assert_fail_closed`) and no other mount can reach the
  transport; `BRAIN_PUBLIC_URL` must be https (loopback only for tests);
  `BRAIN_STATE_DIR` is required; wildcard or non-https
  `BRAIN_ALLOWED_REDIRECTS` entries refuse to start; the adapter is the six
  semantic tools unless `BRAIN_MCP_ADAPTER=legacy` **and**
  `BRAIN_UNSAFE_REMOTE_LEGACY=1` (development only).
- Exact redirect allowlist at registration (default:
  `https://claude.ai/api/mcp/auth_callback`,
  `https://claude.com/api/mcp/auth_callback`); an unregistered redirect gets
  an error page, never a redirect (no open redirect).
- PKCE S256 required (missing challenge or `plain` never reaches consent);
  wrong verifier rejected; authorization code single-use, 300 s.
- Access tokens (1 h) and refresh tokens (30 d) are opaque 256-bit values;
  refresh rotates and kills the old family; reuse of a rotated refresh token
  fails; revocation kills the family.
- Tokens are bound to the resource (`validate_token_resource`) and scope: a
  token for another resource gets 401, a token without `brain` gets 403.
- Owner secret compared with `hmac.compare_digest`; minimum 16 characters;
  5 failures per 5 minutes lock the form (global, so an attacker can delay
  but not brute-force the owner; acceptable for one owner).
- Unauthenticated growth is bounded: at most 64 pending consents and 32
  registered clients (idle clients are evicted first).
- State file `oauth_state.json`: mode 0600 in a 0700 directory; contains
  registered clients and **SHA-256 hashes** of access and refresh tokens,
  never a usable bearer token, never the owner secret. DCR client secrets are
  stored as issued because the SDK authenticator compares them directly;
  alone they grant nothing without a code or refresh token.
- The owner secret is never logged (form body only; uvicorn logs path and
  status). `/healthz` leaks nothing.
- Tool errors are returned as `{"ok": false, "error": {code, message}}`
  with `isError`; exceptions are reduced to their type name (no traceback,
  no path).
- The process runs with `BRAIN_REMOTE_SESSION=1`; the human review CLI
  refuses to act there, and no module of the remote surface imports it or
  runs a shell.

## Proxy assumptions

Caddy terminates TLS and forwards every path on the brain domain to
`brain:8080` with the original `Host`. The server's public identity comes
from `BRAIN_PUBLIC_URL`, not from request headers, so forwarded headers
cannot change issuer, resource or redirect behaviour; they only affect the
client address in logs (`BRAIN_FORWARDED_ALLOW_IPS=*` is safe because port
8080 is not published). DNS-rebinding protection allows only the public
host and loopback in `Host`/`Origin`.

## Remote surface boundary

- Six tools, exact (test-enforced). Never remote: `wiki_read`,
  `wiki_get_media`, `wiki_mark_capture_reviewed`, `wiki_propose`, QMD's own
  MCP tools (`get`, `multi_get`, `query`, ...), any review/accept/promote.
- Refs, not paths: `rec:`, `doc:`, `cap:`, `prop:`, `hist:` with an
  identifier of `[A-Za-z0-9_-]`; traversal, absolute POSIX/Windows paths and
  file names are rejected before any filesystem access
  (`scripts/brain_surface/refs.py`).
- Writes only to `01-inbox/captures/` and `_proposals/`, each committed alone
  under a lock (K9). Canonical changes are human-only
  ([PROPOSALS_AND_REVIEW.md](PROPOSALS_AND_REVIEW.md)).
- Status output contains no absolute paths, environment values, tokens or
  secrets.

## Operational advice

Keep `.env` and backups private (they hold the owner secret). Rotate the
owner secret by editing `.env` and `docker compose up -d`; existing tokens
remain valid until they expire or are revoked (delete
`$BRAIN_STATE_DIR/auth/oauth_state.json` and restart to revoke everything).
