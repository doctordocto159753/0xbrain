# Auth / transport seam (deployment side)

Status: provisional. Agent A selects the MCP/auth stack; this file is the contract the
deployment layer already implements, so integration is configuration, not rework.

## What the deployment expects from the brain server

| Item | Value |
|---|---|
| Start command | auto-detected in `deploy/entrypoint.sh`: `BRAIN_SERVER_CMD`, else `python -m brain_server` (`brain_server/__main__.py`), else `python -m brain_mcp` (`scripts/brain_mcp/__main__.py`). `PYTHONPATH` = `/wiki:/wiki/scripts`. |
| Listen | `0.0.0.0:$BRAIN_PORT` (8080) plain HTTP inside the compose network only. TLS terminates at Caddy. |
| Liveness | `GET /healthz` returns 2xx with **no data** (no counts, versions or index state). Only the container healthcheck and installer use it. `brain_status` stays behind auth (K6). Path overridable with `BRAIN_HEALTH_PATH`. |
| Working dir / repo | `/wiki` (bind mount of the instantiated repo, `.git` included). |
| Writable state | `/wiki` (only noncanonical zones by K9), `/state/auth` (auth state; persistent, backed up), `/state/qmd` (rebuildable), `/tmp` (tmpfs). The root filesystem is read-only. |
| Runs as | numeric uid/gid of the repo owner (`BRAIN_UID`/`BRAIN_GID`), no capabilities, `no-new-privileges`. |
| Git | `GIT_AUTHOR_*`/`GIT_COMMITTER_*` set; `core.hooksPath=.githooks` and `safe.directory=/wiki` injected through `GIT_CONFIG_*`. |
| Search | `qmd` on `PATH`; collection `wiki` exists; index under `$XDG_CACHE_HOME/qmd`. |

## Environment provided (from `.env`)

`BRAIN_DOMAIN`, `BRAIN_PUBLIC_URL` (issuer / resource URL base), `BRAIN_OWNER_EMAIL`,
`BRAIN_PREFIX`, `BRAIN_WIKI_NAME`, `BRAIN_SECRET_KEY` (random 32 bytes hex, for token signing or
sessions), `BRAIN_OWNER_SETUP_TOKEN` (random one-time bootstrap secret for owner enrolment),
`BRAIN_AUTH_STATE_DIR=/state/auth`. `BRAIN_AUTH_*` beyond these are defined by A; add them to
`.env.example`, and they flow to the container through `env_file`.

## Reverse proxy behavior

Caddy forwards **everything** on `BRAIN_DOMAIN` to `brain:8080` without path filtering and with
response buffering disabled (`flush_interval -1`) so streamable HTTP/SSE works. That covers the MCP
endpoint, `/.well-known/oauth-*`, and `/authorize`, `/token`, `/register` if A's server hosts the
authorization server itself.

## The two auth topologies, both wired

1. **Built-in** (A's server is both resource and authorization server): no extra service.
   Default. Install with only `--domain`.
2. **External IdP**: `./install.sh --auth-domain auth.example.com --auth-image <image>`. Adds
   compose profile `auth` (service `auth`, data in `$STATE/auth-idp`, internal port
   `BRAIN_AUTH_PORT`, default 1411) and a Caddy site `auth.caddy` for the auth domain.
   Image, port, data path (`BRAIN_AUTH_IDP_DATA_PATH`) and IdP-specific env are set once A
   chooses. Exercised in tests with a stand-in image only; no real IdP was validated.

## What A must confirm at integration (open items for H)

- start command/module name and that `/healthz` exists and leaks nothing;
- whether `BRAIN_SECRET_KEY` and `BRAIN_OWNER_SETUP_TOKEN` are the right secrets, or which
  `BRAIN_AUTH_*` replace them (the installer generates secrets once and never rotates them);
- the MCP path printed in Claude steps (`/mcp` assumed);
- extra Python dependencies: add them to `requirements.txt` (E owns) and the image rebuild picks
  them up; no change to the Dockerfile is needed.
