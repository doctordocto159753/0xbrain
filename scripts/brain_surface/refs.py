"""The one public ref grammar and its server-side resolver (K3).

Remote callers never name a file. They pass typed, opaque refs that earlier
tool results handed them; this module is the only place that turns such a ref
into a repository file, and the only place that turns an internal id/path back
into a public ref. Internal governance code (evidence_audit.resolve_ref,
reconcile_context) keeps its own permissive internal forms; they are never
reachable with caller-controlled text except through `resolve()` below.

    rec:<record-id>     frontmatter `id` of a record in a canonical-scope zone
    doc:<20 hex>        a canonical-scope file without a frontmatter id
                        (typically an extracted derivative); opaque hash of
                        its repository-relative path
    cap:<capture-id>    capture record (noncanonical intake)
    prop:<proposal-id>  entry in the proposal queue (candidate)
    hist:<doc-stem>     genesis / handoff document (chronology)

The identifier part is [A-Za-z0-9][A-Za-z0-9_-]{0,127}: no '/', '\\', '.',
':' or whitespace can pass, so a traversal, absolute path (POSIX or Windows)
or file name is rejected before any filesystem access. Every resolution is
additionally checked to stay inside its zone.
"""
from __future__ import annotations

import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import evidence_audit as ea  # noqa: E402

