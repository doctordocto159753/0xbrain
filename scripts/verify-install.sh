#!/usr/bin/env bash
# Linux equivalent of verify-install.ps1 for the model-free deployment. The
# Windows script's semantic-query step is intentionally absent (no embeddings/models).
# All search goes through scripts/search_lexical.py (Agent C), never a bare qmd call.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="$(command -v python3 || command -v python)"

echo "1. repository validation";        "$PY" scripts/validate_repo.py --full >/dev/null && echo pass
echo "2. content-release validation";   "$PY" scripts/validate_content_release.py >/dev/null && echo pass
echo "3. search index present and fresh"
"$PY" scripts/search_lexical.py status | "$PY" -c 'import json,sys; s=json.load(sys.stdin); sys.exit(0 if s["index_present"] and not s["stale"] else 4)' \
  || { echo "index missing or stale: run scripts/refresh-search.sh" >&2; exit 4; }
echo pass
echo "4. lexical and exact search return JSON (model-free path)"
"$PY" scripts/search_lexical.py search "wiki" --scope all -n 3 >/dev/null
"$PY" scripts/search_lexical.py exact "wiki" --scope canonical -n 3 >/dev/null
echo pass
echo "5. no model files present"
"$PY" scripts/search_lexical.py verify-model-free >/dev/null || { echo "model artefacts under the QMD home: deployment must stay model-free" >&2; exit 7; }
echo pass
echo "INSTALLATION VERIFICATION PASS"
