# Agent 0 — Reuse Audit, Feature Preservation Matrix & Contract Freeze

Status: **complete, awaiting acceptance.** Supersedes the earlier "blocked" report.
Evidence labels: **[V]** verified by running/reading code in this session,
**[I]** inference, **[U]** unverified (delegated to a named agent).

## 1. Verdict

1. Living Wiki is a strong base. Governance, validators, capture core, proposal
   audit, exports and skills are real, deterministic and model-free by default.
   Baseline is green [V].
2. **Model removal is mostly already done.** No default path needs a local
   model [V]. The only default-path model touchpoint is `qmd embed` in
   `scripts/refresh-search.ps1` (a Windows helper), which is replaced by
   configuration, not code.
3. The real work is narrower than the brief assumes:
   - **Remote transport + auth** (the server is stdio, protocol `2024-11-05`, no auth).
   - **A tightened remote tool surface**: `wiki_propose` accepts free text,
     while the proposal schema and `evidence_audit.py` require structured
     evidence; `wiki_mark_capture_reviewed` lets the model record a *human*
     review with a caller-supplied `actor`.
   - **Capture states** `needs_transcription / needs_description /
     needs_interpretation` do not exist; Claude-produced text has no
     provenance label.
   - **Linux/VPS packaging** (everything operational is `.ps1`).
   - **Test debt**: the capture core and MCP server have no tests in the repo.
4. Three brief assumptions are false at this commit (Section 3, "Corrections").
5. No rewrite is approved. Every "replace" below is empty.

## 2. Sources inspected

| Role | Repo | Commit |
|---|---|---|
| Target (0xBrain) | `doctordocto159753/0xbrain` | `d29bfd0` (was empty: `.gitkeep` + blocker note) |
| Reuse base | `mozareeduge/living-wiki-kit` `main` | **`02399e7`** (114 files, ~630 KB) |

The base was merged into the 0xBrain branch with `--allow-unrelated-histories`
(remote `living-wiki`), so upstream history stays inspectable and future
upstream fixes can be merged. The accepted baseline SHA is the commit that
adds this file (recorded in the PR description).

## 3. Baseline status [V]

| Check | Result |
|---|---|
| `python scripts/validate_repo.py --full` | PASS (warnings: openspec/handoff files lack frontmatter) |
| `python scripts/validate_content_release.py` | PASS |
| `python scripts/check_against_baseline.py` (pre-commit/CI gate) | OK |
| `python tests/test_validator_mutations.py` | 102/102 |
| `pytest tests` (context compiler, evidence audit, reconcile runner) | 24 passed |
| `instantiate.py --prefix zb` on a scratch copy, then validate | PASS |
| MCP stdio smoke: initialize, capture_text, propose, exact | works; capture record validates |
| `qmd` 2.8.3 (npm, MIT): `qmd search` and `qmd query "lex: …" --no-rerank --json` | works, ~0.2 s, **no model downloaded** (cache holds only the SQLite index), stdout is clean JSON, warnings on stderr |
| `to_md.py` on a .docx (deps: pymupdf, python-docx, python-pptx, openpyxl, bs4, lxml) | works, no network |

Corrections to the brief:

- **C1. No semantic benchmark fixtures ship.** `semantic-benchmark-v1.1.0.json` and
  `qmd-collections-v1.1.0.json` are referenced by scripts but absent
  (`00-system/configuration/` holds only `content-release.json`;
  `validate_content_release.py:221` looks for a differently named
  `semantic-benchmark.json`). "Fixtures as evaluation assets" does not hold; Agent C
  must author the lexical evaluation set. The upstream handoff records
  lexical-only recall of 3/30 on the *instance* corpus, so lexical quality on
  natural-language questions is a known weakness, mitigated by Claude
  reformulating queries.
- **C2. Numbered layers mostly do not exist in the kit.** Only `00-system/`,
  `07-genesis/`, `_captures/`, `_proposals/` are present. `01-inbox/captures/`
  appears on the first capture; `02-sources`…`06-relations`, `_originals/` are
  created by use. Validators tolerate absence. "Preserve the layout" means
  preserving these names and the validator's expectations, not scaffolding empty dirs.
