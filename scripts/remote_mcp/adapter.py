"""Adapter seam between the remote transport and the tool surface.

The transport knows nothing about wiki semantics. It asks an adapter for a
list of ``ToolSpec`` and dispatches calls to ``ToolSpec.handler``.

Contract:
  * ``handler(arguments: dict) -> dict | str``: a dict is the JSON result
    (``{"ok": False, ...}`` marks a tool error); a str is passed through.
  * handlers are synchronous and may block; the transport runs them in a
    worker thread.

Production surface (fail-closed): exactly the six semantic K3 tools
(``brain_*``) from ``scripts/brain_surface``. ``BRAIN_MCP_ADAPTER`` accepts
only ``semantic`` (default). ``legacy`` (the stdio ``wiki_*`` tools, which
include path-based reads) is a development-only, NOT-for-product mode that
additionally requires ``BRAIN_UNSAFE_REMOTE_LEGACY=1``; arbitrary module
imports are not supported.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = str(ROOT / "scripts")

SEMANTIC_TOOLS = ("brain_search", "brain_read", "brain_capture",
                  "brain_reconcile_context", "brain_propose", "brain_status")

# Never exposed remotely, whatever an adapter returns: human review, path-based
# reads/media, and QMD's own MCP tools (path get/multi_get, model-backed query).
REMOTE_DENYLIST = frozenset({
    "wiki_mark_capture_reviewed", "wiki_read", "wiki_get_media",
    "get", "multi_get", "query", "vsearch", "search", "status",
    "qmd_get", "qmd_multi_get", "qmd_query", "qmd_vsearch", "qmd_search", "qmd_status",
})


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    handler: Callable[[dict], "dict | str"]


def semantic_adapter(root: Path | None = None) -> list[ToolSpec]:
    """The six K3 tools, bound to one BrainSurface over the WikiBackend."""
    if _SCRIPTS not in sys.path:
        sys.path.insert(0, _SCRIPTS)
    from brain_surface import BrainSurface, TOOLS  # noqa: PLC0415
    from brain_surface.backend import WikiBackend  # noqa: PLC0415

    surface = BrainSurface(WikiBackend(root))

    def bind(name: str) -> Callable[[dict], dict]:
        return lambda args: surface.call(name, args)

    specs = [ToolSpec(t["name"], t["description"], t["inputSchema"], bind(t["name"]))
             for t in TOOLS]
    if tuple(s.name for s in specs) != SEMANTIC_TOOLS:
        raise RuntimeError(f"semantic surface drifted from K3: {[s.name for s in specs]}")
    return specs


def legacy_adapter() -> list[ToolSpec]:
    """UNSAFE for remote use: the stdio wiki_* tools (path-based reads)."""
    if _SCRIPTS not in sys.path:
        sys.path.insert(0, _SCRIPTS)
    import wiki_mcp_server as legacy  # noqa: PLC0415

    return [ToolSpec(t["name"], t["description"], t["inputSchema"], legacy.DISPATCH[t["name"]])
            for t in legacy.TOOLS]


def load_adapter(spec: str | None = None, env: dict | None = None) -> list[ToolSpec]:
    env = os.environ if env is None else env
    spec = (spec if spec is not None else env.get("BRAIN_MCP_ADAPTER", "")).strip() or "semantic"
    if spec == "semantic":
        tools = semantic_adapter()
    elif spec == "legacy":
        if env.get("BRAIN_UNSAFE_REMOTE_LEGACY") != "1":
            raise ValueError("BRAIN_MCP_ADAPTER=legacy is a development-only, unsafe mode; "
                             "it also requires BRAIN_UNSAFE_REMOTE_LEGACY=1")
        tools = legacy_adapter()
    else:
        raise ValueError("BRAIN_MCP_ADAPTER must be 'semantic' (default) or 'legacy'")
    return filter_remote(tools)


def filter_remote(tools: list[ToolSpec]) -> list[ToolSpec]:
    names = [t.name for t in tools]
    if len(set(names)) != len(names):
        raise ValueError("adapter returned duplicate tool names")
    return [t for t in tools if t.name not in REMOTE_DENYLIST]
