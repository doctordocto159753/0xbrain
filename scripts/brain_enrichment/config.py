"""Activation options (CONTRACT-E1 section 6). Invalid values never enable
anything: they fall back to the safe default and are reported in `warnings`."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

MODES = ("off", "shadow", "on")


@dataclass(frozen=True)
class EnrichmentConfig:
    mode: str = "off"
    writes: bool = False            # effective only when mode == "on"
    retrieval: bool = False         # when True the retrieve tool may fall back / use index
    batch_size: int = 1
    max_input_chars: int = 24000
    max_output_bytes: int = 65536
    warnings: tuple[str, ...] = field(default=())

    @property
    def extension_enabled(self) -> bool:
        return self.mode == "on"

    @property
    def writes_enabled(self) -> bool:
        return self.mode == "on" and self.writes

    @property
    def sidecar_enabled(self) -> bool:
        """Store/index may be built (CLI, shadow). Never implies a tool."""
        return self.mode in ("shadow", "on")

    @classmethod
    def from_env(cls, env=None) -> "EnrichmentConfig":
        env = os.environ if env is None else env
        warns: list[str] = []

        def integer(name: str, default: int, lo: int, hi: int) -> int:
            raw = env.get(name)
            if raw in (None, ""):
                return default
            try:
                v = int(str(raw).strip())
            except ValueError:
                warns.append(f"{name}={raw!r} is not an integer; using {default}")
                return default
            if not lo <= v <= hi:
                warns.append(f"{name}={v} outside [{lo},{hi}]; using {default}")
                return default
            return v

        mode = (env.get("BRAIN_ENRICHMENT") or "off").strip().lower()
        if mode not in MODES:
            warns.append(f"BRAIN_ENRICHMENT={mode!r} is not one of {MODES}; using off")
            mode = "off"
        flag = lambda n: (env.get(n) or "0").strip() == "1"  # noqa: E731
        return cls(mode=mode, writes=flag("BRAIN_ENRICHMENT_WRITES"),
                   retrieval=flag("BRAIN_ENRICHMENT_RETRIEVAL"),
                   batch_size=integer("BRAIN_ENRICHMENT_BATCH_SIZE", 1, 1, 5),
                   max_input_chars=integer("BRAIN_ENRICHMENT_MAX_INPUT_CHARS", 24000, 2000, 120000),
                   max_output_bytes=integer("BRAIN_ENRICHMENT_MAX_OUTPUT_BYTES", 65536, 4096, 262144),
                   warnings=tuple(warns))
