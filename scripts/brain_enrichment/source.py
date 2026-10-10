"""SourceReader: corpus files -> SourceProjection (CONTRACT-E1 section 2).

Read-only by construction: every file is opened `O_RDONLY | O_NOFOLLOW`
below an INJECTED root; nothing is ever created, renamed or written. The
pure parsers of the capture core (`wiki_capture.parse_record_text`) are the
only interpretation of a capture; no re-render, no repair. A file that
cannot be parsed unambiguously raises `EnrichmentError("E_VALIDATION")` with
a ref-level message (never a path) and the store records the source as
`failed` instead of guessing.

Holdings are ONE artifact: the source record is the anchor (`rec:<id>`), the
derivative file under `02-sources/text/` is its text layer (`doc:<hash>`
text_ref); the original bytes are never read, only their recorded SHA-256.
"""
from __future__ import annotations

import bisect
import hashlib
import json
import os
import re
import stat
import sys
import threading
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1]
for _p in (_SCRIPTS, _SCRIPTS / "capture"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import wiki_capture as wc  # noqa: E402  (pure parsers only; its REPO_ROOT is never used)
from brain_surface import refs as R  # noqa: E402

from . import contract as C  # noqa: E402

CAPTURES_REL = R.CAPTURES_REL                # "01-inbox/captures"
C_CAPTURE_LEVEL = R.CANDIDATE_LEVEL
RECORDS_REL = "02-sources/records"
TEXT_REL = "02-sources/text"
CANONICAL_RECORD_ZONES = ("03-objects", "04-notes", "05-claims", "06-relations")
MAX_FILE_BYTES = 20_000_000
HEAD_BYTES = 8192
MAX_LIST_LIMIT = 1000
MEDIA_KINDS = frozenset(wc.CAPTURE_KINDS) - {"text"}
_FM_ID = re.compile(r"^id:\s*[\"']?([^\"'\r\n]+?)[\"']?\s*$", re.MULTILINE)
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_CHANNEL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_FLAG_ORDER = {f: i for i, f in enumerate(C.QUALITY_FLAGS)}


def _fail(ref: str, reason: str) -> C.EnrichmentError:
    return C.EnrichmentError("E_VALIDATION", f"{ref}: {reason}", source_ref=ref, stage="projection")


def _short(text: str, limit: int = 160) -> str:
    return " ".join(str(text).split())[:limit]


# ----------------------------------------------------------- front matter
def _scalar(value: str):
    v = value.strip()
    if v[:1] == '"':
        try:
            return json.loads(v)
        except ValueError:
            return v.strip('"')
    if len(v) >= 2 and v[0] == v[-1] == "'":
        return v[1:-1].replace("''", "'")
    if v in ("null", "~"):
        return None
    return v


def simple_front_matter(text: str, *, partial: bool = False) -> dict | None:
    """Top-level `key: scalar` lines of a leading `---` block, or None when
    there is no (closed, unless `partial`) block. Nested values are ignored:
    only the flat scalar keys of source records are needed."""
    lines = text.split("\n")
    if not lines or lines[0].rstrip("\r") != "---":
        return None
    out: dict = {}
    for ln in lines[1:]:
        ln = ln.rstrip("\r")
        if ln == "---":
            return out
        if not ln or ln[0] in " \t-#" or ":" not in ln:
            continue
        key, _, val = ln.partition(":")
        out.setdefault(key.strip(), _scalar(val))
    return out if partial else None


def _ref_from_head(rel: str, head: str) -> str:
    """Public ref for a zone file, equivalent to refs.public_ref_for_path but
    reading only the head of the file."""
    rid = None
    if head.startswith("---"):
        block = head.split("---", 2)[1] if head.count("---") >= 2 else head
        m = _FM_ID.search(block)
        rid = m.group(1).strip() if m else None
    if rid and R.IDENT_RE.fullmatch(rid):
        return f"rec:{rid}"
    return f"doc:{R.doc_ident(rel)}"


# ------------------------------------------------------------------ reader
class _Entry:
    __slots__ = ("ref", "role", "rels")

    def __init__(self, ref: str, role: str, rel: str):
        self.ref, self.role, self.rels = ref, role, [rel]


class SourceReader:
    """Implements contract.SourceReader for the wiki at `root`."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self._lock = threading.RLock()
        self._entries: dict[str, _Entry] = {}
        self._refs: list[str] = []
        self._generation = 0
        self.unaddressable: list[str] = []      # server-side diagnostics only: `cap-*.md` files with no valid ref

    # ------------------------------------------------------------ discovery
    def _walk(self, zone: str):
        base = self.root / zone
        if base.is_symlink() or not base.is_dir():
            return
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(".")
                                 and not (Path(dirpath) / d).is_symlink())
            for name in sorted(filenames):
                if name.startswith(".") or not name.endswith(".md"):
                    continue
                yield (Path(dirpath) / name).relative_to(self.root).as_posix()

    def _head(self, rel: str) -> str:
        p = self.root / rel
        try:
            if p.is_symlink():
                return ""
            with open(p, "rb") as fh:
                return fh.read(HEAD_BYTES).decode("utf-8", errors="ignore")
        except OSError:
            return ""

    def _scan(self) -> None:
        entries: dict[str, _Entry] = {}
        skipped: list[str] = []

        def add(ref: str, role: str, rel: str) -> None:
            ent = entries.get(ref)
            if ent is None:
                entries[ref] = _Entry(ref, role, rel)
            else:
                ent.rels.append(rel)

        for rel in self._walk(CAPTURES_REL):
            stem = Path(rel).stem
            if R.CAPTURE_ID_RE.fullmatch(stem):
                add(f"cap:{stem}", "capture", rel)
            elif stem.startswith("cap-"):
                skipped.append(rel)             # looks like a capture but cannot carry a public ref
        referenced: set[str] = set()
        for rel in self._walk(RECORDS_REL):
            head = self._head(rel)
            fm = simple_front_matter(head, partial=True) or {}
            der = fm.get("extracted_text_path")
            if isinstance(der, str) and der.strip():
                referenced.add(der.strip().replace("\\", "/"))
            add(_ref_from_head(rel, head), "record", rel)
        for rel in self._walk(TEXT_REL):
            if rel not in referenced:
                add(_ref_from_head(rel, self._head(rel)), "orphan", rel)
        for zone in CANONICAL_RECORD_ZONES:
            for rel in self._walk(zone):
                add(_ref_from_head(rel, self._head(rel)), "canonical", rel)
        refs = sorted(entries)
        self._entries, self._refs, self.unaddressable = entries, refs, skipped
        self._generation = int(hashlib.sha256("\n".join(refs).encode("utf-8")).hexdigest()[:12], 16)

    def list_sources(self, cursor: str | None, limit: int) -> C.SourcePage:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIST_LIMIT:
            raise C.EnrichmentError("E_BAD_ARGUMENTS", f"limit must be an integer in 1..{MAX_LIST_LIMIT}")
        after, want_gen = None, None
        if cursor is not None:
            data = C.decode_cursor(cursor, "sources")
            after, want_gen = data.get("a"), data.get("g")
            if not isinstance(after, str) or not isinstance(want_gen, int):
                raise C.EnrichmentError("E_BAD_CURSOR", "cursor is malformed or for a different call")
        with self._lock:
            if cursor is None or want_gen != self._generation or not self._refs:
                self._scan()                    # a new pass, or a restart: fresh snapshot
            refs = self._refs
            start = bisect.bisect_right(refs, after) if after is not None else 0
            page = refs[start:start + limit]
            more = start + limit < len(refs)
            nxt = C.encode_cursor("sources", a=page[-1], g=self._generation) if (more and page) else None
            return C.SourcePage(tuple(page), nxt, self._generation, len(refs))

    # --------------------------------------------------------------- loading
    def _entry(self, ref: str, *, rescan: bool) -> _Entry:
        with self._lock:
            ent = None if rescan else self._entries.get(ref)
            if ent is not None and all(os.path.lexists(self.root / r) for r in ent.rels):
                return ent
            self._scan()
            ent = self._entries.get(ref)
        if ent is None:
            raise C.EnrichmentError("E_NOT_FOUND", f"{ref}: source does not exist", source_ref=ref)
        return ent

    def _read(self, rel: str, zone: str, ref: str) -> bytes:
        """Bytes of a regular file strictly inside `zone` (no symlinks anywhere
        on the way, no escape, bounded size)."""
        parts = Path(rel).parts
        if (not parts or rel.startswith("/") or ".." in parts or "\\" in rel
                or not rel.replace("\\", "/").startswith(zone.rstrip("/") + "/")):
            raise _fail(ref, "file reference is outside its zone")
        cur = self.root
        for part in parts:
            cur = cur / part
            if cur.is_symlink():
                raise _fail(ref, "a symlink is on the path; symlinks are never followed")
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
            fd = os.open(cur, flags)
        except FileNotFoundError:
            raise C.EnrichmentError("E_NOT_FOUND", f"{ref}: file does not exist", source_ref=ref) from None
        except OSError as exc:
            raise C.EnrichmentError("E_UNAVAILABLE", f"{ref}: file cannot be read ({exc.__class__.__name__})",
                                    source_ref=ref, stage="projection") from None
        with os.fdopen(fd, "rb") as fh:
            try:
                resolved = Path(os.path.realpath(cur))
                resolved.relative_to((self.root / zone).resolve())
            except ValueError:
                raise _fail(ref, "file resolves outside its zone") from None
            st = os.fstat(fh.fileno())
            if not stat.S_ISREG(st.st_mode):
                raise _fail(ref, "not a regular file")
            if st.st_size > MAX_FILE_BYTES:
                raise C.EnrichmentError("E_TOO_LARGE", f"{ref}: file exceeds {MAX_FILE_BYTES} bytes",
                                        source_ref=ref)
            try:
                return fh.read()
            except OSError as exc:
                raise C.EnrichmentError("E_UNAVAILABLE", f"{ref}: file cannot be read ({exc.__class__.__name__})",
                                        source_ref=ref, stage="projection") from None

    @staticmethod
    def _decode(raw: bytes, ref: str, what: str) -> str:
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            raise _fail(ref, f"{what} is not valid UTF-8 (parse_warning)") from None

    def load(self, source_ref: str) -> C.SourceProjection:
        try:
            R.parse(source_ref)
        except R.BadRef as exc:
            raise C.EnrichmentError("E_BAD_REF", str(exc)) from None
        if not source_ref.startswith(("cap:", "rec:", "doc:")):
            raise C.EnrichmentError("E_BAD_REF", f"{source_ref}: this ref kind is not projected in R1")
        for attempt in (0, 1):
            ent = self._entry(source_ref, rescan=bool(attempt))
            if len(ent.rels) > 1:
                raise _fail(source_ref, f"ambiguous: {len(ent.rels)} files claim this ref")
            rel = ent.rels[0]
            built = self._build(source_ref, ent.role, rel)
            if built is not None:
                return built
        raise _fail(source_ref, "the record's identity does not match its location (parse_warning)")

    # -------------------------------------------------------------- builders
    def _build(self, ref: str, role: str, rel: str):
        if role == "capture":
            return self._capture(ref, rel)
        if role == "record":
            return self._record(ref, rel)
        if role == "orphan":
            return self._orphan(ref, rel)
        return self._canonical(ref, rel)

    def _finish(self, ref: str, *, kind: str, tier: str, level: int, layers: list[dict], storage: str,
                flags: set[str], warnings: list[str], rel: str, original_sha: str | None = None,
                channel: str | None = None) -> C.SourceProjection:
        built = []
        for spec in layers:
            text = spec["text"]
            built.append(C.Layer(
                layer_id=spec["layer_id"], text_ref=spec["text_ref"], text_sha256=C.sha256_text(text),
                chars=len(text), bytes=len(text.encode("utf-8")), literal=spec.get("literal", True),
                reviewed=spec.get("reviewed", False), text=text, origin=spec["origin"],
                provenance_sha256=spec.get("provenance_sha256"), tier=spec["tier"],
                authority_level=spec["level"]))
        if any("\r" in la.text for la in built):
            flags.add("crlf")
        if not built:
            flags.add("no_text")
        ordered = tuple(sorted(flags, key=lambda f: _FLAG_ORDER[f]))
        version = C.source_version(ref, [la.version_dict() for la in built])
        return C.SourceProjection(
            artifact_id=C.artifact_id(C.instance_id(self.root), ref), source_ref=ref,
            source_version=version, source_kind=kind, source_tier=tier, source_authority_level=level,
            layers=tuple(built), storage_sha256=storage, quality_flags=ordered,
            coverage_warnings=tuple(dict.fromkeys(warnings)),
            processing_state="projected" if built else "no_text", original_sha256=original_sha,
            origin_channel=channel, anchor_path=rel)

    # captures -------------------------------------------------------------
    def _capture(self, ref: str, rel: str):
        ident = ref[4:]
        raw = self._read(rel, CAPTURES_REL, ref)
        text = self._decode(raw, ref, "capture record")
        try:
            fm, sections = wc.parse_record_text(text)
        except wc.CaptureError as exc:
            raise _fail(ref, f"capture record cannot be parsed ({_short(exc.message)}) (parse_warning)") from None
        except Exception as exc:  # noqa: BLE001 - any parser failure is a parse failure
            raise _fail(ref, f"capture record cannot be parsed ({exc.__class__.__name__}) (parse_warning)") from None
        if fm.get("id") != ident:
            return None                         # identity != location: load() rescans once, then fails
        if fm.get("type") != "capture":
            raise _fail(ref, "front matter type is not 'capture' (parse_warning)")
        problems = list(getattr(fm, "ambiguous", []))
        if problems:
            raise _fail(ref, "capture record is ambiguous: " + _short("; ".join(problems), 200)
                        + " (parse_warning)")
        kind = fm.get("capture_kind")
        if kind not in wc.CAPTURE_KINDS:
            raise _fail(ref, "capture_kind is missing or unknown (parse_warning)")
        channel = fm.get("capture_channel")
        channel = channel if isinstance(channel, str) and channel in wc.CHANNELS and _CHANNEL_RE.match(channel) else None
        storage = hashlib.sha256(raw).hexdigest()
        flags: set[str] = set()
        warnings: list[str] = []
        layers: list[dict] = []
        user = sections.get("User-supplied text", "")
        literal = sections.get(wc.LITERAL_SECTION, "")

        def layer(layer_id: str, body: str, reviewed: bool, origin: str) -> dict:
            return {"layer_id": layer_id, "text_ref": ref, "text": body, "literal": True,
                    "reviewed": reviewed, "origin": origin, "tier": "capture", "level": C_CAPTURE_LEVEL}

        if kind == "text":
            if user.strip():
                layers.append(layer("user-text", user, False, "capture:user-text"))
                if fm.get("sha256") != C.sha256_text(user):
                    flags.add("parse_warning")
                    warnings.append("recorded sha256 does not match the stored user text; projection binds to the stored text")
            if literal.strip():
                warnings.append("a literal section is present on a text capture and is not projected")
            out_kind = "capture-text"
        else:
            out_kind = "capture-media"
            if kind == "mixed" and user.strip():
                layers.append(layer("user-text", user, False, "capture:user-text"))
            elif user.strip():
                warnings.append(f"user-supplied text on a {kind} capture is not projected in R1")
            if literal.strip():
                reviewed = fm.get("transcription_reviewed") is True
                layers.append(layer("transcript", literal, reviewed, "capture:transcript"))
                if not reviewed:
                    flags.add("unreviewed_transcript")
            elif not layers:
                flags.add("media_only")
                warnings.append("media-only capture: no literal text exists yet; media bytes are never projected")
            if sections.get(wc.DESCRIPTION_SECTION, "").strip():
                warnings.append("machine description is excluded from the projection in R1")
        if sections.get(wc.INTERPRETATION_SECTION, "").strip():
            warnings.append("interpretation (candidate) is excluded from the projection in R1")
        raw_flags = fm.get("quality_flags")
        if isinstance(raw_flags, list):
            for f in raw_flags:
                if isinstance(f, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", f):
                    if f in C.QUALITY_FLAGS:
                        flags.add(f)
                    else:
                        warnings.append(f"capture quality flag: {f}")
        return self._finish(ref, kind=out_kind, tier="capture", level=C_CAPTURE_LEVEL, layers=layers,
                            storage=storage, flags=flags, warnings=warnings, rel=rel, channel=channel)

    # source records / holdings -------------------------------------------
    def _record(self, ref: str, rel: str):
        raw = self._read(rel, RECORDS_REL, ref)
        text = self._decode(raw, ref, "source record")
        fm = simple_front_matter(text)
        if fm is None:
            raise _fail(ref, "source record has no closed front matter (parse_warning)")
        if ref.startswith("rec:") and str(fm.get("id", "")).strip() != ref[4:]:
            return None
        level = R.level_of(rel)
        is_holding = any(k in fm for k in ("original_path", "extracted_text_path", "holdings_tier"))
        record_sha = hashlib.sha256(raw).hexdigest()
        if not is_holding:
            return self._finish(ref, kind="source-record", tier="source-record", level=level, layers=[],
                                storage=record_sha, flags={"metadata_only"}, warnings=[
                                    "source record without a file: metadata only, no text is projected"], rel=rel)
        flags: set[str] = set()
        warnings: list[str] = []
        sha = fm.get("sha256")
        original_sha = sha.strip().lower() if isinstance(sha, str) and _HEX64.match(sha.strip().lower()) else None
        if original_sha is None:
            flags.add("parse_warning")
            warnings.append("source record has no valid sha256 of the original")
        status = str(fm.get("extraction_status") or "").strip().lower().replace("-", "_")
        if status == "needs_ocr":
            flags.add("needs_ocr")
            warnings.append("needs_ocr: the original has no usable text layer; do not quote the derivative")
        elif status == "preserved_only":
            warnings.append("preserved_only: the original is held but no text was extracted")
        der_rel = fm.get("extracted_text_path")
        der_rel = der_rel.strip().replace("\\", "/") if isinstance(der_rel, str) else ""
        layers: list[dict] = []
        storage = record_sha
        if der_rel and status != "preserved_only":
            try:
                draw = self._read(der_rel, TEXT_REL, ref)
            except C.EnrichmentError as exc:
                if exc.code != "E_NOT_FOUND":
                    raise
                warnings.append("the derivative file recorded for this holding is missing")
                draw = None
            if draw is not None:
                storage = hashlib.sha256(draw).hexdigest()
                dtext = self._decode(draw, ref, "derivative file")
                self._extraction_flags(dtext, flags, warnings)
                if _has_body(dtext):
                    text_ref = _ref_from_head(der_rel, dtext[:HEAD_BYTES])
                    layers.append({"layer_id": "derivative-text", "text_ref": text_ref, "text": dtext,
                                   "literal": True, "reviewed": False, "origin": "derivative:" + TEXT_REL,
                                   "provenance_sha256": original_sha, "tier": R.tier_of(der_rel),
                                   "level": R.level_of(der_rel)})
                else:
                    warnings.append("the derivative holds no extracted text")
        elif not der_rel:
            warnings.append("no derivative text exists for this holding")
        if not layers:
            flags.add("metadata_only")
        return self._finish(ref, kind="ingested-file", tier="source-record", level=level, layers=layers,
                            storage=storage, flags=flags, warnings=warnings, rel=rel, original_sha=original_sha)

    @staticmethod
    def _extraction_flags(dtext: str, flags: set[str], warnings: list[str]) -> None:
        for line in dtext.split("\n", 14)[:14]:
            low = line.lower()
            if line.startswith("> method:"):
                if "truncated:" in low:
                    flags.add("row_limit")
                    warnings.append("extraction truncated: " + _short(line[low.index("truncated:"):], 240))
                if "needs ocr" in low or "no-text-layer" in low:
                    flags.add("extraction_partial")
                    warnings.append("extraction partial: some pages have no text layer (OCR needed)")
            elif line.startswith("> quality:") and "needs-ocr" in low:
                flags.add("needs_ocr")

    def _orphan(self, ref: str, rel: str):
        raw = self._read(rel, TEXT_REL, ref)
        text = self._decode(raw, ref, "derivative file")
        flags: set[str] = set()
        warnings: list[str] = ["derivative without a source record: provenance of the original is unknown"]
        self._extraction_flags(text, flags, warnings)
        layers = []
        if _has_body(text):
            layers.append({"layer_id": "derivative-text", "text_ref": ref, "text": text, "literal": True,
                           "reviewed": False, "origin": "derivative:" + TEXT_REL, "tier": R.tier_of(rel),
                           "level": R.level_of(rel)})
        else:
            flags.add("metadata_only")
        found = _ref_from_head(rel, text[:HEAD_BYTES])
        if found != ref:
            return None
        return self._finish(ref, kind="derivative-text", tier="derivative", level=R.level_of(rel),
                            layers=layers, storage=hashlib.sha256(raw).hexdigest(), flags=flags,
                            warnings=warnings, rel=rel)

    def _canonical(self, ref: str, rel: str):
        zone = rel.split("/", 1)[0]
        raw = self._read(rel, zone, ref)
        text = self._decode(raw, ref, "record file")
        if _ref_from_head(rel, text[:HEAD_BYTES]) != ref:
            return None
        layers = []
        if text.strip():
            layers.append({"layer_id": "record-text", "text_ref": ref, "text": text, "literal": True,
                           "reviewed": False, "origin": "record:" + zone, "tier": R.tier_of(rel),
                           "level": R.level_of(rel)})
        return self._finish(ref, kind="canonical-record", tier="canonical-record", level=R.level_of(rel),
                            layers=layers, storage=hashlib.sha256(raw).hexdigest(), flags=set(),
                            warnings=[], rel=rel)


_HEADER_LINE = re.compile(r"^(# Extracted from .*|> .*)$")


def _has_body(text: str) -> bool:
    """True when the derivative holds text beyond the converter's own header
    (`# Extracted from ...` and the `> ...` provenance lines at the top)."""
    in_header = True
    for line in text.split("\n"):
        s = line.strip()
        if not s:
            continue
        if in_header and _HEADER_LINE.match(s):
            continue
        in_header = False
        return True
    return False
