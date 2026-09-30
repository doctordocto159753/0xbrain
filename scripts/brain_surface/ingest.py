"""Held intake for brain_ingest_file: preserve an owner-supplied file exactly.

This is the adjudication-free part of the existing /wiki-intake procedure
(.claude/skills/wiki-intake/SKILL.md steps 3-7 and the census/register
refresh of step 10), run deterministically and without a model:

  staged upload (exact bytes, owner-authenticated)
    -> type/structure check (hostile input)
    -> duplicate check by SHA-256 against existing source records/manifest
    -> _originals/remote-mcp/<id>--<safe-name>   (new file, O_EXCL, read-only)
    -> content-derived id <prefix>-src-<sha256[:12]>  (intake step 6)
    -> 02-sources/records/<id>.md  source record, holdings_tier
       pending-registration (HOLDINGS_POLICY.json), status pending-registration
    -> 02-sources/text/<id>--<slug>.md  derivative via scripts/file-to-md/to_md.py
    -> CORPUS_STATE held_artifact_count / holdings_by_tier + the four
       `Artifacts held:` entry-page markers (validate_repo census/freshness)
    -> validators -> git_safety lock + commit of exactly these paths (K9)

What it deliberately does NOT do: registration (manifest row, `registered`
tier, source_material_count), family/version assignment, reconciliation or
any canonical interpretation. Those stay human adjudication (/wiki-intake
review, brain_review.py). Nothing existing is ever modified except the
counters and markers; an existing original is never overwritten.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import zipfile
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import git_safety as gs  # noqa: E402

from . import uploads  # noqa: E402

ORIGINALS_DIR = "_originals/remote-mcp"
RECORDS_DIR = "02-sources/records"
TEXT_DIR = "02-sources/text"
CORPUS_STATE = "00-system/registers/CORPUS_STATE.json"
MANIFEST = "00-system/registers/MATERIALS_INDEX.jsonl"
INSTANCE = "00-system/registers/INSTANCE.json"
ENTRY_PAGES = ("HOME.md", "README.md", "SYSTEM_DESIGN.md", "CLAUDE.md")
NEW_FILE_PREFIXES = (ORIGINALS_DIR + "/", RECORDS_DIR + "/", TEXT_DIR + "/")
COUNTER_FILES = (CORPUS_STATE, *ENTRY_PAGES)
HELD_TIER = "pending-registration"
TO_MD = _SCRIPTS / "file-to-md" / "to_md.py"
EXTRACT_TIMEOUT = 120
MAX_ZIP_MEMBERS = 10_000
MAX_ZIP_UNCOMPRESSED = 500 * 1024 * 1024
MAX_ZIP_RATIO = 200

# extension -> (source-record format, MIME type). Nothing executable, no archives.
FORMATS = {
    ".pdf": ("pdf", "application/pdf"),
    ".docx": ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ".pptx": ("pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    ".xlsx": ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ".epub": ("epub", "application/epub+zip"),
    ".html": ("html", "text/html"),
    ".htm": ("html", "text/html"),
    ".txt": ("txt", "text/plain"),
    ".md": ("md", "text/markdown"),
    ".markdown": ("md", "text/markdown"),
}
ZIP_MARKER = {".docx": "word/document.xml", ".pptx": "ppt/presentation.xml",
              ".xlsx": "xl/workbook.xml"}


class _Duplicate(Exception):
    """Internal: identical bytes are already held; nothing is written."""


class IngestError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ------------------------------------------------------------------ policy

def is_ingest_path(rel: str) -> bool:
    """git_safety path policy for the held-intake write class."""
    rel = rel.replace("\\", "/")
    if ".." in rel.split("/") or rel.startswith("/"):
        return False
    return rel in COUNTER_FILES or rel.startswith(NEW_FILE_PREFIXES)


def _tracked(root: Path, rel: str) -> bool:
    return gs._git(root, "cat-file", "-e", f"HEAD:{rel}").returncode == 0


def commit_policy(root: Path):
    """is_ingest_path + new-files-only for originals/records/derivatives: a
    change to anything already committed there (e.g. an edited original) is
    never committed by this write class."""
    root = Path(root)
    return lambda rel: is_ingest_path(rel) and not (
        rel.startswith(NEW_FILE_PREFIXES) and _tracked(root, rel))


# ------------------------------------------------------------------ inspection

def _check_structure(path: Path, ext: str) -> None:
    """Refuse content that does not match its extension, and zip bombs."""
    with open(path, "rb") as fh:
        head = fh.read(8192)
    if ext == ".pdf":
        if not head.startswith(b"%PDF-"):
            raise IngestError("E_TYPE_MISMATCH", "file is not a PDF (missing %PDF- header)")
        return
    if ext in (".docx", ".pptx", ".xlsx", ".epub"):
        if not head.startswith(b"PK\x03\x04"):
            raise IngestError("E_TYPE_MISMATCH", f"{ext} file is not a ZIP container")
        try:
            with zipfile.ZipFile(path) as z:
                infos = z.infolist()
                names = {i.filename for i in infos}
                total = sum(i.file_size for i in infos)
                packed = max(1, sum(i.compress_size for i in infos))
                if len(infos) > MAX_ZIP_MEMBERS or total > MAX_ZIP_UNCOMPRESSED \
                        or total / packed > MAX_ZIP_RATIO:
                    raise IngestError("E_ARCHIVE_ABUSE", "container expands beyond safe bounds")
                if ext == ".epub":
                    if "mimetype" not in names or \
                            z.read("mimetype").strip() != b"application/epub+zip":
                        raise IngestError("E_TYPE_MISMATCH", "not an EPUB container")
                elif ZIP_MARKER[ext] not in names:
                    raise IngestError("E_TYPE_MISMATCH", f"container has no {ZIP_MARKER[ext]}")
        except zipfile.BadZipFile as exc:
            raise IngestError("E_TYPE_MISMATCH", f"damaged ZIP container: {exc}") from exc
        return
    # text formats: UTF-8 (BOM allowed), no NUL, no executable/binary masquerade
    data = path.read_bytes()
    if b"\x00" in data:
        raise IngestError("E_TYPE_MISMATCH", f"{ext} file contains binary data")
    try:
        data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise IngestError("E_TYPE_MISMATCH", f"{ext} file is not valid UTF-8") from exc


def safe_stem(name: str, limit: int = 60) -> str:
    """Filesystem-safe slug that keeps letters/digits of any script (Persian
    included) and never contains a separator, dot-segment or control char."""
    stem = unicodedata.normalize("NFC", Path(name).stem)
    out = "".join(c if (c.isalnum() or c in "-_") else "-" for c in stem)
    out = re.sub(r"-{2,}", "-", out).strip("-_")
    while len(out.encode("utf-8")) > limit:
        out = out[:-1]
    return out or "file"


def _prefix(root: Path) -> str:
    try:
        return json.loads((root / INSTANCE).read_text(encoding="utf-8"))["record_prefix"]
    except (OSError, ValueError, KeyError):
        return "mw"                      # the uninstantiated kit's source-system prefix


def find_existing(root: Path, sha256: str) -> dict | None:
    """Existing artifact with these exact bytes (source records + manifest)."""
    recs = root / RECORDS_DIR
    if recs.is_dir():
        for p in sorted(recs.rglob("*.md")):
            head = p.read_text(encoding="utf-8", errors="ignore")[:4000]
            if re.search(rf"(?m)^sha256:\s*['\"]?{sha256}['\"]?\s*$", head):
                rid = re.search(r"(?m)^id:\s*['\"]?([^'\"\n]+?)['\"]?\s*$", head)
                der = re.search(r"(?m)^extracted_text_path:\s*['\"]?([^'\"\n]+?)['\"]?\s*$", head)
                return {"record_id": rid.group(1) if rid else None,
                        "record_path": p.relative_to(root).as_posix(),
                        "extracted_text_path": der.group(1) if der else None}
    man = root / MANIFEST
    if man.is_file():
        for line in man.read_text(encoding="utf-8-sig").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("sha256") == sha256:
                return {"record_id": row.get("id"), "record_path": row.get("source_record_path"),
                        "extracted_text_path": row.get("extracted_text_path")}
    return None


# ------------------------------------------------------------------ writing

def _copy_exclusive(src: Path, dest: Path) -> str:
    """Create dest (must not exist), copy bytes, fsync, return SHA-256."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
        for chunk in iter(lambda: inp.read(1 << 20), b""):
            h.update(chunk)
            out.write(chunk)
        out.flush()
        os.fsync(out.fileno())
    os.chmod(dest, 0o444)                # immutable original: read-only on disk
    return h.hexdigest()


