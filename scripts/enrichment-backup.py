#!/usr/bin/env python3
"""Bounded backup / verify / restore for the enrichment upgrade (WP-E, python stdlib only).

Why a separate tool: scripts/create-backup.sh tars the data directory with a
plain `tar`, which (a) copies an SQLite WAL database as raw files (not a
consistent snapshot) and (b) does not record the image or the code manifest the
running stack was using. This tool keeps the same coverage but

  * snapshots every SQLite file under `_enrichment/` through the SQLite online
    backup API (the raw `*.sqlite3*` files are NOT archived),
  * includes `_enrichment/` and excludes `_search/` (rebuildable index),
  * archives compose files / the env file that live OUTSIDE the data dir, plus
    any `--extra-path` (e.g. a reverse-proxy config or TLS state), separately,
  * records the old image id and a code manifest, hashes every member while it
    is written, and proves the archive by restoring it into a scratch dir,
  * writes the archive mode 0600 and never prints a secret value.

Every mutating subcommand is a DRY RUN unless `--execute` AND the matching
`--confirm TOKEN` (printed by the dry run) are given. `verify` and `list` only
read the archive (verify extracts into a private scratch dir it removes).
`restore` only ever writes into a NEW/EMPTY directory that is not the live wiki;
there is no force flag and it never stops or starts a stack.

Nothing here talks to a network. Docker/git are reached only through a guarded
command runner (see GUARD / guard_command), so a forbidden command (global
compose down, git reset --hard, git clean, recursive chown, ...) cannot be run
by construction.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from typing import Callable, Iterable

SCHEMA = "enrichment-backup-1"
TOOL = "enrichment-backup.py"
log = logging.getLogger("enrichment_backup")

# --------------------------------------------------------------------------
# layout knowledge (relative to the wiki / data directory)
CORPUS_ZONES = ("_originals", "00-system", "01-inbox", "02-sources", "03-objects", "04-notes",
                "05-claims", "06-relations", "07-genesis", "_proposals", "_captures")
ENRICHMENT_DIR = "_enrichment"
EXCLUDED_TOP = ("_search",)                      # rebuildable index: never archived
EXCLUDED_DIRNAMES = ("__pycache__", ".pytest_cache")
SQLITE_SUFFIXES = (".sqlite3", ".sqlite", ".db")
SQLITE_SIDE = ("-wal", "-shm", "-journal")
STATE_INCLUDE = ("auth", "caddy/data", "caddy/config")   # same as create-backup.sh
SAFE_ENV_KEYS = ("BRAIN_DATA_DIR", "BRAIN_STATE_DIR", "BRAIN_UID", "BRAIN_GID", "BRAIN_PUBLIC_URL",
                 "BRAIN_HTTP_PORT", "BRAIN_HTTPS_PORT", "BRAIN_IMAGE", "BRAIN_PYTHON_BASE",
                 "BRAIN_PREFIX", "BRAIN_MCP_ADAPTER", "BRAIN_ALLOW_STUB", "COMPOSE_FILE",
                 "COMPOSE_PROJECT_NAME", "COMPOSE_PATH_SEPARATOR", "BRAIN_ENRICHMENT",
                 "BRAIN_ENRICHMENT_WRITES", "BRAIN_ENRICHMENT_RETRIEVAL", "BRAIN_ENRICHMENT_BATCH_SIZE",
                 "BRAIN_ENRICHMENT_MAX_INPUT_CHARS", "BRAIN_ENRICHMENT_MAX_OUTPUT_BYTES")
SECRET_NAME_RE = re.compile(r"(SECRET|TOKEN|PASSWORD|PASSPHRASE|API[_-]?KEY|PRIVATE|CREDENTIAL|COOKIE)", re.I)

EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_STOP, EXIT_UNVERIFIED = 0, 1, 2, 3, 7


class ToolError(Exception):
    """A refusal or failure with an operator-readable reason (never a secret)."""

    def __init__(self, message: str, code: int = EXIT_FAIL):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------
# hashing / canonical json
def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def utc_stamp(now: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if now is None else now, timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# --------------------------------------------------------------------------
# secret redaction (applied to every line the tools print or log)
class Redactor:
    """Replaces registered secret values and secret-shaped text. Values are
    registered, never stored in any report."""

    PATTERNS = (
        (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=\-]{6,}"), "Bearer [REDACTED]"),
        (re.compile(r"(?i)\b(authorization|x-api-key|cookie)\s*[:=]\s*[^\s,;]+"), r"\1: [REDACTED]"),
        (re.compile(r"://[^/\s:@]+:[^/\s@]+@"), "://[REDACTED]@"),
        (re.compile(r"(?i)\b([A-Z0-9_\-]*(?:SECRET|TOKEN|PASSWORD|PASSPHRASE|API[_-]?KEY)[A-Z0-9_\-]*)\s*[=:]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"),
         r"\1=[REDACTED]"),
        (re.compile(r"\b(?:sk|pk|ghp|gho|ghs|xox[abprs])[-_][A-Za-z0-9_\-]{16,}"), "[REDACTED]"),
    )

    def __init__(self, values: Iterable[str] = ()):
        self._values: set[str] = set()
        for v in values:
            self.add(v)

    def add(self, value) -> None:
        if isinstance(value, str) and len(value.strip()) >= 6:
            self._values.add(value.strip())

    @property
    def count(self) -> int:
        return len(self._values)

    def text(self, s) -> str:
        s = s if isinstance(s, str) else str(s)
        for v in sorted(self._values, key=len, reverse=True):
            if v in s:
                s = s.replace(v, "[REDACTED]")
        for rx, rep in self.PATTERNS:
            s = rx.sub(rep, s)
        return s

    def obj(self, o):
        if isinstance(o, str):
            return self.text(o)
        if isinstance(o, dict):
            return {self.text(k) if isinstance(k, str) else k: self.obj(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [self.obj(v) for v in o]
        return o


_ACTIVE_REDACTOR = Redactor()


def set_active_redactor(r: Redactor) -> None:
    global _ACTIVE_REDACTOR
    _ACTIVE_REDACTOR = r


class RedactingFilter(logging.Filter):
    """Installed on this tool's loggers: every record is rewritten through the
    active redactor before any handler (console, file, test capture) sees it."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = _ACTIVE_REDACTOR.text(record.getMessage())
            record.args = ()
        except Exception:                                   # never let logging leak by failing open
            record.msg, record.args = "[unprintable log record suppressed]", ()
        return True