- **C3. Capture core and MCP server have no tests in the repo.** `.gitignore`
  excludes `scripts/capture/tests/` and `fixtures/`. `wiki_capture.py` (797
  lines) and `wiki_mcp_server.py` (582 lines) are effectively untested here.
  "Reusable until tests prove otherwise" is therefore unproven; characterization
  tests come first.

## 4. Feature-preservation matrix

Model required: N = no, O = optional path only. Decision key: P preserve as-is,
W wrap, C configure, X extend (additive), D defer.

| Capability | Existing files | Existing tests | Dependency | Model | Decision | Reason | Owner |
|---|---|---|---|---|---|---|---|
| Instantiation / prefix rewrite | `scripts/instantiate.py`, `INSTANTIATE.md` | mutation suite (partial) | PyYAML | N | P + X | Refuses to reseed populated corpus. Rewrites `mozare`→`wiki` in docs; 0xBrain needs a fixed prefix chosen once at install. Installer calls it; do not fork it | G |
| Record grammar, schemas, templates, vocab | `00-system/schemas/*`, `templates/*`, `policies/CONTROLLED_VOCABULARY.md` | via validators | – | N | P | Frozen | E |
| Authority hierarchy, handoffs, genesis | `SYSTEM_DESIGN.md`, `07-genesis/`, `_captures/HANDOFF*`, `wiki-handoff` | via validators | – | N | P | Frozen | E |
| Repo validation | `validate_repo.py` (910 l) | 102 mutation tests | PyYAML | N | P | Green | E |
| Content-release gate | `validate_content_release.py`, `content-release.json` | in mutation suite | – | N | P | Baseline errors file carries mozare-instance entries (`known-baseline-errors.txt`); reset to empty for 0xBrain | E |
| Pre-commit / CI gate | `.githooks/*`, `check_against_baseline.py`, `validate.yml` | in mutation suite | `python` on PATH | N | C | Hook calls `python`; Debian VPS usually has only `python3`. Configure, don't rewrite | E |
| Capture core (text, media, dedupe, state machine, orphan recovery, atomic write) | `scripts/capture/wiki_capture.py` | **none in repo** | stdlib | N | P + X | Add states/labels only additively; write tests first | D |
| STT contract | `stt_contract.py` | none | faster-whisper (lazy) | O | C | Auto path gated by absent `stt-benchmark-pass.json`; manual adapter default. Add a `claude` adapter label that stores text as candidate | D |
| OCR contract | `ocr_contract.py` | none | rapidocr (lazy) | O | C | Optional, not in requirements; keep, off by default | D |
| Telegram/Hermes hook | `telegram_capture_hook.py` | none | – | N | D | Not part of the Claude-remote path | – |
| MCP tool logic | `wiki_mcp_server.py` (12 tools) | **none** | stdlib, `qmd` | N | W | `DISPATCH` functions take a dict and return a JSON string: wrap unchanged. Transport code (`handle`, `main`) is what gets replaced by the SDK | A |
| Exact search | `tool_wiki_exact` | none | – | N | P | Linear grep over five zones; fine at personal scale. Note: case-sensitive, first-hit-per-file | C |
| QMD lexical search | `_qmd_query_lexical`, `tool_wiki_search`, `context_pack.default_search` | `test_context_compiler` (fake search) | `qmd` (Node ≥22) | N | C | Verified model-free. Requires collection `wiki` to exist, but the collections config is absent (C1) | C |
| QMD semantic/hybrid | `SEARCH_GUIDE.md` §4, `refresh-search.ps1` (`qmd embed`), `run-semantic-benchmark.py` | none | qmd models | Y | D | Off by default, documented as optional | C |
| Proposal queue | `tool_wiki_propose`, `_proposals/proposals.jsonl`, `proposal_schema.json` | – | – | N | W + X | Server accepts 3 of 8 schema kinds with free-text body only; new wrapper must take structured fields | A/E |
| Evidence audit | `evidence_audit.py` | `test_evidence_audit` (≈24 total pytest) | – | N | P | Reuse as submit-time validator (import its checks) | E |
| Reconciliation core | `reconcile_runner.py`, `wiki-reconcile` skill | `test_reconcile_runner` | – | N | P | This is corpus-audit planning (batches, exact-once), *not* semantic reconciliation; the package builder is new | B/E |
| Context packer / graph index | `context_pack.py`, `build_graph_index.py` | `test_context_compiler` | sqlite3 | N | W | Basis for the reconciliation package (seed record → 1-hop neighborhood, budgeted, reason-tagged) | B |
| File-to-md | `scripts/file-to-md/to_md.py` | none | pymupdf, docx, pptx, openpyxl, bs4 | N | P | Header records method, bytes, date; **no source SHA-256**. MarkItDown comparison deferred (Section 6) | F |
| Interchange export | `export_interchange.py` | – | – | N | P | PROV-O, SKOS, TEI, RO-Crate check. Namespace `living-wiki-kit.local` to be revisited | F |
| Public export valve | `export_public.py` | – | – | N | P | Gated on `visibility: public` | F |
| Holdings / census / retier / drift | `report_holdings.py`, `retier_holdings.py`, `schema_drift_fixer.py`, `check_research_spans.py` | mutation suite | – | N | P | Ops tools | E |
| Faithfulness benchmark | `run_faithfulness_benchmark.py` | – | `openai` → OpenRouter | **Y (remote LLM)** | D | Developer eval only; keep out of the deploy image; fixture missing (C1) | – |
| Claude skills & guides | `.claude/skills/wiki-*` (8), `CLAUDE.md`, `AGENTS.md`, `GPT_WORKFLOW.md` | – | – | N | X | Source for standing instructions; skills assume a filesystem-attached Claude Code, not remote MCP | B |
| Backup / setup / search helpers | `create-backup.ps1`, `setup-after-clone.ps1`, `verify-install.ps1`, `search-wiki.ps1`, `configure-search.ps1`, `refresh-search.ps1` | – | PowerShell | see below | P + X | Keep; add Linux `.sh` equivalents. `refresh-search.ps1` runs `qmd embed`: Linux variant must run `qmd update` only | G |
| Remote transport/auth | none | – | – | N | **New** | Real gap | A |
| Docker/Caddy/deploy/docs | none | – | – | N | **New** | Real gap | G |

