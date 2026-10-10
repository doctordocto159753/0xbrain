"""The four extension tools (CONTRACT-E1 section 11): names, descriptions and
input schemas. Frozen by AGENT-0; the surface (WP-D) binds handlers to them.
They are NOT part of brain_surface.contract.TOOLS: the seven base tools stay
byte-identical when the extension is off."""
from __future__ import annotations

import json
from pathlib import Path

from .contract import (CHUNK_REF_RE, DEFAULT_BUDGET_TOKENS, DEFAULT_LIMIT, IDEMPOTENCY_KEY_RE,
                       MAX_BUDGET_TOKENS, MAX_LIMIT)

_SCHEMAS = Path(__file__).resolve().parent / "schemas"


def load_schema(name: str) -> dict:
    return json.loads((_SCHEMAS / name).read_text(encoding="utf-8"))


def _inline(schema: dict) -> dict:
    """Tool inputSchemas must be self-contained (clients do not resolve file $refs)."""
    return {k: v for k, v in schema.items() if k not in ("$schema", "title")}


SOURCE_REF_PATTERN = r"^(rec|doc|cap):[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"
_SRC = {"type": "string", "pattern": SOURCE_REF_PATTERN}
_CHK = {"type": "string", "pattern": CHUNK_REF_RE.pattern}
_SHA = {"type": "string", "pattern": "^[0-9a-f]{64}$"}

AUTHORITY_NOTE = (
    "Model-produced metadata (summary, tags, entities, links) is candidate-tier "
    "navigation, not source text and not evidence. Quote and cite the returned "
    "chunk text and its refs. Retrieval rank is never evidence.")

EXTENSION_TOOLS = [
    {"name": "brain_enrichment_pending",
     "description": (
         "Technical work queue for the periodic annotator: returns small pages of "
         "sources that still need candidate metadata, each with its source_version, "
         "current annotation revision and the exact chunk texts of this page. "
         "Pages of a long source continue with next_cursor. Read-only for the "
         "corpus and for annotations; it only refreshes the deterministic "
         "projection sidecar. Not a review queue."),
     "inputSchema": {"type": "object", "additionalProperties": False,
         "properties": {
             "limit": {"type": "integer", "minimum": 1, "maximum": 5, "default": 1},
             "cursor": {"type": "string", "maxLength": 2048},
             "retry_failed": {"type": "boolean", "default": False}},
         "required": []}},
    {"name": "brain_enrichment_submit",
     "description": (
         "Record a CANDIDATE metadata revision for one source version in the "
         "enrichment sidecar. It never changes the corpus, a capture, a source "
         "record, authority or canonical status. Send source_version and "
         "expected_annotation_revision exactly as read; reuse idempotency_key on "
         "retry. Evidence quotes and entity mentions must be exact substrings of "
         "the layer or chunk text. Partial chunk coverage is allowed and is "
         "reported, not hidden."),
     "inputSchema": {"type": "object", "additionalProperties": False,
         "properties": {
             "source_ref": _SRC, "source_version": _SHA,
             "expected_annotation_revision": {"type": "integer", "minimum": 0},
             "idempotency_key": {"type": "string", "pattern": IDEMPOTENCY_KEY_RE.pattern},
             "annotation": _inline(load_schema("annotation.schema.json"))},
         "required": ["source_ref", "source_version", "expected_annotation_revision",
                      "idempotency_key", "annotation"]}},
    {"name": "brain_artifact_read",
     "description": (
         "Read one source as an artifact: its layers, quality flags, current "
         "candidate metadata with coverage (annotated_chunks/total_chunks) and a "
         "paginated chunk list; or, with chunk_ref, the exact chunk text with "
         "neighbour refs. Chunk text is the evidence; metadata is only a guide. "
         "A stale chunk_ref returns E_STALE_SOURCE."),
     "inputSchema": {"type": "object", "additionalProperties": False,
         "properties": {"source_ref": _SRC, "chunk_ref": _CHK,
                        "cursor": {"type": "string", "maxLength": 2048}},
         "required": ["source_ref"]}},
    {"name": "brain_retrieve",
     "description": (
         "Evidence-bearing retrieval across captures and the governed archive "
         "(scope all by default; the two families stay separate groups). Combines "
         "direct chunk text matching with candidate metadata and a bounded graph "
         "walk, and falls back explicitly to the base lexical/exact search when "
         "the enrichment index is missing or incomplete. Each hit carries "
         "source_ref, text_ref, chunk_ref, span, exact text, tier, authority_level, "
         "inclusion_reason and coverage/truncation flags. No model is called at "
         "query time. Answer from the returned text and cite its refs."),
     "inputSchema": {"type": "object", "additionalProperties": False,
         "properties": {
             "query": {"type": "string", "minLength": 1, "maxLength": 1000},
             "scope": {"type": "string", "enum": ["canonical", "captures", "all"], "default": "all"},
             "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": DEFAULT_LIMIT},
             "budget_tokens": {"type": "integer", "minimum": 500, "maximum": MAX_BUDGET_TOKENS,
                               "default": DEFAULT_BUDGET_TOKENS},
             "cursor": {"type": "string", "maxLength": 2048}},
         "required": ["query"]}},
]
EXTENSION_TOOL_NAMES = tuple(t["name"] for t in EXTENSION_TOOLS)
