---
id: wiki-handoff-2026-09-30-agent-c-search
type: handoff
title: "Agent C: model-free search and lexical evaluation"
branch: claude/new-session-fnifpb
commit: "9395ed9 (base; this handoff ships in the next commit)"
corpus_snapshot: wiki-corpus-empty
status: active
created: 2026-09-30
updated: 2026-09-30
schema_version: 1.0.0
---

# Agent C: model-free search and lexical evaluation

## Task and intended result

Baseline `9395ed9f26729910f5ccc868c8598fad54811e19`. Keep QMD, remove every
mandatory model from the default search path, add Linux helpers, K3 scope
behaviour, a checked-in lexical evaluation set (Persian, English, mixed, ...),
and a clean-room no-model proof. No search engine was replaced.

## Files changed

New: `00-system/configuration/qmd-collections.json`, `scripts/search_lexical.py`,
`scripts/run_lexical_eval.py`, `scripts/prove_search_model_free.py`,
`scripts/{configure,refresh,search-wiki}.sh`, `tests/test_search_lexical.py`,
`tests/search_eval/{queries.json,corpus/**.md.fixture}` (30 synthetic records, 79 cases).

Modified (search-only edits): `configure-search.ps1` (delegates to Python),
`refresh-search.ps1` (`qmd embed` only with explicit `-WithEmbeddings`),
`search-wiki.ps1` (lexical; no bare `qmd query`), `verify-install.ps1`,
`setup-after-clone.ps1`, `run-semantic-benchmark.py` (needs `--allow-models`),
`SEARCH_GUIDE.md`, `SETUP_GUIDE_WINDOWS.md`, `INSTANTIATE.md`, `SYSTEM_DESIGN.md`
(section 6 rows), `.claude/skills/wiki-{search,intake,validate}/SKILL.md`
(model-backed commands replaced), `RELEASE_READINESS_REGISTER.md` (gate label
"embedded" removed). K1-K9 contracts and all server/capture code untouched.

## Reused code

`_qmd_query_lexical` idea (typed `lex:` + `--no-rerank`), `tool_wiki_exact` zone list,
the `wiki` collection name, `context_pack` conventions. `wiki_mcp_server.py` and
`context_pack.py` are NOT modified; they still call qmd directly (see integration).

## Decisions accepted

- Collections are root-relative with masks (`wiki` = 03-06; `wiki-source-records`;
  `wiki-derivatives`; `wiki-captures`), so paths are repo-relative and an absent
  numbered layer is valid. Index home `_search/qmd` (git-ignored), overridable with `WIKI_QMD_HOME`.
- `canonical` scope = the five frozen K3 zones, returned as tiers (canonical-record,
  source-record, derivative), never merged by score. `captures` and `all` per K3.
- Lexical = relaxation ladder over the whole scope (strict with orthographic variants,
  content terms, Persian stem prefix, any-term); the winning `strategy` is returned.
  Claude reformulates; there is no server-side expansion.
- Exact fallback is normalisation-aware (NFKC, case, ک/ك, ی/ي/ى, ة, digit scripts,
  harakat, tatweel, ZWNJ) and returns one result per record with `hit_count`/`lines`.
- Code guard `assert_model_free` refuses embed/vsearch/pull/mcp/bare query.

## Validation commands and outcomes

Environment: qmd 2.8.3 (npm, isolated prefix), Node 22.22.2, Python 3.11.15.

```text
git rev-parse HEAD                              9395ed9 (baseline, before edits)
python scripts/validate_repo.py --full          PASS (before and after)
python scripts/validate_content_release.py      PASS (before and after)
python scripts/check_against_baseline.py        OK (before and after)
python tests/test_validator_mutations.py        102/102 before and after
pytest -q                                       after: 177 passed, 1 skipped (24 baseline + 102 mutation + 51 new); needs `pip install -r requirements.txt` (PyYAML was absent in the fresh container: collection error until installed)
WIKI_RUN_SEARCH_EVAL=1 pytest -k full_lexical  passed (185 s): floor hit@5 >= 64, zero leaks, ladder > raw
python scripts/run_lexical_eval.py              see metrics
python scripts/prove_search_model_free.py       PASS
scripts/configure-search.sh; refresh-search.sh  PASS on the real (empty) kit; "stale": false
```

Evaluation (79 cases, 68 with expectations plus 2 negatives, top_k 10, clean index):

