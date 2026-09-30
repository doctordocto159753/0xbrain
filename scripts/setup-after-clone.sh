#!/usr/bin/env bash
# Linux equivalent of setup-after-clone.ps1 for a NON-container workstation (Python 3.12+, Node 22+).
# The VPS path is ./install.sh (Docker). Idempotent.
#   --skip-qmd   skip search setup
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
skip_qmd=0; [ "${1:-}" = "--skip-qmd" ] && skip_qmd=1

[ -f SYSTEM_DESIGN.md ] || { echo "run inside the wiki Git root" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 (3.12+) is required" >&2; exit 1; }
python3 -m pip install --user -r requirements.txt
git config core.hooksPath .githooks
python3 scripts/validate_repo.py --full
python3 scripts/validate_content_release.py

if [ "$skip_qmd" -eq 0 ]; then
  command -v npm >/dev/null || { echo "npm is required (Node 22+)" >&2; exit 1; }
  command -v qmd >/dev/null || npm install -g @tobilu/qmd
  bash scripts/configure-search.sh
  bash scripts/refresh-search.sh
fi
echo "LOCAL STATIC SETUP PASS. Next: scripts/verify-install.sh"
