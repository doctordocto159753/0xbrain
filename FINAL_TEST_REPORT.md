# Final test report (Agent H)

Date: 2026-09-30. Integration branch `claude/ecstatic-planck-50c8vo`
(baseline `9395ed9`). All results below were run on the final code of this
branch unless marked. The earlier "24 tests" figure is obsolete and not used
for comparison.

## 1. Environment

| Item | Value |
|---|---|
| host Python (tests) | 3.11.15 (`.venv`) |
| image Python | 3.12.14 |
| Node / QMD | host v22.22.2 / qmd 2.8.3; image v22.23.3 / qmd 2.8.3 |
| Git (image) | 2.39.5 |
| key packages | mcp 2.2.0, uvicorn 0.54.0, starlette 1.7.0, pydantic 2.13.5, httpx2 2.13.1, PyYAML 6.0.3, pymupdf 1.28.2, python-docx 1.2.0, python-pptx 1.0.2, openpyxl 3.1.5, bs4 4.15.0, lxml 6.1.3, pytest 9.1.1 |
| Docker | Engine 29.3.1, Compose v2 |

Install command (one dev/test environment):

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-remote.txt \
    -r deploy/requirements-image.txt -r requirements-dev.txt
npm install -g @tobilu/qmd@2.8.3
```

## 2. Repository gates

```
$ git diff --check 9395ed9 HEAD                      -> (no output) clean
$ python scripts/validate_repo.py --full             -> PASS: repository structure, metadata, manifest, checksums, and links are valid
$ python scripts/validate_content_release.py         -> PASS: populated wiki content, dispositions, Bases, benchmark, and release structure are valid
$ python scripts/check_against_baseline.py           -> OK: validators clean.
$ python tests/test_validator_mutations.py           -> 102/102 passed
```

`.githooks/known-baseline-errors.txt` is empty (Agent E), so "clean" means
zero validator errors.

## 3. Test suite

```
$ .venv/bin/python -m pytest -q -rs
517 passed, 1 skipped in 127.53s
SKIPPED [1] tests/test_search_lexical.py:307: set WIKI_RUN_SEARCH_EVAL=1 (about 4 minutes)
```

The one skip is the opt-in full lexical evaluation. It was run separately in
the final image (§6), and as `WIKI_RUN_SEARCH_EVAL=1 pytest
tests/test_search_lexical.py::test_full_lexical_evaluation_meets_floor`
→ `1 passed in 221.10s`.
**xfail markers: 0.** No skip hides a known issue: the other skip conditions
are optional externals (`qmd`, `mcp`, conversion libraries, Windows
symlinks), and none of them triggered here.

| file | tests |
|---|---|
| test_brain_fixtures.py (B fixtures on the real backend) | 11 |
| test_brain_surface.py (K3 surface on a real git wiki) | 68 |
| test_capture_claude_states.py | 39 |
| test_capture_core.py | 76 |
| test_capture_integrity.py (D1/D2/D3 mutations) | 38 |
| test_context_compiler.py | 9 |
| test_deploy_static.py | 12 |
| test_evidence_audit.py | 9 |
| test_governance.py (E) | 48 |
| test_io_conversion_exports.py (F) | 13 |
| test_mcp_remote.py (A, HTTP + OAuth) | 12 |
| test_mcp_stdio_characterization.py | 13 |
| test_reconcile_runner.py | 6 |
| test_remote_semantic.py (release gate over HTTP) | 10 |
| test_search_lexical.py (C) | 52 |
| test_validator_mutations.py (pytest-collected) | 102 |

Section 19 security regressions and where they are proven:

- unauthenticated tools/list and tools/call on `/mcp` and `/mcp/`, other
  routes, and `/healthz` contents → `test_unauthenticated_list_and_call_rejected_on_every_route`
- exact tool allowlist over the wire; wiki_read, wiki_get_media,
  wiki_mark_capture_reviewed and QMD get/multi_get/query absent; no
  review/promote → `test_remote_tool_list_is_exact_and_flow_works_end_to_end`,
  `test_denylist_holds_for_any_adapter`, `test_default_adapter_is_exactly_the_six_semantic_tools`
- path/traversal/absolute/Windows refs rejected → `test_read_rejects_paths_and_malformed_refs` (24 cases)
- `../` evidence ref, forged primary and secondary quotes, invalid proposal
  not appended and HEAD unchanged → `test_invalid_proposal_is_rejected_and_not_appended` (15 cases)
- capture commit failure retains the capture; uncommitted state in
  brain_status → `test_capture_commit_failure_keeps_data_and_surfaces_in_status`
- remote session cannot review or promote → `test_remote_session_cannot_run_human_review`
- OAuth:
  - rotation, revocation and restart → `test_refresh_rotation_revocation_and_restart_persistence`
  - PKCE, code reuse, open redirect → `test_pkce_s256_required_code_single_use_and_no_open_redirect`
  - resource and scope binding → `test_token_resource_and_scope_binding_and_revocation`
  - state privacy and secret not logged → `test_auth_state_file_is_private_and_holds_no_bearer_material`
  - bounded state → `test_registration_and_pending_consent_are_bounded`
  - fail-closed startup → `test_startup_configuration_fails_closed`, `test_unprotected_mcp_route_refuses_to_start`

Section 20 data-integrity regressions:

- Persian/English/mixed exactness, reserved headings, CRLF, unknown front
  matter, lossless rewrite after status and derivative changes, original
  bytes unchanged, duplicates unchanged, baseline records still parse →
  `test_capture_integrity.py`
- CRLF through K9 commit and a fresh clone (negative control: fails without
  the `.gitattributes` rule) → `test_crlf_capture_survives_commit_and_fresh_clone`
- derivative SHA provenance → `test_convert_format[*]`
- proposal evidence trace → `test_propose_success_is_candidate_committed_and_readable`
- promotion separate from candidate → `test_promote_is_separate_full_validated_canonical_commit`,
  `test_promote_never_sweeps_in_non_content_paths`

## 4. Authenticated MCP integration (in-process HTTP)

`tests/test_remote_semantic.py::test_remote_tool_list_is_exact_and_flow_works_end_to_end`
runs a real uvicorn server with the production adapter on a real git wiki.
The official `mcp` client performs DCR + PKCE + owner consent, then:

1. `tools/list` returns exactly the six tools.
2. status → capture (committed; the commit touches only that file) → lexical
   search in captures finds it → read → deep reconcile (`verdict: null`) →
   proposal (candidate, committed).
3. A forged proposal is rejected with `isError` and nothing is stored.
4. A path ref is rejected with `E_BAD_REF`.
5. All 14 forbidden names return `isError`.

Result: PASS.

## 5. Docker deployment E2E (disposable host)

Environment-only workarounds, not repository changes: Docker Hub answered
429, so base images came from `mirror.gcr.io` and were tagged locally. Debian
mirrors are blocked in this sandbox, so builds used `--python-base
python:3.12-bookworm` (ships git), `--build-ca` and `--build-network host`.

Fresh install on the final code:

```
$ ./install.sh --domain brain.test --email owner@brain.test --name "E2E Brain" --prefix zb --yes \
    --skip-dns-check --tls-internal --http-port 8080 --https-port 8443 --state-dir /srv/e2e/state \
    --build-ca /root/.ccr/ca-bundle.crt --build-network host --python-base python:3.12-bookworm
