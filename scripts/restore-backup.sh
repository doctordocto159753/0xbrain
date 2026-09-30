#!/usr/bin/env bash
# Restore a create-backup.sh archive, then reindex, restart and validate.
# Works on a fresh host: needs only docker + compose + tar, and this script.
#   restore-backup.sh ARCHIVE [--repo-dir DIR] [--state-dir DIR] [--force] [--no-start]
# Existing targets are never deleted: --force renames them to <dir>.pre-restore-<time>.
# Encrypted archives (*.enc) need BRAIN_BACKUP_PASSPHRASE.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
die() { echo "ERROR: $*" >&2; exit "${2:-1}"; }
ARCH="${1:-}"; [ -n "$ARCH" ] && [ -f "$ARCH" ] || die "usage: restore-backup.sh ARCHIVE [--repo-dir DIR] [--state-dir DIR] [--force] [--no-start]" 2
shift
REPO_DIR="" STATE_DIR="" FORCE=0 START=1
while [ $# -gt 0 ]; do
  case "$1" in --repo-dir) REPO_DIR="$2"; shift 2;; --state-dir) STATE_DIR="$2"; shift 2;;
    --force) FORCE=1; shift;; --no-start) START=0; shift;; *) die "unknown option: $1" 2;; esac
done
command -v docker >/dev/null && docker compose version >/dev/null 2>&1 || die "docker compose is required"

[ ! -f "$ARCH.sha256" ] || ( cd "$(dirname "$ARCH")" && sha256sum -c "$(basename "$ARCH").sha256" >/dev/null ) || die "checksum mismatch: archive is corrupt" 7
work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
tarball="$ARCH"
case "$ARCH" in *.enc)
  [ -n "${BRAIN_BACKUP_PASSPHRASE:-}" ] || die "encrypted archive: set BRAIN_BACKUP_PASSPHRASE" 2
  openssl enc -d -aes-256-cbc -pbkdf2 -pass env:BRAIN_BACKUP_PASSPHRASE -in "$ARCH" -out "$work/dec.tar.gz" || die "decryption failed" 7
  tarball="$work/dec.tar.gz";; esac
tar -xzf "$tarball" -C "$work" BACKUP_INFO.txt || die "not a 0xbrain backup (BACKUP_INFO.txt missing)" 7
info() { grep -E "^$1=" "$work/BACKUP_INFO.txt" | cut -d= -f2-; }
REPO_DIR="${REPO_DIR:-$(info repo_dir)}"; STATE_DIR="${STATE_DIR:-$(info state_dir)}"

# Stop a running stack that owns the target, then move any existing target aside.
if [ -f "$REPO_DIR/.env" ] && [ -f "$REPO_DIR/compose.yaml" ]; then
  docker compose --project-directory "$REPO_DIR" down >/dev/null 2>&1 || true
fi
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
for d in "$REPO_DIR" "$STATE_DIR"; do
  if [ -e "$d" ] && [ -n "$(ls -A "$d" 2>/dev/null)" ]; then
    [ "$FORCE" -eq 1 ] || die "$d is not empty; use --force to move it to $d.pre-restore-$stamp" 3
    mv "$d" "$d.pre-restore-$stamp"; echo "moved existing $d -> $d.pre-restore-$stamp"
  fi
done
mkdir -p "$REPO_DIR" "$STATE_DIR"
tar -xzf "$tarball" -C "$REPO_DIR" --strip-components=1 --wildcards 'repo/*'
tar -xzf "$tarball" -C "$STATE_DIR" --strip-components=1 --wildcards 'state/*' 2>/dev/null || true
[ -d "$REPO_DIR/.git" ] || die "restore incomplete: no .git in $REPO_DIR" 7

export REPO="$REPO_DIR"
# shellcheck source=deploy/lib.sh
. "$REPO_DIR/deploy/lib.sh"
env_set BRAIN_DATA_DIR "$REPO_DIR"; env_set BRAIN_STATE_DIR "$STATE_DIR"
uid="$(env_get BRAIN_UID)"; gid="$(env_get BRAIN_GID)"
mkdir -p "$STATE_DIR"/{qmd,home,backups,caddy/data,caddy/config,caddy/conf.d,auth,auth-idp}
chown -R "$uid:$gid" "$REPO_DIR" "$STATE_DIR"/{qmd,home,backups,auth,auth-idp} 2>/dev/null || true
chmod 600 "$REPO_DIR/.env"; chmod 700 "$STATE_DIR/auth"

log "restored $(info git_head) (uncommitted paths at backup time: $(info uncommitted_paths)); rebuilding image"
dc build brain
log "git integrity"
brain_run git fsck --no-dangling
log "reindex (lexical, model-free)"
brain_run bash scripts/configure-search.sh >/dev/null
brain_run bash scripts/refresh-search.sh --quiet
if [ "$START" -eq 1 ]; then
  dc up -d
  wait_brain_healthy 240 || { dc logs --tail 40 brain >&2; die "brain not healthy after restore" 5; }
  wait_public_health 180 || die "HTTPS not answering after restore" 5
fi
run_validators || die "validators failed after restore" 6
echo "RESTORE OK: head=$(brain_run git rev-parse HEAD | tr -d '\r') uncommitted=$(brain_run git status --porcelain | wc -l | tr -d ' ')"