log.addFilter(RedactingFilter())


def is_secret_key(name: str) -> bool:
    return bool(SECRET_NAME_RE.search(name))


def parse_env_file(path: Path) -> dict[str, str]:
    """Same last-wins / quote-stripping semantics as deploy/lib.sh env_get."""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("export "):
            s = s[7:].lstrip()
        if "=" not in s:
            continue
        k, v = s.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
            out[k] = v
    return out


def secret_values_of(env: dict[str, str]) -> list[str]:
    return [v for k, v in env.items() if is_secret_key(k) and len(v) >= 6]


# --------------------------------------------------------------------------
# guarded command runner. EVERY external command goes through guard_command.
class UnsafeCommand(ToolError):
    def __init__(self, message: str):
        super().__init__(message, EXIT_STOP)


@dataclass
class RunResult:
    rc: int
    out: bytes = b""
    err: bytes = b""

    @property
    def text(self) -> str:
        return self.out.decode("utf-8", errors="replace")

    @property
    def errtext(self) -> str:
        return self.err.decode("utf-8", errors="replace")


GIT_READ_SUBCOMMANDS = {"rev-parse", "status", "remote", "merge-base", "cat-file", "show", "diff",
                        "ls-files", "ls-tree", "log", "rev-list", "merge-file", "for-each-ref",
                        "describe", "config", "version", "fsck"}
COMPOSE_SUBCOMMANDS = {"config", "ps", "up", "build", "version", "images", "logs"}
COMPOSE_GLOBAL_WITH_VALUE = {"-f", "--file", "-p", "--project-name", "--project-directory", "--env-file"}
UP_FLAGS = {"-d", "--detach", "--no-deps", "--force-recreate", "--wait", "--no-build"}
HEALTHCHECK_TAIL = ["python", "/usr/local/lib/brain/healthcheck.py"]


def _basename(argv0: str) -> str:
    return os.path.basename(argv0)


def _git_sub(argv: list[str]) -> tuple[str | None, list[str]]:
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "-C":
            i += 2
        elif a == "-c":
            if i + 1 >= len(argv) or argv[i + 1] not in ("safe.directory=*", "core.quotepath=off"):
                raise UnsafeCommand("git -c is limited to safe.directory=* and core.quotepath=off")
            i += 2
        elif a.startswith("-"):
            raise UnsafeCommand(f"unexpected git global option {a!r}")
        else:
            return a, argv[i + 1:]
    return None, []


def _compose_parts(argv: list[str]) -> tuple[str | None, list[str]]:
    """argv = [docker, compose, <globals...>, SUB, tail...]"""
    i = 2
    while i < len(argv):
        a = argv[i]
        if a in COMPOSE_GLOBAL_WITH_VALUE:
            i += 2
        elif a.startswith("-"):
            raise UnsafeCommand(f"unexpected docker compose global option {a!r}")
        else:
            return a, argv[i + 1:]
    return None, []


# Explicit refusals, checked BEFORE the allowlist so the message names the hazard.
def _forbidden(argv: list[str]) -> str | None:
    exe = _basename(argv[0])
    low = [a.lower() for a in argv]
    if exe == "git":
        sub, rest = _git_sub(argv)
        if sub in ("reset", "clean", "stash", "checkout", "switch", "restore", "revert", "rebase",
                   "merge", "pull", "push", "commit", "cherry-pick", "gc", "prune", "filter-branch"):
            return f"git {sub} is forbidden (no history rewriting, no tree-wide cleaning, no stash)"
        if sub == "merge-file" and "-p" not in rest and "--stdout" not in rest:
            return "git merge-file must print to stdout (-p); it would otherwise edit a file in place"
    if exe == "docker":
        if len(argv) > 1 and argv[1] == "compose":
            sub, tail = _compose_parts(argv)
            if sub in ("down", "rm", "kill", "stop", "restart", "pause", "unpause", "run", "exec", "create", "start", "pull"):
                return f"docker compose {sub} is forbidden (global/other-service effect); only `up -d --no-deps <service>` is allowed"
        if len(argv) > 1 and argv[1] in ("rm", "rmi", "kill", "stop", "restart", "prune", "system", "volume", "network",
                                         "run", "create", "start", "cp", "commit", "tag", "push", "pull", "build", "save", "load", "import"):
            return f"docker {argv[1]} is forbidden"
    if exe in ("chown", "chmod", "chgrp") and any(a in ("-R", "-r", "--recursive") or (a.startswith("-") and not a.startswith("--") and "R" in a[1:]) for a in argv[1:]):
        return f"recursive {exe} is forbidden"
    if exe in ("rm", "rmdir", "mv", "cp", "tar", "rsync", "sh", "bash", "dash", "zsh", "sudo", "eval", "xargs", "find"):
        return f"{exe} is not an allowed executable (use the tool's own file operations)"
    if any(a.endswith(("restore-backup.sh", "upgrade.sh", "create-backup.sh")) for a in argv):
        return "the legacy deployment scripts are never invoked by this tooling"
    if "--hard" in low:
        return "--hard is forbidden"
    return None


