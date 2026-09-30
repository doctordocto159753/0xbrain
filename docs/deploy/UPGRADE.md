# Upgrade

`scripts/upgrade.sh` performs the routine path; this page is what it does and what to do when it stops.

## Routine

```bash
cd /opt/0xbrain/wiki
sudo scripts/upgrade.sh [--remote origin] [--branch main]
```

1. Backup (`create-backup.sh`); the archive path and current commit are recorded in
   `$STATE/backups/last-upgrade.txt`.
2. `git fetch` then `git merge <remote>/<branch>`. A conflict aborts the merge and changes nothing.
3. Rebuild the image (picks up new `requirements.txt` and Dockerfile changes), `docker compose up -d`.
4. Wait for container health and for the public HTTPS `/healthz`.
5. Run `validate_repo.py --full` and `validate_content_release.py`.

Config changes: new variables appear in `.env.example`; add them to `.env` by hand (the upgrade never
rewrites `.env`). Pull a newer Caddy or IdP image with `docker compose pull caddy auth`.

## Rollback

```bash
sudo scripts/upgrade.sh --rollback
```

Uses `git reset --keep <pre-upgrade commit>`, which refuses rather than overwrite local changes, then
rebuilds and re-verifies. If it refuses, or the data itself is suspect, restore the pre-upgrade backup
(`restore-backup.sh --force`, see BACKUP_RESTORE.md). Captures made by the server between upgrade and
rollback are new files and survive `--keep`.

## Known friction

`instantiate.py` rewrites the record prefix inside code and docs, so an instantiated repo diverges from
the upstream kit by one commit and a later merge can conflict in those files. The script aborts cleanly
in that case; resolve on a branch, run the validators, then merge. This is a structural property of the
existing kit, flagged for the integration agent rather than worked around here.

