#!/usr/bin/env bash
# Linux equivalent of verify-install.ps1 for the model-free deployment. The
# Windows script's semantic-query step is intentionally absent (no embeddings/models).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="$(command -v python3 || command -v python)"

echo "1. repository validation";        "$PY" scripts/validate_repo.py --full >/dev/null && echo pass
echo "2. content-release validation";   "$PY" scripts/validate_content_release.py >/dev/null && echo pass
echo "3. QMD status and collection";    qmd status >/dev/null
qmd collection list | grep -Eq '(^|[[:space:]])wiki([[:space:]]|$)' || { echo "missing QMD collection: wiki" >&2; exit 4; }
echo pass
echo "4. lexical search returns JSON"
out="$(qmd query 'lex: wiki' --no-rerank --json -n 3 --collection wiki)"
"$PY" -c 'import json,sys; json.loads(sys.stdin.read()); print("pass")' <<<"$out"
echo "5. no model files present"
cache="${XDG_CACHE_HOME:-$HOME/.cache}/qmd"
if [ -d "$cache" ] && find "$cache" \( -iname '*.gguf' -o -iname '*.onnx' -o -iname '*.safetensors' \) | grep -q .; then
  echo "model files found under $cache: deployment must stay model-free" >&2; exit 7
fi
echo pass
echo "INSTALLATION VERIFICATION PASS"
