# Backup and restore

## What is captured

`scripts/create-backup.sh` writes `0xbrain-backup-<UTC>.tar.gz` plus a `.sha256` sidecar:

- `repo/`: the entire instantiated repository including `.git`, `_originals/`, captures and proposals
  (committed **or not**), `.env`;
- `state/`: `auth/`, `auth-idp/`, `caddy/{data,config,conf.d}`;
- `BACKUP_INFO.txt`: HEAD, branch, number of uncommitted paths, number of original files.

Excluded: the search index (`_search/`, `$STATE/qmd`), caches. All are rebuildable.

The archive contains secrets (`.env`, signing key, auth state). It is written mode 600. For off-box
storage set `BRAIN_BACKUP_PASSPHRASE` and the archive is AES-256 encrypted (`.enc`); the passphrase
is not stored anywhere, so keep it separately.

## Back up

```bash
sudo scripts/create-backup.sh                       # to /var/lib/0xbrain/backups
sudo scripts/create-backup.sh --dest /mnt/x --keep 14
BRAIN_BACKUP_PASSPHRASE=... sudo -E scripts/create-backup.sh   # encrypted
```

The brain container is paused (frozen) for the seconds the tar takes, so a capture cannot be written
half-way into the snapshot, then resumed. `--no-quiesce` skips this. Schedule with cron, for example
`17 3 * * * /opt/0xbrain/wiki/scripts/create-backup.sh --keep 14`, and copy archives off the
server (`rclone`, `scp`); a backup on the same disk does not survive the disk.

## Restore (also the disaster-recovery path onto a new VPS)

```bash
# new host: install Docker, copy the archive and scripts/restore-backup.sh + deploy/lib.sh, or clone the repo first
sudo scripts/restore-backup.sh /path/0xbrain-backup-....tar.gz [--repo-dir DIR] [--state-dir DIR] [--force]
```

It verifies the checksum, refuses to touch a non-empty target unless `--force` (which renames the old
directory to `<dir>.pre-restore-<time>`, never deletes it), extracts, rewrites the paths in `.env` if
you chose different directories, rebuilds the image, runs `git fsck`, reindexes, restarts, waits for
health, and runs both validators. Final line: `RESTORE OK: head=... uncommitted=N`; compare with the
values printed at backup time. On a new server, point DNS at it before restore so Caddy can obtain
its certificate (or it reuses the restored one).

Test your backups: restore into a scratch directory with `--repo-dir /tmp/t/wiki --state-dir /tmp/t/state --no-start`
and read the result, at least after every upgrade of this tooling.
