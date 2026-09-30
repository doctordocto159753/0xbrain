# Troubleshooting

| Symptom | Cause / fix |
|---|---|
| installer: "HTTPS endpoint did not answer" | DNS not pointing here yet, or 80/443 blocked. Fix and re-run `./install.sh`; `docker compose logs caddy` shows the ACME error. |
| image build fails at `apt-get` | Debian mirrors unreachable: `./install.sh --python-base python:3.12-bookworm` (ships git). Behind a TLS-inspecting proxy add `--build-ca FILE`; proxy on the host loopback: `--build-network host`. |
| brain exits with code 78 | not a Git checkout, not instantiated, `BRAIN_PUBLIC_URL`/`BRAIN_OWNER_SECRET`/`BRAIN_STATE_DIR` missing, or `scripts/remote_mcp/server.py` absent. The log line says which. There is no fallback server. |
| brain restarts in a loop | `docker compose logs brain`: a `ValueError`/`SystemExit` at startup is a refused configuration (e.g. wildcard redirect, weak secret, legacy adapter without opt-in). |
| Claude: `invalid_redirect_uri` at connect | Claude used a callback not in the allowlist. Add that exact https URI (plus the two defaults) to `BRAIN_ALLOWED_REDIRECTS`, `docker compose up -d`. Never a wildcard. |
| consent page says "Too many attempts" | 5 wrong secrets in 5 minutes; wait, then use the secret from `.env`. |
| Claude asks to reconnect after every restart | `$BRAIN_STATE_DIR/auth` not persisted or not writable by `BRAIN_UID`; check the mount and `ls -la`. |
| `brain_status`: `uncommitted` > 0 | durable writes whose Git commit failed (hook, lock, disk). Data is safe. `scripts/review.sh status` shows the reason; `scripts/review.sh flush` commits them. |
| `brain_status`: search `stale`/`missing` | index older than the files; the next lexical search refreshes it, or run `docker compose run --rm brain bash scripts/refresh-search.sh --no-validate`. |
| `brain_search` lexical: `E_UNAVAILABLE` | QMD missing or index failed; `mode: exact` still works. Rebuild the index as above. |
| `brain_status`: validation `fail` | run `docker compose run --rm brain python scripts/validate_repo.py --full` and fix the named records; remote writes still work, but promotion is blocked until green. |
| capture writer: `E_AMBIGUOUS_RECORD` | a record written before the heading-escape fix has a bare section heading inside its text; repair it by hand (move the text back into its section) and re-validate. |
| `permission denied` writing captures | repository ownership drifted from `BRAIN_UID`: `sudo chown -R <uid>:<gid> /opt/0xbrain/wiki`. |
| Git commit inside the container refused | the repository gate (`.githooks/pre-commit`) rejected it; for captures the file stays on disk and is reported uncommitted. |
| upgrade stops with "merge conflict" | nothing changed; resolve on a branch ([UPGRADE.md](UPGRADE.md)). |
