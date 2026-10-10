# Request C-1: `list_current` should return the `derived` block and the review state

From: WP-C (indexing/graph/retrieval). To: AGENT-0 (store interface) and WP-A (real store).
Status: not blocking. WP-C works without it, but only with a recomputation.

## Why

CONTRACT-E1 section 5 says the store persists a server-computed `derived` block per
annotation revision (entities with `entity_id`, links with `edge_kind`, and server-resolved
`start_char/end_char/occurrences` for mentions and evidence). The `ProjectionStore.list_current`
docstring only promises `annotation`, `annotation_revision`, `review_state`. WP-C builds the
graph edges from `derived` when it is present and otherwise **recomputes** the same shape from the
validated annotation (`graph.derive_from_annotation`): same entity ids and edge kinds, but no
spans (`start_char/end_char` are `None`).

## Proposed contract text (list_current item)

```
{"projection": SourceProjection, "chunks": list[Chunk],
 "annotation": dict|None,              # current revision, validated copy
 "annotation_revision": int|None,
 "annotation_source_version": str|None,# optional; must equal projection.source_version
 "derived": dict|None,                 # the stored derived block of that revision
 "review_state": "unreviewed"|"confirmed"|"rejected",   # of THAT revision
 "processing_state": str}
```

* `list_current(after, limit)` returns items ordered by `source_ref` ascending, only those with
  `source_ref > after`, at most `limit` (WP-C pages with `after = last source_ref`).
* `review_state` must be that of the current revision. WP-C then indexes no metadata and no
  candidate edge for a `rejected` revision; the text stays searchable.

## Exact patch for `tests/enrichment/support.py` (FakeProjectionStore.list_current)

```diff
             cur = self._current_rev(r, p.source_version)
+            decisions = [x for x in self.reviews if x["source_ref"] == r and cur
+                         and x["annotation_revision"] == cur["revision"]]
             out.append({"projection": p, "chunks": list(self.chunks.get(r, [])),
                         "annotation": cur["annotation"] if cur else None,
                         "annotation_revision": cur["revision"] if cur else None,
-                        "review_state": "unreviewed",
+                        "derived": cur.get("derived") if cur else None,
+                        "review_state": decisions[-1]["decision"] if decisions else "unreviewed",
                         "processing_state": "annotated" if cur else p.processing_state})
```

WP-C tests use their own `retrieval_support.DerivedStore` that does exactly this, so the patch
only removes the need for a subclass.

# Request C-2: `FakeProjectionStore.change_marker` must move on every write

`change_marker()` in the fake is `len(projections) + len(revisions) + len(reviews)`.
Re-projecting an existing `source_ref` with new text (`upsert_projection` with a new
`source_version`) leaves it unchanged, which the contract forbids ("moves on any
projection/annotation/review write"). Consequences in the fake only: `IndexBuilder.status()`
caches its lag per marker and so does not see that write, and
`refresh(trust_marker=True)` would skip it. (WP-C's `refresh` defaults to `trust_marker=False`
and per-candidate freshness catches the stale chunk, so nothing is served stale.)

```diff
     def __init__(self):
+        self._marker = 0
         ...
     def upsert_projection(self, projection, chunks):
         ...
+        self._marker += 1
     def append_annotation(self, request):
         ...  # after the revision is stored (not on idempotent replay)
+        self._marker += 1
     def record_review(self, request, human_context):
         ...
+        self._marker += 1
     def change_marker(self):
-        return len(self.projections) + sum(len(v) for v in self.revisions.values()) + len(self.reviews)
+        return self._marker
```
