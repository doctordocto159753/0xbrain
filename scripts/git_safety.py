#!/usr/bin/env python3
"""K9 Git safety for single-owner noncanonical writes.

Order of operations for every routine remote write (proposal, capture):

    1. take the single-owner lock         (<git-dir>/brain-write.lock)
    2. durable write                       (fsync; atomic replace for rewrites)
    3. relevant validation                 (caller-supplied, runs on disk state)
    4. git add + git commit                (pathspec-limited to the write set)
    5. on ANY failure after step 2: keep the data, record the failure in
       <git-dir>/brain-uncommitted.json, return committed=False

Failed commits never delete, revert, or stash user data. `uncommitted_state()`
lists every uncommitted file under the noncanonical prefixes plus the last
recorded failure, so `brain_status` can expose it; `flush_pending()` retries.

This module refuses to commit anything outside NONCANONICAL_PREFIXES.
Canonical promotion is a different, human-only path (brain_review.py promote)
with full validation and a separate commit. No model call, no network.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Iterable

NONCANONICAL_PREFIXES = ("_proposals/", "01-inbox/captures/", "_captures/")
LOCK_NAME = "brain-write.lock"
FAILURE_NAME = "brain-uncommitted.json"
LOCK_STALE_SECONDS = 600
LOCK_WAIT_SECONDS = 30


class GovernanceError(Exception):
    """Refused operation (canonical path, lock timeout, bad repo)."""


# ---------------------------------------------------------------------------
# git plumbing

def _git(root: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "-c", "core.quotepath=off", *args], cwd=str(root),
        text=True, capture_output=True, encoding="utf-8", errors="replace")
    if check and proc.returncode != 0:
        raise GovernanceError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:400]}")
    return proc


def git_dir(root: Path) -> Path:
    out = _git(root, "rev-parse", "--absolute-git-dir")
    if out.returncode != 0:
        raise GovernanceError(f"not a git repository: {root}")
    return Path(out.stdout.strip())


def is_noncanonical(rel: str) -> bool:
    rel = rel.replace("\\", "/").lstrip("./")
    if ".." in rel.split("/"):
        return False
    return rel.startswith(NONCANONICAL_PREFIXES)


def _rel(root: Path, p: Path | str) -> str:
    p = Path(p)
    if p.is_absolute():
        p = p.resolve().relative_to(root.resolve())
    return p.as_posix()


# ---------------------------------------------------------------------------
# durable file primitives

def _fsync_dir(d: Path) -> None:
    try:
        fd = os.open(str(d), os.O_RDONLY)
    except OSError:
        return  # not supported (e.g. Windows); best effort
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def durable_append(path: Path, line: str) -> None:
    """Append one line and fsync. Caller holds the lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (line.rstrip("\n") + "\n").encode("utf-8")
    with open(path, "ab") as fh:
        # A crash can leave a partial last line; start on a fresh line.
        if path.exists() and path.stat().st_size and _last_byte(path) != b"\n":
            fh.write(b"\n")
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    _fsync_dir(path.parent)


def _last_byte(path: Path) -> bytes:
    with open(path, "rb") as fh:
        fh.seek(-1, os.SEEK_END)
        return fh.read(1)


