# Backup and restore

## What a backup contains

`scripts/create-backup.sh` writes `0xbrain-backup-<UTC>.tar.gz` and a
`.sha256` sidecar:

- `repo/`: the whole repository including `.git`, `_originals/`, captures
  and proposals (**committed or not**), and `.env`;
- `state/`: `auth/` (OAuth clients and hashed tokens: a restored server
  keeps connectors working) and `caddy/{data,config}` (certificates);
- `BACKUP_INFO.txt`: HEAD, branch, number of uncommitted paths, number of
  original files.

Excluded: the search index and caches (rebuildable).

The archive contains secrets (`.env` with `BRAIN_OWNER_SECRET`, OAuth
state). It is mode 600. For off-box storage set `BRAIN_BACKUP_PASSPHRASE`
(AES-256, `.enc`); the passphrase is stored nowhere, keep it separately.

## Back up

```bash
sudo scripts/create-backup.sh                       # to $BRAIN_STATE_DIR/backups
sudo scripts/create-backup.sh --dest /mnt/x --keep 14
BRAIN_BACKUP_PASSPHRASE=... sudo -E scripts/create-backup.sh
```

The brain container is paused for the seconds the archive takes (no
half-written capture), then resumed. Schedule it, e.g.
`17 3 * * * /opt/0xbrain/wiki/scripts/create-backup.sh --keep 14`, and copy
archives off the server; a backup on the same disk does not survive the disk.

## Restore (also disaster recovery onto a new VPS)

```bash
# new host: Docker + Compose; copy the archive (+ .sha256) and scripts/restore-backup.sh
sudo ./restore-backup.sh /path/0xbrain-backup-....tar.gz [--repo-dir DIR] [--state-dir DIR] [--force]
```

It verifies the checksum, never deletes a non-empty target (`--force`
renames it to `<dir>.pre-restore-<time>`), extracts, rewrites `.env` paths
if you chose new directories, rebuilds the image, runs `git fsck`, rebuilds
the search index, starts the stack, waits for health, and runs both
validators. Last line: `RESTORE OK: head=... uncommitted=N`; compare with
the values printed at backup time. Uncommitted captures/proposals come back
uncommitted and appear in `brain_status`; commit them with
`scripts/review.sh flush`.

Point DNS at a new server before restoring so Caddy can use or renew the
certificate. Test your backups: restore into scratch directories with
`--no-start` after every upgrade of this tooling.
