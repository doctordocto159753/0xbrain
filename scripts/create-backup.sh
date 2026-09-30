#!/usr/bin/env bash
# Linux equivalent of create-backup.ps1, and a real one: the whole instantiated repo
# INCLUDING .git, _originals, captures/proposals (committed or not), .env, plus
# self-hosted auth and Caddy state. The search index is rebuildable and excluded.
# The archive holds secrets (.env): mode 600; set BRAIN_BACKUP_PASSPHRASE to encrypt it.
#   --dest DIR      output directory (default <state-dir>/backups)
#   --keep N        keep only the newest N archives in --dest
#   --no-quiesce    do not pause the brain container while archiving
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export REPO="${REPO:-$here}"
# shellcheck source=deploy/lib.sh
. "$here/deploy/lib.sh"

DEST="" KEEP="" QUIESCE=1
while [ $# -gt 0 ]; do
  case "$1" in --dest) DEST="$2"; shift 2;; --keep) KEEP="$2"; shift 2;; --no-quiesce) QUIESCE=0; shift;;
    -h|--help) sed -n 2,10p "$0"; exit 0;; *) die "unknown option: $1" 2;; esac
done
DATA="$(env_get BRAIN_DATA_DIR)"; STATE="$(env_get BRAIN_STATE_DIR)"
[ -d "$DATA/.git" ] && [ -d "$STATE" ] || die "no configured deployment found (.env missing or paths invalid)"
DEST="${DEST:-$STATE/backups}"; mkdir -p "$DEST"

ts="$(date -u +%Y%m%dT%H%M%SZ)"
name="0xbrain-backup-$ts.tar"
work="$(mktemp -d)"; trap 'rm -rf "$work"; [ -z "${paused:-}" ] || dc unpause brain >/dev/null 2>&1 || true' EXIT
g() { git -c safe.directory='*' -C "$DATA" "$@"; }
cat > "$work/BACKUP_INFO.txt" <<INFO
format=1
created_utc=$ts
repo_dir=$DATA
state_dir=$STATE
git_head=$(g rev-parse HEAD 2>/dev/null || echo none)
git_branch=$(g rev-parse --abbrev-ref HEAD 2>/dev/null || echo none)
uncommitted_paths=$(g status --porcelain 2>/dev/null | wc -l | tr -d ' ')
originals_files=$(find "$DATA/_originals" -type f 2>/dev/null | wc -l | tr -d ' ')
INFO

if [ "$QUIESCE" -eq 1 ] && [ -n "$(dc ps -q brain 2>/dev/null || true)" ]; then
  dc pause brain >/dev/null && paused=1   # freeze writers for a consistent snapshot; unpaused by the trap
fi

tar -C "$DATA" --exclude='./_search' --exclude='__pycache__' --exclude='.pytest_cache' \
    --transform 's,^\./,repo/,;s,^\.$,repo,' -cf "$work/$name" .
inc=()
for d in caddy/data caddy/config caddy/conf.d auth auth-idp; do [ -d "$STATE/$d" ] && inc+=("$d"); done
if [ "${#inc[@]}" -gt 0 ]; then
  tar -C "$STATE" --transform 's,^,state/,' -rf "$work/$name" "${inc[@]}"
fi
tar -C "$work" -rf "$work/$name" BACKUP_INFO.txt
[ -z "${paused:-}" ] || { dc unpause brain >/dev/null; paused=""; }

out="$DEST/$name.gz"
gzip -c "$work/$name" > "$work/out"
if [ -n "${BRAIN_BACKUP_PASSPHRASE:-}" ]; then
  out="$out.enc"
  openssl enc -aes-256-cbc -pbkdf2 -salt -pass env:BRAIN_BACKUP_PASSPHRASE -in "$work/out" -out "$work/out.enc"
  mv "$work/out.enc" "$work/out"
fi
( umask 077; mv "$work/out" "$out" ); chmod 600 "$out"
( cd "$DEST" && sha256sum "$(basename "$out")" > "$(basename "$out").sha256" )
# Integrity: the archive must be readable end to end and contain the Git directory.
if [ -z "${BRAIN_BACKUP_PASSPHRASE:-}" ]; then
  tar -tzf "$out" | grep -q '^repo/.git/HEAD$' || die "backup verification failed: repo/.git missing" 7
fi
if [ -n "$KEEP" ]; then
  ls -1t "$DEST"/0xbrain-backup-*.tar.gz* 2>/dev/null | grep -v '\.sha256$' | tail -n +"$((KEEP + 1))" | while read -r old; do rm -f "$old" "$old.sha256"; done
fi
echo "BACKUP OK: $out"
grep -E '^(git_head|uncommitted_paths|originals_files)=' "$work/BACKUP_INFO.txt"
