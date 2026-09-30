#!/usr/bin/env bash
# Linux equivalent of configure-search.ps1: register the rebuildable QMD collections.
# Lexical only: this script never runs `qmd embed`, `vsearch` or bare `qmd query`,
# so no model is downloaded. Idempotent; the index is rebuildable state.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="$ROOT/00-system/configuration/qmd-collections-v1.1.0.json"
cd "$ROOT"

command -v qmd >/dev/null || { echo "ERROR: qmd is not on PATH (npm install -g @tobilu/qmd, Node >= 22)" >&2; exit 1; }

# name<TAB>path<TAB>mask<TAB>context<TAB>include(0|1). Falls back to a single `wiki`
# collection, the one wiki_mcp_server.py queries, when no collection config ships.
specs="$(python3 - "$CONFIG" <<'PY'
import json, os, sys
cfg = sys.argv[1]
rows = []
if os.path.exists(cfg):
    for c in json.load(open(cfg, encoding="utf-8")).get("collections", []):
        rows.append((c["name"], c["path"], c.get("mask", "**/*.md"), c.get("context", ""),
                     "1" if c.get("include_by_default") else "0"))
else:
    rows.append(("wiki", ".", "**/*.md", "Living wiki: canonical records, notes, claims, relations.", "1"))
for r in rows:
    print("\t".join(r))
PY
)"

existing="$(qmd collection list 2>&1 || true)"
managed=" wiki "
while IFS=$'\t' read -r name _; do [ -n "$name" ] && managed="$managed$name "; done <<<"$specs"
for name in $managed; do
  if grep -Eq "(^|[[:space:]])${name}([[:space:]]|$)" <<<"$existing"; then
    echo "removing rebuildable collection registration: $name"
    qmd collection remove "$name" >/dev/null
  fi
done

while IFS=$'\t' read -r name path mask context include; do
  [ -n "$name" ] || continue
  abs="$(cd "$ROOT/$path" && pwd)"
  echo "adding $name from $abs"
  qmd collection add "$abs" --name "$name" --mask "$mask" >/dev/null
  [ -z "$context" ] || qmd context add "qmd://$name" "$context" >/dev/null
  if [ "$include" = "1" ]; then qmd collection include "$name" >/dev/null || true
  else qmd collection exclude "$name" >/dev/null || true; fi
done <<<"$specs"

qmd update >/dev/null
echo "QMD COLLECTIONS CONFIGURED (lexical only; no embedding was run)"
qmd collection list
