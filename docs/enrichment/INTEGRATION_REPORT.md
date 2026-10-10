# R1 enrichment: integration report (AGENT-0)

Base: `a245b3b2620800b1d2d6cc4be0dba6e44fb5b103`. Contract freeze: CONTRACT-E1
(`docs/enrichment/CONTRACT-E1.md`). Environment: Python 3.13, SQLite 3.45.1 (FTS5 present),
no `qmd`, no Docker daemon, no access to the production VPS or a real corpus.

This report states what was run and what it proves. It is not a deployment approval.

## 1. Evidence (commands run in this session)

| Gate | Command | Result |
|---|---|---|
| Whole suite, one process | `python -m pytest -q -p no:cacheprovider tests` | 3 failed, ~1340 passed, 22 skipped. The 3 failures are the pre-existing baseline ones and need the `qmd` binary: `test_brain_fixtures[01-retrieve-prior-context]`, `[05-provisional-relation]`, `test_remote_semantic::test_remote_tool_list_is_exact_and_flow_works_end_to_end`. Baseline at `a245b3b`: 3 failed, 541 passed, 20 skipped. |
| Enrichment tests | `python -m pytest -q -p no:cacheprovider tests/enrichment` | all pass (A: projection/store/chunking, B: annotation/harness, C: index/graph/retrieval, D: surface/CLI/review, E: upgrade, AGENT-0: contract/adapter/HTTP). |
| Repo validator | `python scripts/validate_repo.py --full` | PASS |
| Content validator | `python scripts/validate_content_release.py` | exit 0 (after the agents' worktrees were removed) |
| Baseline gate | `python scripts/check_against_baseline.py` | OK: validators clean (no baseline file edited) |
| Model-free proof, lexical eval | `scripts/prove_search_model_free.py`, `scripts/run_lexical_eval.py` | SKIP: `qmd not on PATH`. Not run. |
| Real HTTP/OAuth transport | `tests/enrichment/test_remote_extension_http.py` | 11 tools listed (7 + 4); submit, idempotent replay, forged-quote rejection, artifact/chunk read, retrieve by text and by a tag-only term, unknown-argument rejection, `chk:` refused by the base tool. |
| Manual run on a seeded wiki copy | CLI `scan`, `index refresh`, `brain_retrieve` | `git status` clean in the wiki after scan/index (zero source mutation); "فیت پلن" capture found with no ref; held file reported as tier `derivative`, level 5. |
| Upgrade preflight on the same copy | `scripts/upgrade-enrichment.py preflight` | file tree hash unchanged (read-only); Docker/compose/port facts reported `UNVERIFIED`. |

## 2. Integration findings fixed (real defects found at merge)

1. `tests/enrichment/conftest.py` shadowed `tests/conftest.py`: whole-suite collection failed with 7 ImportErrors. The fixture moved to the root conftest.
2. The surface lazily built the real `SourceReader` for fake-store tests, which then (correctly) reported synthetic sources as missing. Those tests now opt out explicitly.
3. The real index and retrieval factories needed `index_path` / `wiki_root`, and retrieval was built without an index. Fixed in the factory arguments.
4. Nothing refreshed the index in `on` mode: retrieval would have stayed on the base-search fallback forever. `brain_retrieve` now runs a bounded incremental refresh (`INDEX_REFRESH_BATCH`) before querying; failure leaves the previous generation in service.
5. `test_upgrade` assumed the deployed base lacked the R1 files; the seeded wiki is a copy of this repo. The rig now strips them to model the pre-R1 deployment.
6. Store double and Protocol aligned with WP-C needs (`derived`, per-revision `review_state`, monotonic `change_marker`).
7. Hermes prompt: the queue re-offers a source whose chunks lack `chunk_annotations`; the prompt now says to annotate every chunk of the page.

## 3. Status table

| Item | Status | Notes |
|---|---|---|
| Projection, deterministic chunking, persistent SQLite store (WP-A) | done, tested on fixtures | scan cost is O(corpus) per pass; no large-corpus benchmark |
| Candidate metadata, validator, CAS/idempotency (WP-B) | done, tested | quote/mention must be exact substrings of the chunk; summaries are never evidence |
| Derived FTS5 index with generations, graph, `brain_retrieve` (WP-C) | done, tested | lexical only; no embeddings |
| Extension tools (4), CLI, local review (WP-D) | done, tested | off = exactly 7 tools, on = 7 + 4; review has no MCP path and refuses in a remote session |
| Hermes skill, prompts, disabled job template, dry-run installer, reference loop (WP-B) | done as files and tests | never run against a real Hermes, provider or the Telegram plugin |
| Upgrade tooling: preflight, plan, backup, apply, verify, rollback (WP-E) | done, tested against git/SQLite/tar for real and Docker/compose by a command double | not a deployment proof |
| Compatibility report of the installed instance (WP-0 gate 1) | **blocker** | needs the VPS. Template and read-only preflight exist; run them with the operator. |
| Real Docker rehearsal | **not run** | opt-in test exists; no daemon here |
| R1.1: Telegram file/voice bridge repair, human review UI, author-confirmation path | **deferred** | WP-F/G/G2; need the real plugin and panel source |
| R2: embeddings / LightRAG | **deferred** | gated by a benchmark on real questions |

## 4. What is not proven

* Retrieval quality on a real corpus. The WP-C benchmark (26 fa/en queries over 18 synthetic docs: Recall@10 0.32 for exact substring, 0.80 for R1 without annotations, 0.94 with hand-written annotations) uses same-author data and stand-in annotations. The +10 point pilot target must be measured on the owner's real questions.
* Annotation quality of a real model.
* Behaviour with `qmd` present (base search path, `ensure_index`), and the two model-free proof scripts.
* Production Docker/Compose/Nginx behaviour, uid/gid layout, OAuth refresh across an upgrade and rollback on the real stack.
* Concurrency across processes beyond the SQLite CAS tests (threads and a SIGKILL test exist).
* Scale: discovery rescans the corpus per pass; `get_chunk` re-loads a source per call.

## 5. Next exact operations

1. Operator: run `scripts/upgrade-enrichment.py preflight` against the real instance (read-only) and fill `docs/enrichment/compat-report.template.md`; resolve any diff against `a245b3b`.
2. Rehearse `plan` + `apply --copy-dir` + `rollback` on a restored copy of the real instance with captures, a held file and a dirty proposal (runbook: `docs/enrichment/runbook.md`).
3. Install with `BRAIN_ENRICHMENT=off`, then `shadow` (CLI scan/index on a copy), then `on` for a 10-artifact pilot; measure on real questions.
4. Only then R1.1 (Telegram bridge, review UI) and the R2 benchmark.
