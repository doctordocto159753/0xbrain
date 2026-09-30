# Ingesting files exactly (`brain_ingest_file`)

Use this when you give Claude a document (pdf, docx, pptx, xlsx, html, epub,
txt, md) and say "add this to my wiki exactly as supplied". The archive then
keeps the original **bytes**, not Claude's reading of them.

## How a file reaches the archive

```
you: upload the file at https://<domain>/upload      (owner secret)
      -> the page shows:  ingest upload_ref=upl-...
you -> Claude: "ingest upload_ref=upl-..."
Claude -> brain_ingest_file(upload_ref, title?, description?)
```

A curl example, using a bearer token or the owner secret:

```bash
curl -F "file=@minutes.pdf" -F "secret=$BRAIN_OWNER_SECRET" -H "Accept: application/json" \
     https://<domain>/upload
# {"ok": true, "upload_ref": "upl-...", "filename": "minutes.pdf", "bytes": ..., "sha256": "...", "expires_in": 3600}
```

The `upload_ref` works once and expires after `BRAIN_UPLOAD_TTL` (default 1 h).

### Why there is an upload step (attachment transport)

**ATTACHMENT_TRANSPORT: UNVERIFIED.** There is no documented native path, and
no live Claude test was possible from the integration environment. The
evidence, dated 2026-09-30:

- **MCP protocol.** A tool call carries only `name` and a JSON `arguments`
  object (`CallToolRequestParams` in the MCP SDK 2.2.0 types: `name`,
  `arguments`, `meta`, `task`, ...). Binary content, embedded resources and
  resource links exist only in the server→client direction (tool results
  and resources). The protocol has no field through which a client hands a
  file to a tool.
- **Claude connector documentation**
  (claude.com/docs/connectors/building): servers may expose text and binary
  *resources* and text and image *tool results*. Nothing describes Claude
  forwarding a user's chat attachment to a custom connector tool.
- **What that leaves.** The only conceivable path is Claude re-emitting the
  bytes as a string argument (for example base64 produced in its code
  sandbox). That is model-mediated, bounded by output size, and not
  "native". It is deliberately not implemented, because a reconstruction
  must never be presented as the original.

So a file you attach in an ordinary Claude chat does **not** reach 0xBrain.
Direct attachment ingestion is not claimed. If Anthropic later documents a
native attachment channel, it can feed the same staging store; the archival
intake below does not change.

## What one ingest does

This is the adjudication-free part of `/wiki-intake`
(`.claude/skills/wiki-intake/SKILL.md` steps 3–7 plus the census refresh of
step 10). It is deterministic and uses no model.

1. **Check the file.** The staged bytes must still hash as uploaded. The type
   is chosen by extension and verified by structure:
   - PDF must start with `%PDF-`;
   - DOCX, PPTX and XLSX must be ZIP containers with their main part;
   - EPUB must carry the `application/epub+zip` mimetype;
   - text formats must be UTF-8 without NUL bytes;
   - ZIP containers are bounded: at most 10 000 members, 500 MB unpacked,
     and a 200× expansion ratio.
2. **Duplicate check** by SHA-256 against every source record and manifest
   row. Identical bytes return the existing refs with `duplicate: true`;
   nothing is written or committed.
3. **Original.** `_originals/remote-mcp/<prefix>-src-<sha12>--<name>.<ext>`
   is created as a new file (`O_EXCL`) with a byte-for-byte copy, is
   re-hashed, and is set read-only. An existing path is never overwritten
   (`E_EXISTS`).
4. **Derivative.** `scripts/file-to-md/to_md.py` (Agent F) runs in a
   subprocess with a 120 s timeout and writes
   `02-sources/text/<id>--<name>.md`, whose provenance header carries the
   original's SHA-256. If `to_md` reports needs-OCR, or fails or times out,
   the original is still kept and `extraction` is `needs_ocr` or
   `preserved_only`. There is no OCR or model.
5. **Source record.** `02-sources/records/<id>.md` uses the existing
   grammar:
   - identity fields: `type: source-record`, `filename`, `format`,
     `mime_type`, `sha256`, `bytes`, `original_path`,
     `extracted_text_path`, `extraction_status`;
   - holding fields: `holdings_tier: pending-registration`,
     `status: pending-registration`, `validation_status: unreviewed`;
   - provenance fields: `ingestion_channel: remote-mcp`, `ingested_at`.
6. **Registers.** In `CORPUS_STATE.json`, `held_artifact_count` and
   `holdings_by_tier.pending-registration` go up. The `Artifacts held:`
   marker is updated on HOME, README, SYSTEM_DESIGN and CLAUDE (the census
   and freshness gates in `validate_repo.py`). `MATERIALS_INDEX.jsonl` and
   `source_material_count` are **not** touched, because registration is
   adjudication.
7. **Validation, then commit (K9).**
   - Under the write lock, both validators run before and after the write.
     Only errors the ingest introduced block the commit; this is the same
     rule as the repository gate.
   - The commit is made through `git_safety` and contains exactly the paths
     listed above.
   - If the commit fails, nothing is deleted. The receipt says
     `persisted_uncommitted`, `brain_status.ingest.uncommitted` lists the
     files, and `scripts/review.sh flush-ingest` commits them after full
     validation.

Registration and everything after it stay with you, through `/wiki-intake`
review and `brain_review.py`: registering the source (manifest row,
`registered` tier), family or version assignment, and reconciliation.

## Authority, stated explicitly

| Layer | Status |
|---|---|
| original under `_originals/` | the exact artifact; never modified; corrections enter as new artifacts |
| derivative under `02-sources/text/` | deterministic extraction, level 5; not byte-equal to a binary document and not meant to be |
| Claude's reading of it | candidate (level 7), only through `brain_propose` |
| canonical knowledge | human-governed |

## Contract change (K9 held-intake write class)

K9 allowed remote writes only to `01-inbox/captures/` and `_proposals/`.
This feature adds exactly one more write class, which is narrower than a
general canonical write:

- **New files only**, under `_originals/remote-mcp/`, `02-sources/records/`
  and `02-sources/text/`. This is enforced at write time (`O_EXCL`, not
  tracked in HEAD) and again at commit time (`ingest.commit_policy`), so a
  modified original is never committed.
- **Counter updates** in `CORPUS_STATE.json` and the four `Artifacts held:`
  markers.
- **Registration is untouched:** there is no manifest write and no
  `registered` tier.
- **No change to** the authority hierarchy, record grammar, validators,
  search, or Git architecture: it is the same lock → write → validate →
  path-limited commit sequence.

## Limits

- Size: `BRAIN_UPLOAD_MAX_BYTES`, default 50 MiB.
- Pending uploads: at most `BRAIN_UPLOAD_MAX_PENDING`, default 20.
- Staging lives in `BRAIN_UPLOAD_DIR` (default `$BRAIN_STATE_DIR/uploads`,
  mode 0700). Only a hash of each ref names the staged file.
- Accepted types are exactly the eight listed above. Executables, archives,
  images, audio and legacy Office formats are refused, and there is no URL
  or server-path ingestion.
- Claude's tool call timeout is 240 s. A very large or complex PDF could
  approach it through extraction (120 s cap) plus validation. The original
  is stored before extraction starts.
