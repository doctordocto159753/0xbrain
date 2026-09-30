# Tool reference and usage patterns (K3 + brain_ingest_file)

Seven tools: the six frozen K3 tools plus `brain_ingest_file`. Schemas: `scripts/brain_surface/contract.py`
(`TOOLS`). Every result is a JSON object with `ok`; failures carry
`error: {code, message, ...}` (the MCP result is also marked `isError`) and,
for writes, `persisted: false`.

## What the server does and what Claude does

| Server (deterministic, model-free) | Claude |
|---|---|
| validates arguments; rejects paths, unknown arguments, malformed refs | chooses queries, refs, kinds |
| lexical BM25 (QMD) / exact search, grouped by scope, tier-labelled | reformulates queries, judges relevance after reading |
| stores captures and proposals, commits them to Git (K9), reports `persisted` / `commit_state` | reports exactly that, never more |
| verifies every quote against the record it cites; rejects with reasons | supplies quotes and the reading they support |
| returns graph neighborhoods with reasons, ordered and paged (K5) | decides conflict / supersession / relation type, as candidate |
| labels tiers and authority levels | keeps evidence classes apart in prose |

The server makes no semantic verdict and runs no model.

## Refs

Opaque, typed, and the only way to address anything:

| ref | what |
|---|---|
| `rec:<id>` | record with a frontmatter id in 02-sources, 03-objects, 04-notes, 05-claims, 06-relations |
| `doc:<20 hex>` | a file in those zones without an id (usually an extracted derivative) |
| `cap:<capture-id>` | capture (noncanonical intake) |
| `prop:<proposal-id>` | candidate proposal in the queue |
| `hist:<name>` | genesis / handoff document (chronology) |

A path (`03-objects/x.md`, `../x`, `/etc/...`, `C:\...`) is rejected with
`E_BAD_REF`. Use refs exactly as a tool returned them.

## brain_search(query, mode?, scope?, n?)

- `mode`: `lexical` (default; keyword BM25, not semantic) or `exact`
  (normalised substring: Persian/Arabic letter and digit variants, ZWNJ and
  harakat folded).
- `scope`: `canonical` (default), `captures`, `all`. `all` returns
  `groups.canonical` and `groups.captures` separately; there is no merged rank.
- **Naming nuance.** `canonical` means the governed, non-capture side of the
  archive. It contains canonical records (level 4), source records (level 4),
  interpretive notes (level 6) and extracted derivatives (level 5). Each hit
  carries its own `tier` and `authority_level`: a `derivative` hit is a
  candidate passage from machine-extracted text, not accepted canonical
  evidence.
- Hit fields: `ref`, `tier`, `zone`, `authority_level`, `title`, `snippet`,
  `rank` (attention only); exact mode adds `line`, `lines`, `hit_count`.
  Groups report `strategy` (`strict`, `content-terms`, `persian-stem-prefix`,
  `any-term`) and `attempts`: a relaxed strategy means a weaker match.
- Pattern: exact mode for names/identifiers/quotes; lexical with variants for
  topics; reformulate (synonyms, the other language, spelling variants) and
  retry before saying something is absent; then `brain_read` the hits you
  rely on. `scope: captures` finds what the owner said but nobody reviewed.

## brain_read(ref)

- `rec:`/`doc:`/`hist:` return `content` (up to 400 000 bytes; `truncated` +
  `truncation_reason` beyond), `tier`, `authority_level`.
- `cap:` returns `sections` (user text, literal transcript, machine
  description, review notes, provenance events, interpretation) and
  `metadata` (state, sha256, `interpretation_needs`). `media.inspectable:
  false` means media exists and you cannot see it.
- `prop:` returns the queued proposal record.
- Read before relying: a snippet or a rank is not the record.

## brain_capture(text, language_hint?)

- Verbatim owner words; `language_hint` in `fa`, `en`, `mixed`, `unknown`.
  Channel is fixed server-side (`mcp`). Text is stored byte-exactly (CRLF,
  headings, code fences included); trailing newlines are normalised to one.
- Success = `ok && persisted`. Then report `ref`. `commit_state`:
  `committed` (in Git, `commit` sha given) or `uncommitted` (on disk and safe,
  not yet in Git history; brain_status lists it until the owner flushes it).
  `duplicate_of` means identical text already exists: a new capture was still
  stored and linked to it.
- Errors (`E_BAD_ARGUMENTS`, `E_TOO_LARGE`, `E_UNAVAILABLE`, ...) mean not stored.

## brain_ingest_file(upload_ref, title?, description?)

- For source documents the owner supplies (pdf, docx, pptx, xlsx, html,
  epub, txt, md). The owner first uploads the file at
  `https://<domain>/upload` (owner secret or bearer token) and gives you the
  returned `upload_ref` (`upl-` + 43 characters, single use, expires after an
  hour). There is no file-content, path or URL argument: MCP tool calls carry
  only JSON arguments, so file bytes cannot travel through you.
