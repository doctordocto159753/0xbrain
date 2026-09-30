# Attaching 0xBrain to official Claude: owner checklist

Scope of Agent B: what the conversation side needs. Hosting, OAuth and the
public URL belong to Agent A (auth) and Agent G (deploy); this checklist is
written so it can be run against their output at integration (Agent H).
Nothing here was tested against a live official-Claude connection from this
sandbox.

1. Add the remote MCP server URL in Claude (Settings > Connectors > custom).
   Complete the authorization Agent A's server requires.
2. Confirm exactly six tools appear: `brain_search`, `brain_read`,
   `brain_capture`, `brain_reconcile_context`, `brain_propose`, `brain_status`.
   Any other tool (especially anything reviewing, accepting or reading paths)
   is a defect: stop.
3. Create a Project (or use custom instructions) and paste the body of
   `STANDING_INSTRUCTIONS.md`. Attach nothing else; the archive is reached
   only through the tools.
4. Run the nine scenarios in `fixtures/` by hand in a fresh chat (each
   fixture lists a prompt and the expected behavior). A capture round trip:
   "capture this: <sentence>" -> ref -> `brain_status` shows it and, until
   Agent A's commit path lands, `uncommitted`.
5. Kill the server and repeat one request: Claude must say the connector is
   unavailable and must not claim anything was saved (fixture 8).

Known limits to keep in mind: the connector is text-first; Claude does not see
media; lexical search misses paraphrase (upstream recall on natural-language
questions was 3/30), so good queries matter; "not found" is relative to the
queries tried.
