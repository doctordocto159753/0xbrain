# Capture

A capture is noncanonical intake (level 7): what someone said or sent, kept
verbatim with provenance, never evidence by itself. Records live under
`01-inbox/captures/YYYY/YYYYMM/cap-YYYYMMDD-HHMMSS-xxxx.md`. The capture core
is `scripts/capture/wiki_capture.py` (all channels: CLI, Telegram hook,
Obsidian, stdio MCP, remote MCP).

## Remote capture (`brain_capture`)

Text only. The server fixes the channel (`mcp`), creates state `received`,
hashes the text (SHA-256 of the stored text), flags exact duplicates
(`duplicate_of`), then runs the K9 sequence:

```
lock -> durable write -> validate_record -> git add/commit (only that file)
```

If the commit fails (hook, lock, disk), the capture stays on disk and the
response says `commit_state: uncommitted`; `brain_status` lists it until the
owner runs `scripts/review.sh flush`. Nothing is rolled back or retried
destructively.

## Exactness guarantees

- User text is stored byte-exactly: CRLF and mixed newlines, Unicode line
  separators, backslashes, Markdown code fences, Persian/English/mixed text,
  and lines that look like the record's own section headings. The one
  normalisation (unchanged from the baseline) is that trailing newlines
  collapse to one; the recorded SHA-256 is of that stored form.
- Heading-like body lines are escaped on disk (`body_encoding:
  escaped-headings-v1`, present only when needed) so no later rewrite (state
  change, transcript, description, interpretation) can split or lose text.
  Records without such lines keep the exact baseline byte layout.
- Unknown front-matter keys written by other tools survive every rewrite.
- `.gitattributes` marks captures `-text`, so Git never normalises their
  line endings on commit, clone or restore.
- A legacy record whose body is already ambiguous (written before this fix
  with a bare heading line in the text) is refused by every writer
  (`E_AMBIGUOUS_RECORD`) and flagged by `validate`; repair it by hand.

## States and needs

`received -> processing -> transcribed/described -> needs-review -> reviewed
-> promoted/parked` (human actors only). `interpretation_needs`
(`needs_transcription`, `needs_description`, `needs_interpretation`) is
computed from the record; Claude-produced layers are stored separately,
labelled `method: claude`, tier `candidate`, never reviewed. Remote Claude
cannot run these transitions; see `scripts/capture/docs/`.
