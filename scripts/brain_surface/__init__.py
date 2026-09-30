"""Claude-facing semantic surface (frozen K3) over the Living Wiki core.

Transport, auth and Git commit policy live elsewhere (Agent A / K9). This
package owns only the tool contract: argument validation, ref discipline,
result envelopes, authority labelling and the K5 pagination envelope.
"""
from .contract import TOOLS, TOOL_NAMES  # noqa: F401
from .surface import BrainSurface  # noqa: F401
