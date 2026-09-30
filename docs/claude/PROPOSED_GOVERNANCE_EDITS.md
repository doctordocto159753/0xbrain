# Proposed edits for E (not applied by B)

Per Agent 0 hotspots, `CLAUDE.md` / `AGENTS.md` are proposed by B and merged
by E. None of the following is applied on this branch.

1. `AGENTS.md`, section "Machine contract": add a bullet
   "Remote conversations reach the archive only through the six `brain_*`
   tools (`docs/claude/TOOL_USAGE.md`); standing instructions:
   `docs/claude/STANDING_INSTRUCTIONS.md`. The stdio `wiki_*` tools remain the
   local-harness surface."
2. `AGENTS.md`, "What you MAY do": replace "or use MCP tool `wiki_propose`"
   with "or, remotely, `brain_propose` (structured fields, verbatim evidence)".
3. `CLAUDE.md`, "Always-on rules": add rule "In a conversation using the
   remote connector, never claim persistence or retrieval without the tool
   result (`docs/claude/STANDING_INSTRUCTIONS.md` section 1)."
4. `CLAUDE.md`, "Side-effect workflows": note that `/wiki-*` skills assume a
   filesystem-attached Claude Code and do not apply to remote conversations;
   canonical promotion stays human CLI (K9).
5. `SYSTEM_DESIGN.md`: none. No record grammar or hierarchy is altered.
