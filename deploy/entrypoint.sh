#!/bin/sh
# brain-entrypoint: resolves the remote server command through the transport seam
# (docs/deploy/AUTH_TRANSPORT_SEAM.md) and starts it. Other args are exec'd as-is,
# so `docker compose run --rm brain python scripts/validate_repo.py --full` works.
set -eu

if [ "${1:-serve}" != "serve" ]; then
  exec "$@"
fi

WIKI="${BRAIN_WIKI_DIR:-/wiki}"
cd "$WIKI"

if [ ! -d "$WIKI/.git" ]; then
  echo "brain: $WIKI is not a Git repository; refusing to start (mount the instantiated repo)." >&2
  exit 78
fi
if [ ! -f "$WIKI/00-system/registers/INSTANCE.json" ]; then
  echo "brain: $WIKI is not instantiated (00-system/registers/INSTANCE.json missing); run install.sh." >&2
  exit 78
fi

mkdir -p "${HOME:-/state/home}" "${XDG_CACHE_HOME:-/state/qmd/cache}" \
         "${XDG_CONFIG_HOME:-/state/qmd/config}" "${BRAIN_AUTH_STATE_DIR:-/state/auth}" 2>/dev/null || true

# Search index is rebuildable state: build it if absent. Lexical only, never embeds.
# Failure is non-fatal here; brain_status/verify report index freshness.
if [ -x "$WIKI/scripts/refresh-search.sh" ] || [ -f "$WIKI/scripts/refresh-search.sh" ]; then
  if ! sh -c 'bash "$0" --quiet' "$WIKI/scripts/refresh-search.sh"; then
    echo "brain: warning: search index build failed; serving without a fresh index." >&2
  fi
fi

export BRAIN_HOST="${BRAIN_HOST:-0.0.0.0}"
export BRAIN_PORT="${BRAIN_PORT:-8080}"
export PYTHONPATH="$WIKI:$WIKI/scripts${PYTHONPATH:+:$PYTHONPATH}"

if [ -n "${BRAIN_SERVER_CMD:-}" ]; then
  echo "brain: starting via BRAIN_SERVER_CMD" >&2
  exec sh -c "$BRAIN_SERVER_CMD"
fi
if [ -f "$WIKI/brain_server/__main__.py" ]; then
  echo "brain: starting brain_server" >&2
  exec python -m brain_server
fi
if [ -f "$WIKI/scripts/brain_mcp/__main__.py" ]; then
  echo "brain: starting scripts/brain_mcp" >&2
  exec python -m brain_mcp
fi
if [ "${BRAIN_ALLOW_STUB:-0}" = "1" ]; then
  echo "brain: WARNING: no remote MCP server found; running the deployment STUB (health only, no MCP)." >&2
  exec python /usr/local/lib/brain/stub_server.py
fi
echo "brain: no remote MCP server found (set BRAIN_SERVER_CMD, or provide brain_server/ or scripts/brain_mcp/)." >&2
echo "brain: for deployment-plumbing tests only, set BRAIN_ALLOW_STUB=1." >&2
exit 78
