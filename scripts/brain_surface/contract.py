"""Frozen K3 tool schemas and shared constants (single source of truth)."""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROPOSAL_SCHEMA = ROOT / "00-system" / "policies" / "proposal_schema.json"

SEARCH_MODES = ("lexical", "exact")
SEARCH_SCOPES = ("canonical", "captures", "all")
DEPTHS = ("focused", "deep")
LANG_HINTS = ("fa", "en", "mixed", "unknown")
CAPTURE_CHANNEL = "mcp"  # fixed server-side; never caller-supplied

# Ref grammar: opaque, kind-prefixed, no separators. A caller can never
# express a filesystem path: "/", "\\", "..", "." and extensions cannot match.
#   rec  canonical record id      cap  capture id
#   prop proposal id              hist genesis / handoff document stem
REF_RE = re.compile(r"^(rec|cap|prop|hist):[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

RECONCILE_SECTIONS = (
    "canonical_records", "claims", "relations", "source_records", "captures",
    "superseded", "open_proposals", "unresolved", "chronology",
)

# Interpretation of SYSTEM_DESIGN.md section 2 (8 levels, 1-indexed so that
# "model output = level 7" holds). Adapter assumption, see integration notes.
LEVEL_BY_ZONE = {
    "02-sources": 4, "03-objects": 4, "05-claims": 4, "06-relations": 4,
    "04-notes": 6, "01-inbox/captures": 7, "_proposals": 7,
    "07-genesis": 4, "_captures": 7,
}
CANDIDATE_LEVEL = 7

AUTHORITY_NOTE = (
    "Retrieval and inclusion order organize attention; they are never "
    "evidence. Tool output is a route to records, not a verdict. Anything "
    "a model derives from it is candidate-tier (level 7).")
CAPTURE_NOTE = (
    "Captures are noncanonical level-7 intake. They record what was said, "
    "not what is true, and are never promoted from here.")


def remote_proposal_kinds(schema_path: Path = PROPOSAL_SCHEMA) -> dict:
    """Kinds exposed remotely, derived from the shipped schema.

    intake-registration is excluded: its evidence is a filesystem path plus
    a hash of an original, which a remote caller can neither supply nor be
    allowed to name (K3: no arbitrary paths).
    """
    schema = json.loads(schema_path.read_text(encoding="utf-8-sig"))
    out = {}
    for kind, spec in schema["kinds"].items():
        if kind == "intake-registration":
            continue
        req = [f for f in spec["required"] if f != "source_passage"]
        out[kind] = {"required": req,
                     "extra": {k: v for k, v in spec.items()
                               if k not in ("required", "_desc")}}
    return out


def _proposal_kinds_enum() -> list[str]:
    try:
        return sorted(remote_proposal_kinds())
    except (OSError, ValueError, KeyError):
        return []


TOOLS = [
    {"name": "brain_search",
     "description": (
         "Find records in the archive by lexical relevance or exact text. "
         "scope: canonical (accepted records), captures (unreviewed intake), "
         "or all (two separate groups, never one merged ranking). Results are "
         "navigation only: open the record with brain_read before relying on "
         "it. Lexical search is keyword-based, not semantic: reformulate with "
         "the exact names, terms and spellings the records would use, and "
         "retry with alternates (also Persian/English variants) before "
         "concluding something is absent."),
     "inputSchema": {"type": "object", "additionalProperties": False,
         "properties": {
             "query": {"type": "string", "minLength": 1},
             "mode": {"type": "string", "enum": list(SEARCH_MODES),
                      "default": "lexical"},
             "scope": {"type": "string", "enum": list(SEARCH_SCOPES),
                       "default": "canonical"},
             "n": {"type": "integer", "minimum": 1, "maximum": 50}},
         "required": ["query"]}},
    {"name": "brain_read",
     "description": (
         "Read one record by its ref (as returned by brain_search or "
         "brain_reconcile_context). Refs are opaque identifiers; file paths "
         "are never accepted."),
     "inputSchema": {"type": "object", "additionalProperties": False,
         "properties": {"ref": {"type": "string", "pattern": REF_RE.pattern}},
         "required": ["ref"]}},
    {"name": "brain_capture",
     "description": (
         "Store durable material verbatim as an unreviewed noncanonical "
         "capture (level 7). Text only. Do not summarize, title or interpret "
         "in the text. Report success only if the result says ok and "
         "persisted."),
     "inputSchema": {"type": "object", "additionalProperties": False,
         "properties": {
             "text": {"type": "string", "minLength": 1},
             "language_hint": {"type": "string", "enum": list(LANG_HINTS),
                               "default": "unknown"}},
         "required": ["text"]}},
    {"name": "brain_reconcile_context",
     "description": (
         "Read-only context package around a seed record or query, for "
         "reconciling new material with prior work. focused = the seed's "
         "direct neighborhood; deep = multi-hop, pageable (follow "
         "next_cursor until null; check total vs returned and truncated). "
         "It returns records and reasons, never a verdict: judging conflict, "
         "supersession or relation type is your job, stated as candidate."),
     "inputSchema": {"type": "object", "additionalProperties": False,
         "properties": {
             "seed_ref": {"type": "string", "pattern": REF_RE.pattern},
             "query": {"type": "string"},
             "depth": {"type": "string", "enum": list(DEPTHS),
                       "default": "focused"},
             "cursor": {"type": "string"},
             "sections": {"type": "array",
                          "items": {"type": "string",
                                    "enum": list(RECONCILE_SECTIONS)}},
             "expand": {"type": "array",
                        "items": {"type": "string", "pattern": REF_RE.pattern}}},
         "required": []}},
    {"name": "brain_propose",
     "description": (
         "Submit a structured candidate proposal (relation, claim amendment, "
         "correction, ...) for later human adjudication. evidence_refs must "
         "cite canonical records with a verbatim quote of at least 20 "
         "characters; proposals without checkable evidence are rejected "
         "with reasons and nothing is stored. A stored proposal is a "
         "candidate, not a change to the archive."),
     "inputSchema": {"type": "object", "additionalProperties": False,
         "properties": {
             "kind": {"type": "string", "enum": _proposal_kinds_enum()},
             "structured_fields": {"type": "object"},
             "evidence_refs": {"type": "array", "minItems": 1, "items": {
                 "type": "object", "additionalProperties": False,
                 "properties": {"ref": {"type": "string",
                                        "pattern": REF_RE.pattern},
                                "quote": {"type": "string", "minLength": 20}},
                 "required": ["ref", "quote"]}}},
         "required": ["kind", "structured_fields", "evidence_refs"]}},
    {"name": "brain_status",
     "description": (
         "Archive state: snapshot, counts, captures awaiting transcription/"
         "description/interpretation, proposals, validator and search-index "
         "state, and uncommitted noncanonical writes. Use it to answer "
         "'is my capture safe?' and to detect a degraded system."),
     "inputSchema": {"type": "object", "additionalProperties": False,
                     "properties": {}, "required": []}},
]
TOOL_NAMES = tuple(t["name"] for t in TOOLS)
