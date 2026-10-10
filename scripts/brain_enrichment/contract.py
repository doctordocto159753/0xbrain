"""CONTRACT-E1 (frozen by AGENT-0): constants, errors, deterministic hashes,
shared value types and the internal service interfaces.

Everything here is pure stdlib and import-time side-effect free. Changing a
value or a hash definition is a contract change: bump SCHEMA_VERSION or the
relevant *_VERSION and update tests/enrichment/fixtures together.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Protocol

# ----------------------------------------------------------------- versions
SCHEMA_VERSION = "1.0"           # envelope / annotation JSON shape
PROJECTION_VERSION = "proj-1"    # projection algorithm (layer selection, quality flags)
CHUNKER_VERSION = "chunk-1"      # chunking algorithm
STORE_SCHEMA_VERSION = 1         # _enrichment/store-v1.sqlite3 (PRAGMA user_version)
INDEX_SCHEMA_VERSION = 1         # _search/enrichment/search-v1.sqlite3
STORE_REL = "_enrichment/store-v1.sqlite3"
INDEX_REL = "_search/enrichment/search-v1.sqlite3"
ENRICHMENT_DIR = "_enrichment"

# ------------------------------------------------------------------ chunking
SOFT_MAX = 4000        # code points; single-chunk threshold and greedy target
HARD_MAX = 6000        # no chunk may exceed this
MIN_TAIL = 400         # a trailing chunk shorter than this merges backwards when <= HARD_MAX
OVERLAP = 0            # R1: no overlap; use prev/next links

# ------------------------------------------------------------ enumerations
SOURCE_KINDS = ("capture-text", "capture-media", "ingested-file", "canonical-record",
                "source-record", "derivative-text")
SOURCE_TIERS = ("capture", "source-record", "derivative", "canonical-record")
PROCESSING_STATES = ("discovered", "projected", "annotated", "failed", "stale", "no_text")
REVIEW_STATES = ("unreviewed", "confirmed", "rejected")
MESSAGE_ROLES = ("idea", "question", "observation", "decision-proposal", "correction",
                 "reference", "task", "other")
ENTITY_KINDS = ("concept", "person", "project", "organization", "work", "place", "other")
LINK_RELATIONS = ("about", "mentions", "part_of_project", "related_to", "supports",
                  "contradicts")
READING_RELATIONS = ("supports", "contradicts")     # candidate reading, never fact
EDGE_KINDS = ("structural", "registered", "semantic", "reading")
STRUCTURAL_EDGES = ("contains", "next_chunk", "derived_from")
LAYER_IDS = ("user-text", "transcript", "extraction", "derivative-text", "record-text")
QUALITY_FLAGS = ("needs_ocr", "extraction_partial", "row_limit", "no_text", "media_only",
                 "crlf", "unreviewed_transcript", "parse_warning", "metadata_only")

# -------------------------------------------------------- annotation limits
LIMITS = {
    "summary": 800, "topics": 5, "tags": 12, "categories": 3, "entities": 10, "links": 10,
    "uncertainty": 8, "topic_len": 120, "tag_len": 64, "category_len": 64,
    "entity_name_len": 200, "aliases": 5, "mention_len": 200, "quote_min": 8,
    "quote_max": 500, "note_len": 300, "uncertainty_len": 300, "chunk_summary": 300,
    "chunk_tags": 6, "producer_field_len": 80, "idempotency_key_len": 128,
}

# ------------------------------------------------------------- retrieval
RRF_K = 60
RRF_WEIGHTS = {"direct": 1.0, "graph": 1.0}
DIRECT_MIN_SHARE = 0.5          # of the initial result capacity, when enough direct hits exist
GRAPH_MAX_SEEDS = 8
GRAPH_MAX_DEPTH = 2
GRAPH_MAX_NODES = 40
DEFAULT_BUDGET_TOKENS = 6000
MAX_BUDGET_TOKENS = 32000
DEFAULT_LIMIT = 10
MAX_LIMIT = 50
PAGE_MAX_CHUNKS = 20            # chunks per pending/artifact page
INDEX_REFRESH_BATCH = 25        # artifacts refreshed per incremental pass

# ------------------------------------------------------------ refs / ids
CHUNK_REF_RE = re.compile(r"^chk:[0-9a-f]{32}$")
ENTITY_ID_RE = re.compile(r"^ent:[0-9a-f]{24}$")
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ARTIFACT_ID_RE = SHA256_RE

# ------------------------------------------------------------------- errors
ERROR_CODES = (
    "E_BAD_ARGUMENTS", "E_UNKNOWN_ARGUMENT", "E_BAD_REF", "E_NOT_FOUND",
    "E_STALE_SOURCE", "E_CONFLICT", "E_IDEMPOTENCY_CONFLICT", "E_STALE_CURSOR",
    "E_BAD_CURSOR", "E_TOO_LARGE", "E_VALIDATION", "E_UNAVAILABLE", "E_DISABLED",
    "E_REVIEW_FORBIDDEN", "E_SCHEMA_UNSUPPORTED", "E_INTERNAL",
)


class EnrichmentError(Exception):
    """Contract error. `code` is one of ERROR_CODES; `extra` is JSON-safe and
    never carries paths, secrets or tracebacks."""

    def __init__(self, code: str, message: str, **extra: Any):
        if code not in ERROR_CODES:
            raise ValueError(f"unknown error code {code!r}")
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra

    def to_public(self) -> dict:
        return {"ok": False, "error": {"code": self.code, "message": self.message, **self.extra}}


# ----------------------------------------------------- deterministic hashing
def canonical_json(obj: Any) -> bytes:
    """Deterministic JSON: sorted keys, no spaces, UTF-8, non-ASCII kept."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def sha256_text(text: str) -> str:
    """SHA-256 of the exact UTF-8 encoding of `text` (no newline handling)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def artifact_id(instance_id: str, source_ref: str) -> str:
    """sha256(INSTANCE.id + NUL + source_ref), 64 lowercase hex."""
    return hashlib.sha256(f"{instance_id}\0{source_ref}".encode("utf-8")).hexdigest()


def source_version(source_ref: str, layers: list[dict]) -> str:
    """SHA-256 over canonical JSON of the selected layers and their effective
    provenance. Each layer dict needs: layer_id, text_ref, text_sha256, origin,
    provenance_sha256 (original-file SHA for file layers, else null). Layer
    order is the projection order and is significant.
    Payload: {"v":1,"source_ref":..,"layers":[{layer_id,text_ref,text_sha256,
    origin,provenance_sha256}...]}; projection_version is deliberately NOT
    part of it (an algorithm bump must not re-version unchanged text)."""
    keep = ("layer_id", "text_ref", "text_sha256", "origin", "provenance_sha256")
    payload = {"v": 1, "source_ref": source_ref,
               "layers": [{k: layer.get(k) for k in keep} for layer in layers]}
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def chunk_digest(source_ref: str, src_version: str, layer_id: str, start: int, end: int,
                 chunker_version: str = CHUNKER_VERSION) -> str:
    """Full 64-hex digest kept in the store to detect chk: collisions."""
    payload = [source_ref, src_version, layer_id, int(start), int(end), chunker_version]
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def chunk_ref(digest: str) -> str:
    return "chk:" + digest[:32]


def request_digest(request: dict) -> str:
    """Idempotency fingerprint: canonical JSON of the request without its
    idempotency_key and without the server-computed `derived` block."""
    return hashlib.sha256(canonical_json({k: v for k, v in request.items()
                                          if k not in ("idempotency_key", "derived")})).hexdigest()


def entity_id(source_ref: str, normal: str) -> str:
    """Per-source entity node id. Two sources naming the same thing get two
    nodes; joining them is a query-time/candidate matter, never a silent merge."""
    return "ent:" + hashlib.sha256(f"ent\0{source_ref}\0{normal}".encode("utf-8")).hexdigest()[:24]


def instance_id(root) -> str:
    """INSTANCE.id of the wiki at `root`; the uninstantiated kit uses "kit"."""
    from pathlib import Path
    try:
        data = json.loads((Path(root) / "00-system/registers/INSTANCE.json").read_text(encoding="utf-8-sig"))
        value = data.get("id")
        return value if isinstance(value, str) and value else "kit"
    except (OSError, ValueError, AttributeError):
        return "kit"


# ------------------------------------------------------------ normal forms
_ZW = {"‌": " ", "‍": "", "‎": "", "‏": ""}
_FOLD = str.maketrans({"ك": "ک", "ي": "ی", "ى": "ی", "ې": "ی", "ة": "ه", "ۀ": "ه",
                       "أ": "ا", "إ": "ا", "ٱ": "ا"})
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_MARKS = re.compile("[ً-ٰٟـ]")     # harakat, dagger alif, tatweel


def normal_form(text: str) -> str:
    """Index/entity/tag normal form. NEVER used for stored text, hashes,
    offsets or quotes. NFKC, casefold, ک/ی folding, digits -> ASCII, harakat
    and tatweel dropped, ZWNJ -> space, whitespace collapsed."""
    t = unicodedata.normalize("NFKC", text)
    t = "".join(_ZW.get(c, c) for c in t).translate(_FOLD).translate(_DIGITS)
    t = _MARKS.sub("", t).casefold()
    return " ".join(t.split())


# ------------------------------------------------------------- token budget
def estimate_tokens(text: str) -> int:
    """Conservative estimate, NOT an exact token count: ASCII ~4 chars/token,
    other scripts (Persian, Arabic) ~2 chars/token."""
    ascii_n = sum(1 for c in text if ord(c) < 128)
    return math.ceil(ascii_n / 4 + (len(text) - ascii_n) / 2)


def fits_budget(text: str, budget_tokens: int) -> bool:
    """A text fits only if BOTH the token estimate and the hard character/byte
    ceilings (4 chars and 12 bytes per budgeted token) hold."""
    return (estimate_tokens(text) <= budget_tokens and len(text) <= budget_tokens * 4
            and len(text.encode("utf-8")) <= budget_tokens * 12)


# ------------------------------------------------------------------ cursors
def encode_cursor(kind: str, **payload: Any) -> str:
    raw = canonical_json({"k": kind, **payload})
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str, kind: str) -> dict:
    """Opaque cursor -> payload. Raises E_BAD_CURSOR on any malformation or
    kind mismatch. Staleness (generation/freshness mismatch) is the caller's
    E_STALE_CURSOR decision."""
    try:
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 2048:
            raise ValueError
        data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if not isinstance(data, dict) or data.get("k") != kind:
            raise ValueError
        return data
    except Exception:  # noqa: BLE001 - any malformation is one public error
        raise EnrichmentError("E_BAD_CURSOR", "cursor is malformed or for a different call") from None


# ------------------------------------------------------------- value types
@dataclass(frozen=True)
class Layer:
    """One selected text layer of an artifact. `text` and `origin_path` are
    server-side only and are dropped by `to_public()`."""
    layer_id: str
    text_ref: str                 # public ref carrying this literal text (cap:/doc:/rec:)
    text_sha256: str              # sha256_text(text)
    chars: int                    # len(text) in code points
    bytes: int                    # len(text.encode("utf-8"))
    literal: bool
    reviewed: bool
    text: str = field(repr=False, default="")
    origin: str = ""              # e.g. "capture:user-text", "derivative:02-sources/text"
    provenance_sha256: str | None = None   # original-file SHA for file layers
    origin_path: str | None = field(repr=False, default=None)
    tier: str = "capture"
    authority_level: int = 7

    def to_public(self) -> dict:
        return {"layer_id": self.layer_id, "text_ref": self.text_ref,
                "text_sha256": self.text_sha256, "chars": self.chars, "bytes": self.bytes,
                "literal": self.literal, "reviewed": self.reviewed, "tier": self.tier,
                "authority_level": self.authority_level}

    def version_dict(self) -> dict:
        return {"layer_id": self.layer_id, "text_ref": self.text_ref,
                "text_sha256": self.text_sha256, "origin": self.origin,
                "provenance_sha256": self.provenance_sha256}


@dataclass(frozen=True)
class SourceProjection:
    """Server-computed envelope of one source (CONTRACT-E1 section 7.3)."""
    artifact_id: str
    source_ref: str               # public ref of the artifact anchor (cap:/rec:/doc:)
    source_version: str
    source_kind: str
    source_tier: str
    source_authority_level: int
    layers: tuple[Layer, ...]
    storage_sha256: str           # sha256 of the file currently on disk (not a text hash)
    quality_flags: tuple[str, ...] = ()
    coverage_warnings: tuple[str, ...] = ()
    processing_state: str = "projected"
    projection_version: str = PROJECTION_VERSION
    schema_version: str = SCHEMA_VERSION
    original_sha256: str | None = None     # ingested files: SHA of the original bytes
    origin_channel: str | None = None      # only when server-side evidence exists
    anchor_path: str | None = field(repr=False, default=None)

    def to_public(self) -> dict:
        return {"schema_version": self.schema_version, "artifact_id": self.artifact_id,
                "source_ref": self.source_ref, "source_version": self.source_version,
                "source_kind": self.source_kind, "source_tier": self.source_tier,
                "source_authority_level": self.source_authority_level,
                "layers": [la.to_public() for la in self.layers],
                "quality_flags": list(self.quality_flags),
                "coverage_warnings": list(self.coverage_warnings),
                "processing_state": self.processing_state,
                "projection_version": self.projection_version,
                "original_sha256": self.original_sha256,
                "current_annotation_revision": None}


@dataclass(frozen=True)
class Chunk:
    chunk_ref: str
    chunk_digest: str
    source_ref: str
    source_version: str
    text_ref: str
    layer_id: str
    index: int
    start_char: int               # zero-based, inclusive, code points of decoded layer text
    end_char: int                 # exclusive
    text: str = field(repr=False, default="")
    prev_chunk_ref: str | None = None
    next_chunk_ref: str | None = None
    quality_flags: tuple[str, ...] = ()
    heading_path: tuple[str, ...] = ()     # informational only

    @property
    def chars(self) -> int:
        return self.end_char - self.start_char

    def to_public(self, with_text: bool = True) -> dict:
        out = {"chunk_ref": self.chunk_ref, "source_ref": self.source_ref,
               "source_version": self.source_version, "text_ref": self.text_ref,
               "layer_id": self.layer_id, "index": self.index,
               "start_char": self.start_char, "end_char": self.end_char, "chars": self.chars,
               "prev_chunk_ref": self.prev_chunk_ref, "next_chunk_ref": self.next_chunk_ref,
               "quality_flags": list(self.quality_flags)}
        if with_text:
            out["text"] = self.text
        return out


@dataclass(frozen=True)
class ChunkPolicy:
    soft_max: int = SOFT_MAX
    hard_max: int = HARD_MAX
    min_tail: int = MIN_TAIL
    overlap: int = OVERLAP
    chunker_version: str = CHUNKER_VERSION


@dataclass(frozen=True)
class SourcePage:
    refs: tuple[str, ...]
    next_cursor: str | None
    inventory_generation: int
    total_known: int | None = None


@dataclass(frozen=True)
class ProjectionReceipt:
    source_ref: str
    artifact_id: str
    source_version: str
    chunk_count: int
    state: str                     # "created" | "unchanged" | "new_version" | "no_text"
    previous_version: str | None = None


@dataclass(frozen=True)
class PendingPage:
    items: tuple[dict, ...]        # each: artifact view slice + chunk page, see CONTRACT-E1 11.1
    total: int
    returned: int
    truncated: bool
    next_cursor: str | None
    discovery: dict = field(default_factory=dict)


@dataclass(frozen=True)
class AnnotationReceipt:
    source_ref: str
    source_version: str
    annotation_revision: int
    idempotent_replay: bool
    coverage: dict                 # {"annotated_chunks": n, "total_chunks": m}
    processing_state: str
    index_dirty: bool = True


@dataclass(frozen=True)
class ReviewReceipt:
    source_ref: str
    annotation_revision: int
    decision: str                  # confirmed | rejected
    reviewed_by: str               # server-resolved human actor
    reviewed_at: str
    canonical_promotion: bool = False      # ALWAYS False: review never promotes


@dataclass(frozen=True)
class IndexReceipt:
    generation: int
    activated: bool
    artifacts_indexed: int
    artifacts_pending: int
    lag: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ArtifactView:
    projection: dict               # SourceProjection.to_public() (+ current revision)
    annotation: dict | None        # current annotation revision (metadata) or None
    coverage: dict                 # {"annotated_chunks","total_chunks"}
    review: dict                   # {"state", "revision"}
    chunk_count: int
    stale: bool = False


# ------------------------------------------------------------- interfaces
class SourceReader(Protocol):
    def list_sources(self, cursor: str | None, limit: int) -> SourcePage: ...
    def load(self, source_ref: str) -> SourceProjection: ...      # raises E_NOT_FOUND / E_VALIDATION(parse)


class Chunker(Protocol):
    def build(self, projection: SourceProjection, policy: ChunkPolicy) -> list[Chunk]: ...


class ProjectionStore(Protocol):
    def upsert_projection(self, projection: SourceProjection, chunks: list[Chunk]) -> ProjectionReceipt: ...
    def pending(self, cursor: str | None, limit: int, retry_failed: bool = False) -> PendingPage: ...
    def get(self, source_ref: str, source_version: str | None = None) -> ArtifactView: ...
    def append_annotation(self, request: dict) -> AnnotationReceipt: ...
    def record_review(self, request: dict, human_context: dict) -> ReviewReceipt: ...
    # read helpers shared by annotation (B), indexing/retrieval (C) and the surface (D)
    def get_chunks(self, source_ref: str, source_version: str | None = None) -> list[Chunk]: ...
    def get_chunk(self, chunk_ref: str) -> Chunk: ...   # E_NOT_FOUND | E_STALE_SOURCE (superseded version)
    def entity_exists(self, entity_id: str) -> bool: ...
    def list_current(self, after: str | None, limit: int) -> list[dict]: ...
    # list_current item: {"projection": SourceProjection, "chunks": list[Chunk], "annotation": dict|None,
    #   "annotation_revision": int|None, "review_state": str, "processing_state": str}
    def change_marker(self) -> int: ...                 # monotonic; moves on any projection/annotation/review write


class AnnotationService(Protocol):
    def submit(self, request: dict) -> AnnotationReceipt: ...


class IndexBuilder(Protocol):
    def refresh(self, batch_limit: int) -> IndexReceipt: ...


class RetrievalEngine(Protocol):
    def retrieve(self, request: dict) -> dict: ...                # RetrievalEnvelope, section 10


class ExtensionSurface(Protocol):
    def call(self, name: str, arguments: dict | None) -> dict: ...
