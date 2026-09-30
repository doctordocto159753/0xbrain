# Proposals and human review

## Candidate proposals (remote, Claude)

`brain_propose(kind, structured_fields, evidence_refs)` is the only way
Claude can suggest a change. The server maps the public refs to internal
ones and hands the proposal to the single validator
(`evidence_audit.validate_submission`, via `brain_proposals.submit_proposal`):

- `kind` from `00-system/policies/proposal_schema.json`, minus
  `intake-registration` (needs an original file and hash; intake stays a
  local `/wiki-intake` workflow);
- every required field present, non-empty string, no unknown fields;
- at least one `rec:`/`doc:` ref with a verbatim quote (>= 20 characters);
  the first becomes `source_passage`, and **every** attached quote must occur
  verbatim in the record it cites (a forged or paraphrased quote is
  rejected); `cap:` refs may support but never carry the evidence.

A rejected proposal writes nothing. An accepted one is appended to
`_proposals/proposals.jsonl` as `authority_tier: candidate`, `status: new`,
and committed alone (K9). The archive itself has not changed.

## Human review (owner, on the server)

```bash
scripts/review.sh list                           # new / audited proposals
scripts/review.sh inspect PROP_ID
scripts/review.sh accept  PROP_ID --actor NAME [--note "..."]
scripts/review.sh reject  PROP_ID --actor NAME --note "why"
scripts/review.sh defer   PROP_ID --actor NAME
scripts/review.sh edit    PROP_ID --actor NAME --set field=value
scripts/review.sh status | flush                 # K9 uncommitted state / retry commits
```

`review.sh` runs `scripts/brain_review.py` inside the image as an explicit
human context. The same CLI refuses to act in the server's environment
(`BRAIN_REMOTE_SESSION=1`), and no MCP tool imports it. Every mutation needs
`--actor` and interactive confirmation (type the id) unless `--yes`.
`accept` re-checks the evidence against the current records.

## Promotion to canonical (owner)

1. Accept the proposal.
2. Author the canonical change yourself (edit the record under `03-objects/`
   etc., or use `/wiki-write` in a local Claude Code session). Update
   registers/entry pages if the validators require it.
3. `scripts/review.sh promote PROP_ID --actor NAME [--paths ...]`

`promote` requires `accepted`, refuses `_originals/`, runs the full
validation (`validate_repo.py --full`, `validate_content_release.py`,
`check_against_baseline.py`), then commits **only the canonical paths** in a
commit of its own (`canonical: promote PROP_ID (human: NAME)`), and records
the promotion in the queue in a separate noncanonical commit. Dirty files
outside the archive content (code, deploy files) are never swept in: name
them with `--paths` or commit them separately. A validation failure leaves
the working tree exactly as you left it.
