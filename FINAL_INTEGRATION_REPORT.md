# Agent H: final integration report

## 1. Verdict

**CONDITIONAL PASS — blocked only on real official-Claude external E2E.**

Every locally available release gate passes, including a fresh
Docker/Caddy/HTTPS install, the authenticated MCP flow with the official MCP
client, restart, backup → destroy → restore, upgrade/conflict/rollback, and
the human promotion path (evidence: `FINAL_TEST_REPORT.md`). No public
DNS/HTTPS host was available in the integration environment, so the
official Claude custom-connector test was not performed. Its exact procedure
is `docs/CONNECT_CLAUDE.md` ("Release gate").

## 2. Branch and final SHA

- Integration branch: `claude/ecstatic-planck-50c8vo`. This is the branch the
  session was required to push to. The brief suggested `integration/a-g`;
  the name is the only difference.
- Created from the accepted Agent-0 baseline
  `9395ed9f26729910f5ccc868c8598fad54811e19`.
- Final SHA: see the PR head. The report ships in the final commit, so it
  cannot name its own SHA.

## 3. Source PRs and heads integrated

| Agent | PR | Head | Responsibility |
|---|---|---|---|
| F | doctordocto159753/0xbrain#3 | `ca07c08062752bf9d4f717211563c5d3e2d32874` | file conversion + exports |
| A | doctordocto159753/0xbrain#4 | `1cc33772a229075dff7c637118fb37c5c6dd348f` | Remote MCP + OAuth |
| D | doctordocto159753/0xbrain#5 | `4b6a1a0321c9417165dee15665bc2658b0e5d980` | capture characterization + K4 |
| B | doctordocto159753/0xbrain#6 | `a6d05d042c0ee44d92f896949703eb982574f3c3` | K3 Claude semantic surface |
| E | doctordocto159753/0xbrain#7 | `11a11e4272d430438fcee90b17e0ca06a974499b` | governance, proposals, K5, K9 |
| C | doctordocto159753/0xbrain#8 | `1610f69a34c38a170ee1b5ed69c564c973147b91` | model-free search |
| G | doctordocto159753/0xbrain#9 | `d8ebb6d8e4afd2e1112cedade3be98019572d274` | VPS deployment |

All seven heads were fetched from `refs/pull/N/head` and their SHAs matched.
All seven branched from the baseline.

## 4. Merge order

F → D → E → C → B → A → G, the suggested order. Each head is an explicit
`--no-ff` merge commit (`integrate Agent X (#N) <sha12>`), so every workstream
stays traceable. F, D, E, C, B and A merged without Git conflicts. Their
semantic overlaps were resolved in later commits (section 5).

## 5. Conflicts and resolutions

