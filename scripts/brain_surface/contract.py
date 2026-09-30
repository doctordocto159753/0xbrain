"""Frozen K3 tool schemas and shared constants (single source of truth)."""
from __future__ import annotations

import json
from pathlib import Path

from .refs import REF_RE

ROOT = Path(__file__).resolve().parents[2]
PROPOSAL_SCHEMA = ROOT / "00-system" / "policies" / "proposal_schema.json"

SEARCH_MODES = ("lexical", "exact")
SEARCH_SCOPES = ("canonical", "captures", "all")
DEPTHS = ("focused", "deep")
LANG_HINTS = ("fa", "en", "mixed", "unknown")

# Frozen K5 section names (reconcile_context.SECTION_ORDER uses the same).
RECONCILE_SECTIONS = (
    "canonical_records", "claims", "relations", "source_records", "captures",
    "superseded", "open_proposals", "unresolved", "chronology",
)
REMOTE_EXCLUDED_KINDS = ("intake-registration",)

AUTHORITY_NOTE = (
    "Retrieval rank and inclusion order organize attention; they are never "
    "evidence. Tool output is a route to records, not a verdict. Anything a "
    "model derives from it is candidate-tier (level 7).")
SCOPE_NOTE = (
    "scope 'canonical' means the governed, non-capture side of the archive: "
    "canonical records, source records and extracted derivatives. Each result "
    "keeps its own tier and authority_level; a derivative (level 5) is a "
    "candidate passage, not accepted canonical evidence.")
CAPTURE_NOTE = (
    "Captures are noncanonical level-7 intake. They record what was said, "
    "not what is true, and are never promoted from here.")


def remote_proposal_kinds(schema_path: Path = PROPOSAL_SCHEMA) -> dict:
    """Kinds exposed remotely, derived from the shipped schema.

    intake-registration is excluded (CCR-3): its evidence is an original file
    path plus a hash, which a remote caller can neither supply nor name.
    `source_passage` is not a caller field: it is derived server-side from the
    first quoted rec:/doc: evidence ref.
    """
    schema = json.loads(schema_path.read_text(encoding="utf-8-sig"))
    return {kind: [f for f in spec["required"] if f != "source_passage"]
            for kind, spec in schema["kinds"].items() if kind not in REMOTE_EXCLUDED_KINDS}


def _kinds_doc() -> tuple[list[str], str]:
    try:
        kinds = remote_proposal_kinds()
    except (OSError, ValueError, KeyError):
        return [], ""
    return sorted(kinds), "; ".join(f"{k}: {', '.join(v)}" for k, v in sorted(kinds.items()))


_KINDS, _KINDS_DOC = _kinds_doc()
_REF = {"type": "string", "pattern": REF_RE.pattern}

