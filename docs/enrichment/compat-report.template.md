# Compatibility report (fill in before the first live step)

Copy this file somewhere private (it records environment facts, never secrets). For every row
write **VERIFIED** only when you have evidence (a command output, a preflight report field, a
file you opened) and put that evidence in the last column; otherwise write **UNVERIFIED**. A row
left empty counts as UNVERIFIED. Do not paste secret values, tokens or the owner secret.

Date / operator: ____________  Release target SHA (40 hex): ____________  Base SHA: ____________
Preflight report: `preflight.json` (path) ____________  status (PASS/WARN/STOP): ____________

| # | Item | Value | VERIFIED / UNVERIFIED | Evidence |
|---|---|---|---|---|
| 1 | Wiki path (DATA, the git checkout bind-mounted at `/wiki`) | | | `paths.data` |
| 2 | State dir (`BRAIN_STATE_DIR`: auth, qmd, caddy) | | | `paths.state` |
| 3 | Enrichment store location (must be `_enrichment/store-v1.sqlite3`, not under `_search`) | | | `paths.enrichment_store` |
| 4 | Service name and container (name, id) | | | `container.*` |
| 5 | Compose project name | | | `compose.project` |
| 6 | Compose files, **in merge order** (base, override...) and whether an override defines/changes `brain` | | | `compose.files[*]`, `compose.override_after_base` |
| 7 | Env file used by compose (path only) | | | `compose.env_file` |
| 8 | How the files were discovered (container labels / flags / COMPOSE_FILE / guessed names) | | | `compose.source` (guessed = UNVERIFIED) |
| 9 | Image tag, image id, repo digest currently running | | | `container.image_*`, `repo_digests` |
| 10 | Image decision for this release (reuse / new tag `name:tag`) - never overwrite the old tag | | | `plan.image` |
| 11 | uid:gid of the container user, of `BRAIN_UID/GID`, of the wiki, of `auth/`, of `_enrichment/` (they should agree) | | | `identity.*` |
| 12 | Public URL (`BRAIN_PUBLIC_URL`) - **unchanged** by the upgrade | | | `endpoint.public_url` (verify: config PASS) |
| 13 | OAuth callbacks (count and fingerprint; effective list is the default or `BRAIN_ALLOWED_REDIRECTS`) - **unchanged** | | | `config.callbacks` |
| 14 | Front end in use (Caddy from `compose.yaml`, Nginx, other) and where its config and TLS state live (add them to `backup --extra-path`) | | | |
| 15 | Other containers / services and published ports on the host (the no-touch baseline) | | | `others[*]`, `listening` |
| 16 | Auth state file present, mode 0600, hash recorded (never its content) | | | `auth_state` |
| 17 | Baseline `tools/list` = the 7 `brain_*` tools (needs a token) | | | `tools` (SKIP = UNVERIFIED) |
| 18 | Public and internal health at baseline | | | `health_public`, `health_internal` |
| 19 | Validator baseline (`validate_repo --full`, `validate_content_release`) rc and error counts | | | `validators` |
| 20 | Dirty / untracked paths and uncommitted capture/proposal inventory | | | `git.dirty_*`, `proposals` |
| 21 | Free disk (data and state filesystems) and free RAM | | | `resources` |
| 22 | Hermes (external annotation harness): install path | | | |
| 23 | Hermes: Python version | | | |
| 24 | Hermes: YAML loader in use (PyYAML safe_load / ruamel / other) and whether the job template loads with it | | | |
| 25 | Hermes: OAuth helper / how it obtains the MCP access token (file, env) - never paste the token | | | |
| 26 | Is there a web panel beyond `/upload` (extra routes, a second UI) that the upgrade could affect? | | | |
| 27 | Telegram file/voice bridge: present? state? (R1 does not repair it; record as found) | | | |
| 28 | Last verified backup (archive name, sha256, date) and a scratch-restore result | | | `enrichment-backup.py verify` |
| 29 | Rollback decision owner and the stop conditions you accepted (see `upgrade.md` section 4) | | | |

Sign-off: all of rows 1-9, 12-13, 15-16 and 28 are VERIFIED, and no STOP remains in the preflight.
Rows 14, 17 and 22-27 may stay UNVERIFIED for Stage 0/1 but must be resolved before Stage 3
(pilot). Anything UNVERIFIED is a risk you accept explicitly, not a pass.
