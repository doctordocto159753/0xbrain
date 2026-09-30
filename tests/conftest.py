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


# ---------------------------------------------------------------- brain wiki
# A real, throwaway wiki: the kit's tracked files copied into tmp, committed
# as `main`, with wiki_capture re-rooted onto it. Used by the integrated
# K3 surface / remote MCP tests (Agent H). No hooks unless a test installs one.

import shutil as _shutil  # noqa: E402
import subprocess as _sp  # noqa: E402

_SKIP_PREFIXES = ("tests/", "docs/", "openspec/", "_captures/", "07-genesis/")


def _git(root: Path, *args: str) -> str:
    return _sp.run(["git", *args], cwd=root, text=True, capture_output=True,
                   check=True).stdout


def record(root: Path, rel: str, rid: str, title: str, body: str,
           links=(), status: str = "active", fm_extra: str = "") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    tail = "".join(f" [[{t}]]" for t in links)
    p.write_text(f"---\nid: {rid}\ntype: object\ntitle: {title}\nstatus: {status}\n{fm_extra}"
                 f"---\n\n# {title}\n\n{body}{tail}\n", encoding="utf-8")
    return p


@pytest.fixture()
def brain_wiki(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "wiki"
    files = _sp.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                    cwd=KIT_ROOT, text=True,
                    capture_output=True, check=True).stdout.split("\0")
    for rel in files:
        if not rel or rel.startswith(_SKIP_PREFIXES):
            continue
        src = KIT_ROOT / rel
        if src.is_file():
            dst = root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            _shutil.copy2(src, dst)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "brain@example.invalid")
    _git(root, "config", "user.name", "brain-test")
    _git(root, "config", "core.hooksPath", "/dev/null")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    captures = root / "01-inbox" / "captures"
    monkeypatch.setattr(wc, "REPO_ROOT", root)
    monkeypatch.setattr(wc, "CAPTURES_ROOT", captures)
    monkeypatch.setattr(wc, "ORPHAN_QUARANTINE", captures / ".quarantine")
    monkeypatch.delenv("WIKI_QMD_HOME", raising=False)
    monkeypatch.delenv("BRAIN_REMOTE_SESSION", raising=False)
    return root


def commit_all(root: Path, msg: str = "fixture") -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "--allow-empty", "-m", msg)