def durable_replace(path: Path, text: str) -> None:
    """Atomic full rewrite (temp file + fsync + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    with open(tmp, "wb") as fh:
        fh.write(text.encode("utf-8"))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


# ---------------------------------------------------------------------------
# single-owner lock

def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class BrainLock:
    """Exclusive lock file in the git dir (never committed, never in tree)."""

    def __init__(self, root: Path, timeout: float = LOCK_WAIT_SECONDS,
                 stale_after: float = LOCK_STALE_SECONDS):
        self.path = git_dir(Path(root)) / LOCK_NAME
        self.timeout = timeout
        self.stale_after = stale_after
        self._held = False

    def _stale(self) -> bool:
        try:
            info = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            try:
                return time.time() - self.path.stat().st_mtime > self.stale_after
            except OSError:
                return False  # vanished: not stale, just gone
        same_host = info.get("host") == socket.gethostname()
        if same_host and not _pid_alive(int(info.get("pid", 0) or 0)):
            return True
        return time.time() - float(info.get("ts", 0)) > self.stale_after

    def acquire(self) -> None:
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if self._stale():
                    try:
                        self.path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
                if time.monotonic() >= deadline:
                    raise GovernanceError(
                        f"write lock busy ({self.path}); retry later")
                time.sleep(0.05)
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"pid": os.getpid(), "host": socket.gethostname(),
                           "ts": time.time()}, fh)
            self._held = True
            return

    def release(self) -> None:
        if self._held:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            self._held = False

    def __enter__(self) -> "BrainLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


# ---------------------------------------------------------------------------
# failure journal + uncommitted state

def _failure_path(root: Path) -> Path:
    return git_dir(root) / FAILURE_NAME


def _record_failure(root: Path, stage: str, error: str, paths: list[str]) -> None:
    rec = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "stage": stage, "error": error[:2000], "paths": sorted(paths)}
    try:
        durable_replace(_failure_path(root), json.dumps(rec, indent=1))
    except OSError:
        pass  # journal is advisory; git status remains the source of truth


def _clear_failure(root: Path) -> None:
    try:
        _failure_path(root).unlink()
    except (FileNotFoundError, GovernanceError):
        pass


def uncommitted_state(root: Path) -> dict:
    """Uncommitted noncanonical files + last recorded write failure.

    Source of truth is `git status`; the journal only adds the reason.
    """
    root = Path(root)
    out = _git(root, "status", "--porcelain=v1", "-z", "-uall", "--",
               *[p.rstrip("/") for p in NONCANONICAL_PREFIXES])
    files: list[dict] = []
    if out.returncode == 0:
        entries = out.stdout.split("\0")
        i = 0
        while i < len(entries):
            e = entries[i]
            i += 1
            if len(e) < 4:
                continue
            code, path = e[:2], e[3:]
            if code[0] in "RC":
                i += 1  # skip the "from" path of a rename/copy
            files.append({"path": path, "status": code.strip() or "?"})
    files.sort(key=lambda f: f["path"])
    failure = None
    try:
        failure = json.loads(_failure_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError, GovernanceError):
        failure = None
    return {"uncommitted_count": len(files), "uncommitted": files,
            "last_failure": failure,
            "clean": not files and failure is None}


# ---------------------------------------------------------------------------
# write + validate + commit

def _commit_paths(root: Path, paths: list[str], message: str) -> tuple[bool, str | None, str]:
    """git add + pathspec-limited commit. Returns (committed, sha, detail)."""
    add = _git(root, "add", "--", *paths)
    if add.returncode != 0:
        return False, None, f"git add failed: {add.stderr.strip()[:800]}"
    staged = _git(root, "diff", "--cached", "--quiet", "--", *paths)
    if staged.returncode == 0:
        return True, None, "no changes to commit"
    com = _git(root, "commit", "-m", message, "--", *paths)
    if com.returncode != 0:
        detail = (com.stdout + com.stderr).strip()[-1500:]
        # Leave working files untouched; unstage so the index is not left dirty.
        _git(root, "reset", "-q", "--", *paths)
        return False, None, f"git commit failed: {detail}"
    sha = _git(root, "rev-parse", "HEAD").stdout.strip()
    return True, sha, "committed"


def locked_write_commit(root: Path, write: Callable[[], Iterable[str | Path]],
                        message: str,
                        validate: Callable[[], list[str]] | None = None,
                        timeout: float = LOCK_WAIT_SECONDS) -> dict:
    """The K9 sequence. `write()` performs the durable write(s) and returns
    the written paths; `validate()` returns error strings (empty = ok).

    Always returns a dict: {durable, committed, commit, paths, stage, error,
    uncommitted}. Raises GovernanceError only for refusals BEFORE any write
    (canonical path, busy lock). After the write nothing raises: data is
    preserved and the failure is recorded.
    """
    root = Path(root)
    with BrainLock(root, timeout=timeout):
        written = [_rel(root, p) for p in write()]
        bad = [p for p in written if not is_noncanonical(p)]
        if bad:
            # The write already happened; never delete it, but never commit it.
            _record_failure(root, "refused-canonical", f"canonical paths written: {bad}", written)
            return _result(root, True, False, None, written, "refused-canonical",
                           f"refusing to auto-commit non-noncanonical paths: {bad}")
        if validate is not None:
            try:
                errs = validate()
            except Exception as exc:  # noqa: BLE001
                errs = [f"validator crashed: {type(exc).__name__}: {exc}"]
            if errs:
                _record_failure(root, "validation", "; ".join(errs), written)
                return _result(root, True, False, None, written, "validation", "; ".join(errs)[:2000])
        try:
            ok, sha, detail = _commit_paths(root, written, message)
        except Exception as exc:  # noqa: BLE001
            ok, sha, detail = False, None, f"{type(exc).__name__}: {exc}"
        if not ok:
            _record_failure(root, "commit", detail, written)
            return _result(root, True, False, None, written, "commit", detail)
        if not uncommitted_state(root)["uncommitted"]:
            _clear_failure(root)
        return _result(root, True, True, sha, written, None, detail)


def _result(root: Path, durable: bool, committed: bool, sha: str | None,
            paths: list[str], stage: str | None, detail: str) -> dict:
    return {"durable": durable, "committed": committed, "commit": sha,
            "paths": paths, "stage": stage,
            "error": None if committed else detail,
            "detail": detail,
            "uncommitted": uncommitted_state(root)["uncommitted"]}


def flush_pending(root: Path, message: str = "brain: commit pending noncanonical writes",
                  validate: Callable[[], list[str]] | None = None) -> dict:
    """Retry committing every uncommitted noncanonical file (owner action)."""
    root = Path(root)

    def _paths() -> list[str]:
        return [f["path"] for f in uncommitted_state(root)["uncommitted"]]

    if not _paths():
        return _result(root, True, True, None, [], None, "nothing pending")
    return locked_write_commit(root, _paths, message, validate)


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="K9 uncommitted-state inspector / flusher")
    ap.add_argument("--root", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("flush")
    args = ap.parse_args(argv)
    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parents[1]
    if args.cmd == "status":
        st = uncommitted_state(root)
        print(json.dumps(st, ensure_ascii=False, indent=1))
        return 0 if st["clean"] else 1
    res = flush_pending(root)
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0 if res["committed"] else 1


if __name__ == "__main__":
    sys.exit(main())
