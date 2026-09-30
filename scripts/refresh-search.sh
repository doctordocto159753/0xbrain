#!/bin/sh
# Re-index changed files (qmd update only; embeddings are never generated),
# then run the two validators like refresh-search.ps1. Use --no-validate to
# skip the validators (e.g. from a cron job that only wants a fresh index).
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PY=${PYTHON:-python3}
command -v "$PY" >/dev/null 2>&1 || PY=python
command -v qmd >/dev/null 2>&1 || {
    echo "qmd not found on PATH. Install: npm install -g @tobilu/qmd (Node >= 22)" >&2
    exit 2
}
"$PY" "$ROOT/scripts/search_lexical.py" --root "$ROOT" refresh
"$PY" "$ROOT/scripts/search_lexical.py" --root "$ROOT" status
if [ "${1:-}" != "--no-validate" ]; then
    (cd "$ROOT" && "$PY" scripts/validate_repo.py --full) || exit 1
    (cd "$ROOT" && "$PY" scripts/validate_content_release.py) || exit 2
fi
echo "SEARCH REFRESH PASS"
