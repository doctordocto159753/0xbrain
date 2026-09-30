# Agent E — Governance report

Base: `9395ed9f26729910f5ccc868c8598fad54811e19`. Branch: `claude/sweet-goldberg-v3w3cs`
(assigned in place of `agent/e-governance`; rename/merge is Agent H's call).
K1–K9 contracts unchanged. No model call anywhere in this work.

## Baseline cleanup (commit 1)
- `.githooks/known-baseline-errors.txt` reset to empty. Proof: with the file
  empty, `check_against_baseline.py` prints `OK: validators clean.` (0 errors).
  The removed lines named files of another instance's corpus.
- `.githooks/pre-commit` picks `BRAIN_PYTHON`, `python3`, `python`, `py -3`
  (Python 3 verified); if none exists the commit is **blocked**, never skipped.
  File mode set executable. Checked: works with only `python3` on PATH.

## Validation API (`scripts/evidence_audit.py`, extended — no second validator)
- `validate_body(root, kind, struct)` — the per-kind rules, now used by both
  `audit()` and the new gate.
- `validate_submission(root, kind, structured_fields, evidence_refs) -> SubmissionCheck`
  (`.ok`, `.errors`, `.body`, `.refs`). Checks kind, structured fields (required,
  non-empty strings, unknown keys rejected, size), evidence refs (must resolve),
  and that a required `source_passage.path` is covered by a canonical ref.
- `resolve_ref(root, ref)`: capture id, canonical record id, or canonical-zone
  path. Never an arbitrary path.
- Hardening of the existing check: `03-objects/../_originals/x.md` used to pass
  the prefix test; now rejected (resolved path must stay inside its zone).
- `brain_proposals.submit_proposal(root, kind, structured_fields, evidence_refs)`
  is the only proposal function the MCP layer should call: reject-before-queue,
  then K9 sequence. Returns `{accepted, proposal_id, committed, commit, uncommitted, errors}`.

## Human review (`scripts/brain_review.py`, local CLI only)
`list | inspect | accept | reject | defer | edit | promote | status | flush`.
Every mutation needs `--actor` and interactive confirmation (type the id) or
`--yes`; refuses when `BRAIN_REMOTE_SESSION` is set. accept re-validates evidence
against current disk; edit re-validates and logs `prior_body_sha256`; reject needs
a note. The MCP server does not import this module and exposes no review verb
(test enforced). Honest limit: a module boundary plus tests, not a sandbox; the
remote process must simply not be given a shell or this CLI (Agent A/G deployment).

## K9 Git safety (`scripts/git_safety.py`)
`locked_write_commit(root, write, message, validate)`: lock
(`<git-dir>/brain-write.lock`, stale-pid/age recovery) → durable write (fsync,
atomic replace, partial-line repair) → validation on disk state → pathspec-limited
`git add/commit` (unrelated staged files are never swept in) → on any failure
after the write the data stays, index is unstaged, failure recorded in
`<git-dir>/brain-uncommitted.json`. `uncommitted_state(root)` (source of truth:
`git status` over noncanonical prefixes, plus last failure) is for `brain_status`;
`flush_pending()` / `brain_review.py flush` retries. Canonical paths are never
auto-committed (`refused-canonical`, file kept). Noncanonical prefixes:
`_proposals/`, `01-inbox/captures/`, `_captures/`.
`promote`: proposal must be `accepted`; canonical dirty paths only (never
`_originals/`); `validate_repo --full` + `validate_content_release` +
`check_against_baseline` must pass; canonical commit separate from the queue
commit that records `promoted_commit`. Push is out of scope (no network here).

## K5 reconciliation (`scripts/reconcile_context.py`)
`brain_reconcile_context(root, seed_ref?, query?, depth, cursor?, sections?, expand?, page_size?, search_fn?)`.
Reuses `build_graph_index`, `context_pack._neighborhood`, `evidence_audit`,
`reconcile_runner` state, `wiki_capture` parser. Output: `total, returned, offset,
next_cursor, truncated, reason, section_totals, items[], meta{verdict:null}`.
Sections (fixed order): canonical claims relations sources captures superseded
open_proposals unresolved chronology. Focused = 1-hop; deep = multi-hop, default
page 50 (paging, not a cap). Cursor bound to a fingerprint → `cursor_stale` on
change. `truncated`/`reason` only for safety limits (`max_hops_safety_limit`=8,
`content_byte_limit`=400 000 per expanded record). Errors: `no_anchor, bad_depth,
bad_sections, bad_cursor, cursor_stale, seed_not_found, bad_page_size`.

## Contract change requests (none blocking)
1. **Ref grammar** (K3 `brain_read(ref)`): I implemented capture id | record id |
   canonical path in `evidence_audit.resolve_ref`. Agent A/B/C should share this
   function so `brain_read` and `brain_propose` accept the same refs.
2. **Evidence coverage rule** is my interpretation of K3: for kinds requiring a
   passage, `evidence_refs` must be non-empty and include the passage path.
3. Agent D capture writes should call `git_safety.locked_write_commit`.

## Integration for Agent H
- `brain_propose` → `brain_proposals.submit_proposal`; `brain_status` should
  merge `git_safety.uncommitted_state(root)`; `brain_reconcile_context` →
  `reconcile_context.brain_reconcile_context(root, **args)`.
- Legacy `wiki_propose` (free text) still exists in `wiki_mcp_server.py`; I did
  not touch it (Agent A territory) — it must be removed/replaced at integration.
- Server startup must ensure `BRAIN_REMOTE_SESSION=1` in the remote process env.

## Evidence
`tests/test_governance.py`: 43 tests; full suite 169 passed; mutation suite
102/102; validators PASS; gate OK.
