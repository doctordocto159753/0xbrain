# Source map: standing instructions -> existing guidance

Reuse-first traceability. Each rule in `STANDING_INSTRUCTIONS.md` is derived
from, not invented beside, existing repository guidance. "Changed" marks where
the remote-conversation setting forced a difference.

| Standing rule (section) | Derived from | Changed for remote conversation |
|---|---|---|
| 1 tool honesty | `CLAUDE.md` rule 10 (show commands and decisive results before claiming success) | "commands" become tool results; adds `persisted`/`commit_state` (K9) |
| 2 retrieve when it matters | `wiki-search` skill; `CLAUDE.md` rule 5 (canonical first) | skill's `qmd query` hybrid is model-backed; replaced by lexical+exact with query reformulation (K7, Agent 0 risk 4) |
| 3 capture durable, verbatim | `wiki-voice-capture` skill ("verbatim, no summaries/titles/tags"; "suggest, do not capture unbidden") | proactive capture of clearly durable material with a one-line notice; skill's "suggest only" kept for uncertain cases; media capture removed (K3 text-first) |
| 4 authority hierarchy | `SYSTEM_DESIGN.md` section 2.2; `AGENTS.md` non-negotiable 2-3; `CLAUDE.md` rule 4, 6 | none |
| 5 source/original/derivative | `CLAUDE.md` rule 3, 5; `wiki-search` steps 5-6 | originals unreachable remotely, so the rule becomes "say the original must be consulted" |
| 6 relation before type, history, claims | `SYSTEM_DESIGN.md` section 2.3; `GPT_WORKFLOW.md` section 5; `wiki-write` steps 3-6 | applied to proposals, not page writing |
| 7 conflict and reconciliation | `wiki-reconcile` skill (corpus-wide, manifest-accounted); `GPT_WORKFLOW.md` section 4 | the full-corpus gate is NOT reproduced remotely: `brain_reconcile_context` is a bounded neighborhood, and the standing text says a partial package is partial |
| 8 proposals | `AGENTS.md` "Propose"; `proposal_schema.json`; `evidence_audit.py` | structured fields + evidence refs replace free-text body; refs replace paths |
| 9 media limits | `stt_contract.py` / capture core (`manual` default; no auto path without benchmark); Agent 0 section 5 | remote Claude cannot open media; `needs_*` reported, never filled |
| 10 quality, no editing tools | `AGENTS.md` non-negotiable 1, 4-5; Agent 0 K3 "not exposed" | none |
