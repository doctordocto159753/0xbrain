"""Private, short-lived staging of owner-uploaded files for brain_ingest_file.

Why this exists: an MCP tool call carries only JSON `arguments`; neither the
MCP specification nor Claude's connector client passes a user's attachment
bytes to a server tool. The owner therefore uploads the exact file over the
same owner authentication boundary (POST /upload on the brain server) and
hands Claude the returned `upload_ref`. Claude never re-types file bytes.

Properties:
  * refs are `upl-` + 256-bit random token; on disk only sha256(ref) names
    the entry, so a directory listing does not reveal usable refs;
  * files are 0600 in a 0700 directory outside the repository;
  * size bound (BRAIN_UPLOAD_MAX_BYTES, default 50 MiB), count bound
    (BRAIN_UPLOAD_MAX_PENDING, default 20), expiry (BRAIN_UPLOAD_TTL,
    default 3600 s); expired entries are deleted on every access;
  * single use: a ref is consumed when the ingest has persisted the original;
  * the stored filename is metadata only; it never selects a server path.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
import unicodedata
from pathlib import Path
from typing import BinaryIO

REF_RE = re.compile(r"upl-[A-Za-z0-9_-]{43}")
DEFAULT_MAX_BYTES = 50 * 1024 * 1024
DEFAULT_TTL = 3600
DEFAULT_MAX_PENDING = 20
MAX_FILENAME_CHARS = 200


class UploadError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def upload_dir() -> Path | None:
    d = os.environ.get("BRAIN_UPLOAD_DIR")
    return Path(d) if d else None


def max_bytes() -> int:
    return int(os.environ.get("BRAIN_UPLOAD_MAX_BYTES", DEFAULT_MAX_BYTES))


def ttl() -> int:
    return int(os.environ.get("BRAIN_UPLOAD_TTL", DEFAULT_TTL))


def max_pending() -> int:
    return int(os.environ.get("BRAIN_UPLOAD_MAX_PENDING", DEFAULT_MAX_PENDING))


def clean_filename(name: str) -> str:
    """The user's filename as metadata: basename only (both separator kinds),
    NFC, no control characters, bounded length. Never used as a path here."""
    name = str(name or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = unicodedata.normalize("NFC", name)
    # drop control/format characters (incl. bidi overrides that spoof extensions)
    # but keep ZWNJ/ZWJ, which are orthographically meaningful in Persian
    name = "".join(c for c in name if c in "\u200c\u200d"
                   or unicodedata.category(c)[0] != "C").strip().strip(".")
    if not name:
        raise UploadError("E_BAD_FILENAME", "filename is empty after sanitising")
    return name[:MAX_FILENAME_CHARS]


def _root() -> Path:
    d = upload_dir()
    if d is None:
        raise UploadError("E_UNAVAILABLE", "upload staging is not configured on this server")
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def _key(ref: str) -> str:
    return hashlib.sha256(ref.encode("ascii")).hexdigest()


def cleanup(now: float | None = None) -> int:
    d = upload_dir()
    if d is None or not d.is_dir():
        return 0
    now = time.time() if now is None else now
    removed = 0
    for meta in d.glob("*.json"):
        try:
            info = json.loads(meta.read_text(encoding="utf-8"))
            expired = float(info["expires_at"]) <= now
        except (OSError, ValueError, KeyError):
            expired = True
        if expired:
            for p in (meta, meta.with_suffix(".bin")):
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass
            removed += 1
    for part in d.glob("*.part"):              # interrupted uploads
        try:
            if now - part.stat().st_mtime > ttl():
                part.unlink()
        except OSError:
            pass
    return removed


def stage(stream: BinaryIO, filename: str, content_type: str | None = None) -> dict:
    """Copy a stream into staging (bounded) and return a fresh upload ref."""
    d = _root()
    cleanup()
    if len(list(d.glob("*.json"))) >= max_pending():
        raise UploadError("E_TOO_MANY", "too many pending uploads; ingest or wait for expiry")
    name = clean_filename(filename)
    ref = "upl-" + secrets.token_urlsafe(32)
    key = _key(ref)
    part = d / f"{key}.part"
    h, n, limit = hashlib.sha256(), 0, max_bytes()
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = stream.read(1 << 20)
                if not chunk:
                    break
                n += len(chunk)
                if n > limit:
                    raise UploadError("E_TOO_LARGE", f"file exceeds {limit} bytes")
                h.update(chunk)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        if n == 0:
            raise UploadError("E_EMPTY", "empty file")
    except BaseException:
        try:
            part.unlink()
        except FileNotFoundError:
            pass
        raise
    now = time.time()
    info = {"filename": name, "bytes": n, "sha256": h.hexdigest(),
            "content_type": (content_type or "")[:100], "staged_at": now,
            "expires_at": now + ttl()}
    os.replace(part, d / f"{key}.bin")
    meta = d / f"{key}.json"
    fd = os.open(meta, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(info, fh)
    return {"upload_ref": ref, **{k: info[k] for k in ("filename", "bytes", "sha256")},
            "expires_in": ttl()}


def claim(ref: str) -> tuple[Path, dict]:
    """Resolve an upload ref to (staged file, metadata) without consuming it."""
    if not isinstance(ref, str) or not REF_RE.fullmatch(ref):
        raise UploadError("E_BAD_UPLOAD_REF", "upload_ref must be the exact 'upl-...' value "
                                              "returned by the upload page")
    d = _root()
    cleanup()
    key = _key(ref)
    meta, data = d / f"{key}.json", d / f"{key}.bin"
    if not meta.is_file() or not data.is_file():
        raise UploadError("E_UPLOAD_NOT_FOUND", "unknown, expired or already used upload_ref")
    info = json.loads(meta.read_text(encoding="utf-8"))
    if data.is_symlink() or data.stat().st_size != info["bytes"]:
        raise UploadError("E_UPLOAD_CORRUPT", "staged file does not match its metadata")
    return data, info


def consume(ref: str) -> None:
    d = upload_dir()
    if d is None or not REF_RE.fullmatch(str(ref)):
        return
    key = _key(ref)
    for p in (d / f"{key}.bin", d / f"{key}.json"):
        try:
            p.unlink()
        except FileNotFoundError:
            pass


def pending() -> int:
    d = upload_dir()
    if d is None or not d.is_dir():
        return 0
    cleanup()
    return len(list(d.glob("*.json")))