EXIT=0   (validators PASS x3, SEARCH REFRESH PASS, brain healthy, HTTPS /healthz ok)
$ curl -k https://brain.test:8443/healthz                     -> {"ok":true}
$ curl -k -X POST https://brain.test:8443/mcp (tools/list)    -> 401
```

The owner then added one canonical record by a normal human commit (the
pre-commit gate first rejected it for a missing inbound link, which was
fixed).

```
$ python scripts/remote_smoke.py --url https://brain.test:8443 --insecure \
    --evidence rec:zb-obj-nil --quote "Project Nil is the working title of the second novel."
ok=true consent_performed=true tools=[brain_search, brain_read, brain_capture,
brain_reconcile_context, brain_propose, brain_status] capture={persisted:true,
commit_state:committed} search_found_capture=true read_verbatim=true
path_ref_rejected=true proposal={ok:true, authority_tier:candidate, commit_state:committed}
```

Human path:

```
$ docker compose run --rm brain python scripts/brain_review.py accept P --actor owner --yes
ERROR: review/promotion is forbidden in a remote session          (server environment)
$ scripts/review.sh accept P --actor owner --yes                  -> committed
$ (owner edits 03-objects/works/zb-obj-nil.md)
$ scripts/review.sh promote P --actor owner --yes
promoted=true paths=["03-objects/works/zb-obj-nil.md"]
git log: capture (mcp) / propose / accept / canonical: promote P (human: owner) / record promotion
canonical commit stat: 1 file changed (only the record)
```

Restart (`docker compose down && up -d`), then the smoke again with
`--reuse-only`: ok=true, consent_performed=false, status not degraded,
capture committed.

In-container checks after this sequence: `git fsck --no-dangling` OK,
validate_repo PASS, content_release PASS, check_against_baseline clean,
verify-install PASS. Search scripts in the image:

- `configure-search.sh` twice OK; `refresh-search.sh --quiet --no-validate`
  twice OK;
- status fresh (disk = indexed per collection);
- `verify-model-free` true, with only `index.sqlite` and `index.yml`;
- lexical and exact hits returned;
- the index persists on `/state/qmd`.

## 6. Search qualification

`scripts/run_lexical_eval.py` in the final image (`--network none`), 2m53s:

| strategy | cases | hit@1 | hit@5 | MRR | false pos. | scope leaks |
|---|---|---|---|---|---|---|
| raw-lex | 68 | 39 | 40 | 0.581 | 0/2 | 0 |
| variants | 68 | 55 | 56 | 0.816 | 0/2 | 0 |
| ladder | 68 | 65 | 66 | 0.963 | 0/2 | 0 |
| exact | 76 | 47 | 49 | 0.632 | 0/2 | 0 |
| ladder+exact | 68 | 66 | 67 | 0.978 | 0/2 | 0 |
| reformulated | 9 | 9 | 9 | 1.0 | 0/0 | 0 |

Remaining misses:

- `ex-06` exact `حافظه جمعی`: known limit, exact is not fuzzy (ezafe);
- `pp-02` Persian question about an English record: known limit,
  cross-language.

This is a synthetic set that measures the mechanism, not real-world quality.
Paraphrase questions reach 8/9 only through the ladder, and 9/9 only with
Claude-side reformulation.

No-model proof (`scripts/prove_search_model_free.py`):

- final image, `--network none`: result PASS, model artefacts none, QMD home
  holds only `cache/qmd/index.sqlite` and `config/qmd/index.yml`;
- host, network namespace + strace active: result PASS, model_file_opens [],
  non_local_connects [].

## 7. Restart, backup/restore, upgrade/rollback (Docker)

Run on the integrated application (before the final documentation-only
commits, same runtime code):

1. **Restart after the access-token fix:** re-smoke with `--reuse-only` →
   ok, consent_performed=false. Before the fix the same test failed: the
   client re-ran consent. That failure is how the defect was found.
2. **Forced commit failure** (`.git/index.lock`): capture
   `persisted:true, commit_state:uncommitted`; status `degraded`,
   `uncommitted=1`.
3. **Backup:**
   ```
   create-backup.sh -> BACKUP OK; uncommitted_paths=1
   docker compose down -v; rm -rf wiki state
   restore-backup.sh ARCHIVE -> RESTORE OK: head=6ecf687 uncommitted=1 (17.8 s)
   ```
   Uncommitted capture sha256: OK. OAuth state sha256: OK. HEAD preserved.
   `git fsck`, validators, reindex, start: PASS. Smoke `--reuse-only`: ok,
   no consent, uncommitted=1 reported. `review.sh flush` committed it; the
   next smoke is not degraded.
4. **Upgrade** (merge of new upstream commits): `UPGRADE OK` (three times).
5. **Conflicting upstream:**
   ```
   upgrade.sh --branch e2e-conflict
   EXIT=3 "CONFLICT (content): Merge conflict in HOME.md" / "merge conflict; nothing was changed"
   ```
   HEAD unchanged, working tree clean, brain healthy again.
6. **Rollback** after a post-upgrade capture:
   ```
   upgrade.sh --rollback -> ROLLBACK OK (revert of the upgrade merge)
   ```
   Upgrade code removed, post-upgrade capture commit kept, gate PASS.

## 8. Exports and conversion

`tests/test_io_conversion_exports.py`: 13 passed (six formats with a SHA-256
header, scanned PDF NEEDS-OCR, legacy refusal, no network, PROV/SKOS/TEI,
TEI unknown id, RO-Crate, public privacy valve).

## 9. Not run

Official Claude custom-connector E2E on a public host (no public DNS/HTTPS
in this environment). Procedure: `docs/CONNECT_CLAUDE.md`, "Release gate".
