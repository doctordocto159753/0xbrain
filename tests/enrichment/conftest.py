"""Shared enrichment fixtures (AGENT-0). `enrichment_wiki` is a real throwaway
git wiki (root conftest `brain_wiki`) seeded with the cases CONTRACT-E1 must
survive: Persian/CRLF/escaped-heading captures, a long multi-chunk capture,
duplicate text, a voice capture with and without transcript, held files with
derivative / without derivative / needing OCR, a canonical record and an
unregistered proposal. Nothing here touches a real corpus."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for p in (HERE, ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from support import SeededWiki, seed_wiki  # noqa: E402


@pytest.fixture()
def enrichment_wiki(brain_wiki) -> SeededWiki:
    return seed_wiki(brain_wiki)
