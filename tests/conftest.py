"""Shared fixtures for the capture-core tests (Agent D).

`cap` re-roots wiki_capture onto a throwaway tree so tests never touch the
real `01-inbox/captures`. Inert for every other test module.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

KIT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT_ROOT / "scripts" / "capture"))

import wiki_capture as wc  # noqa: E402

# Minimal byte strings whose headers satisfy wiki_capture.MAGIC.
OGG = b"OggS" + b"\x00" * 60
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 60
PDF = b"%PDF-1.4\n" + b"\x00" * 60


@pytest.fixture()
def cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "wiki"
    captures = root / "01-inbox" / "captures"
    captures.mkdir(parents=True)
    monkeypatch.setattr(wc, "REPO_ROOT", root)
    monkeypatch.setattr(wc, "CAPTURES_ROOT", captures)
    monkeypatch.setattr(wc, "ORPHAN_QUARANTINE", captures / ".quarantine")
    return wc


@pytest.fixture()
def media(tmp_path: Path):
    """Factory: write source media outside the captures root."""
    src = tmp_path / "src"
    src.mkdir()

    def make(name: str, data: bytes) -> str:
        p = src / name
        p.write_bytes(data)
        return str(p)

    return make
