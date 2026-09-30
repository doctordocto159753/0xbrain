#!/usr/bin/env bash
# Owner-only (host shell): run the HUMAN review/promotion CLI inside the brain
# image against the mounted wiki. The server process runs with
# BRAIN_REMOTE_SESSION=1 and brain_review.py refuses to act there; this wrapper
# is the explicit human context, so it clears that marker for this one command.
#   scripts/review.sh list
#   scripts/review.sh inspect PROP_ID
#   scripts/review.sh accept  PROP_ID --actor NAME [--note ..]
#   scripts/review.sh promote PROP_ID --actor NAME [--paths ...]
# See docs/PROPOSALS_AND_REVIEW.md. Never expose this through MCP.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export REPO="${REPO:-$here}"
# shellcheck source=deploy/lib.sh
. "$here/deploy/lib.sh"
tty=(); [ -t 0 ] || tty=(-T)
exec docker compose --project-directory "$REPO" run --rm --no-deps "${tty[@]}" \
  -e BRAIN_REMOTE_SESSION= -e GIT_AUTHOR_NAME="${BRAIN_REVIEWER:-owner}" \
  -e GIT_COMMITTER_NAME="${BRAIN_REVIEWER:-owner}" \
  brain python scripts/brain_review.py "$@"