def guard_command(argv: list[str], allowed_exec: Iterable[str] = ()) -> None:
    """Raise UnsafeCommand unless argv is one of the few commands this tooling is
    allowed to run. Read-only discovery plus ONE write form: `docker compose ...
    up -d --no-deps [--force-recreate] <one service>` (and `build <one service>`)."""
    if not argv or not all(isinstance(a, str) for a in argv):
        raise UnsafeCommand("empty or non-string command")
    why = _forbidden(argv)
    if why:
        raise UnsafeCommand(why)
    exe = _basename(argv[0])
    if argv[0] in set(allowed_exec):
        return                                   # operator-registered writer pause/resume hook
    if exe == "git":
        sub, rest = _git_sub(argv)
        if sub not in GIT_READ_SUBCOMMANDS:
            raise UnsafeCommand(f"git {sub} is not allowed (read-only subcommands only)")
        if any(a.startswith("--output") for a in rest):
            raise UnsafeCommand("git --output is not allowed")
        if sub == "config" and not any(a.startswith(("--get", "--list", "-l")) for a in rest):
            raise UnsafeCommand("git config is read-only (--get/--list)")
        return
    if exe == "docker":
        if len(argv) < 2:
            raise UnsafeCommand("bare docker")
        sub = argv[1]
        if sub in ("info", "version", "ps", "inspect"):
            return
        if sub == "image" and len(argv) > 2 and argv[2] in ("inspect", "ls"):
            return
        if sub in ("pause", "unpause"):
            if len(argv) != 3 or argv[2].startswith("-"):
                raise UnsafeCommand(f"docker {sub} takes exactly one container id")
            return
        if sub == "exec":
            if len(argv) == 5 and not argv[2].startswith("-") and argv[3:] == HEALTHCHECK_TAIL:
                return
            raise UnsafeCommand("docker exec is limited to the container healthcheck probe")
        if sub == "compose":
            csub, tail = _compose_parts(argv)
            if csub not in COMPOSE_SUBCOMMANDS:
                raise UnsafeCommand(f"docker compose {csub} is not allowed")
            if csub == "up":
                flags = [t for t in tail if t.startswith("-")]
                pos = [t for t in tail if not t.startswith("-")]
                if not set(flags) <= UP_FLAGS:
                    raise UnsafeCommand(f"docker compose up flags not allowed: {sorted(set(flags) - UP_FLAGS)}")
                if "--no-deps" not in flags or not ({"-d", "--detach"} & set(flags)):
                    raise UnsafeCommand("docker compose up must be `up -d --no-deps <service>`")
                if len(pos) != 1:
                    raise UnsafeCommand("docker compose up must name exactly one service (never the whole stack)")
            if csub == "build":
                pos = [t for t in tail if not t.startswith("-")]
                if len(pos) != 1:
                    raise UnsafeCommand("docker compose build must name exactly one service")
            return
        raise UnsafeCommand(f"docker {sub} is not allowed")
    if exe == "ss":
        return
    if argv[0] == sys.executable or exe.startswith("python"):
        if argv[1:] in (["scripts/validate_repo.py", "--full"], ["scripts/validate_content_release.py"]):
            return
        raise UnsafeCommand("python may only run the two repository validators")
    raise UnsafeCommand(f"executable {exe!r} is not allowed")


class Runner:
    """Real runner. Subclass and override `_exec` for fakes: the guard still runs."""

    def __init__(self, allowed_exec: Iterable[str] = ()):
        self.log: list[list[str]] = []
        self.allowed_exec = set(allowed_exec)

    def allow_exec(self, path: str) -> None:
        self.allowed_exec.add(path)

    def run(self, argv, *, cwd=None, env: dict | None = None, timeout: int = 120) -> RunResult:
        argv = [str(a) for a in argv]
        guard_command(argv, self.allowed_exec)
        self.log.append(list(argv))
        return self._exec(argv, cwd, env, timeout)

    def _exec(self, argv, cwd, env, timeout) -> RunResult:
        full = dict(os.environ)
        full.update({"GIT_OPTIONAL_LOCKS": "0", "PYTHONDONTWRITEBYTECODE": "1", "LC_ALL": "C.UTF-8"})
        if env:
            full.update(env)
        try:
            p = subprocess.run(argv, cwd=cwd, env=full, capture_output=True, timeout=timeout, shell=False)
            return RunResult(p.returncode, p.stdout, p.stderr)
        except FileNotFoundError:
            return RunResult(127, b"", f"{argv[0]}: not found".encode())
        except subprocess.TimeoutExpired:
            return RunResult(124, b"", b"timeout")


def git(runner, path: Path | str, *args: str, timeout: int = 120) -> RunResult:
    return runner.run(["git", "-c", "safe.directory=*", "-c", "core.quotepath=off", "-C", str(path), *args], timeout=timeout)


def parse_status_z(text: str) -> list[tuple[str, str]]:
    """`git status --porcelain=v1 -z` -> [(XY, path)] (names only; rename sources skipped)."""
    entries, out, i = text.split("\0"), [], 0
    while i < len(entries):
        e = entries[i]
        i += 1
        if len(e) < 4:
            continue
        if e[0] in "RC":
            i += 1
        out.append((e[:2], e[3:]))
    return out


# --------------------------------------------------------------------------
# confirmation tokens: tie --execute to the exact reviewed plan
def confirm_token(subcommand: str, plan_digest: str) -> str:
    return "CONFIRM-" + sha256_bytes(f"{subcommand}\0{plan_digest}".encode()).upper()[:16]


def require_confirmation(subcommand: str, plan_digest: str, execute: bool, confirm: str | None) -> bool:
    """False = dry run. True = execute. Raises when --execute lacks a valid token."""
    if not execute:
        if confirm:
            raise ToolError("--confirm without --execute does nothing; add --execute or drop --confirm", EXIT_USAGE)
        return False
    expected = confirm_token(subcommand, plan_digest)
    if not confirm:
        raise ToolError(f"--execute needs --confirm {expected} (printed by the dry run)", EXIT_USAGE)
    if confirm != expected:
        raise ToolError("confirmation token does not match the current plan; re-run the dry run and review it", EXIT_USAGE)
    return True


def write_private(path: Path, data: bytes | str) -> None:
    """Atomic write, mode 0600, directory 0700 when created."""
    path = Path(path)
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------
# SQLite snapshots (online backup API; a raw copy of a WAL database is not a backup)
def is_sqlite_name(name: str) -> bool:
    return name.endswith(SQLITE_SUFFIXES)


def is_sqlite_side(name: str) -> bool:
    return any(name.endswith(s + x) for s in SQLITE_SUFFIXES for x in SQLITE_SIDE)


def snapshot_sqlite(src: Path, dst: Path) -> dict:
    """Consistent snapshot of `src` into `dst` (single file, journal_mode=DELETE),
    then PRAGMA integrity_check. The source is opened read-only."""
    src, dst = Path(src), Path(dst)
    if dst.exists():
        raise ToolError(f"snapshot target exists: {dst.name}")
    uri = "file:" + quote(src.resolve().as_posix()) + "?mode=ro"
    try:
        s = sqlite3.connect(uri, uri=True, timeout=30)
    except sqlite3.Error as exc:
        raise ToolError(f"cannot open {src.name} read-only for snapshot: {exc}") from exc
    try:
        d = sqlite3.connect(str(dst))
        try:
            s.backup(d)
            d.execute("PRAGMA journal_mode=DELETE")
            d.commit()
        finally:
            d.close()
    except sqlite3.Error as exc:
        dst.unlink(missing_ok=True)
        raise ToolError(f"sqlite backup of {src.name} failed: {exc}") from exc
    finally:
        s.close()
    info = check_sqlite(dst)
    if info["integrity"] != "ok":
        raise ToolError(f"snapshot of {src.name} failed integrity_check")
    os.chmod(dst, 0o600)
    return info


