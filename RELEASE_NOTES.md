# 0xBrain release notes

## 1.0.0-rc2 (unreleased): `brain_ingest_file`

- Seventh tool, `brain_ingest_file(upload_ref, title?, description?)`: adds a
  user-supplied document (pdf, docx, pptx, xlsx, html, epub, txt, md) with
  its **exact original bytes**, SHA-256, a pending-registration source record
  and a deterministic `to_md.py` derivative; duplicates by SHA are detected;
  failed extraction or commit never loses the original.
- Owner-authenticated `/upload` page (owner secret or bearer token) returns a
  single-use, short-lived `upload_ref`. Reason: an MCP tool call carries only
  JSON arguments and Claude documents no forwarding of chat attachments to
  connector tools, so direct attachment ingestion from an ordinary Claude
  chat is **not** available (ATTACHMENT_TRANSPORT: UNVERIFIED).
- K9 gains one narrow write class (new files only under `_originals/remote-mcp/`,
  `02-sources/records/`, `02-sources/text/` + held counters). Registration stays
  human. `scripts/review.sh flush-ingest` retries an uncommitted ingest.
- Docs: `docs/INGEST.md`; standing instructions section 11; release gate
  steps 12-16.

# 0xBrain 1.0.0-rc1 release notes

Status: **release candidate**, CONDITIONAL PASS. It is blocked only on the
official-Claude custom-connector test on a public host
(`docs/CONNECT_CLAUDE.md`).

## What this release is

A personal, single-owner deployment of the Living Wiki kit that official
Claude can use remotely. Claude gets six semantic tools (`brain_search`,
`brain_read`, `brain_capture`, `brain_reconcile_context`, `brain_propose`,
`brain_status`) over HTTPS with OAuth. The server runs no model. Everything
Claude writes is noncanonical, and only the owner promotes changes into
canonical records, through a local CLI.

## New

- **Remote MCP server** (`scripts/remote_mcp/`):
  - MCP SDK 2.2.0 with stateless streamable HTTP;
  - built-in single-owner OAuth 2.1 (DCR, PKCE S256, owner consent,
    rotating refresh tokens, revocation), with no external identity
    provider;
  - fail-closed to the six tools.
- **Semantic surface** (`scripts/brain_surface/`): the K3 contract, one
  public ref grammar (`rec:`/`doc:`/`cap:`/`prop:`/`hist:`), and one backend
  wired to the governed subsystems.
- **Model-free search** (`scripts/search_lexical.py`): QMD BM25 + normalised
  exact search, explicit scopes, Persian/Arabic orthography handling, a
  checked-in evaluation set, and a no-model proof.
- **Governance:**
  - one proposal validator (every quote verified);
  - K9 locked, path-limited Git commits for captures and proposals;
  - K5 reconciliation packages (focused/deep, paged, no verdict);
  - human review and promotion CLI (`scripts/review.sh`).
- **Deployment:** Docker + Caddy + `install.sh`, backup/restore with auth
  state, upgrade with a safe rollback, and the owner smoke test
  (`scripts/remote_smoke.py`).
- **Conversion:** the derivative provenance header carries the original's
  SHA-256; the RO-Crate and export fixes are included.

## Fixed

- **Capture data integrity:**
  - heading-like lines in user text can no longer lose text on a later
    rewrite;
  - CRLF text is kept exactly;
  - unknown front-matter keys survive rewrites;
  - Git no longer normalises capture line endings.
- A server restart no longer forces a new owner consent.
- Rollback no longer discards archive commits made after an upgrade.
- Canonical promotion no longer sweeps unrelated files into its commit.

## Upgrading from the kit

Pull the release. Then:

1. Run `./install.sh` on a VPS, or install the dev environment from
   `docs/README.md`.
2. Existing capture records remain valid and byte-stable.
3. A record already damaged by the old heading defect is reported
   (`E_AMBIGUOUS_BODY`), and writers refuse to modify it, until you repair
   it by hand.

## Known limitations

- Lexical search does not cross languages and misses paraphrase unless
  Claude reformulates the query.
- The remote connector is text-only: no media.
- Each remote write passes the full repository gate. The data is durable
  before the commit, but writes get slower on very large archives.
- See `FINAL_INTEGRATION_REPORT.md` §25.

## Documentation

Start at `docs/README.md`. Architecture: `FINAL_ARCHITECTURE.md`. Evidence:
`FINAL_TEST_REPORT.md`, `FINAL_INTEGRATION_REPORT.md`.
