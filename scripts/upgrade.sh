#!/usr/bin/env bash
# Upgrade code and image, with a backup first and a health+validator gate after.
#   upgrade.sh [--remote origin] [--branch main]     merge <remote>/<branch>, rebuild, restart, verify
#   upgrade.sh --rollback                            return to the pre-upgrade commit (git reset --keep)
# A merge conflict aborts cleanly (nothing changed). Rollback refuses if it would lose local work.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export REPO="${REPO:-$here}"
# shellcheck source=deploy/lib.sh
. "$here/deploy/lib.sh"
REMOTE=origin BRANCH=main ROLLBACK=0
while [ $# -gt 0 ]; do
  case "$1" in --remote) REMOTE="$2"; shift 2;; --branch) BRANCH="$2"; shift 2;; --rollback) ROLLBACK=1; shift;;
    *) die "unknown option: $1" 2;; esac
done
DATA="$(env_get BRAIN_DATA_DIR)"; STATE="$(env_get BRAIN_STATE_DIR)"
uid="$(env_get BRAIN_UID)"; gid="$(env_get BRAIN_GID)"
MARK="$STATE/backups/last-upgrade.txt"
g() { git -c safe.directory='*' -C "$DATA" "$@"; }

gate() {
  build_image
  dc up -d
  wait_brain_healthy 240 || { dc logs --tail 40 brain >&2; return 5; }
  wait_public_health 180 || return 5
  run_validators
}

if [ "$ROLLBACK" -eq 1 ]; then
  [ -f "$MARK" ] || die "no recorded pre-upgrade commit ($MARK)"
  prev="$(cut -d' ' -f1 "$MARK")"
  log "rolling back to $prev"
  g reset --keep "$prev" || die "rollback would overwrite local changes; restore from the pre-upgrade backup instead ($(cut -d' ' -f2- "$MARK"))" 3
  chown -R "$uid:$gid" "$DATA"
  gate && echo "ROLLBACK OK: $prev"
  exit
fi

log "1/5 backup"
out="$("$here/scripts/create-backup.sh" | tee /dev/stderr | sed -n 's/^BACKUP OK: //p')"
before="$(g rev-parse HEAD)"
printf '%s %s\n' "$before" "$out" > "$MARK"
log "2/5 fetch and merge $REMOTE/$BRANCH"
g fetch "$REMOTE" "$BRANCH"
if ! g merge --no-edit "$REMOTE/$BRANCH"; then
  g merge --abort || true
  die "merge conflict; nothing was changed. Resolve manually (see docs/deploy/UPGRADE.md)" 3
fi
chown -R "$uid:$gid" "$DATA"
log "3-5/5 rebuild, restart, health, validators"
if ! gate; then
  warn "post-upgrade gate FAILED. Roll back with: scripts/upgrade.sh --rollback"; exit 6
fi
echo "UPGRADE OK: $before -> $(g rev-parse HEAD)"
