# Agent 0 reuse audit: 0xBrain source acquisition blocker

**Status:** Blocked before source archaeology; this document is not an accepted
contract freeze and must not be used to start Agents A–G.

## 1. Verdict

The repository being changed is the local checkout at `/workspace/0xbrain`.
Its intended GitHub repository is `doctordocto159753/0xbrain`; the earlier
report incorrectly treated `mozareeduge/living-wiki-kit` as the destination
repository rather than an external implementation base to inspect and reuse.

The required reuse audit still cannot be completed from this checkout. It
contains only `.gitkeep`, the local initialization commit, and this blocker
report. Neither the actual `doctordocto159753/0xbrain` contents nor the external
`living-wiki-kit` source are available locally. Outbound GitHub access is denied
by the execution environment with HTTP 403 responses.

It would be unsafe to turn the feature summary supplied in the task into an
"exact" preservation matrix. The task explicitly says that summary is not a
substitute for source inspection. No architecture decision, replacement,
external dependency adoption, shared contract, ownership assignment, or agent
branch is therefore approved by this report.

## 2. Source commit inspected

No source commit from `doctordocto159753/0xbrain` or `living-wiki-kit` was
available to inspect.

The only available commit is `185b4a8` (`Initialize repository`). It contains
only `.gitkeep` and has not been verified as a commit from the target GitHub
repository.

Acquisition of the actual target was attempted with:

```sh
git remote set-url upstream https://github.com/doctordocto159753/0xbrain.git
git fetch upstream --tags
```

The fetch failed with `CONNECT tunnel failed, response 403`. Earlier attempts
to fetch the external Living Wiki repository and its GitHub archives failed for
the same environmental reason.

## 3. Baseline validator/test status

No baseline validators or tests exist in this checkout, so none could be run.
This is an acquisition failure, not a passing baseline.

## 4. Feature-preservation matrix

The exact required schema is frozen below, but every row remains unclassified
until the implementation base is available. Claims in the prompt are recorded
only as an inspection queue, never as verified facts.

| Capability | Existing implementation/files | Existing tests | Current dependency | Model required? | Decision | Reason | Owning agent |
|---|---|---|---|---|---|---|---|
| Repository/instance layout and instantiation | UNVERIFIED: inspect numbered layers, special directories, `scripts/instantiate.py` | UNVERIFIED | UNVERIFIED | UNVERIFIED | **Unclassified** | Source absent | Unassigned |
| Governance, authority, record grammar, and handoffs | UNVERIFIED: inspect `SYSTEM_DESIGN.md`, `00-system/**`, templates/registers | UNVERIFIED | UNVERIFIED | UNVERIFIED | **Unclassified** | Source absent | Unassigned |
| Repository and content-release validation | UNVERIFIED: inspect validator scripts, fixtures, hooks, and CI | UNVERIFIED | UNVERIFIED | UNVERIFIED | **Unclassified** | Source absent | Unassigned |
| Governed multimodal capture and recovery | UNVERIFIED: inspect `scripts/capture/**` and tests | UNVERIFIED | UNVERIFIED | UNVERIFIED | **Unclassified** | Source absent | Unassigned |
| MCP read/search/proposal/capture tools | UNVERIFIED: inspect `scripts/wiki_mcp_server.py` and tests | UNVERIFIED | UNVERIFIED | UNVERIFIED | **Unclassified** | Source absent | Unassigned |
| Exact search | UNVERIFIED: inspect `wiki_exact` implementation and tests | UNVERIFIED | UNVERIFIED | UNVERIFIED | **Unclassified** | Source absent | Unassigned |
| QMD lexical/BM25 search | UNVERIFIED: inspect QMD scripts/configuration and benchmark assets | UNVERIFIED | UNVERIFIED | UNVERIFIED | **Unclassified** | Source absent | Unassigned |
| Semantic/hybrid search | UNVERIFIED: inspect QMD modes and benchmark fixtures | UNVERIFIED | UNVERIFIED | UNVERIFIED | **Unclassified** | Source absent | Unassigned |
| File-to-Markdown conversion | UNVERIFIED: inspect converters, provenance output, dependencies, fixtures | UNVERIFIED | UNVERIFIED | UNVERIFIED | **Unclassified** | Source absent | Unassigned |
| Proposal queue and canonical promotion | UNVERIFIED: inspect proposal scripts/schema/validators/tests | UNVERIFIED | UNVERIFIED | UNVERIFIED | **Unclassified** | Source absent | Unassigned |
| Reconciliation context assembly | UNVERIFIED: inspect skills, scripts, records, and tests | UNVERIFIED | UNVERIFIED | UNVERIFIED | **Unclassified** | Source absent | Unassigned |
| Interchange and public export | UNVERIFIED: inspect export scripts, schemas, and tests | UNVERIFIED | UNVERIFIED | UNVERIFIED | **Unclassified** | Source absent | Unassigned |
| Claude skills and repository workflows | UNVERIFIED: inspect `.claude/skills/**`, `CLAUDE.md`, and workflow guides | UNVERIFIED | UNVERIFIED | UNVERIFIED | **Unclassified** | Source absent | Unassigned |
| Backup/restore and platform helpers | UNVERIFIED: discover helper scripts and documentation | UNVERIFIED | UNVERIFIED | UNVERIFIED | **Unclassified** | Source absent | Unassigned |
| Remote MCP transport/authentication | UNVERIFIED: first establish current protocol and seams | UNVERIFIED | UNVERIFIED | UNVERIFIED | **Unclassified** | Source absent; spike cannot be scoped safely | Unassigned |
| Personal-VPS deployment | UNVERIFIED: discover current packaging and deployment assets | UNVERIFIED | UNVERIFIED | UNVERIFIED | **Unclassified** | Source absent | Unassigned |

