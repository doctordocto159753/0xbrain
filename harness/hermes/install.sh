#!/usr/bin/env bash
# Install the 0xBrain annotator skill and a DISABLED job template into a Hermes home.
#
#   install.sh --hermes-home DIR --account NAME            # dry run (default): prints, writes nothing
#   install.sh --hermes-home DIR --account NAME --apply    # copies the files
#
# Installs only: <home>/skills/0xbrain-annotator/{SKILL.md,prompts/*} and
# <home>/accounts/<account>/jobs/0xbrain-annotator.yaml (enabled: false).
# It never pip-installs, never touches Docker or the archive, never reads or
# writes a credential, never enables the job, and never overwrites an existing
# file unless --force is given (a force never changes an existing job's
# enabled state: it refuses to replace a job file that is enabled).
# The target layout is an assumption; check it against your Hermes version
# (docs/enrichment/hermes.md).
set -euo pipefail

usage() {
  sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

HERMES_HOME=""
ACCOUNT=""
APPLY=0
FORCE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --hermes-home) HERMES_HOME="${2:-}"; shift 2 ;;
    --account) ACCOUNT="${2:-}"; shift 2 ;;
    --apply) APPLY=1; shift ;;
    --force) FORCE=1; shift ;;
    -h|--help) usage 0 ;;
    *) echo "unknown argument: $1" >&2; usage 2 ;;
  esac
done

[ -n "$HERMES_HOME" ] || { echo "--hermes-home is required (no default is guessed)" >&2; exit 2; }
[ -n "$ACCOUNT" ] || { echo "--account is required (no default is guessed)" >&2; exit 2; }
case "$HERMES_HOME" in /*) ;; *) echo "--hermes-home must be an absolute path" >&2; exit 2 ;; esac
case "$HERMES_HOME" in *..*) echo "--hermes-home must not contain '..'" >&2; exit 2 ;; esac
[ "$HERMES_HOME" != "/" ] || { echo "refusing '/' as --hermes-home" >&2; exit 2; }
[ -d "$HERMES_HOME" ] || { echo "--hermes-home does not exist or is not a directory: $HERMES_HOME" >&2; exit 2; }
case "$ACCOUNT" in
  *[!A-Za-z0-9_-]*|"") echo "--account may contain only letters, digits, '_' and '-'" >&2; exit 2 ;;
esac
[ "${#ACCOUNT}" -le 64 ] || { echo "--account is too long" >&2; exit 2; }

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$HERMES_HOME/skills/0xbrain-annotator"
JOB_DIR="$HERMES_HOME/accounts/$ACCOUNT/jobs"
JOB_FILE="$JOB_DIR/0xbrain-annotator.yaml"

for f in SKILL.md job.template.yaml prompts/annotate.md prompts/capture_policy.md prompts/retrieval_policy.md; do
  [ -f "$SRC/$f" ] || { echo "kit file missing: $f" >&2; exit 1; }
done
grep -Eq '^enabled:[[:space:]]*false([[:space:]]|$)' "$SRC/job.template.yaml" || { echo "template is not disabled; refusing" >&2; exit 1; }

if [ "$APPLY" -eq 1 ]; then echo "APPLY: writing under $HERMES_HOME"; else echo "DRY-RUN: nothing will be written (add --apply)"; fi

plan_copy() { # src dest
  local src="$1" dest="$2"
  if [ -e "$dest" ] && [ "$FORCE" -ne 1 ]; then
    echo "  skip (exists, use --force): $dest"
    return 0
  fi
  if [ "$APPLY" -eq 1 ]; then
    mkdir -p "$(dirname "$dest")"
    cp "$src" "$dest"
    echo "  wrote: $dest"
  else
    echo "  would write: $dest"
  fi
}

echo "skill:"
plan_copy "$SRC/SKILL.md" "$SKILL_DIR/SKILL.md"
for p in annotate capture_policy retrieval_policy; do
  plan_copy "$SRC/prompts/$p.md" "$SKILL_DIR/prompts/$p.md"
done

echo "job (disabled):"
if [ -e "$JOB_FILE" ] && grep -Eq '^enabled:[[:space:]]*true' "$JOB_FILE"; then
  echo "  skip: $JOB_FILE is enabled; never replaced"
else
  plan_copy "$SRC/job.template.yaml" "$JOB_FILE"
fi

echo "done. The job stays disabled; enable it by hand after the checklist in docs/enrichment/hermes.md."
