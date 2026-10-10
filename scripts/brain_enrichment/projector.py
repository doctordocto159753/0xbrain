"""Glue between the reader, the chunker and the store: load -> chunk -> upsert.

`project_source` is the one place that decides how a failure is recorded:
a parse/validation failure is *stable* (needs an explicit reprocess after
the file is fixed), an unavailable/internal failure is *transient* (retried
automatically, at most twice, with spacing). Nothing is repaired or skipped
silently: a source that cannot be projected is stored as `failed` with a
ref-level message.
"""
from __future__ import annotations

from . import contract as C

TRANSIENT_CODES = frozenset({"E_UNAVAILABLE", "E_INTERNAL"})


def is_transient(code: str) -> bool:
    return code in TRANSIENT_CODES


def project_source(store, reader, chunker, policy, source_ref: str):
    """Project one source into `store`.

    Returns `(outcome, receipt_or_error)` where outcome is one of
    `"projected"` (receipt: ProjectionReceipt with chunks), `"no_text"`
    (receipt), `"failed"` (the EnrichmentError that was recorded) or
    `"gone"` (the source no longer exists; it was marked missing)."""
    try:
        projection = reader.load(source_ref)
    except C.EnrichmentError as exc:
        if exc.code == "E_NOT_FOUND":
            store.mark_missing(source_ref)
            return "gone", exc
        store.record_failure(source_ref, exc)
        return "failed", exc
    except Exception as exc:  # noqa: BLE001 - a reader bug must not abort the pass
        err = C.EnrichmentError("E_INTERNAL", f"{source_ref}: reader failed ({exc.__class__.__name__})",
                                source_ref=source_ref)
        store.record_failure(source_ref, err)
        return "failed", err
    try:
        chunks = chunker.build(projection, policy)
    except Exception as exc:  # noqa: BLE001 - a chunker defect must not abort the pass
        err = C.EnrichmentError("E_INTERNAL", f"{source_ref}: chunking failed ({exc.__class__.__name__})",
                                source_ref=source_ref)
        store.record_failure(source_ref, err)
        return "failed", err
    try:
        receipt = store.upsert_projection(projection, chunks)
    except C.EnrichmentError as exc:
        if exc.code != "E_VALIDATION":      # store trouble is not the source's fault
            raise
        store.record_failure(source_ref, exc)
        return "failed", exc
    return ("no_text" if receipt.state == "no_text" else "projected"), receipt