def _write_exclusive(dest: Path, text: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "wb") as out:
        out.write(text.encode("utf-8"))
        out.flush()
        os.fsync(out.fileno())


def _yaml_str(v: str) -> str:
    return json.dumps(str(v), ensure_ascii=False)


def _extract(original: Path, dest_tmp: Path) -> tuple[str, dict]:
    """Run the existing converter in a subprocess (isolation + timeout)."""
    try:
        p = subprocess.run([sys.executable, str(TO_MD), str(original), "-o", str(dest_tmp)],
                           capture_output=True, text=True, timeout=EXTRACT_TIMEOUT,
                           env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    except subprocess.TimeoutExpired:
        return "preserved_only", {"error": f"extraction timed out after {EXTRACT_TIMEOUT}s"}
    try:
        summary = json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        summary = {"ok": False, "error": (p.stderr or "no output").strip()[-300:]}
    if p.returncode != 0 or not summary.get("ok") or not dest_tmp.is_file():
        return "preserved_only", {"error": str(summary.get("error", "extraction failed"))[:300]}
    if summary.get("needs_ocr"):
        return "needs_ocr", summary
    return "complete", summary


def _bump_counters(root: Path) -> tuple[int, list[str]]:
    state_path = root / CORPUS_STATE
    state = json.loads(state_path.read_text(encoding="utf-8"))
    old = state.get("held_artifact_count")
    held = len([p for p in (root / "_originals").rglob("*") if p.is_file()])
    state["held_artifact_count"] = held
    tiers = state.setdefault("holdings_by_tier", {})
    tiers[HELD_TIER] = int(tiers.get(HELD_TIER, 0)) + (held - int(old or 0))
    state["updated"] = datetime.now(timezone.utc).date().isoformat()
    gs.durable_replace(state_path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    changed = [CORPUS_STATE]
    for page in ENTRY_PAGES:
        p = root / page
        if not p.is_file():
            continue
        text = p.read_text(encoding="utf-8")
        new = re.sub(rf"Artifacts held: {old}\b", f"Artifacts held: {held}", text) \
            if old is not None else text
        if new != text:
            gs.durable_replace(p, new)
            changed.append(page)
    return held, changed


def _record(rid, name, fmt, mime, sha, size, orig_rel, der_rel, extraction, note,
            title, description, ingested_at) -> str:
    status = {"complete": "extracted (deterministic, unreviewed)",
              "needs_ocr": "needs-ocr: no usable text layer; do not quote the derivative",
              "preserved_only": "not extracted: " + (note or "converter failed")}[extraction]
    lines = [
        "---",
        f"id: {rid}",
        "type: source-record",
        f"title: {_yaml_str(title or name)}",
        f"filename: {_yaml_str(name)}",
        "aliases: []",
        f"format: {fmt}",
        f"mime_type: {_yaml_str(mime)}",
        f"status: {HELD_TIER}",
        f"holdings_tier: {HELD_TIER}",
        "validation_status: unreviewed",
        "authority_scope: held-original-not-yet-adjudicated",
        "visibility: private",
        "sensitivity: ordinary",
        f"sha256: {sha}",
        f"bytes: {size}",
        f"original_path: {orig_rel}",
        f"extracted_text_path: {der_rel or ''}",
        f"extraction_status: {extraction}",
        "ingestion_channel: remote-mcp",
        f"ingested_at: {ingested_at}",
        f"created: {ingested_at[:10]}",
        f"updated: {ingested_at[:10]}",
        "schema_version: 1.0.0",
        "---",
        "",
        f"# {title or name}",
        "",
        "## Artifact identity",
        "",
        f"Exact bytes received through the remote connector (owner-authenticated "
        f"upload), held unchanged at `{orig_rel}`; SHA-256 `{sha}`, {size} bytes, "
        f"{mime}. Original filename: {name}.",
        "",
        "## Authority and use",
        "",
        "Held original, pending registration: not yet adjudicated into the corpus of "
        "record (no manifest row). The original is the authority; the derivative is a "
        "deterministic extraction; any interpretation is candidate material.",
        "",
        "## Extraction status",
        "",
        status + (f" Derivative: `{der_rel}`." if der_rel else ""),
        "",
    ]
    if description:
        lines += ["## Owner description (as supplied, unverified)", "", description.strip(), ""]
    lines += ["## Reconciliation rule", "",
              "Registration, family/version assignment and reconciliation are human steps "
              "(/wiki-intake review). Corrections enter as new artifacts.", ""]
    return "\n".join(lines)


def ingest(root: Path, upload_ref: str, title: str | None = None,
           description: str | None = None, validate_cmds=None) -> dict:
    """Held intake of one staged upload. Returns a receipt dict; raises
    IngestError / uploads.UploadError for refusals before anything is kept."""
    root = Path(root)
    box: dict = {}

    def write():  # noqa: C901 - one linear intake sequence
        staged, info = uploads.claim(upload_ref)
        name = info["filename"]
        ext = Path(name).suffix.lower()
        if ext not in FORMATS:
            raise IngestError("E_UNSUPPORTED_TYPE",
                              f"{ext or 'no extension'} is not an ingestible type; allowed: "
                              f"{sorted(FORMATS)}")
        fmt, mime = FORMATS[ext]
        sha = hashlib.sha256(staged.read_bytes()).hexdigest()
        if sha != info["sha256"]:
            raise IngestError("E_UPLOAD_CORRUPT", "staged bytes changed since upload")
        _check_structure(staged, ext)
        box.update(name=name, fmt=fmt, mime=mime, sha=sha, size=info["bytes"])
        existing = find_existing(root, sha)
        if existing:
            box["duplicate"] = existing
            uploads.consume(upload_ref)
            raise _Duplicate()
        box["baseline_errors"] = validator_errors()          # pre-write state, under the lock
        rid = f"{_prefix(root)}-src-{sha[:12]}"
        orig_rel = f"{ORIGINALS_DIR}/{rid}--{safe_stem(name)}{ext}"
        rec_rel = f"{RECORDS_DIR}/{rid}.md"
        der_rel = f"{TEXT_DIR}/{rid}--{safe_stem(name)}.md"
        for rel in (orig_rel, rec_rel, der_rel):
            if (root / rel).exists() or (root / rel).is_symlink() or _tracked(root, rel):
                raise IngestError("E_EXISTS", f"refusing to overwrite existing {rel}")
        for d in (ORIGINALS_DIR, RECORDS_DIR, TEXT_DIR):   # symlinked zone = escape attempt
            p = root
            for part in d.split("/"):
                p = p / part
                if p.is_symlink():
                    raise IngestError("E_UNSAFE_PATH", f"{d} contains a symlink")
        # 1. the original: exact bytes, new file only
        if _copy_exclusive(staged, root / orig_rel) != sha:
            raise IngestError("E_WRITE_MISMATCH", "stored original does not hash as received")
        written = [orig_rel]
        uploads.consume(upload_ref)            # the bytes are now held in the archive
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        box.update(rid=rid, orig_rel=orig_rel, rec_rel=rec_rel, der_rel=None,
                   extraction="preserved_only", held=None, ingested_at=now)
        # From here on nothing may undo the preserved original: a failure is
        # recorded, validation then fails, and the K9 journal keeps it visible.
        try:
            # 2. derivative (existing converter), subordinate and optional
            tmpdir = Path(tempfile.mkdtemp(prefix="brain-ingest-"))
            try:
                extraction, summary = _extract(root / orig_rel, tmpdir / "out.md")
                if extraction == "complete":
                    _write_exclusive(root / der_rel,
                                     (tmpdir / "out.md").read_text(encoding="utf-8"))
                    written.append(der_rel)
                else:
                    der_rel = None
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)
            box.update(der_rel=der_rel, extraction=extraction)
            # 3. source record (existing grammar; pending-registration tier)
            _write_exclusive(root / rec_rel, _record(
                rid, name, fmt, mime, sha, info["bytes"], orig_rel, der_rel, extraction,
                summary.get("error"), title, description, now))
            written.append(rec_rel)
            # 4. registers: held count + entry-page markers
            held, changed = _bump_counters(root)
            written += changed
            box["held"] = held
        except Exception as exc:  # noqa: BLE001
            box["partial_error"] = f"{type(exc).__name__}: {exc}"[:300]
        return written

    cmds = validate_cmds if validate_cmds is not None else (
        ["scripts/validate_repo.py", "--full"], ["scripts/validate_content_release.py"])

    def validator_errors() -> set[str]:
        found = set()
        for cmd in cmds:
            p = subprocess.run([sys.executable, *cmd], cwd=root, capture_output=True, text=True,
                               timeout=600)
            if p.returncode != 0:
                lines = [ln.strip() for ln in (p.stdout + p.stderr).splitlines()
                         if ln.startswith("ERROR")]
                found |= set(lines) or {f"{cmd[0]} exit {p.returncode}"}
        return found

    def validate():
        # Same rule as the repository gate (check_against_baseline): the ingest
        # is refused a commit only for validator errors it introduced.
        errs = [f"ingest incomplete after the original was preserved: {box['partial_error']}"] \
            if box.get("partial_error") else []
        if (root / box["orig_rel"]).exists() and hashlib.sha256(
                (root / box["orig_rel"]).read_bytes()).hexdigest() != box["sha"]:
            errs.append("original bytes changed after write")
        new = sorted(validator_errors() - box.get("baseline_errors", set()))
        if new:
            errs.append("new validator errors: " + "; ".join(new)[:600])
        return errs

    def run():
        return gs.locked_write_commit(root, write, "brain: ingest held original (remote-mcp)",
                                      validate, allow=commit_policy(root))

    try:
        res = run()
    except _Duplicate:
        existing = box.pop("duplicate")
        return {**box, "duplicate": True, "existing": existing}
    except gs.GovernanceError as exc:
        raise IngestError("E_UNAVAILABLE", str(exc)) from exc
    return {"duplicate": False, **box, "k9": res}