- Receipt: `source_ref` (= `original_ref`: the original is identified by its
  source record; originals are not readable through the connector),
  `derivative_ref` (`doc:`; null when there is no usable text), `sha256`,
  `bytes`, `mime_type`, `filename`, `holdings_tier` (`pending-registration`),
  `extraction` (`complete` | `needs_ocr` | `preserved_only`), `duplicate`,
  `validation`, `commit_state` (`committed` | `persisted_uncommitted` |
  `not_needed` for a duplicate).
- `duplicate: true`: identical bytes were already held; the existing refs are
  returned and nothing new is stored.
- Errors, nothing stored: `E_BAD_UPLOAD_REF`, `E_UPLOAD_NOT_FOUND` (unknown,
  expired or already used), `E_UNSUPPORTED_TYPE`, `E_TYPE_MISMATCH`,
  `E_ARCHIVE_ABUSE`, `E_EXISTS`.

## brain_reconcile_context(seed_ref?, query?, depth?, cursor?, sections?, expand?)

- Needs `seed_ref` (`rec:`, `doc:`, `cap:` or `hist:`) and/or `query`.
  `focused`: the seed's direct (1-hop) neighborhood, one page. `deep`:
  multi-hop, paged.
- Sections (K5): `canonical_records, claims, relations, source_records,
  captures, superseded, open_proposals, unresolved, chronology`. Each has
  `total`, `returned`, `items`. Items carry `id`, `ref`, `authority_level`,
  `reason`, `distance`, `title`, `status`, and an `excerpt` (or `content`
  for refs you listed in `expand`). Order is deterministic: authority level,
  graph distance, id.
- Paging: a non-null `next_cursor` means more items; pass it back with the
  same other arguments until it is null. `total` / `returned` / `offset` are
  over the whole package. A changed argument or archive gives
  `E_STALE_CURSOR`: restart without a cursor.
- `truncated: true` is reserved for internal safety bounds (hop ceiling,
  per-record content ceiling) and comes with `truncation_reason`; ordinary
  paging is not truncation.
- `unresolved` items have `ref: null` (dangling links / open statuses).
- `verdict` is always null. You judge; label it candidate.

## brain_propose(kind, structured_fields, evidence_refs)

Kinds and required fields come from `00-system/policies/proposal_schema.json`
(`intake-registration` is not offered remotely: its evidence is an original
file and hash, which a remote caller cannot name).

| kind | structured_fields (all strings) |
|---|---|
| relation-edge | target_id, proposed_type, why |
| claim-amendment | claim_id, amendment, why |
| object-note | object_id, note, why |
| tier-change | target_id, from_tier, to_tier (pending-registration, registered, held), why |
| record-correction | target_id, field, old_value, new_value, why |
| link-repair | target_id, broken_link, repaired_link, why |
| retirement-request | target_id, why, superseded_by |

`evidence_refs`: `[{ref, quote?}, ...]`. At least one `rec:` or `doc:` ref
with a verbatim quote (>= 20 characters, copied from `brain_read` output):
the first one becomes the proposal's `source_passage`. Every quote you give
is checked against the record it cites. `cap:` refs may be added as
supporting context but never carry the evidence. Do not send
`source_passage` or unknown fields. On rejection (`E_PROPOSAL_REJECTED`) read
`error.reasons`, repair the evidence, and retry; nothing was queued.

Provisional relation: `relation-edge` with `proposed_type: "unclassified"` and
a `why` that describes the observed connection and its basis (relation
before type; candidate status; may-note strength).

## brain_status()

`snapshot`, `counts` (registered materials, held artifacts, records per
zone, captures per state), `captures_pending` (captures awaiting review,
transcription, description or interpretation), `proposals` (per status,
`pending`), `search` (model-free QMD index present/stale), `validation`
(last known validator result, cached per Git state), `uncommitted` (durable
noncanonical writes not yet in Git + the last commit failure), `git`
(head, branch, uncommitted canonical file count). `degraded` +
`degraded_reasons` tell you what to mention before relying on results.

## Error codes

`E_BAD_ARGUMENTS`, `E_UNKNOWN_ARGUMENT`, `E_BAD_REF`, `E_NOT_FOUND`,
`E_TOO_LARGE`, `E_BAD_CURSOR`, `E_STALE_CURSOR`, `E_PROPOSAL_REJECTED`,
the ingest codes above,
`E_UNAVAILABLE`, `E_UNKNOWN_TOOL`, `E_INTERNAL`, plus capture codes such as
`E_EMPTY`. Transport/auth failures happen before this layer; when a call
cannot be made at all, treat it as "connector unavailable".

## Safety bounds (reported, never silent)

Search `n` <= 50 (noted when clamped). Capture <= 500 000 characters
(`E_TOO_LARGE`). Read <= 400 000 bytes (`truncated`). Deep reconciliation
pages 50 items per page (paging, not a cap) with a hop ceiling of 8 and a
per-expanded-record ceiling of 400 000 bytes, both reported via
`truncated` / `truncation_reason`.
