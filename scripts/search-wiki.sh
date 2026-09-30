#!/bin/sh
# Model-free wiki search. Usage:
#   scripts/search-wiki.sh "query" [canonical|captures|all] [n]
#   scripts/search-wiki.sh --exact "text" [canonical|captures] [n]
# Lexical mode relaxes deterministically (strict, content terms, Persian stem
# prefix, any term); the winning strategy is reported. Rank is navigation, not
# evidence. To rephrase or translate a query, call again with the new query.
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PY=${PYTHON:-python3}
command -v "$PY" >/dev/null 2>&1 || PY=python
if [ "${1:-}" = "--exact" ]; then
    shift
    exec "$PY" "$ROOT/scripts/search_lexical.py" --root "$ROOT" exact "$1" --scope "${2:-canonical}" -n "${3:-10}"
fi
exec "$PY" "$ROOT/scripts/search_lexical.py" --root "$ROOT" search "$1" --scope "${2:-canonical}" -n "${3:-10}"
