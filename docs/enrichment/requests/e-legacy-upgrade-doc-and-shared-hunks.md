# Request from WP-E (AGENT-E) to AGENT-0: shared-file changes

Status: proposal only. WP-E edited none of these files (they are outside its exclusive list).

## 1. `docs/UPGRADE.md`: point R1 upgrades at the bounded tooling

Why: the legacy `scripts/upgrade.sh` has hazards that an enrichment upgrade on a customised
production stack must not inherit (evidence with file:line in `docs/enrichment/upgrade.md`
section 2). Apply with `patch -p1 < this-hunk` from the repository root (`a/` and `b/` prefixes):

```diff
--- a/docs/UPGRADE.md	2026-10-10 10:53:57.048502075 +0000
+++ b/docs/UPGRADE.md	2026-10-10 10:53:57.061655149 +0000
@@ -33,6 +33,15 @@
 (`git revert <revert-commit>`) before the next `upgrade.sh`: Git considers
 the reverted commits already merged.
 
+## Enrichment layer (R1) upgrades
+
+Do **not** use `scripts/upgrade.sh` to introduce the enrichment layer on a stack that has a
+compose override, a non-Caddy front, or any service besides `brain` that must not move: it runs
+`dc up -d` without a service name and a recursive `chown -R` over the archive, and its rollback
+reverts a commit range in the repository that also holds the archive. Use the bounded,
+rehearsable tooling instead: `docs/enrichment/upgrade.md` (hazards with file:line evidence,
+stages, stop conditions) and the one-page `docs/enrichment/runbook.md`.
+
 ## Known friction
 
 `instantiate.py` rewrites the record prefix inside some code and docs, so an
```

## 2. Shared-file hunk list for `plan --shared-hunks`

`scripts/upgrade-enrichment.py plan` already treats these shared files as bounded, merge-aware
hunks (400 changed lines max each): `.gitignore`, `.env.example`, `AGENTS.md`,
`scripts/validate_repo.py`, `scripts/validate_content_release.py`, `scripts/remote_mcp/adapter.py`,
plus the instance-marker files `README.md`, `HOME.md`, `SYSTEM_DESIGN.md`, `CLAUDE.md`.
If the integration commit touches any other shared file (for example `.github/workflows/validate.yml`
or `scripts/brain_surface/*`), AGENT-0 must list it explicitly, otherwise `plan` STOPs on it:

```json
{"allow": [".github/workflows/validate.yml"]}
```

Paths in forbidden zones (`_originals/`, `01-inbox/` ... `07-genesis/`, `00-system/`, `_proposals/`,
`_captures/`, `_enrichment/`, `_search/`, `.git/`, `.env*`) are refused even if listed. Pass the file
with `--shared-hunks`. Do **not** list `compose*.yaml`, `Dockerfile`, `deploy/` or `requirements*`:
R1 must not change them (the image holds runtimes only, app code is bind-mounted).

## 3. Heads-up for the integration commit

* `tests/enrichment/test_upgrade.py` runs the two WP-E scripts against throwaway git repos; it needs
  `git` on PATH and, for the real-server MCP test, the optional `mcp`/`uvicorn` packages (it skips
  that one test otherwise).
* The wiring commit must keep `scripts/upgrade-enrichment.py` and `scripts/enrichment-backup.py`
  executable (mode 100755) and in the release allowlist (they are).
