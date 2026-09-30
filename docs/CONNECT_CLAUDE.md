# Connect official Claude

Evidence status: the full OAuth + MCP flow is proven with the official MCP
Python client over HTTPS through Caddy in a disposable Docker deployment
(`FINAL_TEST_REPORT.md`). A connection from the official Claude apps to a
public host has **not** been performed from the integration environment (no
public DNS/HTTPS host). The one-page procedure below is that release gate.

## 1. Endpoint

```
https://<BRAIN_DOMAIN>/mcp          (install.sh prints it from BRAIN_PUBLIC_URL)
```

`curl -fsS https://<BRAIN_DOMAIN>/healthz` prints `{"ok":true}` (liveness
only). `curl -si -X POST https://<BRAIN_DOMAIN>/mcp` answers `401` with a
`WWW-Authenticate: Bearer ... resource_metadata=...` header and nothing else.

## 2. Add the connector

claude.ai: Settings -> Connectors -> Add custom connector. Name `0xBrain`,
URL from step 1. Claude discovers the authorization server
(`/.well-known/oauth-protected-resource/mcp`,
`/.well-known/oauth-authorization-server`), registers itself (dynamic client
registration; only allowlisted redirect URIs are accepted) and opens the
owner consent page on your domain. Type `BRAIN_OWNER_SECRET`
(`sudo grep ^BRAIN_OWNER_SECRET= /opt/0xbrain/wiki/.env`) and Authorize.
Desktop and mobile use the same account connectors.

The consent page shows the requesting client's name and the host it returns
to; it must be `claude.ai` or `claude.com`. Never type the secret anywhere
else. Five wrong attempts within five minutes lock the form for the rest of
the window.

## 3. Standing instructions

Create a Project (or use personal preferences) and paste the body of
[claude/STANDING_INSTRUCTIONS.md](claude/STANDING_INSTRUCTIONS.md). Attach
nothing else: the archive is reached only through the tools.

## 4. First checks

Ask: "Call brain_status and show the raw result." Expect `ok: true`,
`snapshot`, counts, `search.state: ok`, `uncommitted.count: 0`.
Then: "Capture this verbatim: <a sentence with a unique word>", "search the
captures for <word>", "read that capture".

## Owner smoke without Claude

```bash
export BRAIN_OWNER_SECRET=...        # from .env; never printed
.venv/bin/python scripts/remote_smoke.py --url https://<domain> \
    [--evidence rec:<id> --quote "<verbatim sentence of that record>"]
```

It runs the same OAuth flow (redirect URI = Claude's callback, which it
never follows) and checks: exactly seven tools, status, capture persisted and
committed, search and read find it, a path-style ref is rejected, optional
proposal. `--reuse-only` proves a restart kept the tokens valid.

## Release gate: official Claude end to end (one page)

Run on a public host with real DNS and a public certificate; record date,
Claude client (web/desktop/mobile) and each result.

1. `./install.sh` with the real domain (no `--tls-internal`). From outside:
   `curl https://<domain>/healthz` -> `{"ok":true}`; `curl -si -X POST
   https://<domain>/mcp` -> 401 with `resource_metadata`.
2. Add the custom connector with `https://<domain>/mcp`.
3. Complete the owner consent. If registration fails with
   `invalid_redirect_uri`, read the rejected URI from `docker compose logs
   brain` (POST /register 400) and, **only if it is a genuine Claude callback
   host**, add that exact URI to `BRAIN_ALLOWED_REDIRECTS` (comma list; the
   two defaults must be repeated), `docker compose up -d`, retry. Never add
   a wildcard.
4. In a chat with the connector enabled, confirm the tool list shows exactly
   `brain_search`, `brain_read`, `brain_capture`, `brain_reconcile_context`,
   `brain_propose`, `brain_status`, and nothing that reviews, accepts,
   promotes, or reads paths (`wiki_*`, `get`, `multi_get`, `query`).
5. `brain_status` -> `ok: true`.
6. `brain_capture` a sentence with a unique marker -> `persisted: true`,
   `commit_state: committed`; on the server `git log -1 --stat` shows only
   that capture file.
7. `brain_search` (scope `captures`) and `brain_read` return the marker text
   verbatim.
8. Ask Claude to propose an `object-note` on an existing record with a
   verbatim quote -> `ok: true`, `authority_tier: candidate`; the queue has a
   new line; nothing under `03-objects/` changed.
9. Ask Claude to "mark the capture reviewed" or "read
   03-objects/<file>.md": it must say no such capability exists / the ref is
   rejected.
10. `docker compose restart brain`; in the same chat call `brain_status`
    again. Expected: works without a new consent (tokens persist as hashes).
11. Revoke the connector in Claude settings (or `POST /revoke`); the next
    call must fail with an auth error. (Reconnect before the ingest steps.)

File ingestion (extends the gate; see [INGEST.md](INGEST.md)):

12. Attach a PDF in the Claude chat and say "add this to my wiki exactly".
    Expected (fixture 10): Claude calls no write tool, says nothing was
    added, and asks for an upload at `https://<domain>/upload`. Record
    whether Claude offers any way to pass the attachment bytes to a tool; if
    it does, capture the exact tool call it makes (this would be the first
    evidence for a native attachment channel; until then
    ATTACHMENT_TRANSPORT stays UNVERIFIED).
13. Upload the same PDF at `/upload` (owner secret), paste the shown
    `upload_ref` to Claude and ask it to ingest. Expected: `brain_ingest_file`
    receipt with `ok`, `sha256`, `source_ref`, `derivative_ref`,
    `extraction: complete`, `commit_state: committed`.
14. On the server: `sha256sum _originals/remote-mcp/<id>--*.pdf` equals the
    receipt and `sha256sum` of your local file; `git log -1 --stat` lists the
    original, record, derivative, `CORPUS_STATE.json` and entry pages only.
15. Ask Claude to find a phrase from the document (`brain_search`) and read
    the derivative (`brain_read` on the `doc:` ref).
16. `docker compose restart brain`; ask for `brain_read` of the `source_ref`
    again: the material is still there.

Pass = every step as expected. Direct ingestion from an ordinary Claude
attachment is claimed only if step 12 shows the original bytes reaching the
server, which the current protocol does not provide. Until this is recorded, the release verdict
stays **CONDITIONAL PASS — blocked only on real official-Claude external E2E**.
