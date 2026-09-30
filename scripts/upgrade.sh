#!/usr/bin/env bash
# Upgrade code and image, with a backup first and a health+validator gate after.
#   upgrade.sh [--remote origin] [--branch main]   merge <remote>/<branch>, rebuild, restart, verify
#   upgrade.sh --rollback                          undo the last upgrade with a revert commit
#
# Code and archive share one repository, so history is never rewound:
# - the brain server is stopped while merging (no concurrent K9 commits);
# - a merge conflict aborts cleanly (nothing changed, server restarted);
# - rollback REVERTS the commits the last upgrade brought in. Captures,
#   proposals and canonical commits made after the upgrade stay in place.
#   A revert conflict aborts cleanly; restore the pre-upgrade backup instead.
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
MARK="$STATE/backups/last-upgrade.txt"      # "<before> <after> <backup-archive>"
g() { git -c safe.directory='*' -C "$DATA" "$@"; }

gate() {
  chown -R "$uid:$gid" "$DATA"
  build_image
  dc up -d
  wait_brain_healthy 240 || { dc logs --tail 40 brain >&2; return 5; }
  wait_public_health 180 || return 5
  run_validators
}

if [ "$ROLLBACK" -eq 1 ]; then
  [ -f "$MARK" ] || die "no recorded upgrade ($MARK)"
  read -r before after backup < "$MARK"
  [ -n "${after:-}" ] && [ "$after" != "$before" ] || die "the last upgrade changed nothing; nothing to roll back"
  g merge-base --is-ancestor "$after" HEAD || die "upgrade commit $after is not in the current history"
  log "stopping brain; reverting $before..$after (later archive commits are kept)"
  dc stop brain >/dev/null 2>&1 || true
  if [ "$(g rev-list --parents -n 1 "$after" | wc -w)" -gt 2 ]; then
    revert=(revert --no-edit -m 1 "$after")                 # the upgrade was a merge commit
  else
    revert=(revert --no-edit "$before..$after")             # the upgrade was a fast-forward
  fi
  if ! g "${revert[@]}"; then
    g revert --abort >/dev/null 2>&1 || true
    dc up -d >/dev/null 2>&1 || true
    die "revert conflicts with later changes; nothing was changed. Restore the pre-upgrade backup ($backup) with scripts/restore-backup.sh" 3
  fi
  printf '%s %s %s\n' "$before" "$before" "$backup" > "$MARK"
  gate && echo "ROLLBACK OK: now $(g rev-parse HEAD) (code of $before, archive commits kept)"
  exit
fi

log "1/5 backup"
out="$("$here/scripts/create-backup.sh" | tee /dev/stderr | sed -n 's/^BACKUP OK: //p')"
before="$(g rev-parse HEAD)"
log "2/5 fetch and merge $REMOTE/$BRANCH (brain stopped during the merge)"
g fetch "$REMOTE" "$BRANCH"
dc stop brain >/dev/null 2>&1 || true
if ! g merge --no-edit "$REMOTE/$BRANCH"; then
  g merge --abort || true
  dc up -d >/dev/null 2>&1 || true
  die "merge conflict; nothing was changed. Resolve manually (see docs/UPGRADE.md)" 3
fi
after="$(g rev-parse HEAD)"
printf '%s %s %s\n' "$before" "$after" "$out" > "$MARK"
log "3-5/5 rebuild, restart, health, validators"
if ! gate; then
  warn "post-upgrade gate FAILED. Roll back with: scripts/upgrade.sh --rollback"; exit 6
fi
echo "UPGRADE OK: $before -> $after"
