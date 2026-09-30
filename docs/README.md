# 0xBrain documentation: start here

0xBrain is a Living Wiki instance (a governed, Git-versioned archive with an
authority hierarchy) that Claude can use remotely through six semantic MCP
tools. The server is model-free: search is lexical (QMD BM25) plus exact
text; all interpretation happens in Claude and stays candidate-tier until a
human promotes it.

| I want to... | Read |
|---|---|
| deploy it on a VPS | [INSTALL.md](INSTALL.md) |
| connect official Claude | [CONNECT_CLAUDE.md](CONNECT_CLAUDE.md) |
| know what to ask Claude, day to day | [USAGE.md](USAGE.md) |
| understand search and its limits | [../SEARCH_GUIDE.md](../SEARCH_GUIDE.md) |
| understand capture (verbatim intake) | [CAPTURE.md](CAPTURE.md) |
| review and promote Claude's proposals | [PROPOSALS_AND_REVIEW.md](PROPOSALS_AND_REVIEW.md) |
| understand reconciliation packages | [RECONCILIATION.md](RECONCILIATION.md) |
| back up / restore / move hosts | [BACKUP_RESTORE.md](BACKUP_RESTORE.md) |
| upgrade or roll back | [UPGRADE.md](UPGRADE.md) |
| fix something | [TROUBLESHOOTING.md](TROUBLESHOOTING.md) |
| see the architecture | [../FINAL_ARCHITECTURE.md](../FINAL_ARCHITECTURE.md) |
| review auth and security | [SECURITY.md](SECURITY.md) |
| the Claude-side contract | [claude/STANDING_INSTRUCTIONS.md](claude/STANDING_INSTRUCTIONS.md), [claude/TOOL_USAGE.md](claude/TOOL_USAGE.md) |
| the governance rules of the archive itself | [../SYSTEM_DESIGN.md](../SYSTEM_DESIGN.md), [../CLAUDE.md](../CLAUDE.md), [../AGENTS.md](../AGENTS.md) |

Local (non-remote) use of the kit is unchanged: `SETUP_GUIDE_WINDOWS.md`,
`INSTANTIATE.md`, the `/wiki-*` skills and the stdio MCP server
(`scripts/wiki_mcp_server.py`, `.mcp.json`) for Claude Code on a machine that
holds the repository.

## Development and tests

One reproducible environment (Python 3.11+; Node 22+ only for QMD tests):

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-remote.txt \
    -r deploy/requirements-image.txt -r requirements-dev.txt
npm install -g @tobilu/qmd@2.8.3          # optional: lexical-search tests
.venv/bin/python -m pytest -q
```

Release gates: `FINAL_TEST_REPORT.md`.
