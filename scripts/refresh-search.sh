#!/bin/sh
# Re-index changed files (qmd update only; embeddings are never generated),
# then run the two validators like refresh-search.ps1.
#   --no-validate  skip the validators (cron / container start)
#   --validate     run the validators (default; accepted for compatibility)
#   --quiet        print only failures
# On a fresh or restored deployment with no index yet, the managed
# collections are registered first (configure-search.sh), so this script is
# safe to run at any time. Single implementation: scripts/search_lexical.py.
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PY=${PYTHON:-python3}
command -v "$PY" >/dev/null 2>&1 || PY=python
validate=1; quiet=0
for a in "$@"; do
    case "$a" in
        --no-validate) validate=0 ;;
        --validate) validate=1 ;;
        --quiet) quiet=1 ;;
        *) echo "unknown option: $a" >&2; exit 2 ;;
    esac
done
command -v qmd >/dev/null 2>&1 || {
    echo "qmd not found on PATH. Install: npm install -g @tobilu/qmd (Node >= 22)" >&2
    exit 2
}
# run CMD...: show its output unless --quiet; on failure always show it.
run() {
    if [ "$quiet" -eq 1 ]; then
        _o=$("$@" 2>&1) || { _rc=$?; printf '%s\n' "$_o" >&2; return $_rc; }
    else
        "$@"
    fi
}
S="$ROOT/scripts/search_lexical.py"
if ! "$PY" "$S" --root "$ROOT" status 2>/dev/null | "$PY" -c 'import json,sys; sys.exit(0 if json.load(sys.stdin)["index_present"] else 1)'; then
    run "$PY" "$S" --root "$ROOT" configure
fi
run "$PY" "$S" --root "$ROOT" refresh
run "$PY" "$S" --root "$ROOT" status
if [ "$validate" -eq 1 ]; then
    cd "$ROOT"
    run "$PY" scripts/validate_repo.py --full || exit 1
    run "$PY" scripts/validate_content_release.py || exit 2
fi
[ "$quiet" -eq 1 ] || echo "SEARCH REFRESH PASS (lexical only)"
