# Using 0xBrain with Claude

## The division of labour

- **Server (deterministic, model-free):** finds records (lexical and exact),
  returns them by ref, stores captures and proposals verbatim, checks
  evidence quotes, assembles reconciliation packages, commits noncanonical
  writes to Git, reports its own state.
- **Claude:** chooses what to search and how to rephrase, reads, interprets,
  decides whether something conflicts, drafts proposals. Everything Claude
  produces is candidate-tier (level 7).
- **You (owner):** decide. Accepting a proposal and changing canonical
  records are human steps on the server ([PROPOSALS_AND_REVIEW.md](PROPOSALS_AND_REVIEW.md)).

## Typical requests

| You say | Claude does |
|---|---|
| "What did we decide about X?" | `brain_search` (canonical, then variants), `brain_read` the hits, answers with refs |
| "Save this: ..." | `brain_capture` verbatim, reports `cap:` ref and commit state |
| "Add this document exactly" | asks you to upload it at `/upload`, then `brain_ingest_file(upload_ref)`; reports SHA-256, source/derivative refs ([INGEST.md](INGEST.md)) |
| "Does this contradict what we have?" | `brain_read` the older record, `brain_reconcile_context`, states a candidate reading, may capture + propose |
| "Everything about X" | `brain_reconcile_context` depth `deep`, follows `next_cursor` to the end |
| "Propose linking A and B" | `brain_propose` `relation-edge` with a verbatim quote |
| "Is my note safe?" | `brain_status`: `uncommitted`, `captures_pending` |

## What Claude cannot do (by design)

Edit, delete or promote canonical records; accept or review proposals or
captures; register ingested sources; read files by path; receive the bytes
of a file you attach in chat (use the upload page); see media (voice, images) through the
connector; run anything on the server. These are not missing features: the
remote surface has exactly seven tools.

## Good habits

- Ask Claude to cite refs; open anything you will rely on.
- For names, titles, quotes: "search exactly for ...".
- For topics: let Claude retry with synonyms and the other language; lexical
  search does not understand paraphrase ([../SEARCH_GUIDE.md](../SEARCH_GUIDE.md)).
- Review the proposal queue regularly: `scripts/review.sh list`.
- Back up and copy backups off the server ([BACKUP_RESTORE.md](BACKUP_RESTORE.md)).