## 5. Model-removal matrix

No inference path can be classified without tracing code, configuration,
dependency manifests, and tests.

| Inference category | Implementation/dependency | Default behavior | Required disposition | Verified decision |
|---|---|---|---|---|
| Local embeddings | UNVERIFIED | UNVERIFIED | Assess `remove from default` versus `keep optional` | Unclassified |
| Local reranking | UNVERIFIED | UNVERIFIED | Assess `remove from default` versus `keep optional` | Unclassified |
| Local query expansion | UNVERIFIED | UNVERIFIED | Assess `remove from default` versus `keep optional` | Unclassified |
| Local OCR | UNVERIFIED | UNVERIFIED | Assess `delegate to Claude` and `manual fallback` | Unclassified |
| Local STT | UNVERIFIED | UNVERIFIED | Assess `delegate to Claude` and `manual fallback` | Unclassified |
| Local vision | UNVERIFIED | UNVERIFIED | Assess `delegate to Claude` and `manual fallback` | Unclassified |
| Other local inference | UNVERIFIED | UNVERIFIED | Discover through source/dependency search | Unclassified |

## 6. External reuse recommendations

No candidate is adopted or rejected here because compatibility must be checked
against the missing implementation. Once source access is restored, the spike
order requested by the architecture brief should be evaluated: Supergateway,
official MCP Python SDK v2, then FastMCP with Pocket ID where an IdP is actually
needed. MarkItDown must be compared with the existing converter rather than
adopted pre-emptively. Current versions, licenses, maintenance status, protocol
compatibility, runtime cost, and backup/upgrade implications must be recorded
from primary sources at spike time.

## 7. Frozen shared contracts

No contracts are frozen. In particular, the folder/record grammar, reusable MCP
function signatures, remote aliases, auth boundary, search result contract,
capture state contract, reconciliation package, and deployment configuration
names all require inspection of the implementation base first.

The invariants in the architecture brief remain requirements, but are not a
substitute for recording their current implementation and test coverage.

## 8. File ownership map

No file ownership is assigned. Assigning paths before the files are present
would create precisely the overlap and architecture drift that Agent 0 is meant
to prevent.

## 9. Parallel branch plan

The intended names are reserved but must not yet be created:

```text
agent/a-mcp-auth
agent/b-claude-surface
agent/c-search
agent/d-capture
agent/e-governance
agent/f-io
agent/g-deploy
```

All seven must branch from the same future accepted baseline commit, after this
audit is replaced by a complete, evidence-backed report.

## 10. Risks and merge hotspots

The immediate risk is treating an asserted baseline as inspected source. Other
risks—MCP module overlap, capture schema changes, search configuration, validator
coupling, dependency manifests, Compose/Caddy integration, and documentation
ownership—cannot be bounded until the repository is present.

## 11. Instructions to Agents A–G

**Do not start parallel implementation.** The mandatory matrix has not been
accepted because it could not be produced from source. After source restoration,
Agent 0 must inspect the required files, discover additional relevant files, run
the complete baseline test suite, replace every `UNVERIFIED` cell, freeze the
contracts and ownership map, and identify the accepted baseline commit. Only
then should all agent branches be created from that exact commit.

## Unblocking procedure

Provide the target `doctordocto159753/0xbrain` repository contents in this
workspace (including Git history), or permit outbound access to GitHub. The
Living Wiki repository must then be made available as a separate reuse source.
Use distinct remotes so their roles cannot be confused:

```sh
git remote add origin https://github.com/doctordocto159753/0xbrain.git
git remote add living-wiki https://github.com/mozareeduge/living-wiki-kit.git
git fetch --all --tags
```

If source is supplied by an archive instead, its exact upstream commit SHA must
also be supplied or otherwise verifiably recovered. The audit should resume
from the target repository's actual source commit, record the distinct Living
Wiki commit inspected, and never replace the target repository with the reuse
source.