KINDS = ("rec", "doc", "cap", "prop", "hist")
REF_RE = re.compile(r"^(rec|doc|cap|prop|hist):[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
IDENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
DOC_RE = re.compile(r"^[0-9a-f]{20}$")
CAPTURE_ID_RE = ea.CAPTURE_ID_RE
CANONICAL_ZONES = ea.CANONICAL_ZONES
CAPTURES_REL = "01-inbox/captures"
HISTORY_GLOBS = ("07-genesis/**/*.md", "_captures/HANDOFF*.md")

CANDIDATE_LEVEL = ea.CANDIDATE_LEVEL
TIER_BY_ZONE = {
    "02-sources/records": "source-record", "02-sources/text": "derivative",
    "02-sources": "source-record", "03-objects": "canonical-record",
    "04-notes": "canonical-record", "05-claims": "canonical-record",
    "06-relations": "canonical-record", "07-genesis": "history",
    "_captures": "history", CAPTURES_REL: "capture", "_proposals": "candidate",
}


class BadRef(ValueError):
    """The text is not a well-formed public ref."""


@dataclass(frozen=True)
class Resolved:
    ref: str
    kind: str
    ident: str
    path: str | None          # repository-relative, server-side only
    zone: str
    tier: str
    authority_level: int


def parse(ref) -> tuple[str, str]:
    if not isinstance(ref, str) or not REF_RE.fullmatch(ref):
        raise BadRef("ref must be a typed opaque id such as 'rec:<id>' or 'cap:<id>' "
                     "taken from a prior tool result; paths are never accepted")
    kind, ident = ref.split(":", 1)
    if kind == "doc" and not DOC_RE.fullmatch(ident):
        raise BadRef("malformed doc ref")
    return kind, ident


def zone_of(rel: str) -> str:
    for z in ("02-sources/records", "02-sources/text", CAPTURES_REL):
        if rel.startswith(z + "/"):
            return z
    return rel.split("/", 1)[0]


def level_of(rel: str) -> int:
    return ea.authority_level(rel)


def tier_of(rel: str) -> str:
    z = zone_of(rel)
    return TIER_BY_ZONE.get(z, TIER_BY_ZONE.get(rel.split("/", 1)[0], "unknown"))


def doc_ident(rel: str) -> str:
    return hashlib.sha256(("0xbrain-doc\0" + rel).encode("utf-8")).hexdigest()[:20]


def _inside(root: Path, rel: str, zone: str) -> bool:
    try:
        (root / rel).resolve().relative_to((root / zone).resolve())
        return True
    except (ValueError, OSError):
        return False


def _canonical_files(root: Path):
    for zone in CANONICAL_ZONES:
        d = root / zone
        if d.is_dir():
            for p in sorted(d.rglob("*.md")):
                if p.is_file():
                    yield p.relative_to(root).as_posix()


def _front_id(root: Path, rel: str) -> str | None:
    try:
        head = (root / rel).read_text(encoding="utf-8", errors="ignore")[:4000]
    except OSError:
        return None
    if not head.startswith("---"):
        return None
    block = head.split("---", 2)[1] if head.count("---") >= 2 else head
    m = ea._FM_ID_RE.search(block)
    return m.group(1).strip() if m else None


def history_docs(root: Path) -> dict[str, str]:
    """stem -> relative path. A stem that names two documents is dropped from
    the public namespace rather than resolved arbitrarily."""
    seen: dict[str, list[str]] = {}
    for pattern in HISTORY_GLOBS:
        for p in sorted(root.glob(pattern)):
            if p.is_file() and IDENT_RE.fullmatch(p.stem):
                rel = p.relative_to(root).as_posix()
                if rel not in seen.setdefault(p.stem, []):
                    seen[p.stem].append(rel)
    return {stem: rels[0] for stem, rels in seen.items() if len(rels) == 1}


def _proposal(root: Path, ident: str) -> dict | None:
    for rec in ea._load_proposals(root):
        if rec.get("id") == ident:
            return rec
    return None


def resolve(root: Path, ref) -> Resolved:
    """Public ref -> Resolved. Raises BadRef (malformed) or KeyError (unknown)."""
    root = Path(root)
    kind, ident = parse(ref)
    if kind == "rec":
        hit = ea.resolve_ref(root, ident)      # record-id branch only: ident has no '/' or '.'
        if hit is None or hit["tier"] != "canonical" or not _inside(root, hit["path"], hit["path"].split("/", 1)[0]):
            raise KeyError(ref)
        rel = hit["path"]
        return Resolved(ref, kind, ident, rel, zone_of(rel), tier_of(rel), level_of(rel))
    if kind == "doc":
        for rel in _canonical_files(root):
            if doc_ident(rel) == ident and _inside(root, rel, rel.split("/", 1)[0]):
                return Resolved(ref, kind, ident, rel, zone_of(rel), tier_of(rel), level_of(rel))
        raise KeyError(ref)
    if kind == "cap":
        if not CAPTURE_ID_RE.fullmatch(ident):
            raise KeyError(ref)
        hit = ea.resolve_ref(root, ident)
        if hit is None or hit["tier"] != "capture" or not _inside(root, hit["path"], CAPTURES_REL):
            raise KeyError(ref)
        return Resolved(ref, kind, ident, hit["path"], CAPTURES_REL, "capture", CANDIDATE_LEVEL)
    if kind == "prop":
        if _proposal(root, ident) is None:
            raise KeyError(ref)
        return Resolved(ref, kind, ident, ea.QUEUE_REL.as_posix(), "_proposals", "candidate",
                        CANDIDATE_LEVEL)
    rel = history_docs(root).get(ident)
    if rel is None:
        raise KeyError(ref)
    return Resolved(ref, kind, ident, rel, zone_of(rel), tier_of(rel), level_of(rel))


def public_ref_for_path(root: Path, rel: str, record_id: str | None = None) -> str | None:
    """Internal repository path (+ optional known id) -> public ref, or None
    when the file is not publicly addressable."""
    rel = rel.replace("\\", "/")
    if rel.startswith(CAPTURES_REL + "/"):
        stem = Path(rel).stem
        return f"cap:{stem}" if CAPTURE_ID_RE.fullmatch(stem) else None
    if rel.startswith(tuple(z + "/" for z in CANONICAL_ZONES)):
        rid = record_id if record_id is not None else _front_id(root, rel)
        if rid and IDENT_RE.fullmatch(str(rid)):
            return f"rec:{rid}"
        return f"doc:{doc_ident(rel)}"
    if rel.startswith(("07-genesis/", "_captures/")):
        stem = Path(rel).stem
        return f"hist:{stem}" if history_docs(root).get(stem) == rel else None
    return None


def public_ref_for_id(ident: str) -> str | None:
    """Internal id (record id, capture id or proposal id) -> public ref."""
    ident = str(ident)
    if CAPTURE_ID_RE.fullmatch(ident):
        return f"cap:{ident}"
    if IDENT_RE.fullmatch(ident):
        return f"rec:{ident}"
    return None
