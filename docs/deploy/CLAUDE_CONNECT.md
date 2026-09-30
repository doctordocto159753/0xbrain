# Connect Claude to your brain

Status of evidence: the deployment side (HTTPS host, health, persistence) is tested. A connection from
the **official Claude apps to a public host is not proven from this branch**: it needs the real MCP/auth
server from Agent A and a public DNS name. The final public-host test is handed to Agent H (checklist
at the end). Items marked (A) depend on A's chosen stack.

## 1. Remote MCP URL

```
https://<BRAIN_DOMAIN>/mcp        (A: confirm path; printed by install.sh from BRAIN_PUBLIC_URL)
```

Check it is reachable and has a valid certificate: `curl -fsS https://<BRAIN_DOMAIN>/healthz` prints
`ok` (liveness only; no data).

## 2. Auth

Single owner (K6). Unauthenticated requests receive nothing, `brain_status` included.

- Built-in auth (A): Claude opens the authorization page on your domain; you authenticate as the owner
  (email from install). First-time enrolment uses `BRAIN_OWNER_SETUP_TOKEN` from `.env`
  (`grep BRAIN_OWNER_SETUP_TOKEN /opt/0xbrain/wiki/.env`). (A: confirm flow.)
- External IdP: installed with `--auth-domain`; register the owner there first (A: per chosen IdP).

Never paste `.env` values anywhere except the auth page.

## 3. Add the connector in Claude

claude.ai (Pro/Max/Team/Enterprise): Settings, Connectors, Add custom connector; name `0xBrain`; URL
from step 1; complete the sign-in. On Team/Enterprise an owner adds it for the organization first.
Claude Desktop/mobile use the same account connectors. Enable the connector in a conversation
from the tools menu. (Exact menu labels change; the URL and OAuth flow are what matter.)

## 4. Standing instruction

Add the text prepared by Agent B (`docs/claude/`) to a Project's instructions or your profile
preferences. Minimum until B's text is integrated:

> You have the `0xBrain` connector. Use `brain_search` (scope explicit: canonical, captures or all) and
> `brain_read` by ref before answering from the archive. Search rank is navigation, never evidence.
> Store new material with `brain_capture` (text). Propose changes only with `brain_propose`. You never
> promote anything to canonical; that is a human step.

## 5. First status check

Ask Claude: "Call brain_status and show the raw result." Expect: counts, validator status, search-index
freshness, pending `needs_*` captures, and uncommitted noncanonical writes (should be 0 right after
install). Then `brain_search(query="wiki", mode="lexical", scope="canonical")` and one
`brain_capture` of a short test text; confirm the file appears under `01-inbox/captures/` on the server
and `brain_status` shows it committed.

## Checklist handed to Agent H (public-host test)

1. Public DNS to the VPS; `./install.sh` with real domain; `curl https://<domain>/healthz` from outside.
2. With A's server present, `docker compose ps` shows brain healthy; `curl -i https://<domain>/mcp`
   without a token returns 401 and nothing else.
3. Add the connector in official Claude; complete OAuth; run steps 5 above.
4. Record: date, Claude client, result of each call. Until this is done nothing here claims official-Claude
   compatibility.
