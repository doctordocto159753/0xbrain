"""Adapter seam between the remote transport and the tool surface.

The transport knows nothing about wiki semantics. It asks an adapter for a
list of ``ToolSpec`` and dispatches calls to ``ToolSpec.handler``.

Contract (stable for Agent B / Agent H):
  * ``handler(arguments: dict) -> str`` returns the tool result text (JSON).
  * handlers are synchronous and may block; the transport runs them in a
    worker thread.
  * an adapter is a zero-argument callable returning ``list[ToolSpec]``.

Selecting an adapter: ``BRAIN_MCP_ADAPTER=package.module:callable``.
Default: ``legacy_adapter`` (the twelve existing ``wiki_*`` stdio tools).
"""
from __future__ import annotations

import importlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[2]

# Tools the REMOTE surface must never expose, whatever an adapter returns.
# Global rule: remote MCP never performs human review or canonical acceptance.
REMOTE_DENYLIST = frozenset({"wiki_mark_capture_reviewed"})


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    handler: Callable[[dict], str]


def legacy_adapter() -> list[ToolSpec]:
    """Wrap the existing stdio tool functions unchanged (reuse, no rewrite)."""
    scripts = str(ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import wiki_mcp_server as legacy  # noqa: PLC0415

    return [
        ToolSpec(t["name"], t["description"], t["inputSchema"], legacy.DISPATCH[t["name"]])
        for t in legacy.TOOLS
    ]


def load_adapter(spec: str | None = None) -> list[ToolSpec]:
    spec = spec or os.environ.get("BRAIN_MCP_ADAPTER") or ""
    if not spec:
        tools = legacy_adapter()
    else:
        mod_name, _, attr = spec.partition(":")
        if not mod_name or not attr:
            raise ValueError("BRAIN_MCP_ADAPTER must look like 'package.module:callable'")
        tools = getattr(importlib.import_module(mod_name), attr)()
    names = [t.name for t in tools]
    if len(set(names)) != len(names):
        raise ValueError("adapter returned duplicate tool names")
    return [t for t in tools if t.name not in REMOTE_DENYLIST]
