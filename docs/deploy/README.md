# Deploy 0xBrain on a personal Linux VPS

Target: one small Debian/Ubuntu VPS, a domain you control, about 10 minutes.
Services: `brain` (remote MCP server) and `caddy` (HTTPS); optional `auth` (external IdP).
No database, Redis, vector store, queue, or model of any kind.

## Prerequisites

- A Linux VPS with Docker Engine and the Compose v2 plugin, `git`, `curl`, `openssl`.
  Debian/Ubuntu: `curl -fsSL https://get.docker.com | sh`.
- DNS `A`/`AAAA` record for the brain domain pointing at the VPS, and ports 80 and 443 open
  (ACME needs both).
- Nothing else: Python, Node and QMD live in the image.

## Install

```bash
git clone <your 0xbrain repo URL> /opt/0xbrain/wiki
cd /opt/0xbrain/wiki
sudo ./install.sh
```

The installer asks for four values (or pass them as options; see `./install.sh --help`):

| Value | Meaning |
|---|---|
| brain domain | e.g. `brain.example.com` |
| owner email | ACME contact and owner identity |
| wiki name | recorded in `INSTANCE.json` |
| record prefix | 2-8 chars, e.g. `zb`; becomes the record-ID prefix; cannot be changed later |

It then, in order: checks prerequisites; creates the state directories; builds the image;
instantiates the wiki **only if fresh** (never on an existing one) and commits the bootstrap;
configures model-free QMD; starts Caddy and the brain; waits for health; runs both validators;
prints the Claude steps. Secrets are generated once into `.env` (mode 600, git-ignored) and are
never rotated by a re-run. Re-running `install.sh` is safe.

If the wiki repository is owned by `root`, the installer creates a `brain` system user and hands
the repo to it, so containers never run as root. Use `sudo -u brain git ...` afterwards.

## Where things live

| What | Path | Backed up |
|---|---|---|
| Wiki repo, `.git`, `_originals`, captures, proposals, `.env` | the clone (`BRAIN_DATA_DIR`) | yes |
| Auth state (built-in) / IdP data | `/var/lib/0xbrain/auth`, `auth-idp` | yes |
| Caddy certificates and config | `/var/lib/0xbrain/caddy` | yes (avoids ACME rate limits) |
| Search index | `/var/lib/0xbrain/qmd` | no, rebuildable |
| Backups | `/var/lib/0xbrain/backups` | (they are the backups) |

Captures written by the server are durable on disk the instant they are written, whether or not
their Git commit succeeded (K9); the backup archives the working tree, so uncommitted captures are
included and the archive records how many there were.

## Day-2 commands

```bash
docker compose ps                         # service state
docker compose logs -f brain              # server log
scripts/verify-install.sh                 # in-container: docker compose run --rm brain bash scripts/verify-install.sh
docker compose run --rm brain bash scripts/refresh-search.sh   # reindex (lexical, never embeds)
scripts/create-backup.sh --keep 14        # see BACKUP_RESTORE.md
scripts/upgrade.sh                        # see UPGRADE.md
```

Windows helpers (`*.ps1`) remain for workstation use; `*.sh` are the Linux equivalents:
`setup-after-clone.sh`, `verify-install.sh`, `configure-search.sh`, `refresh-search.sh`,
`create-backup.sh` (plus `restore-backup.sh`, `upgrade.sh`).

## Model-free guarantee

The image contains no model weights and installs no STT/OCR/vision/embedding/reranker package. Search
is QMD lexical (BM25) via `qmd update` and typed `lex:` queries with `--no-rerank`; the scripts never
call `qmd embed`, `vsearch` or a bare `qmd query`. `scripts/verify-install.sh` fails if any `.gguf`,
`.onnx` or `.safetensors` file appears in the QMD cache. GPU backends of QMD's llama.cpp runtime are
pruned from the image because lexical search never loads it.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| installer: "did not answer (DNS/ports/certificate?)" | DNS not pointing here yet, or 80/443 blocked. Fix, then re-run `./install.sh`. `docker compose logs caddy` shows the ACME error. |
| brain exits with code 78 | no MCP server found: A's server is not in the repo yet, or `BRAIN_SERVER_CMD` is wrong. `BRAIN_ALLOW_STUB=1` runs a health-only stub for plumbing tests; Claude cannot connect to it. |
| `permission denied` writing captures | repo ownership drifted from `BRAIN_UID`; `chown -R $(grep BRAIN_UID .env|cut -d= -f2) .` |
| Git commit inside the container refused | the repository gate (`.githooks`) rejected it; the capture stays on disk and `brain_status` reports it uncommitted. |