| strategy | hit@1 | hit@5 | MRR | scope leaks |
|---|---|---|---|---|
| raw `qmd search` (upstream) | 39/68 | 40/68 | 0.581 | 0 |
| raw typed `lex:` (old MCP route) | 39/68 | 40/68 | 0.581 | 0 |
| cleaned + variants | 55/68 | 56/68 | 0.816 | 0 |
| + relaxation ladder (default) | 65/68 | 66/68 | 0.963 | 0 |
| ladder, then exact if empty | 66/68 | 67/68 | 0.978 | 0 |
| reformulated paraphrase (9 cases) | 9/9 | 9/9 | 1.0 | 0 |

Two failures under the recommended route, both documented limits: exact phrase across
an ezafe (`حافظه جمعی` vs `حافظه‌ی جمعی`), and a Persian question whose records are in English.

Caveats that limit these numbers: the corpus (30 records) and the queries have one author;
the reformulations were written knowing the corpus; the any-term step will be much noisier
on a real corpus (upstream measured 3/30 lexical recall on natural-language questions over
a real instance). This is a regression and mechanism check, not a recall estimate.

No-model evidence (`prove_search_model_free.py`): empty HOME, network namespace with no
route (`unshare -rn`), `strace -f`. Result PASS: configure, lexical (fa/en), scope=all,
exact and guard checks succeed; 0 model-file opens, 0 non-local connects, no native
llama binding loaded, cache holds only `index.sqlite` and `index.yml`. Negative control:
`qmd embed` in the same setup is caught (DNS connect to 8.8.8.8, `node-llama-cpp` gguf modules).

Persian findings (measured on qmd 2.8.3; details in `SEARCH_GUIDE.md` section 4):
no folding of ک/ك, ی/ي, or digit scripts; ZWNJ in a query matches nothing (index splits on it);
harakat inside indexed text split words apart (bare form cannot match: exact fallback needed);
`name-1402.pdf` style tokens match nothing unquoted; multiple `lex:` lines are OR-ed.

## Unresolved findings

1. Vocalised (harakat) text is unreachable by lexical search; only exact finds it.
2. Cross-language retrieval is not bridged; the caller must reformulate.
3. `آ` vs `ا` and `أ/إ` are not folded (deliberate: different words).
4. PowerShell helpers are edited but unexercised on Windows (no PowerShell here).
5. CI (`validate.yml`) does not install qmd; the qmd integration tests skip there.
6. The frozen-K3 "canonical = five zones" includes machine-extracted derivatives (`02-sources/text`).
   Treated as a tier with an explicit label; confirm this is intended.

## Negative constraints

Never `qmd embed`, `vsearch`, `pull`, bare `qmd query`, or `qmd mcp` in the default path.
Rank is navigation only; no numeric score is returned. No path is accepted from a caller
by search; `brain_read` mapping stays with Agents A/B.

## Contract change requests and integration notes (for Agent H)

- CCR-1 (security/K3): `.mcp.json` registers `qmd mcp`. Measured tools: `query` (accepts
  `vec:`/`hyde:` sub-queries, model-backed) and `get`/`multi_get` (read any indexed file by
  path, bypassing `brain_read` ref-only). Remove it from the remote surface and from the
  default `.mcp.json`; expose only `brain_search`.
- CCR-2: `wiki_mcp_server.tool_wiki_search` uses raw `lex:` (40/68 on this set) and
  `tool_wiki_exact` is case-sensitive and stops at the first hit per file. Route
  `brain_search` through `search_lexical.search(query, mode, scope, n, root)` instead;
  returns `groups.{canonical,captures}.results[]` with `path,id,tier,zone,rank,snippet`,
  plus `strategy`, `attempts`, and `authority_note`. Map `path` to the opaque `ref` in the wrapper.
- `n` applies per tier (lexical) or per record (exact); `more_available`/`truncated` are reported.
- `brain_status`: call `search_lexical.index_freshness(root)` (`stale`, per-collection counts).
- Refresh after every noncanonical commit that adds captures/records: `search_lexical.refresh(root)`
  (cheap); the K9 auto-commit path should trigger it.
- Deploy image needs Node >= 22 and `@tobilu/qmd@2.8.3`; set `WIKI_QMD_HOME` to a persistent volume
  or leave the default `_search/qmd`. No model cache is needed.

## Next exact operation

Agent H: merge, then wire `brain_search` to `search_lexical.search`, apply CCR-1, and run
`python scripts/prove_search_model_free.py` and `python scripts/run_lexical_eval.py` in the
deploy image.

## Rollback

`git revert` the Agent C commit. The index is disposable: delete `_search/`.
