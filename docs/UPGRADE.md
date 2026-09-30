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

## Known friction

`instantiate.py` rewrites the record prefix inside some code and docs, so an
instantiated repository differs from the upstream kit in those lines and a
later merge can conflict there. The upgrade aborts cleanly; resolve on a
branch, run the validators, then merge.
