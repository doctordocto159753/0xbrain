#!/bin/sh
# Register the model-free lexical QMD collections and build the index.
# Linux/macOS equivalent of configure-search.ps1. Idempotent. Never runs
# `qmd embed`. Config: 00-system/configuration/qmd-collections.json
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PY=${PYTHON:-python3}
command -v "$PY" >/dev/null 2>&1 || PY=python
command -v qmd >/dev/null 2>&1 || {
    echo "qmd not found on PATH. Install: npm install -g @tobilu/qmd (Node >= 22)" >&2
    exit 2
}
exec "$PY" "$ROOT/scripts/search_lexical.py" --root "$ROOT" configure
