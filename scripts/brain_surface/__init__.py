"""Claude-facing semantic surface (frozen K3) over the Living Wiki core.

Transport and auth live in scripts/remote_mcp (Agent A). This package owns the
tool contract (contract.py), the one public ref grammar (refs.py), the
envelopes (surface.py) and the single production backend (backend.py) that
wires the surface to search (C), capture (D) and governance (E).
"""
from .contract import TOOLS, TOOL_NAMES  # noqa: F401
from .surface import BrainSurface  # noqa: F401