## 5. Model-removal matrix [V unless marked]

| Inference | Where | Default today | Disposition |
|---|---|---|---|
| Local embeddings | `qmd embed` (`refresh-search.ps1`), `qmd vsearch`, bare `qmd query` (SEARCH_GUIDE) | `embed` runs in the Windows refresh helper | **Remove from default**; Linux refresh = `qmd update`; document semantic as optional |
| Reranking | `--no-rerank` in `_qmd_query_lexical` and `context_pack` | already off | Keep flag; assert it in a test |
| Query expansion | bare `qmd query` (minutes, model-backed); typed `lex:` avoids it | MCP path already typed | Keep typed `lex:`; forbid bare `qmd query` in server code |
| STT | `FasterWhisperAdapter`, lazy import | disabled until benchmark file exists; `manual` default | **Delegate to Claude** (Claude reads audio only if it can access it; otherwise state `needs_transcription`) + manual fallback |
| OCR | `RapidOCAdapter`, lazy import | not installed | **Delegate to Claude** for literal extraction when the image is viewable; else `needs_transcription`; keep RapidOCR optional |
| Vision / description | none: manual-only by design | n/a | **Delegate to Claude**; `needs_description` |
| Interpretation | none | n/a | Claude; `needs_interpretation` |
| Remote LLM | `run_faithfulness_benchmark.py` (OpenRouter) | dev only | Defer; exclude from image |
| Conversion | `to_md.py` | no model | Nothing to remove |

Capture states today: `received, processing, transcribed, described,
needs-review, processing-failed, reviewed, parked, promoted`, with separate
`transcription_state: not-requested|pending|complete|needs-review|failed`.
The three `needs_*` states are **additive and not yet defined**; see contract K4.

## 6. External reuse recommendations

Versions from registry lookups this session [V]: `mcp` 2.2.0, `fastmcp` 4.0.10,
`markitdown` 0.1.8, `supergateway` 4.0.0, `@tobilu/qmd` 2.8.3 (MIT). Pocket ID
release/license lookup returned nothing through the proxy [U]. Maintenance and
license checks for the rest are **not yet done** [U]; Agent A must record them.

