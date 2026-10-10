"""Chunk-ref resolver helpers (CONTRACT-E1 sections 3 and 9).

A `chk:` ref is valid only in `brain_artifact_read` and `brain_retrieve`
output; it never enters the base `brain_read`/`brain_propose` grammar.
"""
from __future__ import annotations

import re

from . import contract as C

SOURCE_REF_RE = re.compile(r"^(rec|doc|cap):[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def resolve_chunk_ref(store, source_ref: str, chunk_ref: str) -> C.Chunk:
    """The current chunk `chunk_ref` of `source_ref`.

    * malformed ref, or a chunk that belongs to a different source -> E_BAD_REF
    * the chunk's source changed since the ref was issued          -> E_STALE_SOURCE
    * unknown chunk, or its source was deleted                     -> E_NOT_FOUND
    Stale text is never returned as current evidence.
    """
    if not isinstance(source_ref, str) or not SOURCE_REF_RE.match(source_ref):
        raise C.EnrichmentError("E_BAD_REF", "source_ref is not a valid public ref")
    if not isinstance(chunk_ref, str) or not C.CHUNK_REF_RE.match(chunk_ref):
        raise C.EnrichmentError("E_BAD_REF", "chunk_ref must look like chk:<32 hex>")
    try:
        chunk = store.get_chunk(chunk_ref)
    except C.EnrichmentError as exc:
        owner = exc.extra.get("source_ref")
        if exc.code == "E_STALE_SOURCE" and owner is not None and owner != source_ref:
            raise C.EnrichmentError("E_BAD_REF", "chunk_ref does not belong to this source_ref") from None
        raise
    if chunk.source_ref != source_ref:
        raise C.EnrichmentError("E_BAD_REF", "chunk_ref does not belong to this source_ref")
    return chunk


def chunk_neighbours(store, chunk: C.Chunk) -> tuple[C.Chunk | None, C.Chunk | None]:
    """(previous, next) chunks within the same layer, resolved through the store
    (each neighbour is a current chunk of the same source version)."""
    out = []
    for ref in (chunk.prev_chunk_ref, chunk.next_chunk_ref):
        out.append(resolve_chunk_ref(store, chunk.source_ref, ref) if ref else None)
    return out[0], out[1]
