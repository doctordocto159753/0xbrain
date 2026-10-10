"""Test projector for WP-C: builds SourceProjection/chunks the way WP-A will
(layers per CONTRACT-E1 section 2) into any ProjectionStore double, plus
helpers to annotate and mutate. TEST-ONLY: chunking uses support.naive_chunks
(fixed width, default wide enough that short texts are single-chunk like
chunk-1). Not a reference implementation of the projector."""
from __future__ import annotations

import itertools

from brain_enrichment import contract as C
from support import FakeProjectionStore, make_projection, naive_chunks

_seq = itertools.count(1)


def cap_ref(n: int | None = None) -> str:
    n = n if n is not None else next(_seq)
    return f"cap:cap-20261001-{n:06d}-{n % 65536:04x}"


class DerivedStore(FakeProjectionStore):
    """FakeProjectionStore whose list_current also returns the server-computed
    `derived` block and the annotation's review_state (what WP-A's store is
    expected to do). `review_state_of` lets a test mark a revision rejected."""

    def __init__(self):
        super().__init__()
        self.review_state_of: dict[tuple, str] = {}

    def change_marker(self):
        return super().change_marker() + len(self.review_state_of)       # a review moves the marker (CONTRACT-E1 s.6)

    def list_current(self, after, limit):
        out = super().list_current(after, limit)
        for it in out:
            ref = it["projection"].source_ref
            rev = it["annotation_revision"]
            if rev is not None:
                it["review_state"] = self.review_state_of.get((ref, rev), "unreviewed")
                cur = self._current_rev(ref, it["projection"].source_version)
                it["derived"] = cur.get("derived") if cur else None
        return out


class Corpus:
    def __init__(self, store=None, *, instance: str = "zb-instance", chunk_size: int = 4000):
        self.store = store if store is not None else FakeProjectionStore()
        self.instance = instance
        self.chunk_size = chunk_size
        self.proj: dict[str, C.SourceProjection] = {}
        self._keys = itertools.count(1)

    # -- projections
    def _put(self, projection, size=None):
        chunks = naive_chunks(projection, size or self.chunk_size)
        self.store.upsert_projection(projection, chunks)
        self.proj[projection.source_ref] = projection
        return projection

    def capture(self, text: str, ref: str | None = None, *, size=None, flags=()):
        ref = ref or cap_ref()
        return self._put(make_projection(ref, [("user-text", ref, text, {})], instance=self.instance,
                                         kind="capture-text", tier="capture", level=7, flags=flags), size)

    def canonical(self, rid: str, text: str, *, size=None, level: int = 4):
        ref = f"rec:{rid}"
        return self._put(make_projection(ref, [("record-text", ref, text, {})], instance=self.instance,
                                         kind="canonical-record", tier="canonical-record", level=level), size)

    def ingested(self, rid: str, text: str, *, doc: str | None = None, size=None, flags=(),
                 original_sha: str | None = None):
        """One holding: anchor rec:<rid> (source-record, level 4); the single
        layer is the DERIVATIVE text (tier derivative, level 5)."""
        ref = f"rec:{rid}"
        doc = doc or "doc:" + C.sha256_text("doc" + rid)[:20]
        proj = make_projection(
            ref, [("derivative-text", doc, text,
                   {"tier": "derivative", "authority_level": 5, "literal": True,
                    "provenance_sha256": original_sha or C.sha256_text("orig" + rid),
                    "origin": "derivative:02-sources/text"})],
            instance=self.instance, kind="ingested-file", tier="source-record", level=4, flags=flags)
        return self._put(proj, size)

    def no_text(self, rid: str):
        ref = f"rec:{rid}"
        return self._put(make_projection(ref, [], instance=self.instance, kind="source-record",
                                         tier="source-record", level=4, flags=("no_text", "metadata_only")))

    def replace_text(self, source_ref: str, text: str, *, size=None):
        old = self.proj[source_ref]
        layer = old.layers[0]
        extra = {"tier": layer.tier, "authority_level": layer.authority_level, "origin": layer.origin,
                 "provenance_sha256": layer.provenance_sha256}
        new = make_projection(source_ref, [(layer.layer_id, layer.text_ref, text, extra)],
                              instance=self.instance, kind=old.source_kind, tier=old.source_tier,
                              level=old.source_authority_level, flags=old.quality_flags)
        return self._put(new, size)

    def delete(self, source_ref: str):
        self.store.projections.pop(source_ref, None)
        self.store.chunks.pop(source_ref, None)
        self.proj.pop(source_ref, None)

    # -- annotation
    def chunks(self, source_ref):
        return self.store.chunks[source_ref]

    def annotate(self, source_ref: str, annotation: dict, *, derived: dict | None = None) -> int:
        """`annotation` may use the placeholder "FIRST" / "LAST" as a chunk_ref."""
        chs = self.store.chunks[source_ref]
        sub = {"FIRST": chs[0].chunk_ref, "LAST": chs[-1].chunk_ref}

        def fix(o):
            if isinstance(o, dict):
                return {k: fix(v) for k, v in o.items()}
            if isinstance(o, list):
                return [fix(v) for v in o]
            return sub.get(o, o) if isinstance(o, str) else o

        ann = fix(annotation)
        proj = self.store.projections[source_ref]
        cur = self.store._current_rev(source_ref)
        n = cur["revision"] if cur else 0
        req = {"source_ref": source_ref, "source_version": proj.source_version,
               "expected_annotation_revision": n, "idempotency_key": f"idem-{next(self._keys):06d}-x",
               "annotation": ann}
        if derived is not None:
            req["derived"] = fix(derived)
        return self.store.append_annotation(req).annotation_revision