def check_sqlite(path: Path) -> dict:
    con = sqlite3.connect("file:" + quote(Path(path).resolve().as_posix()) + "?mode=ro", uri=True)
    try:
        integ = con.execute("PRAGMA integrity_check").fetchall()
        uv = con.execute("PRAGMA user_version").fetchone()[0]
        jm = con.execute("PRAGMA journal_mode").fetchone()[0]
        tables = con.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    finally:
        con.close()
    return {"integrity": "ok" if integ == [("ok",)] else "FAILED", "user_version": uv,
            "journal_mode": jm, "tables": tables, "size": Path(path).stat().st_size}


# --------------------------------------------------------------------------
# tree walking
def _excluded_dir(rel: str, name: str) -> bool:
    return name in EXCLUDED_DIRNAMES or rel in EXCLUDED_TOP


def walk_tree(root: Path) -> Iterable[tuple[str, Path, str]]:
    """Yield (rel, abs, kind) for the data dir in sorted order; kind in file|dir|symlink.
    Excludes `_search/`, caches, and raw SQLite files under `_enrichment/` (snapshot instead)."""
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        reld = Path(dirpath).relative_to(root).as_posix()
        reld = "" if reld == "." else reld
        keep = []
        for d in sorted(dirnames):
            rel = f"{reld}/{d}" if reld else d
            full = Path(dirpath) / d
            if _excluded_dir(rel, d):
                continue
            if full.is_symlink():
                yield rel, full, "symlink"
                continue
            keep.append(d)
            yield rel, full, "dir"
        dirnames[:] = keep
        for f in sorted(filenames):
            rel = f"{reld}/{f}" if reld else f
            full = Path(dirpath) / f
            if reld == ENRICHMENT_DIR and (is_sqlite_name(f) or is_sqlite_side(f)):
                continue
            if full.is_symlink():
                yield rel, full, "symlink"
            elif full.is_file():
                yield rel, full, "file"


def enrichment_sqlite_files(root: Path) -> list[Path]:
    d = Path(root) / ENRICHMENT_DIR
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir() if p.is_file() and not p.is_symlink() and is_sqlite_name(p.name))


def code_manifest(root: Path) -> dict[str, str]:
    """sha256 of every file outside the corpus zones / .git / runtime dirs: what code the
    stack was running at backup time (the image does not contain the app code)."""
    out: dict[str, str] = {}
    skip_top = set(CORPUS_ZONES) | {".git", ENRICHMENT_DIR, *EXCLUDED_TOP}
    for rel, full, kind in walk_tree(root):
        top = rel.split("/", 1)[0]
        if top in skip_top or kind != "file":
            continue
        if top == ".env" or rel.startswith(".env.") and rel != ".env.example":
            continue
        out[rel] = sha256_file(full)
    return out


# --------------------------------------------------------------------------
# writer pause coordination
class WriterPause:
    """Pause/resume whatever writes into the corpus while the archive is built."""

    mode = "none"

    def pause(self) -> dict:
        return {"mode": self.mode, "paused": False}

    def resume(self) -> None:
        return None


class DockerPause(WriterPause):
    mode = "docker-pause"

    def __init__(self, runner, container: str):
        self.runner, self.container, self._paused = runner, container, False

    def pause(self) -> dict:
        r = self.runner.run(["docker", "pause", self.container])
        if r.rc != 0:
            raise ToolError(f"could not pause the brain container: {r.errtext.strip()[:200]}")
        self._paused = True
        return {"mode": self.mode, "paused": True}

    def resume(self) -> None:
        if self._paused:
            self.runner.run(["docker", "unpause", self.container])
            self._paused = False


class HookPause(WriterPause):
    mode = "operator-hook"

    def __init__(self, runner, pause_hook: str, resume_hook: str):
        for h in (pause_hook, resume_hook):
            if not os.path.isabs(h) or not os.access(h, os.X_OK):
                raise ToolError(f"hook {h!r} must be an absolute path to an executable file", EXIT_USAGE)
            runner.allow_exec(h)
        self.runner, self.p, self.r, self._paused = runner, pause_hook, resume_hook, False

    def pause(self) -> dict:
        r = self.runner.run([self.p])
        if r.rc != 0:
            raise ToolError("pause hook failed")
        self._paused = True
        return {"mode": self.mode, "paused": True}

    def resume(self) -> None:
        if self._paused:
            self.runner.run([self.r])
            self._paused = False


# --------------------------------------------------------------------------
@dataclass
class BackupConfig:
    wiki: Path
    dest: Path
    state_dir: Path | None = None
    compose_files: list[str] = field(default_factory=list)
    env_file: str | None = None
    extra_paths: list[str] = field(default_factory=list)
    image: dict = field(default_factory=dict)            # {id, tag, digest}
    container: str | None = None
    compose: dict = field(default_factory=dict)           # {project, project_dir, service}
    writer_pause: str = "auto"                            # auto|docker|none|hook
    pause_hook: str | None = None
    resume_hook: str | None = None


def inside(child: Path, parent: Path) -> bool:
    try:
        Path(child).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


def _outside_data(paths: list[str], wiki: Path) -> list[str]:
    return [p for p in paths if p and Path(p).exists() and not inside(Path(p), wiki)]


