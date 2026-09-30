#!/usr/bin/env bash
# Linux equivalent of refresh-search.ps1, minus `qmd embed`: re-index the BM25/FTS
# store only. Rebuildable state; never downloads or loads a model.
#   --quiet     print only failures
#   --validate  also run both repository validators afterwards
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
quiet=0; validate=0
for a in "$@"; do case "$a" in --quiet) quiet=1;; --validate) validate=1;; *) echo "unknown option: $a" >&2; exit 2;; esac; done

command -v qmd >/dev/null || { echo "ERROR: qmd is not on PATH" >&2; exit 1; }
# First run on an empty index (fresh container/restore): register collections first.
if ! qmd collection list 2>/dev/null | grep -Eq '(^|[[:space:]])wiki([[:space:]]|$)'; then
  bash "$ROOT/scripts/configure-search.sh" >/dev/null
fi
if [ "$quiet" -eq 1 ]; then qmd update >/dev/null; else qmd update; qmd status; fi

if [ "$validate" -eq 1 ]; then
  python3 scripts/validate_repo.py --full
  python3 scripts/validate_content_release.py
fi
[ "$quiet" -eq 1 ] || echo "SEARCH REFRESH PASS (lexical only)"
