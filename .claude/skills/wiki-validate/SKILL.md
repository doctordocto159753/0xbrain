---
name: wiki-validate
description: Run deterministic validation and prepare a semantic audit report. Read-only unless the user separately approves repairs.
---

# Wiki Validate

1. Run `python scripts/validate_repo.py --full`.
2. Run `python scripts/search_lexical.py status` (index freshness; `stale` must be false).
3. Run the lexical regression set: `python scripts/run_lexical_eval.py` (synthetic fixture, clean temporary index, no model). Then run any instance-specific queries with `python scripts/search_lexical.py search "<query>" --scope canonical`.
4. Check:
   - duplicate or drifting terms;
   - unsupported claim upgrades;
   - repeated AI reports treated as corroboration;
   - stale current-version labels;
   - source/derivative confusion;
   - public records depending on private or unverified sources;
   - broken lineage and relation links.
5. Produce `_audits/<date>--validation.md`.
6. Do not silently repair canonical content. Separate deterministic repair proposals from interpretive decisions.