- **Agent A spike order** (adopt exactly one framework):
  1. Official `mcp` SDK v2 wrapping `DISPATCH`, with its auth primitives.
  2. FastMCP if the SDK's auth leaves substantial OAuth/DCR glue.
  3. Supergateway only as a *timeboxed* 1-hour baseline: it fixes transport
     but, **[I]** not Claude's OAuth requirement, and Caddy cannot supply OAuth
     discovery/DCR by itself. Adopt only if paired auth stays simple.
  Pocket ID only if the chosen path needs an external IdP. Acceptance is a
  real connection from official Claude, not a local client test; if that
  cannot be tested from a sandbox, say so and hand the user a verified checklist.
- **MarkItDown**: **Defer/keep `to_md.py`.** It works, has no models or network,
  is 240 lines; its docstring claims pdf/epub (pymupdf), docx, pptx, xlsx, html. MarkItDown's possible benefit is format breadth [U].
  Comparison is Agent F's optional task, with a fixed corpus and provenance
  checklist. The kit's gap is **source SHA-256 in the header**, which is fixable in
  ~5 lines regardless of converter.
- **QMD**: keep, lexical only.
- **Caddy**: adopt at deploy time.
- Rejected by default: mcp-auth-proxy (Redis), full QMD semantic stack,
  replacing Living Wiki with another KB.

## 7. Frozen shared contracts

**K1 Layout/grammar.** Section 4 of the brief plus the shipped schemas are
frozen. Changes need an entry in `07-genesis/`.

**K2 Stable low-level units** (import, do not rewrite): `wiki_capture.{capture_text,
capture_media, read_capture, list_captures, set_state, record_transcript,
record_description, validate_record, recover_orphans}`,
`evidence_audit` validators, `tool_wiki_read/exact/search` bodies,
`context_pack`, `build_graph_index`, `to_md.py`, both exporters.

**K3 Remote tool surface** (thin wrappers, names frozen):

| Tool | Maps to | Notes |
|---|---|---|
| `brain_search(query, n, mode="lexical"\|"exact")` | `tool_wiki_search`, `tool_wiki_exact` | Results carry `authority_note`; captures excluded unless `scope="captures"` |
| `brain_read(path \| capture_id)` | `tool_wiki_read`, `read_capture`, `wiki_get_media` | Read-only |
| `brain_capture(kind, text?, media?, language_hint)` | `capture_text`/`capture_media` | Always `received`; channel fixed `mcp` |
| `brain_reconcile_context(seed_id, hops, query?)` | `context_pack` + package (K5) | Read-only, budgeted |
| `brain_propose(kind, fields…)` | proposal queue | Structured per `proposal_schema.json`; **validated at submit** by `evidence_audit` logic; reject with reasons instead of queueing junk |
| `brain_status()` | new, trivial | counts, validator status, search index freshness, pending `needs_*` captures |

**Not exposed remotely:** `wiki_mark_capture_reviewed` (a human decision with
an unauthenticated `actor` string). Review and promotion stay CLI/human. Also
not exposed: any write to canonical zones.

**K4 Capture states.** Additive only. Interpretation gaps are recorded in
`transcription_state` / a new list field, not by renaming existing states;
`schema_version` stays `1.0.0` unless a breaking change is unavoidable (readers
reject unknown versions). Claude-produced transcript/description is stored in
the existing separate sections with a method label `claude` and
`transcription_reviewed: false`. Literal and interpretive fields remain separate.

**K5 Reconciliation package** (JSON, read-only, no verdicts): `seed`,
`canonical_records[]`, `claims[]`, `relations[]`, `source_records[]`,
`captures[]`, `superseded[]`, `open_proposals[]`, `unresolved[]`, `chronology[]`
(genesis/handoff refs). Every item has `id`, `path`, `authority_level`, `reason`
(as `context_pack` already emits). Budget default 16 000 est. tokens, 30 records.

**K6 Auth boundary.** Single owner. Unauthenticated requests get nothing,
including `brain_status`. Secrets only via `.env` (git-ignored); never in the repo.

**K7 Search.** Lexical BM25 (`qmd search` / typed `lex:`) + exact. Scores are
navigation. `qmd embed`, `vsearch`, bare `query` never run by default.

