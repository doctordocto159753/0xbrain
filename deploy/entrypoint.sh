#!/bin/sh
# brain-entrypoint: starts the real 0xBrain Remote MCP server
# (scripts/remote_mcp/server.py: streamable HTTP + single-owner OAuth, six
# brain_* tools) from the mounted wiki. Other args are exec'd as-is, so
# `docker compose run --rm brain python scripts/validate_repo.py --full` works.
#
# There is no fallback: if the server cannot start, the container exits and
# the healthcheck fails. The health-only STUB runs ONLY when BRAIN_ALLOW_STUB=1
# is set explicitly (deployment-plumbing tests); it is never chosen implicitly.
set -eu

if [ "${1:-serve}" != "serve" ]; then
  exec "$@"
fi

WIKI="${BRAIN_WIKI_DIR:-/wiki}"
cd "$WIKI"

if [ "${BRAIN_ALLOW_STUB:-0}" = "1" ]; then
  echo "brain: WARNING: BRAIN_ALLOW_STUB=1: running the deployment STUB (health only, NO MCP)." >&2
  exec python /usr/local/lib/brain/stub_server.py
fi

if [ ! -d "$WIKI/.git" ]; then
  echo "brain: $WIKI is not a Git repository; refusing to start (mount the instantiated repo)." >&2
  exit 78
fi
if [ ! -f "$WIKI/00-system/registers/INSTANCE.json" ]; then
  echo "brain: $WIKI is not instantiated (00-system/registers/INSTANCE.json missing); run install.sh." >&2
  exit 78
fi
for v in BRAIN_PUBLIC_URL BRAIN_OWNER_SECRET BRAIN_STATE_DIR; do
  eval "val=\${$v:-}"
  [ -n "$val" ] || { echo "brain: $v is not set; refusing to start." >&2; exit 78; }
done
if [ ! -f "$WIKI/scripts/remote_mcp/server.py" ]; then
  echo "brain: scripts/remote_mcp/server.py missing from the mounted wiki; refusing to start." >&2
  exit 78
fi

mkdir -p "${HOME:-/state/home}" "${WIKI_QMD_HOME:-/state/qmd}" "$BRAIN_STATE_DIR" 2>/dev/null || true

# Search index is rebuildable state: register/refresh it (lexical only, never
# embeds). Failure is not fatal: brain_search self-heals the index on demand
# and brain_status reports its state.
if ! bash "$WIKI/scripts/refresh-search.sh" --quiet --no-validate; then
  echo "brain: warning: search index refresh failed; brain_status will report it." >&2
fi

export BRAIN_REMOTE_SESSION=1
exec python "$WIKI/scripts/remote_mcp/server.py"
