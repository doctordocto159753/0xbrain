# Upgrade and rollback

```bash
cd /opt/0xbrain/wiki
sudo scripts/upgrade.sh [--remote origin] [--branch main]
```

1. Backup (`create-backup.sh`).
2. `git fetch`; the brain server is stopped; `git merge <remote>/<branch>`.
   A conflict aborts the merge, changes nothing, restarts the server, and
   exits 3.
3. Record `<before> <after> <backup>` in `$BRAIN_STATE_DIR/backups/last-upgrade.txt`.
4. Rebuild the image (new requirements/Dockerfile), `docker compose up -d`,
   wait for container health and public `/healthz`, run both validators.

New configuration appears in `.env.example`; add it to `.env` by hand (the
upgrade never rewrites `.env`). Newer Caddy: `docker compose pull caddy`.

## Rollback

```bash
sudo scripts/upgrade.sh --rollback
```

Code and archive share one repository, so rollback never rewinds history:
it **reverts** the commits the last upgrade brought in (`git revert -m 1
<merge>`, or the fast-forwarded range) and re-runs the gate. Captures,
proposals and canonical commits made after the upgrade stay. If the revert
conflicts with later changes it aborts cleanly and points at the
pre-upgrade backup (`restore-backup.sh --force`).

To re-apply the same upstream code after a rollback, revert the revert
(`git revert <revert-commit>`) before the next `upgrade.sh`: Git considers
the reverted commits already merged.

## Enrichment layer (R1) upgrades

Do **not** use `scripts/upgrade.sh` to introduce the enrichment layer on a stack that has a
compose override, a non-Caddy front, or any service besides `brain` that must not move: it runs
`dc up -d` without a service name and a recursive `chown -R` over the archive, and its rollback
reverts a commit range in the repository that also holds the archive. Use the bounded,
rehearsable tooling instead: `docs/enrichment/upgrade.md` (hazards with file:line evidence,
stages, stop conditions) and the one-page `docs/enrichment/runbook.md`.

## Known friction

`instantiate.py` rewrites the record prefix inside some code and docs, so an
instantiated repository differs from the upstream kit in those lines and a
later merge can conflict there. The upgrade aborts cleanly; resolve on a
branch, run the validators, then merge.