def plan_backup(cfg: BackupConfig) -> dict:
    wiki = cfg.wiki.resolve()
    if not (wiki / ".git").exists():
        raise ToolError(f"{wiki} is not a git checkout", EXIT_USAGE)
    if inside(cfg.dest, wiki) or inside(wiki, cfg.dest):
        raise ToolError("the backup destination must be outside the wiki tree (and not contain it)", EXIT_USAGE)
    if cfg.writer_pause == "hook" or (cfg.writer_pause == "auto" and (cfg.pause_hook or cfg.resume_hook)):
        for h in (cfg.pause_hook, cfg.resume_hook):
            if not h or not os.path.isabs(h) or not os.access(h, os.X_OK):
                raise ToolError("--pause-hook and --resume-hook must both be absolute paths to executable files", EXIT_USAGE)
    files = bytes_ = 0
    counts = {"corpus": 0, "git": 0, "other": 0}
    for rel, full, kind in walk_tree(wiki):
        if kind != "file":
            continue
        files += 1
        try:
            bytes_ += full.stat().st_size
        except OSError:
            pass
        top = rel.split("/", 1)[0]
        counts["git" if top == ".git" else "corpus" if top in CORPUS_ZONES else "other"] += 1
    snaps = [p.name for p in enrichment_sqlite_files(wiki)]
    state_parts = []
    if cfg.state_dir:
        for sub in STATE_INCLUDE:
            if (cfg.state_dir / sub).is_dir():
                state_parts.append(sub)
    outside = _outside_data(cfg.compose_files + ([cfg.env_file] if cfg.env_file else []), wiki)
    extras = [p for p in cfg.extra_paths if Path(p).exists()]
    missing_extras = [p for p in cfg.extra_paths if not Path(p).exists()]
    plan = {
        "wiki": str(wiki), "dest": str(cfg.dest.resolve()), "state_dir": str(cfg.state_dir) if cfg.state_dir else None,
        "files_in_repo_part": files, "bytes_in_repo_part": bytes_, "repo_part_by_area": counts,
        "excluded": {"top_level": list(EXCLUDED_TOP), "dirnames": list(EXCLUDED_DIRNAMES),
                     "raw_sqlite_under__enrichment": "replaced by SQLite-backup-API snapshots"},
        "enrichment_included": (wiki / ENRICHMENT_DIR).is_dir(),
        "sqlite_snapshots": snaps, "state_parts": state_parts,
        "outside_data_config_files": outside, "extra_paths": extras, "missing_extra_paths": missing_extras,
        "writer_pause": cfg.writer_pause, "container": bool(cfg.container),
        "image": {k: cfg.image.get(k) for k in ("id", "tag", "digest")},
    }
    plan["digest"] = sha256_bytes(canonical({k: plan[k] for k in (
        "wiki", "dest", "state_dir", "sqlite_snapshots", "state_parts", "outside_data_config_files", "extra_paths",
        "writer_pause")}))
    return plan


class _HashReader:
    def __init__(self, fh):
        self.fh, self.h, self.n = fh, hashlib.sha256(), 0

    def read(self, n: int = -1) -> bytes:
        b = self.fh.read(n)
        self.h.update(b)
        self.n += len(b)
        return b


class UnstableFile(ToolError):
    pass


class _Writer:
    """Streams members into the tar while recording sha256/size of exactly what was written."""

    def __init__(self, tar: tarfile.TarFile):
        self.tar, self.members, self.skipped = tar, {}, []

    def add_dir(self, arc: str, src: Path):
        ti = self.tar.gettarinfo(str(src), arcname=arc)
        self.tar.addfile(ti)
        self.members[arc] = {"kind": "dir", "mode": ti.mode}

    def add_symlink(self, arc: str, src: Path):
        ti = self.tar.gettarinfo(str(src), arcname=arc)
        self.tar.addfile(ti)
        self.members[arc] = {"kind": "symlink", "target": ti.linkname}

    def add_file(self, arc: str, src: Path, kind: str = "file", extra: dict | None = None):
        try:
            fh = open(src, "rb")
        except FileNotFoundError:
            self.skipped.append({"path": arc, "reason": "vanished before it could be read"})
            return
        with fh:
            before = os.fstat(fh.fileno())
            ti = self.tar.gettarinfo(arcname=arc, fileobj=fh)
            ti.name = arc
            hr = _HashReader(fh)
            try:
                self.tar.addfile(ti, hr)
            except OSError as exc:
                raise UnstableFile(f"{arc} changed while it was archived ({exc}); pause the writer and retry") from exc
            after = os.fstat(fh.fileno())
            if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
                raise UnstableFile(f"{arc} changed while it was archived; pause the writer and retry")
        self.members[arc] = {"kind": kind, "sha256": hr.h.hexdigest(), "size": hr.n, "mode": ti.mode, **(extra or {})}

    def add_bytes(self, arc: str, data: bytes, kind: str = "meta"):
        ti = tarfile.TarInfo(arc)
        ti.size, ti.mode, ti.mtime = len(data), 0o600, int(time.time())
        self.tar.addfile(ti, io.BytesIO(data))
        self.members[arc] = {"kind": kind, "sha256": sha256_bytes(data), "size": len(data), "mode": 0o600}

    def add_tree(self, prefix: str, root: Path, pairs: Iterable[tuple[str, Path, str]]):
        for rel, full, kind in pairs:
            arc = f"{prefix}/{rel}" if rel else prefix
            if kind == "dir":
                self.add_dir(arc, full)
            elif kind == "symlink":
                self.add_symlink(arc, full)
            else:
                self.add_file(arc, full)


