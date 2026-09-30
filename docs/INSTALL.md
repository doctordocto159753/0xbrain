# Install on a personal Linux VPS

Target: one small Debian/Ubuntu VPS, a domain you control, about 10 minutes.
Services: `brain` (Remote MCP server with built-in single-owner OAuth) and
`caddy` (HTTPS). No database, Redis, vector store, queue, external identity
provider, or model of any kind.

## Prerequisites

- Docker Engine with the Compose v2 plugin, `git`, `curl`, `openssl`
  (Debian/Ubuntu: `curl -fsSL https://get.docker.com | sh`).
- A DNS `A`/`AAAA` record for the brain domain pointing at the VPS; ports 80
  and 443 open (ACME needs both).
- Nothing else: Python, Git, Node, QMD and the MCP SDK live in the image.

## Install

```bash
git clone <your 0xbrain repository URL> /opt/0xbrain/wiki
cd /opt/0xbrain/wiki
sudo ./install.sh
```

The installer asks for four values (or pass them; `./install.sh --help`):

| Value | Meaning |
|---|---|
| brain domain | e.g. `brain.example.com` |
| owner email | ACME contact |
| wiki name | recorded in `INSTANCE.json` |
| record prefix | 2-8 chars, e.g. `zb`; becomes the record-ID prefix; cannot be changed later |

Then, in order: checks prerequisites; creates the state directories; writes
`.env` (mode 600, git-ignored) including a generated `BRAIN_OWNER_SECRET`;
builds the image; instantiates the wiki **only if fresh** and commits the
bootstrap; configures model-free QMD search; starts Caddy and the brain;
waits for container health and for public HTTPS `/healthz`; runs both
validators; prints the MCP URL. Re-running `install.sh` is safe: identity and
secret are never rotated and an instantiated wiki is never touched.

If the clone is owned by `root`, the installer creates a `brain` system user
and hands the repository to it, so containers never run as root.

Build-environment options (rarely needed): `--python-base IMAGE` (e.g.
`python:3.12-bookworm` when Debian mirrors are unreachable: it already ships
git), `--build-ca FILE` (TLS-inspecting proxy CA), `--build-network host`
(egress proxy on the host loopback). `--tls-internal` with custom ports is for
disposable tests only.

## Configuration (`.env`)

| Variable | Meaning |
|---|---|
| `BRAIN_DOMAIN`, `BRAIN_OWNER_EMAIL` | Caddy site and ACME contact |
| `BRAIN_PUBLIC_URL` | exact public origin; the OAuth issuer and MCP resource (`<url>/mcp`) |
| `BRAIN_OWNER_SECRET` | owner passphrase typed on the consent page (>= 16 chars) |
| `BRAIN_ALLOWED_REDIRECTS` | optional, exact OAuth redirect URIs (comma list, no wildcards); default: Claude's two callbacks |
| `BRAIN_DATA_DIR` | the repository (code + archive + `.git`) |
| `BRAIN_STATE_DIR` | host state root: `auth/` (OAuth state), `qmd/` (index), `caddy/`, `backups/` |
| `BRAIN_UID`, `BRAIN_GID` | owner of the repository; containers run as this user |
| `BRAIN_GIT_NAME`, `BRAIN_GIT_EMAIL` | identity of the server's noncanonical commits |
| `BRAIN_UPLOAD_MAX_BYTES`, `BRAIN_UPLOAD_TTL`, `BRAIN_UPLOAD_MAX_PENDING` | optional bounds of the `/upload` staging for `brain_ingest_file` (50 MiB, 3600 s, 20) |

Fixed in `compose.yaml` for the brain container: `BRAIN_STATE_DIR=/state/auth`
(the server's persistent OAuth directory), `WIKI_QMD_HOME=/state/qmd`,
`BRAIN_REMOTE_SESSION=1`, `BRAIN_MCP_ADAPTER=semantic`, `BRAIN_HOST=0.0.0.0`,
`BRAIN_PORT=8080`, `BRAIN_FORWARDED_ALLOW_IPS=*` (the port is reachable only
from Caddy on the compose network). `BRAIN_ALLOW_STUB=1` replaces the server
with a health-only stub for plumbing tests; never set it on a real install.

## Where things live

| What | Path | Backed up |
|---|---|---|
| repository, `.git`, `_originals`, captures, proposals, `.env` | `BRAIN_DATA_DIR` | yes |
| OAuth state (clients, hashed tokens); `uploads/` staging (short-lived) | `$BRAIN_STATE_DIR/auth` | yes |
| Caddy certificates | `$BRAIN_STATE_DIR/caddy` | yes |
| search index | `$BRAIN_STATE_DIR/qmd` | no, rebuildable |
| backups | `$BRAIN_STATE_DIR/backups` | (they are the backups) |

## Verify

```bash
docker compose ps                                        # brain healthy, caddy up
curl -fsS https://<domain>/healthz                       # {"ok":true}
curl -si -X POST https://<domain>/mcp | head -1          # 401: nothing without a token
docker compose run --rm brain bash scripts/verify-install.sh
.venv/bin/python scripts/remote_smoke.py --url https://<domain>   # owner smoke, see CONNECT_CLAUDE.md
```

Next: [CONNECT_CLAUDE.md](CONNECT_CLAUDE.md).

## Model-free guarantee

The image contains no model weights and installs no STT/OCR/vision/embedding/
reranker/LLM package. Search is QMD BM25 through `scripts/search_lexical.py`,
which refuses `qmd embed`, `vsearch`, bare `qmd query`, reranking and QMD's
own MCP server. `verify-install.sh` fails if a model file appears under the
QMD home.