| Conflict | Resolution |
|---|---|
| C ↔ G `scripts/configure-search.sh`, `refresh-search.sh` (add/add) | C's version kept as the single implementation (calls `search_lexical.py`). `refresh-search.sh` also accepts G's `--quiet`/`--validate` plus `--no-validate`, and registers collections on a fresh or restored index. One implementation each of configure/refresh/search-wiki. |
| C ↔ G `verify-install.sh` (bare `qmd query 'lex: wiki'`, caught by C's test) | rewritten on `search_lexical.py` (status, search, exact, verify-model-free) |
| D ↔ G `.gitignore` | D's tracked capture tests/fixtures and G's `!.env.example` both kept. `.venv/` and `**/__pycache__/` added. |
| A ↔ G provisional auth (`BRAIN_SECRET_KEY`, `BRAIN_OWNER_SETUP_TOKEN`, `BRAIN_AUTH_*`, IdP `auth` service, `BRAIN_SERVER_CMD` auto-detection) | removed. A's variables are the only scheme. The entrypoint execs `scripts/remote_mcp/server.py`. The stub runs only with explicit `BRAIN_ALLOW_STUB=1`. |
| B ↔ C/D/E (B's `LocalBackend` + `FakeBackend`, own proposal validator, own K5 pager, raw `wiki_mcp_server` search) | replaced by one `WikiBackend` over `search_lexical`, `wiki_capture` + `git_safety`, `brain_proposals`/`evidence_audit`, `reconcile_context`. B's duplicate validator and pager are deleted. |
| E section names (`canonical`, `sources`) vs frozen K5 (`canonical_records`, `source_records`) | E corrected to the K5 names at the source. Items gained `id` and `authority_level`, and ordering is authority → distance → id. |
| A default adapter = 12 legacy tools | production default is exactly the six semantic tools. Legacy requires `BRAIN_MCP_ADAPTER=legacy` + `BRAIN_UNSAFE_REMOTE_LEGACY=1`. |

## 6. K1–K9 compliance

- **K1** Layout and grammar unchanged. The only schema-adjacent addition is
  the optional capture field `body_encoding`, which is written only when
  needed and rejected if unknown.
- **K2** Stable units imported, not rewritten. `wiki_capture` got defect fixes
  only (section 8).
- **K3** Exactly six tools with frozen names and arguments. No path, actor or
  channel argument. No review, promote or media tool (test-enforced over HTTP).
- **K4** Additive capture states preserved. D's tests pass.
- **K5** Frozen sections, per-item `id/ref/authority_level/reason`,
  deterministic order, cursor/next_cursor bound to arguments and corpus,
  `total/returned/offset`, `sections`, `expand`, and `truncated` + reason
  only for safety bounds. No verdict, no product token cap.
- **K6** Single owner. Unauthenticated callers get nothing, including
  `brain_status`.
- **K7** Search is BM25 + exact. No `embed`, `vsearch`, bare `query` or rerank
  (guarded in code, proven in the image).
- **K8** Deployment names follow G/A: `BRAIN_DOMAIN`, `BRAIN_DATA_DIR`,
  `BRAIN_STATE_DIR`, `BRAIN_PUBLIC_URL`, `BRAIN_OWNER_SECRET`; services
  `caddy` and `brain`.
- **K9** Remote writes are limited to captures and `_proposals`. Each write
  goes lock → write → validate → path-limited commit. On commit failure the
  data is kept and `brain_status` reports it. Canonical promotion is a human
  CLI step with full validation and a separate commit.

## 7. Required defects fixed

1. Capture D1/D2/D3 (section 8).
2. `.gitattributes` `* text=auto` would have normalised CRLF inside committed
   captures on clone/restore and silently re-broken D2. Captures are now
   `-text`, with a negative-controlled test.
3. The remote default surface exposed 12 legacy tools, including path reads.
   It now fails closed to six.
4. After a server restart the official MCP client re-ran owner consent,
   because access tokens were memory-only and the client does not refresh on
   401. Access-token hashes now persist, and restart needs no re-consent
   (proven in Docker).
5. G's `upgrade.sh --rollback` used `git reset --keep`, which in a shared
   code+archive repository drops every capture/proposal/canonical commit made
   after the upgrade. Rollback now reverts only the upgrade's commits, and the
   server is stopped during the merge.
6. E's `promote` swept every dirty non-noncanonical path into the canonical
   commit (in Docker it swept two helper scripts). Non-content paths now
   require `--paths`.
7. E's validator checked only the primary passage quote. Every attached quote
   is now verified, so forged secondary evidence is rejected.
8. Unbounded unauthenticated growth (pending consents, DCR registry) is now
   capped at 64 and 32.
9. `.mcp.json` auto-offered QMD's own MCP server (path `get`/`multi_get`,
   model-capable `query`). It now registers no server by default.
10. The validators scanned a local `.venv`. It is now ignored like
    `.pytest_cache/`.

## 8. Capture D1/D2/D3: design and tests

- **D1, heading injection.** On write, every body line matching `^\\*##` gets
  one extra leading backslash. On read, exactly one is removed. The marker
  `body_encoding: escaped-headings-v1` is emitted only when at least one line
  needed escaping, so all baseline records and most new ones keep exact
  legacy bytes. In escaped mode a section header must equal `## <name>\n`
  exactly.
  - Legacy records are protected by a reader rule: a duplicate or
    out-of-order section, or a text-hash mismatch, marks the record
    ambiguous. Every writer then refuses (`E_AMBIGUOUS_RECORD`) and leaves
    the bytes untouched, and `validate` reports `E_AMBIGUOUS_BODY`.
- **D2, CRLF.** Records are read with `newline=""` (`read_record_file`) and
  split on LF only, so `\r`, `\x85`, ` ` and similar characters stay
  inside lines. The `.gitattributes` rule prevents Git normalisation.
- **D3, unknown front matter.** A `FrontMatter` dict keeps the raw text of
  unknown keys, including block lists and nested mappings, and re-emits it
  verbatim unless the value was changed. Values are parsed with PyYAML for
  readers.
- **Tests.** `tests/test_capture_integrity.py` (38 cases):
  - 20 hostile texts driven through every writer, including every reserved
    heading, repeats, fences, blank lines, backslashes, Persian/English/mixed
    text, CRLF, mixed newlines and Unicode separators;
  - media layers and unchanged original bytes;
  - baseline-writer fixtures (`tests/fixtures/capture/legacy-*.md`, generated
    with the 9395ed9 writer) that parse and re-render byte-identically;
  - a legacy backslash-heading record upgraded losslessly, and a legacy
    damaged record refused;
  - unknown `body_encoding` rejected;
  - seven unknown front-matter shapes across all writers;
  - duplicates unchanged.
- **Also.** `tests/test_brain_surface.py` includes a CRLF capture that
  survives commit and a fresh clone. D's three strict xfails are now ordinary
  passing tests. There are **0 xfail markers** in the suite.

## 9. Final public ref grammar

`rec:<record-id>`, `doc:<20-hex>`, `cap:<capture-id>`, `prop:<proposal-id>`,
`hist:<doc-stem>`. The identifier alphabet is `[A-Za-z0-9_-]{1,128}`, matched
with `fullmatch`, so a trailing newline is rejected too.

- One boundary: `scripts/brain_surface/refs.py`. It resolves via E's
  `evidence_audit.resolve_ref` for record and capture ids only, and re-checks
  that the resolved file stays inside its zone.
- E's permissive internal forms (paths) are unreachable from caller text.
- `doc:` was added because extracted derivatives usually have no frontmatter
  id and would otherwise be unreadable remotely. It is an opaque hash of the
  repository path, not a path.

## 10. Final six-tool MCP schema

See `scripts/brain_surface/contract.py` and `docs/claude/TOOL_USAGE.md`.
Arguments are unchanged from K3 and every schema has
`additionalProperties: false`. The one shape decision:

- `brain_propose.evidence_refs` items are `{ref, quote?}`. At least one
  `rec:`/`doc:` item must carry a verbatim quote of ≥ 20 characters, and the
  first such quote becomes `source_passage`. This is a mapping onto E's
  existing schema, not a schema expansion (CCR-2).

## 11. Search wiring

`brain_search` → `search_lexical.search(query, mode, scope, n)`.

- Separate groups for `scope=all`.
- Every hit keeps its `tier` and `authority_level`. The derivative tier is
  level 5 and documented as a candidate passage.
- Before a lexical query the backend brings the rebuildable index current:
  it configures a missing index and refreshes a stale one, so a new capture
  is findable immediately.
- Without QMD, lexical mode returns `E_UNAVAILABLE` and exact mode still
  works.

## 12. Proposal and evidence wiring

`brain_propose` → surface shape checks → `WikiBackend.propose`:

1. Resolve each public ref. `hist:`/`prop:` are not evidence.
2. Map to internal refs (record id, capture id, or path for `doc:`).
3. Derive `source_passage`.
4. Call `brain_proposals.submit_proposal`, which runs
   `evidence_audit.validate_submission`, then the K9 append and commit.

A rejection writes nothing. Stored body: `source_passage` +
`evidence_refs: [{ref, quote}]` (evidence trace). CCR outcomes:

- **CCR-1:** no private helper was exposed. The public `validate_submission`
  was extended.
- **CCR-2:** no new schema keys. `supporting_captures`/`additional_passages`
  were dropped in favour of quoted `evidence_refs`.
- **CCR-3:** `intake-registration` is excluded remotely (test-enforced).
- **CCR-4:** deferred. No integrated test found a record that cannot be read
  within the 400 000-byte bound. `truncated` + `truncation_reason` are
  reported.

## 13. Reconciliation wiring

`brain_reconcile_context` → `reconcile_context.brain_reconcile_context`
(E's K5). The backend maps public seed/expand refs to internal ids, injects
C's lexical search as `search_fn`, groups E's flat page into K5 sections,
converts every item ref to a public ref, and strips `path`. B's pager was
removed.

## 14. Git K9 wiring

Both remote writes use `git_safety.locked_write_commit`:

- **Capture:** `capture_text` runs inside the lock, then `validate_record`,
  then a commit of that one file.
- **Proposal:** E's queue validation.

Commit failure yields `commit_state: uncommitted` and `commit_error`
(filesystem paths scrubbed), and `brain_status.uncommitted` lists the file.
`scripts/review.sh flush` retries. The server process sets
`BRAIN_REMOTE_SESSION=1`, `brain_review.py` refuses to act there, and no
remote module imports it (tested).

## 15. Auth and security review

Reviewed as security-sensitive code. Findings and fixes:

- Fail-closed route check.
- `BRAIN_STATE_DIR` is required.
- Exact https redirect allowlist; wildcards refuse to start.
- Persisted access-token hashes.
- Bounded pending consents and DCR registry.
- 0700 state directory.
- Exception text is not leaked.
- Stateless HTTP, so no session is lost on restart.

Verified by tests:

- PKCE S256 enforced; wrong verifier rejected.
- Codes are single-use.
- Refresh rotation and reuse rejection.
- Revocation kills the token family.
- Resource binding (401) and scope enforcement (403).
- `hmac.compare_digest` for the owner secret; the secret is never logged or
  stored.
- No open redirect.
- `/mcp/`, `/sse` and other routes are not unauthenticated bypasses.
- `/healthz` returns `{"ok":true}` only.

Accepted residuals, documented in `docs/SECURITY.md`:

- DCR client secrets are stored as issued, because the SDK authenticator
  compares plaintext. Alone they grant nothing.
- The login lockout is global, so an attacker can delay the owner but cannot
  brute-force the secret.

## 16. Deployment runtime services

`caddy` (TLS/ACME, reverse proxy of every path to `brain:8080`) and `brain`.
The `brain` container runs `server.py` and is:

- read-only, `cap_drop: ALL`, no-new-privileges;
- run as the repository owner;
- mounts `/wiki`, `/state/auth`, `/state/qmd` and `/state/home`;
- healthchecked on `/healthz`.

## 17. Runtime dependencies

**Image:**

- Python 3.12, Git 2.39, Node 22, QMD 2.8.3;
- `requirements.txt` (PyYAML);
- `requirements-remote.txt` (`mcp==2.2.0` and transitive starlette,
  uvicorn, pydantic, httpx2);
- `deploy/requirements-image.txt` (pymupdf, python-docx, python-pptx,
  openpyxl, beautifulsoup4, lxml);
- git-lfs installed when apt is reachable. The repository's LFS hooks are
  not executable, so it is optional.

**Dev/test:** the same, plus `requirements-dev.txt` (pytest). Setup:

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-remote.txt \
    -r deploy/requirements-image.txt -r requirements-dev.txt
```

## 18. Removed or rejected dependencies

- FastMCP, Supergateway, Pocket ID and the external-IdP compose service:
  none present (test-enforced).
- No embedding, reranker, OCR, STT, vision or LLM package; no model weights.
- `run-semantic-benchmark.py` and `run_faithfulness_benchmark.py` stay
  opt-in and are not installed in the image.

## 19. Fresh-install evidence

Disposable host, final code (`FINAL_TEST_REPORT.md` §5):

- `./install.sh` (self-signed TLS, ports 8080/8443) exits 0 with validators
  PASS and the brain healthy behind Caddy.
- `curl /healthz` → `{"ok":true}`; unauthenticated `POST /mcp` → 401.
- Owner smoke over HTTPS: exactly six tools; capture persisted and committed;
  search and read find it; path ref rejected; candidate proposal committed.
- Human accept → author → promote gives a separate canonical commit
  containing only the record.
- Restart without re-consent.
- In-container `git fsck`, validators, `verify-install.sh`: PASS.

## 20. Backup/restore evidence

1. A capture was forced uncommitted (index lock); `brain_status` reported
   `uncommitted=1`.
2. `create-backup.sh` → `docker compose down -v` → `rm -rf` of the repository
   and state.
3. `restore-backup.sh` gave `RESTORE OK`.
4. The following were preserved: HEAD, the uncommitted capture (byte-exact
   sha256 match) and the OAuth state (sha256 match).
5. `git fsck`, both validators, reindex and start all passed.
6. The authenticated smoke passed without re-consent, and still reported the
   uncommitted capture.
7. `review.sh flush` committed it, and status was clean afterwards.

## 21. Upgrade/rollback evidence

- Upgrade (backup → stop → merge → rebuild → health → validators): PASS, three
  times against the integrated application.
- Conflicting upstream (`HOME.md`): exit 3, "merge conflict; nothing was
  changed". HEAD and the working tree were unchanged and the server came back
  healthy.
- Rollback after a post-upgrade capture: the merge was reverted, the code
  returned to the pre-upgrade state, and the capture commit stayed. Gate PASS.

## 22. Model-free proof

- `prove_search_model_free.py` in the final image with `--network none`:
  PASS, no model artefact, QMD home contains only `index.sqlite` and
  `index.yml`.
- On the host with network namespace + strace active: PASS, 0 model-file
  opens, 0 non-local connects.
- `search_lexical.py verify-model-free` in the deployment: `model_free=True`.

## 23. Export/conversion qualification

`tests/test_io_conversion_exports.py`, 13 passed after all merges:

- PDF/DOCX/PPTX/XLSX/HTML/EPUB with the SHA-256 header equal to the original;
- scanned PDF → NEEDS-OCR, no invented text;
- legacy/unsupported formats refused; converter needs no network;
- PROV-O/SKOS/TEI; RO-Crate absent/valid; public privacy valve.

Documentation added:

- The file-to-md skill documents the SHA-256 header, which is the same value
  as the manifest (`MATERIALS_INDEX.jsonl` already requires `sha256`), not a
  second checksum system.
- Interchange exports are documented and flagged at runtime as full, possibly
  private archival exports. `export_public.py` remains the publication valve.
  No `--public-only` mode was added.

## 24. Real Claude test: sole remaining blocker

Not performed: no public DNS/HTTPS host in this environment. The
protocol-equivalent path was proven with the official MCP Python client
through Caddy/HTTPS:

- DCR with Claude's default callback URI;
- PKCE;
- owner consent;
- token;
- stateless streamable HTTP;
- restart persistence.

The redirect allowlist remains exactly the two default Claude callbacks (no
wildcard). One-page manual procedure: `docs/CONNECT_CLAUDE.md` → "Release
gate".

## 25. Known limitations

- Lexical search cannot retrieve across languages. It has ezafe/ZWNJ edge
  cases in exact/phrase matching, and low natural-language recall without
  reformulation. The synthetic eval (ladder+exact hit@5 67/68) measures the
  mechanism, not real-world quality. The standing instructions require
  iterative reformulation.
- Every capture/proposal commit passes the repository pre-commit gate, which
  runs `validate_repo.py --full` (hashes all originals). On a very large
  archive each remote write gets slower. The write is durable before the
  commit starts.
- Remote media ingestion is out of the frozen K3 surface.
- After a rollback, re-applying the same upstream needs "revert the revert"
  (documented).
- The image is 2.2 GB with the `python:3.12-bookworm` base, which was needed
  here because Debian mirrors are blocked in this sandbox. The default slim
  base is smaller.
- Environment-only workarounds, not repository changes:
  - Docker Hub rate-limited anonymous pulls, so base images were pulled from
    `mirror.gcr.io` and tagged locally.
  - Builds used `--build-network host`, `--build-ca`, and
    `--python-base python:3.12-bookworm`.
- The local stdio `wiki_mcp_server.py` keeps its own typed-`lex:` QMD call
  and free-text `wiki_propose` for backward compatibility. It is not part of
  the remote surface, and queued proposals are still audited by
  `evidence_audit.audit`.

## 26. No unresolved silent contract drift

The contract-relevant changes are all listed here and tested:

- `doc:` ref kind added;
- `evidence_refs` item shape `{ref, quote?}`;
- K5 `truncated` means safety bounds only, while paging uses `next_cursor`
  (fixture 07 updated);
- E's section names corrected to K5;
- `body_encoding` optional capture field;
- access-token persistence;
- rollback semantics;
- `promote --paths` requirement for non-content paths;
- stateless HTTP.

No frozen tool name or argument changed. No CCR is outstanding except CCR-4,
which is deferred with the reason given above.