TOOLS = [
    {"name": "brain_search",
     "description": (
         "Find records by lexical (BM25 keyword) relevance or exact text. "
         "scope: canonical (governed archive side: canonical records, source "
         "records, derivatives; each hit keeps its own tier), captures "
         "(unreviewed intake), or all (two separate groups, never one merged "
         "ranking). Results are navigation only: open a hit with brain_read "
         "before relying on it. Lexical search is not semantic: reformulate "
         "with the names, terms and spellings the records would use, try "
         "Persian/English variants and synonyms, and retry before concluding "
         "something is absent. mode=exact finds a literal phrase."),
     "inputSchema": {"type": "object", "additionalProperties": False,
         "properties": {
             "query": {"type": "string", "minLength": 1},
             "mode": {"type": "string", "enum": list(SEARCH_MODES), "default": "lexical"},
             "scope": {"type": "string", "enum": list(SEARCH_SCOPES), "default": "canonical"},
             "n": {"type": "integer", "minimum": 1, "maximum": 50}},
         "required": ["query"]}},
    {"name": "brain_read",
     "description": (
         "Read one item by its ref as returned by brain_search or "
         "brain_reconcile_context: rec:<id> record, doc:<hash> document "
         "without id (e.g. extracted derivative), cap:<id> capture, "
         "prop:<id> proposal, hist:<name> genesis/handoff. File paths are "
         "never accepted."),
     "inputSchema": {"type": "object", "additionalProperties": False,
         "properties": {"ref": _REF}, "required": ["ref"]}},
    {"name": "brain_capture",
     "description": (
         "Store the user's substantive text verbatim as an unreviewed, "
         "noncanonical capture (level 7). Text only. Do not summarize, title "
         "or interpret in the text. Report success only when the result says "
         "ok and persisted; say 'not yet committed' when commit_state is "
         "uncommitted."),
     "inputSchema": {"type": "object", "additionalProperties": False,
         "properties": {
             "text": {"type": "string", "minLength": 1},
             "language_hint": {"type": "string", "enum": list(LANG_HINTS), "default": "unknown"}},
         "required": ["text"]}},
    {"name": "brain_ingest_file",
     "description": (
         "Add a user-supplied source document to the archive with its EXACT original "
         "bytes: the original is held immutably with its SHA-256, a source record "
         "(pending registration) is created, and a deterministic text derivative is "
         "extracted where possible (pdf, docx, pptx, xlsx, html, epub, txt, md). "
         "The file must first be uploaded by the owner through the archive's upload "
         "page, which returns an upload_ref; you cannot pass file contents here. Never "
         "substitute a summary or your own transcription for the file. Report the "
         "receipt fields (source_ref, sha256, extraction, commit_state) as returned."),
     "inputSchema": {"type": "object", "additionalProperties": False,
         "properties": {
             "upload_ref": {"type": "string", "pattern": "^upl-[A-Za-z0-9_-]{43}$"},
             "title": {"type": "string", "maxLength": 200},
             "description": {"type": "string", "maxLength": 2000}},
         "required": ["upload_ref"]}},
    {"name": "brain_reconcile_context",
     "description": (
         "Read-only evidence package around a seed ref and/or a query, for "
         "reconciling new material with prior work. focused = the seed's direct "
         "neighborhood in one page; deep = multi-hop and paged: follow "
         "next_cursor until null, compare each section's total with what you "
         "have, and check truncated/truncation_reason. It returns records and "
         "reasons, never a verdict: judging conflict, supersession or relation "
         "type is your job, stated as a candidate reading."),
     "inputSchema": {"type": "object", "additionalProperties": False,
         "properties": {
             "seed_ref": _REF,
             "query": {"type": "string"},
             "depth": {"type": "string", "enum": list(DEPTHS), "default": "focused"},
             "cursor": {"type": "string"},
             "sections": {"type": "array", "items": {"type": "string",
                                                     "enum": list(RECONCILE_SECTIONS)}},
             "expand": {"type": "array", "items": _REF}},
         "required": []}},
    {"name": "brain_propose",
     "description": (
         "Submit a structured candidate proposal for later human adjudication. "
         "structured_fields per kind (" + _KINDS_DOC + "). evidence_refs: "
         "at least one rec:/doc: ref with a verbatim quote (>= 20 characters, "
         "copied exactly from brain_read output); every quote given is checked "
         "and cap: refs can only support, never carry, the evidence. Proposals "
         "without checkable evidence are rejected with reasons and nothing is "
         "stored. A stored proposal is a candidate, not a change to the archive."),
     "inputSchema": {"type": "object", "additionalProperties": False,
         "properties": {
             "kind": {"type": "string", "enum": _KINDS},
             "structured_fields": {"type": "object",
                                   "additionalProperties": {"type": "string"}},
             "evidence_refs": {"type": "array", "minItems": 1, "maxItems": 100, "items": {
                 "type": "object", "additionalProperties": False,
                 "properties": {"ref": _REF,
                                "quote": {"type": "string", "minLength": 20}},
                 "required": ["ref"]}}},
         "required": ["kind", "structured_fields", "evidence_refs"]}},
    {"name": "brain_status",
     "description": (
         "Archive state: snapshot, counts, captures awaiting transcription/"
         "description/interpretation/review, pending proposals, validation and "
         "search-index state, and durable writes not yet committed to Git. "
         "Use it to answer 'is my capture safe?' and to detect a degraded system."),
     "inputSchema": {"type": "object", "additionalProperties": False,
                     "properties": {}, "required": []}},
]
TOOL_NAMES = tuple(t["name"] for t in TOOLS)