def _walk_sub(root: Path) -> Iterable[tuple[str, Path, str]]:
    """Like walk_tree but for state/extra directories (no exclusions besides caches)."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        reld = Path(dirpath).relative_to(root).as_posix()
        reld = "" if reld == "." else reld
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_DIRNAMES)
        for d in dirnames:
            full = Path(dirpath) / d
            yield (f"{reld}/{d}" if reld else d), full, ("symlink" if full.is_symlink() else "dir")
        dirnames[:] = [d for d in dirnames if not (Path(dirpath) / d).is_symlink()]
        for f in sorted(filenames):
            full = Path(dirpath) / f
            yield (f"{reld}/{f}" if reld else f), full, ("symlink" if full.is_symlink() else "file")


def create_backup(cfg: BackupConfig, runner, *, clock: Callable[[], float] = time.time, pause: WriterPause | None = None) -> dict:
    wiki = cfg.wiki.resolve()
    plan = plan_backup(cfg)
    dest = cfg.dest.resolve()
    dest.mkdir(parents=True, exist_ok=True, mode=0o700)
    # --- git facts (read-only)
    head = git(runner, wiki, "rev-parse", "HEAD").text.strip() or "none"
    branch = git(runner, wiki, "rev-parse", "--abbrev-ref", "HEAD").text.strip() or "none"
    st = git(runner, wiki, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    dirty = sorted({p for _, p in parse_status_z(st.text)}) if st.rc == 0 else []
    if pause is None:
        pause = make_pause(cfg, runner)
    stamp = utc_stamp(clock())
    name = f"0xbrain-enrichment-backup-{stamp}-{head[:12]}.tar.gz"
    final = dest / name
    if final.exists():
        raise ToolError(f"{name} already exists; refusing to overwrite a backup")
    scratch = Path(tempfile.mkdtemp(dir=str(dest), prefix=".tmp-backup-"))
    os.chmod(scratch, 0o700)
    tmp_archive = scratch / "archive.tar.gz"
    pause_info = {"mode": pause.mode, "paused": False}
    manifest: dict = {}
    writer = None
    try:
        pause_info = pause.pause()
        locks = [p for p in (".git/index.lock", ".git/brain-write.lock") if (wiki / p).exists()]
        snaps: list[dict] = []
        snapdir = scratch / "snap"
        snapdir.mkdir(mode=0o700)
        for src in enrichment_sqlite_files(wiki):
            dst = snapdir / src.name
            info = snapshot_sqlite(src, dst)
            snaps.append({"name": src.name, "arc": f"repo/{ENRICHMENT_DIR}/{src.name}", **info})
        fd = os.open(tmp_archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
            writer = _Writer(tar)
            writer.add_dir("repo", wiki)
            writer.add_tree("repo", wiki, walk_tree(wiki))
            for s in snaps:
                writer.add_file(s["arc"], snapdir / s["name"], kind="sqlite-snapshot",
                                extra={"integrity": s["integrity"], "user_version": s["user_version"]})
            if cfg.state_dir:
                for sub in STATE_INCLUDE:
                    p = cfg.state_dir / sub
                    if p.is_dir():
                        writer.add_dir(f"state/{sub}", p)
                        writer.add_tree(f"state/{sub}", p, _walk_sub(p))
            outside_cfg: list[dict] = []
            for i, f in enumerate(plan["outside_data_config_files"]):
                arc = f"compose/{i}-{Path(f).name}"
                writer.add_file(arc, Path(f), kind="config")
                outside_cfg.append({"arc": arc, "original_path": f})
            extras_rec: list[dict] = []
            for i, p in enumerate(plan["extra_paths"]):
                pp = Path(p)
                if pp.is_dir():
                    writer.add_dir(f"extra/{i}", pp)
                    writer.add_tree(f"extra/{i}", pp, _walk_sub(pp))
                else:
                    writer.add_file(f"extra/{i}/{pp.name}", pp, kind="extra")
                extras_rec.append({"arc": f"extra/{i}", "original_path": p})
            manifest = {
                "schema": SCHEMA, "tool": TOOL, "created_utc": stamp, "archive": name,
                "repo_dir": str(wiki), "state_dir": str(cfg.state_dir) if cfg.state_dir else None,
                "git": {"head": head, "branch": branch, "dirty_paths": dirty},
                "image": {k: cfg.image.get(k) for k in ("id", "tag", "digest")},
                "compose": {**{k: cfg.compose.get(k) for k in ("project", "project_dir", "service")},
                            "files": cfg.compose_files, "env_file": cfg.env_file},
                "outside_data_config": outside_cfg, "extra_paths": extras_rec,
                "code_manifest": code_manifest(wiki),
                "sqlite_snapshots": snaps, "enrichment_included": (wiki / ENRICHMENT_DIR).is_dir(),
                "excluded": plan["excluded"], "writer_pause": pause_info, "locks_seen_while_paused": locks,
                "skipped": writer.skipped, "files": dict(writer.members),
            }
            writer.add_bytes("BACKUP_MANIFEST.json", json.dumps(manifest, sort_keys=True, ensure_ascii=False, indent=1).encode(), "manifest")
    except BaseException:
        shutil.rmtree(scratch, ignore_errors=True)
        raise
    finally:
        pause.resume()
    os.replace(tmp_archive, final)
    shutil.rmtree(scratch, ignore_errors=True)
    os.chmod(final, 0o600)
    digest = sha256_file(final)
    write_private(Path(str(final) + ".sha256"), f"{digest}  {name}\n")
    write_private(Path(str(final) + ".manifest.json"), json.dumps(manifest, sort_keys=True, ensure_ascii=False, indent=1))
    result = {"archive": str(final), "sha256": digest, "size": final.stat().st_size, "mode": oct(final.stat().st_mode & 0o777),
              "head": head, "branch": branch, "dirty_paths": len(dirty), "members": len(manifest["files"]),
              "sqlite_snapshots": [{k: s[k] for k in ("name", "integrity", "user_version", "tables")} for s in snaps],
              "image": manifest["image"], "writer_pause": pause_info, "locks_seen_while_paused": locks,
              "skipped": writer.skipped if writer else []}
    # prove it: restore into a scratch dir and compare with the recorded hashes
    v = verify_archive(final)
    result["verify"] = {k: v[k] for k in ("ok", "checked", "problems")}
    if not v["ok"]:
        raise ToolError(f"archive written to {final} but verification FAILED: {v['problems'][:3]}", EXIT_UNVERIFIED)
    return result


def make_pause(cfg: BackupConfig, runner) -> WriterPause:
    mode = cfg.writer_pause
    if mode == "hook" or (mode == "auto" and cfg.pause_hook and cfg.resume_hook):
        if not (cfg.pause_hook and cfg.resume_hook):
            raise ToolError("--writer-pause hook needs --pause-hook and --resume-hook", EXIT_USAGE)
        return HookPause(runner, cfg.pause_hook, cfg.resume_hook)
    if mode == "docker" or (mode == "auto" and cfg.container):
        if not cfg.container:
            raise ToolError("--writer-pause docker needs a container (discovery or --container)", EXIT_USAGE)
        return DockerPause(runner, cfg.container)
    if mode in ("none", "auto"):
        return WriterPause()
    raise ToolError(f"unknown writer-pause mode {mode!r}", EXIT_USAGE)


# --------------------------------------------------------------------------
# reading / verifying / restoring
def read_manifest(archive: Path) -> dict:
    with tarfile.open(archive, "r:gz") as tar:
        try:
            f = tar.extractfile("BACKUP_MANIFEST.json")
        except KeyError as exc:
            raise ToolError("not an enrichment backup (BACKUP_MANIFEST.json missing)", EXIT_UNVERIFIED) from exc
        data = json.loads(f.read().decode("utf-8"))
    if data.get("schema") != SCHEMA:
        raise ToolError(f"unsupported backup schema {data.get('schema')!r}", EXIT_UNVERIFIED)
    return data


def _safe_member_path(dest: Path, name: str) -> Path:
    if name.startswith("/") or ".." in name.split("/") or "\\" in name:
        raise ToolError(f"unsafe archive member name {name!r}", EXIT_UNVERIFIED)
    target = (dest / name).resolve()
    if not inside(target, dest):
        raise ToolError(f"archive member escapes the target: {name!r}", EXIT_UNVERIFIED)
    return target


def extract_archive(archive: Path, dest: Path, *, preserve_owner: bool = False, only: Callable[[str], bool] | None = None) -> list[str]:
    """Safe extraction: no absolute/.. names, no hard links or devices, symlinks only when relative
    and contained, setuid/setgid stripped. Returns extracted member names."""
    dest = Path(dest).resolve()
    names: list[str] = []
    deferred_dirs: list[tuple[Path, int, float]] = []
    with tarfile.open(archive, "r:gz") as tar:
        for m in tar:
            if only and not only(m.name):
                continue
            target = _safe_member_path(dest, m.name)
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
                deferred_dirs.append((target, m.mode & 0o777, m.mtime))
            elif m.isreg():
                target.parent.mkdir(parents=True, exist_ok=True)
                src = tar.extractfile(m)
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
                with os.fdopen(fd, "wb") as out:
                    shutil.copyfileobj(src, out)
                os.chmod(target, (m.mode & 0o777) or 0o600)
                os.utime(target, (m.mtime, m.mtime))
            elif m.issym():
                link = m.linkname
                if os.path.isabs(link) or not inside((target.parent / link), dest):
                    raise ToolError(f"unsafe symlink {m.name!r} -> {link!r}", EXIT_UNVERIFIED)
                target.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(link, target)
            else:
                raise ToolError(f"unsupported member type for {m.name!r}", EXIT_UNVERIFIED)
            if preserve_owner and os.geteuid() == 0 and not m.issym():
                os.chown(target, m.uid, m.gid)
            names.append(m.name)
    for d, mode, mtime in reversed(deferred_dirs):
        os.chmod(d, mode or 0o700)
    return names


def verify_archive(archive: Path, against: Path | None = None) -> dict:
    """Restore into a private scratch dir and compare every regular member with the
    manifest (and, when given, `against` = the original wiki tree, for files that exist there)."""
    archive = Path(archive)
    problems: list[str] = []
    side = Path(str(archive) + ".sha256")
    if side.is_file():
        want = side.read_text().split()[0]
        if sha256_file(archive) != want:
            problems.append("archive sha256 does not match its .sha256 sidecar")
    manifest = read_manifest(archive)
    scratch = Path(tempfile.mkdtemp(prefix="eb-verify-"))
    os.chmod(scratch, 0o700)
    checked = 0
    try:
        names = set(extract_archive(archive, scratch))
        for arc, rec in manifest["files"].items():
            if arc not in names:
                problems.append(f"missing member {arc}")
                continue
            if rec["kind"] in ("dir", "symlink"):
                continue
            p = scratch / arc
            if sha256_file(p) != rec["sha256"]:
                problems.append(f"hash mismatch {arc}")
            checked += 1
        if "repo/.git/HEAD" not in names:
            problems.append("repo/.git/HEAD missing")
        if any(n == "repo/_search" or n.startswith("repo/_search/") for n in names):
            problems.append("_search is present in the archive (must be excluded)")
        for n in names:
            if n.startswith(f"repo/{ENRICHMENT_DIR}/") and (is_sqlite_side(n) ):
                problems.append(f"raw SQLite side file archived: {n}")
        for s in manifest.get("sqlite_snapshots", []):
            info = check_sqlite(scratch / s["arc"])
            if info["integrity"] != "ok" or info["user_version"] != s["user_version"]:
                problems.append(f"snapshot {s['name']} fails integrity/user_version")
        if against is not None:
            against = Path(against)
            for arc, rec in manifest["files"].items():
                if not arc.startswith("repo/") or rec["kind"] != "file":
                    continue
                orig = against / arc[len("repo/"):]
                if orig.is_file() and sha256_file(orig) != rec["sha256"]:
                    problems.append(f"differs from source {arc}")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return {"ok": not problems, "checked": checked, "problems": problems, "members": len(manifest["files"]),
            "head": manifest["git"]["head"], "image": manifest.get("image")}


def check_restore_targets(manifest: dict, target: Path, state_target: Path | None, live_wiki: Path | None) -> None:
    """Refuse (STOP) unless every target is new/empty and separate from the live wiki."""
    for d in (target, state_target):
        if d is None:
            continue
        d = Path(d)
        if d.exists() and (not d.is_dir() or any(d.iterdir())):
            raise ToolError(f"{d} exists and is not empty; restore only writes into a new or empty directory", EXIT_STOP)
        for live in filter(None, [live_wiki, Path(manifest["repo_dir"]) if manifest.get("repo_dir") else None]):
            if inside(d, live) or inside(live, d):
                raise ToolError("the restore target must be separate from the live wiki", EXIT_STOP)


def restore_archive(archive: Path, target: Path, state_target: Path | None, *, live_wiki: Path | None, preserve_owner: bool) -> dict:
    """Write the archive's repo/ (and state/) into NEW or EMPTY directories. Never into the live
    wiki, never over existing content, never touches containers."""
    manifest = read_manifest(archive)
    check_restore_targets(manifest, target, state_target, live_wiki)
    v = verify_archive(archive)
    if not v["ok"]:
        raise ToolError(f"archive verification failed: {v['problems'][:3]}", EXIT_UNVERIFIED)
    out = {}
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(dir=str(target.parent), prefix=".eb-restore-"))
    os.chmod(stage, 0o700)
    try:
        extract_archive(archive, stage, preserve_owner=preserve_owner)
        if target.exists():
            target.rmdir()
        shutil.move(str(stage / "repo"), str(target))
        out["repo"] = str(target)
        if state_target is not None and (stage / "state").is_dir():
            state_target = Path(state_target)
            state_target.parent.mkdir(parents=True, exist_ok=True)
            if state_target.exists():
                state_target.rmdir()
            shutil.move(str(stage / "state"), str(state_target))
            out["state"] = str(state_target)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    out["head"] = manifest["git"]["head"]
    return out


# --------------------------------------------------------------------------
# CLI
def _cfg_from_args(a, runner) -> BackupConfig:
    disc = {}
    if a.discovery:
        disc = json.loads(Path(a.discovery).read_text(encoding="utf-8"))
    comp = disc.get("compose") or {}
    cont = disc.get("container") or {}
    state = a.state_dir or (disc.get("paths") or {}).get("state")
    return BackupConfig(
        wiki=Path(a.wiki), dest=Path(a.dest), state_dir=Path(state) if state else None,
        compose_files=list(a.compose_file or [f["path"] for f in comp.get("files", [])]),
        env_file=a.env_file or comp.get("env_file"), extra_paths=list(a.extra_path or []),
        image={"id": a.image_id or cont.get("image_id"), "tag": a.image_tag or cont.get("image_tag"),
               "digest": a.image_digest or cont.get("image_digest")},
        container=a.container or cont.get("id"), writer_pause=a.writer_pause,
        compose={k: comp.get(k) for k in ("project", "project_dir", "service")},
        pause_hook=a.pause_hook, resume_hook=a.resume_hook)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=TOOL, description="Enrichment-aware backup / verify / restore (dry run unless --execute --confirm).")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create", help="archive repo + state + enrichment store snapshot (dry run by default)")
    add_create_args(c)
    v = sub.add_parser("verify", help="restore an archive into a scratch dir and compare hashes (read-only)")
    v.add_argument("--archive", required=True)
    v.add_argument("--against", help="original wiki dir to compare member hashes with")
    ls = sub.add_parser("list", help="summarise an archive (read-only)")
    ls.add_argument("--archive", required=True)
    s = sub.add_parser("snapshot-store", help="SQLite-backup-API snapshot of one database file")
    s.add_argument("--wiki", required=True)
    s.add_argument("--db", default=f"{ENRICHMENT_DIR}/store-v1.sqlite3", help="path relative to the wiki")
    s.add_argument("--out", required=True)
    s.add_argument("--execute", action="store_true")
    s.add_argument("--confirm")
    r = sub.add_parser("restore", help="restore into a NEW/EMPTY directory (never the live wiki)")
    r.add_argument("--archive", required=True)
    r.add_argument("--target", required=True)
    r.add_argument("--state-target")
    r.add_argument("--live-wiki", help="the live wiki path (refused as a target)")
    r.add_argument("--preserve-owner", action="store_true")
    r.add_argument("--execute", action="store_true")
    r.add_argument("--confirm")
    return p


def add_create_args(c: argparse.ArgumentParser) -> None:
    c.add_argument("--wiki", required=True)
    c.add_argument("--dest", required=True, help="output directory (outside the wiki)")
    c.add_argument("--state-dir")
    c.add_argument("--discovery", help="private preflight JSON to take compose/container/image facts from")
    c.add_argument("--compose-file", action="append")
    c.add_argument("--env-file")
    c.add_argument("--extra-path", action="append", help="extra file/dir to archive (reverse proxy config, TLS state)")
    c.add_argument("--container")
    c.add_argument("--image-id")
    c.add_argument("--image-tag")
    c.add_argument("--image-digest")
    c.add_argument("--writer-pause", default="auto", choices=("auto", "docker", "hook", "none"))
    c.add_argument("--pause-hook")
    c.add_argument("--resume-hook")
    c.add_argument("--execute", action="store_true")
    c.add_argument("--confirm")


def run_create(a, runner, emit) -> int:
    cfg = _cfg_from_args(a, runner)
    plan = plan_backup(cfg)
    do = require_confirmation("backup-create", plan["digest"], a.execute, a.confirm)
    emit(f"backup plan: {plan['files_in_repo_part']} files / {plan['bytes_in_repo_part']} bytes in repo part; "
         f"_enrichment included={plan['enrichment_included']}; sqlite snapshots={plan['sqlite_snapshots']}; "
         f"state parts={plan['state_parts']}; outside-data config={len(plan['outside_data_config_files'])}; "
         f"extras={len(plan['extra_paths'])}; excluded={plan['excluded']['top_level']}")
    if plan["missing_extra_paths"]:
        emit(f"WARN: extra paths not found (not archived): {plan['missing_extra_paths']}")
    if not do:
        emit("DRY RUN: nothing was written. To execute: " + f"--execute --confirm {confirm_token('backup-create', plan['digest'])}")
        return EXIT_OK
    res = create_backup(cfg, runner)
    emit("BACKUP OK: " + json.dumps(res, sort_keys=True))
    return EXIT_OK


def main(argv=None, runner=None, out=None) -> int:
    a = build_parser().parse_args(argv)
    runner = runner or Runner()
    out = out or sys.stdout

    def emit(s: str) -> None:
        print(_ACTIVE_REDACTOR.text(s), file=out)

    try:
        if a.cmd == "create":
            return run_create(a, runner, emit)
        if a.cmd == "verify":
            res = verify_archive(Path(a.archive), Path(a.against) if a.against else None)
            emit(json.dumps(res, sort_keys=True))
            return EXIT_OK if res["ok"] else EXIT_UNVERIFIED
        if a.cmd == "list":
            m = read_manifest(Path(a.archive))
            top: dict[str, int] = {}
            for arc in m["files"]:
                top[arc.split("/", 1)[0]] = top.get(arc.split("/", 1)[0], 0) + 1
            emit(json.dumps({"created": m["created_utc"], "head": m["git"]["head"], "image": m["image"], "sections": top,
                             "sqlite_snapshots": [s["name"] for s in m["sqlite_snapshots"]],
                             "dirty_paths": len(m["git"]["dirty_paths"])}, sort_keys=True))
            return EXIT_OK
        if a.cmd == "snapshot-store":
            src = Path(a.wiki) / a.db
            dig = sha256_bytes(canonical({"src": str(src.resolve()), "out": str(Path(a.out).resolve())}))
            if not require_confirmation("snapshot-store", dig, a.execute, a.confirm):
                emit(f"DRY RUN: would snapshot {src.name} -> {a.out}. To execute: --execute --confirm {confirm_token('snapshot-store', dig)}")
                return EXIT_OK
            if inside(Path(a.out), Path(a.wiki)):
                raise ToolError("--out must be outside the wiki", EXIT_USAGE)
            Path(a.out).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            emit("SNAPSHOT OK: " + json.dumps(snapshot_sqlite(src, Path(a.out)), sort_keys=True))
            return EXIT_OK
        if a.cmd == "restore":
            arch = Path(a.archive)
            dig = sha256_bytes(canonical({"archive": sha256_file(arch), "target": str(Path(a.target).resolve()),
                                          "state": a.state_target}))
            if not require_confirmation("restore", dig, a.execute, a.confirm):
                emit(f"DRY RUN: would restore {arch.name} into {a.target}. To execute: --execute --confirm {confirm_token('restore', dig)}")
                return EXIT_OK
            res = restore_archive(arch, Path(a.target), Path(a.state_target) if a.state_target else None,
                                  live_wiki=Path(a.live_wiki) if a.live_wiki else None, preserve_owner=a.preserve_owner)
            emit("RESTORE OK (no stack was touched): " + json.dumps(res, sort_keys=True))
            return EXIT_OK
    except ToolError as exc:
        print(_ACTIVE_REDACTOR.text(f"ERROR: {exc}"), file=sys.stderr)
        return exc.code
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