**K8 Deployment names** (proposed): env `BRAIN_DOMAIN`, `BRAIN_DATA_DIR`
(repo checkout), `BRAIN_PREFIX`, `BRAIN_OWNER_EMAIL`, `BRAIN_AUTH_*`
(defined by Agent A); compose services `caddy`, `brain`, optional `auth`.
Container needs Python 3.12 + Node ≥22 (qmd).

**K9 Git history (open, default chosen).** Nothing in the base commits
server-written files. Default: the server writes to the working tree only;
a snapshot job commits `01-inbox/` and `_proposals/` to a dedicated branch
through the existing hook gate; canonical promotion stays human PRs.
The user may overrule.

## 8. File ownership

| Agent | Owns | Must not touch |
|---|---|---|
| A mcp-auth | new `brain_server/` (or `scripts/brain_mcp/`), auth config, protocol tests | existing `scripts/wiki_mcp_server.py` bodies (import only; edits go through D/C/E owners) |
| B claude-surface | new `docs/claude/`: standing instructions, connector guide, tool-usage prompts; adapt skill text | code |
| C search | `scripts/configure-search.sh`, `refresh-search.sh`, search eval set, QMD config JSON, `SEARCH_GUIDE.md` | MCP transport |
| D capture | `scripts/capture/**`, new `tests/test_capture_*.py` | validators |
| E governance | `scripts/validate_*.py`, `evidence_audit.py`, `.githooks`, `.github`, `known-baseline-errors.txt`, `proposal_schema.json`, `tests/test_validator_mutations.py` | capture |
| F io | `scripts/file-to-md/**`, `export_*.py`, conversion tests | – |
| G deploy | `Dockerfile`, `compose.yaml`, `Caddyfile`, `install.sh`, `scripts/*.sh`, `docs/deploy/**`, `instantiate` wrapper | app code |

Shared hotspots: `requirements.txt` (E owns, others request lines), `SYSTEM_DESIGN.md`
/ `CLAUDE.md` / `AGENTS.md` (B proposes, E merges), `instantiate.py` TARGET_GLOBS
(any new file with `mw-`/`mozare` strings must be added, or it will leak the
old prefix), the `.gitignore` (D/E).

## 9. Branch plan

Not created yet: creation waits for acceptance. After acceptance, from the
accepted SHA:

```sh
for b in a-mcp-auth b-claude-surface c-search d-capture e-governance f-io g-deploy; do
  git branch agent/$b <accepted-sha>
done
```

## 10. Risks

1. **Auth is the critical unknown** and cannot be proven without an actual
   official-Claude connection to a public HTTPS host.
2. Untested capture/MCP code: D and A must add characterization tests before
   modifying behavior.
3. Instantiate leakage: `MOZARE_WORDS` blanket-replaces "mozare" in docs; any
   new doc or script listing must be reviewed against it.
4. Lexical recall (3/30 upstream, instance-level) makes Claude-side query
   reformulation and `brain_search` result guidance important.
5. The `proposals.jsonl` append has no lock; fine for one owner, revisit if
   concurrent clients appear.
6. `known-baseline-errors.txt` contains another instance's tolerated errors and
   must be emptied for 0xBrain, in the same commit that proves validators pass.

## 11. Instructions to Agents A–G

- **All:** branch from the accepted SHA; run the six baseline commands in
  Section 3 before and after; write characterization tests before changing
  behavior; answer the reuse-first questions in your PR; do not edit files you
  do not own.
- **A:** timeboxed spike per Section 6; deliverable is a working remote server
  exposing K3 through wrapped `DISPATCH` functions, plus a written comparison
  and a connection test.
- **B:** derive standing instructions from `CLAUDE.md` + skills; state
  what Claude does versus what the server does, and how `needs_*` items are handled.
- **C:** author lexical eval set and collections config (C1); Linux refresh
  scripts without `qmd embed`; test that no model file is downloaded.
- **D:** tests for existing capture core first; then additive K4.
- **E:** empty the baseline file, make hook interpreter-portable, move
  proposal validation into an importable function for `brain_propose`.
- **F:** add source SHA-256 to conversion headers; MarkItDown comparison
  optional and evidence-based.
- **G:** Docker/Caddy/install/backup/restore/upgrade docs; consume A's auth choice.
