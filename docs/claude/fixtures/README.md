# Behavioral fixtures

Nine reference scenarios (`NN-*.json`). Two uses:

1. Automated (`tests/test_brain_fixtures.py`): replays `reference_calls`
   against the real surface with a fake backend, checks results, forbidden
   tools, and lints `reference_reply` (no persistence claim without a
   successful persisted write; cited refs must come from tool results;
   required/forbidden phrases). This checks the tools and the reference
   behavior, not a live model.
2. Manual (official Claude, real connector): give `user_prompt` in a fresh
   chat with `STANDING_INSTRUCTIONS.md` installed; pass if the tool-call
   sequence matches `reference_calls` in intent, no `forbidden_tools` are
   called, and the reply satisfies `reply_must_match` / `reply_must_not_match`.
   Seed the archive as `setup` describes (or adapt the ids).

| # | Scenario | Rule |
|---|---|---|
| 01 | retrieval of prior project context | retrieve, read, cite refs |
| 02 | durable decision capture | verbatim, report ref and state |
| 03 | trivial chat not captured | zero tool calls |
| 04 | conflict with older record | read + reconcile, capture verbatim, propose correction, candidate wording, nothing overwritten |
| 05 | provisional relation | unclassified candidate relation |
| 06 | evidence-backed proposal | verbatim quote; paraphrase rejected, nothing stored |
| 07 | deep reconciliation pagination | follow `next_cursor` to null, state coverage |
| 08 | connector unavailable | no persistence claim, keep text, offer retry |
| 09 | media exists, not inspectable | no description, name the `needs_*` state |
