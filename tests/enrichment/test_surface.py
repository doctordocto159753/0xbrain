"""WP-D: the extension tool surface (CONTRACT-E1 section 9), against injected
fakes of the WP-A/B/C services and the REAL base surface on `enrichment_wiki`.

Also home of the small fakes (`RichStore`, `FakeAnnotation`, ...) that
test_review.py and test_cli.py import. Nothing here needs a landed WP-A/B/C
module; test_e2e_extension.py is the real-stack counterpart."""
from __future__ import annotations

import ast
import copy
import json
import sqlite3
from pathlib import Path

import pytest

from brain_enrichment import contract as C
from brain_enrichment import tools_spec as T
from brain_enrichment.config import EnrichmentConfig
from brain_enrichment.surface import Services, build_extension, tool_specs
from brain_surface import TOOLS as BASE_TOOLS, TOOL_NAMES as BASE_NAMES, BrainSurface
from brain_surface.backend import WikiBackend
from support import FakeProjectionStore, corpus_hashes, make_projection, naive_chunks

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
ON = EnrichmentConfig(mode="on", writes=True, retrieval=True, batch_size=1)
ON_NO_WRITES = EnrichmentConfig(mode="on", writes=False, retrieval=True)
KEY = "key-00000001"


# ------------------------------------------------------------------- fakes
class RichStore(FakeProjectionStore):
    """FakeProjectionStore + what the real store provides: current revision in
    get(), review state, retired (stale) chunk refs, contract-shaped pending
    items and a scriptable discovery report."""

    def __init__(self):
        super().__init__()
        self.retired: dict[str, C.Chunk] = {}
        self.pending_calls: list[tuple] = []
        self.pending_error: C.EnrichmentError | None = None
        self.discovery_script: list[dict] = []

    def upsert_projection(self, projection, chunks):
        prev = self.projections.get(projection.source_ref)
        if prev is not None and prev.source_version != projection.source_version:
            for c in self.chunks.get(projection.source_ref, []):
                self.retired[c.chunk_ref] = c
        return super().upsert_projection(projection, chunks)

    def get_chunk(self, chunk_ref):
        if chunk_ref in self.retired:
            raise C.EnrichmentError("E_STALE_SOURCE", "chunk belongs to a superseded version")
        return super().get_chunk(chunk_ref)

    def _review_of(self, ref, cur):
        mine = [r for r in self.reviews if r["source_ref"] == ref and cur and r["annotation_revision"] == cur["revision"]]
        return ({"state": mine[-1]["decision"], "revision": mine[-1]["annotation_revision"]}
                if mine else {"state": "unreviewed", "revision": None})

    def get(self, source_ref, source_version=None):
        view = super().get(source_ref, source_version)
        proj = self.projections[source_ref]
        cur = self._current_rev(source_ref, proj.source_version)
        old_only = cur is None and bool(self.revisions.get(source_ref))
        pub = dict(view.projection)
        pub["current_annotation_revision"] = cur["revision"] if cur else 0
        if cur:
            pub["processing_state"] = "annotated"
        elif old_only:
            pub["processing_state"] = "stale"
        return C.ArtifactView(pub, view.annotation, view.coverage, self._review_of(source_ref, cur),
                              view.chunk_count, old_only)

    def list_current(self, after, limit):
        rows = super().list_current(after, limit)
        for r in rows:
            view = self.get(r["projection"].source_ref)
            r["review_state"] = view.review["state"]
            r["processing_state"] = view.projection["processing_state"]
        return rows

    def _item(self, ref):
        proj = self.projections[ref]
        view = self.get(ref)
        chunks = self.chunks[ref][:C.PAGE_MAX_CHUNKS]
        return {"source_ref": ref, "source_version": proj.source_version, "artifact_id": proj.artifact_id,
                "source_kind": proj.source_kind, "source_tier": proj.source_tier, "origin_channel": None,
                "layers": view.projection["layers"], "quality_flags": [], "coverage_warnings": [],
                "current_annotation_revision": 0, "expected_annotation_revision": 0,
                "coverage": dict(view.coverage), "existing_annotation": None,
                "page": {"chunks": [{**c.to_public(True), "annotated": False} for c in chunks], "offset": 0,
                         "returned": len(chunks), "total": len(self.chunks[ref]),
                         "truncated": len(self.chunks[ref]) > len(chunks)}}

    def pending(self, cursor, limit, retry_failed=False):
        self.pending_calls.append((cursor, limit, retry_failed))
        if self.pending_error:
            raise self.pending_error
        todo = [r for r, p in sorted(self.projections.items())
                if p.layers and self._current_rev(r, p.source_version) is None]
        off = C.decode_cursor(cursor, "pending")["o"] if cursor else 0
        sel = todo[off:off + limit]
        nxt = C.encode_cursor("pending", o=off + len(sel)) if off + len(sel) < len(todo) else None
        disc = (self.discovery_script.pop(0) if self.discovery_script else
                {"pass": 1, "inventory_generation": 1, "scanned": 0, "projected": 0, "failed": 0, "no_text": 0})
        return C.PendingPage(tuple(self._item(r) for r in sel), len(todo), len(sel), nxt is not None, nxt, disc)


class FakeAnnotation:
    """AnnotationService double: rejects payloads containing 'reject-me' (as a
    validator would), otherwise appends through the store like the real one."""

    def __init__(self, store):
        self.store, self.requests = store, []

    def submit(self, request):
        self.requests.append(copy.deepcopy(request))
        if "reject-me" in json.dumps(request["annotation"]):
            raise C.EnrichmentError("E_VALIDATION", "annotation rejected", reasons=["tags[0]: forged"])
        return self.store.append_annotation({**request, "derived": {"entities": [], "links": []}})


class FakeRetrieval:
    def __init__(self, result=None, error=None):
        self.requests, self.result, self.error = [], result, error

    def retrieve(self, request):
        self.requests.append(dict(request))
        if self.error:
            raise self.error
        return self.result or {
            "ok": True, "query": request["query"], "scope": request["scope"], "limit": request["limit"],
            "path": "enrichment", "uses_candidate_annotations": False,
            "groups": {"canonical": {"count": 0, "results": []}, "captures": {"count": 0, "results": []}},
            "truncated": False, "next_cursor": None}


class FakeIndex:
    def __init__(self):
        self.calls = []

    def refresh(self, batch_limit):
        self.calls.append(batch_limit)
        return C.IndexReceipt(generation=1, activated=True, artifacts_indexed=1, artifacts_pending=0)


class FakeReader:
    """SourceReader double: `live` maps source_ref -> SourceProjection as it is on disk now."""

    def __init__(self, live=None):
        self.live = dict(live or {})

    def load(self, source_ref):
        if source_ref not in self.live:
            raise C.EnrichmentError("E_NOT_FOUND", f"{source_ref} no longer exists")
        return self.live[source_ref]


def load_source(store, w, name, size=60):
    """Project one seeded source into the fake store (naive fixed-width chunks)."""
    if name == "held_ok":
        ref = w.refs["held_ok"]
        proj = make_projection(ref, [("derivative-text", w.refs["held_ok_text"], w.texts["held_ok_text"],
                                      {"tier": "derivative", "authority_level": 5})],
                               kind="ingested-file", tier="source-record", level=4)
    else:
        ref = w.refs[name]
        proj = make_projection(ref, [("user-text", ref, w.texts[name], None)])
    chunks = naive_chunks(proj, size)
    store.upsert_projection(proj, chunks)
    return proj, chunks


def annotate(store, proj, chunks, *, key=KEY, expected=0, tags=("a",), chunk=False):
    ann = {"tags": list(tags)}
    if chunk:
        ann["chunk_annotations"] = [{"chunk_ref": chunks[0].chunk_ref, "summary": "first piece"}]
    return store.append_annotation({"source_ref": proj.source_ref, "source_version": proj.source_version,
                                    "expected_annotation_revision": expected, "idempotency_key": key,
                                    "annotation": ann})


class Rig:
    """store + services + extension over one enrichment_wiki."""

    def __init__(self, w, *, config=ON, legacy="auto", reader=None, importer=None, with_retrieval=True):
        self.w, self.config = w, config
        self.store = RichStore()
        self.annotation = FakeAnnotation(self.store)
        self.retrieval = FakeRetrieval() if with_retrieval else None
        self.index = FakeIndex()
        self.reader = reader
        self.legacy = WikiBackend(w.root) if legacy == "auto" else legacy
        self.importer = importer
        self.ext = build_extension(w.root, config, store=self.store, annotation=self.annotation,
                                   retrieval=self.retrieval, index=self.index, legacy_backend=self.legacy,
                                   source_reader=reader, importer=importer)

    def services(self) -> Services:
        return self.ext.s


@pytest.fixture()
def rig(enrichment_wiki):
    return Rig(enrichment_wiki)


def walk(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, v
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)


def merged_tools(root, config, **services):
    """Test-side stand-in for the adapter: base seven + (when on) the four."""
    base = BrainSurface(WikiBackend(root))
    names = [t["name"] for t in base.tools()]
    ext = build_extension(root, config, **services)
    if ext is not None:
        names += [name for name, *_ in tool_specs(ext)]
    return names


# ------------------------------------------------------- tool list / dispatch
def test_off_leaves_exactly_the_seven_base_tools_untouched(enrichment_wiki):
    snapshot = json.dumps(BASE_TOOLS, sort_keys=True).encode()
    names0 = tuple(BASE_NAMES)
    for cfg in (EnrichmentConfig(), EnrichmentConfig(mode="shadow", writes=True, retrieval=True)):
        assert build_extension(enrichment_wiki.root, cfg) is None
        assert tuple(merged_tools(enrichment_wiki.root, cfg)) == names0
    assert len(names0) == 7
    assert json.dumps(BASE_TOOLS, sort_keys=True).encode() == snapshot
    assert tuple(BASE_NAMES) == names0


def test_on_adds_exactly_the_four_tools_after_the_seven(rig):
    names = merged_tools(rig.w.root, ON, store=rig.store, annotation=rig.annotation)
    assert tuple(names[:7]) == tuple(BASE_NAMES) and tuple(names[7:]) == T.EXTENSION_TOOL_NAMES
    assert len(names) == 11 and len(set(names)) == 11
    specs = tool_specs(rig.ext)
    assert [s[0] for s in specs] == list(T.EXTENSION_TOOL_NAMES)
    for (name, desc, schema, handler), frozen in zip(specs, T.EXTENSION_TOOLS):
        assert desc == frozen["description"] and schema is frozen["inputSchema"] and callable(handler)
    assert json.dumps(BASE_TOOLS, sort_keys=True).encode() == json.dumps(
        copy.deepcopy(BASE_TOOLS), sort_keys=True).encode()


def test_handler_returns_plain_dict_for_the_transport(rig):
    handler = {n: h for n, _, _, h in tool_specs(rig.ext)}["brain_enrichment_pending"]
    out = handler({})
    assert isinstance(out, dict) and out["ok"] is True
    assert handler({"nope": 1})["ok"] is False


@pytest.mark.parametrize("tool,args", [
    ("brain_enrichment_pending", {"path": "x"}),
    ("brain_enrichment_submit", {"source_ref": "cap:cap-1", "actor": "owner"}),
    ("brain_artifact_read", {"source_ref": "cap:cap-1", "path": "01-inbox/x.md"}),
    ("brain_retrieve", {"query": "q", "channel": "telegram"}),
    ("brain_enrichment_pending", {"approve": True}),
    ("brain_artifact_read", {"source_ref": "cap:cap-1", "authority_level": 1}),
    ("brain_enrichment_submit", {"decision": "confirmed"}),
])
def test_unknown_arguments_are_rejected_with_accepted_list(rig, tool, args):
    out = rig.ext.call(tool, args)
    assert out["ok"] is False and out["error"]["code"] == "E_UNKNOWN_ARGUMENT"
    assert out["error"]["accepted"] == sorted(T.EXTENSION_TOOLS[T.EXTENSION_TOOL_NAMES.index(tool)]["inputSchema"]["properties"])
    assert (out.get("persisted") is False) == (tool == "brain_enrichment_submit")
    assert rig.annotation.requests == [] and rig.retrieval.requests == [] and rig.store.pending_calls == []


def test_unknown_tool_and_non_object_arguments_never_raise(rig):
    for name in ("brain_review", "brain_promote", "wiki_read", "", None, 5):
        out = rig.ext.call(name, {})
        assert out["ok"] is False and out["error"]["code"] == "E_UNKNOWN_ARGUMENT"
        assert out["error"]["accepted"] == list(T.EXTENSION_TOOL_NAMES)
    out = rig.ext.call("brain_retrieve", ["not", "an", "object"])
    assert out["error"]["code"] == "E_BAD_ARGUMENTS"
    assert rig.ext.call("brain_enrichment_pending", None)["ok"] is True       # None == {}


def test_no_tool_schema_or_name_allows_review_approve_or_promote():
    verbs = ("review", "approve", "promote", "accept", "adjudicate", "confirm", "reject")
    for t in T.EXTENSION_TOOLS:
        assert not any(v in t["name"] for v in verbs)
        keys = {k for k, _ in walk(t["inputSchema"])}
        for v in ("actor", "approve", "promote", "decision", "reviewed_by", "canonical_promotion", "review"):
            assert v not in keys, (t["name"], v)
        enums = [x for k, vals in walk(t["inputSchema"]) if k == "enum" for x in vals]
        assert not [x for x in enums if any(v in str(x) for v in verbs)], t["name"]
    for rel in ("surface.py",):
        tree = ast.parse((SCRIPTS / "brain_enrichment" / rel).read_text(encoding="utf-8"))
        imported = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        imported |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert not any("brain_review" in m or m.endswith("review") or m.endswith("cli") for m in imported)


# ---------------------------------------------------------------- pending
def test_pending_delegates_to_store_and_shapes_the_response(rig):
    for name in ("fa_short", "crlf", "held_ok"):
        load_source(rig.store, rig.w, name)
    out = rig.ext.call("brain_enrichment_pending", {"limit": 2, "retry_failed": True})
    assert out["ok"] is True and out["returned"] == 2 and out["total"] == 3 and out["truncated"] is True
    assert rig.store.pending_calls == [(None, 2, True)]
    item = out["items"][0]
    for key in ("source_ref", "source_version", "artifact_id", "source_kind", "source_tier", "layers",
                "current_annotation_revision", "expected_annotation_revision", "coverage", "page"):
        assert key in item
    assert set(out["discovery"]) >= {"pass", "inventory_generation", "scanned", "projected", "failed", "no_text"}
    assert out["authority_note"] == T.AUTHORITY_NOTE
    nxt = rig.ext.call("brain_enrichment_pending", {"limit": 2, "cursor": out["next_cursor"]})
    assert nxt["returned"] == 1 and nxt["next_cursor"] is None
    assert rig.store.pending_calls[-1][0] == out["next_cursor"] and rig.store.pending_calls[-1][1] == 2


def test_pending_default_limit_follows_batch_size_and_bounds_are_enforced(enrichment_wiki):
    rig = Rig(enrichment_wiki, config=EnrichmentConfig(mode="on", batch_size=3))
    rig.ext.call("brain_enrichment_pending", {})
    assert rig.store.pending_calls[-1][1] == 3
    for bad in (0, 6, -1, "2", 1.5, True, None.__class__):
        out = rig.ext.call("brain_enrichment_pending", {"limit": bad})
        assert out["error"]["code"] == "E_BAD_ARGUMENTS", bad
    assert rig.ext.call("brain_enrichment_pending", {"retry_failed": "yes"})["error"]["code"] == "E_BAD_ARGUMENTS"
    assert rig.ext.call("brain_enrichment_pending", {"cursor": ""})["error"]["code"] == "E_BAD_CURSOR"
    assert rig.ext.call("brain_enrichment_pending", {"cursor": "x" * 3000})["error"]["code"] == "E_BAD_CURSOR"


def test_pending_maps_store_errors_and_never_touches_the_corpus(rig):
    before = corpus_hashes(rig.w.root)
    load_source(rig.store, rig.w, "fa_short")
    assert rig.ext.call("brain_enrichment_pending", {"cursor": "bm90LWEtY3Vyc29y"})["error"]["code"] == "E_BAD_CURSOR"
    rig.store.pending_error = C.EnrichmentError("E_STALE_CURSOR", "source changed", source_ref="cap:cap-x")
    out = rig.ext.call("brain_enrichment_pending", {})
    assert out["error"]["code"] == "E_STALE_CURSOR" and out["error"]["source_ref"] == "cap:cap-x"
    assert "persisted" not in out                                       # read tool
    assert corpus_hashes(rig.w.root) == before


# ----------------------------------------------------------------- submit
def req(proj, **over):
    return {"source_ref": proj.source_ref, "source_version": proj.source_version,
            "expected_annotation_revision": 0, "idempotency_key": KEY,
            "annotation": {"tags": ["fitness"], "summary": "a plan"}, **over}


def test_submit_is_disabled_without_writes_and_stores_nothing(enrichment_wiki):
    rig = Rig(enrichment_wiki, config=ON_NO_WRITES)
    proj, _ = load_source(rig.store, rig.w, "fa_short")
    out = rig.ext.call("brain_enrichment_submit", req(proj))
    assert out["ok"] is False and out["error"]["code"] == "E_DISABLED" and out["persisted"] is False
    assert rig.annotation.requests == [] and rig.store.revisions == {}


def test_submit_success_reports_only_what_the_receipt_says(rig):
    proj, _ = load_source(rig.store, rig.w, "fa_short")
    out = rig.ext.call("brain_enrichment_submit", req(proj))
    assert out["ok"] is True and out["persisted"] is True and out["annotation_revision"] == 1
    assert out["idempotent_replay"] is False and out["index_dirty"] is True
    assert out["source_ref"] == proj.source_ref and out["source_version"] == proj.source_version
    assert out["processing_state"] == "annotated" and out["coverage"]["total_chunks"] >= 1
    assert out["authority_note"] == T.AUTHORITY_NOTE
    assert rig.annotation.requests[0]["annotation"] == {"tags": ["fitness"], "summary": "a plan"}
    replay = rig.ext.call("brain_enrichment_submit", req(proj))
    assert replay["ok"] and replay["idempotent_replay"] is True and replay["annotation_revision"] == 1
    assert len(rig.store.revisions[proj.source_ref]) == 1


@pytest.mark.parametrize("over,code", [
    ({"annotation": {"tags": ["reject-me"]}}, "E_VALIDATION"),
    ({"expected_annotation_revision": 3}, "E_CONFLICT"),
    ({"source_version": "0" * 64}, "E_STALE_SOURCE"),
    ({"source_ref": "cap:cap-20990101-000000-abcd"}, "E_NOT_FOUND"),
])
def test_submit_errors_carry_code_and_persisted_false(rig, over, code):
    proj, _ = load_source(rig.store, rig.w, "fa_short")
    out = rig.ext.call("brain_enrichment_submit", req(proj, **over))
    assert out["ok"] is False and out["error"]["code"] == code and out["persisted"] is False
    if code == "E_CONFLICT":
        assert out["error"]["current_revision"] == 0
    if code == "E_VALIDATION":
        assert out["error"]["reasons"] == ["tags[0]: forged"]
    assert rig.store.revisions == {}


def test_submit_idempotency_conflict_and_argument_shapes(rig):
    proj, _ = load_source(rig.store, rig.w, "fa_short")
    assert rig.ext.call("brain_enrichment_submit", req(proj))["ok"]
    out = rig.ext.call("brain_enrichment_submit", req(proj, annotation={"tags": ["other"]}))
    assert out["error"]["code"] == "E_IDEMPOTENCY_CONFLICT" and out["persisted"] is False
    bad = [({"source_ref": "03-objects/x.md"}, "E_BAD_REF"), ({"source_ref": "prop:abc"}, "E_BAD_REF"),
           ({"source_version": "xyz"}, "E_BAD_ARGUMENTS"), ({"expected_annotation_revision": -1}, "E_BAD_ARGUMENTS"),
           ({"expected_annotation_revision": True}, "E_BAD_ARGUMENTS"), ({"idempotency_key": "short"}, "E_BAD_ARGUMENTS"),
           ({"annotation": "tags"}, "E_BAD_ARGUMENTS"), ({"annotation": {"tags": [float("nan")]}}, "E_BAD_ARGUMENTS")]
    for over, code in bad:
        out = rig.ext.call("brain_enrichment_submit", req(proj, **over))
        assert out["error"]["code"] == code and out["persisted"] is False, over
    out = rig.ext.call("brain_enrichment_submit", {"source_ref": proj.source_ref})
    assert out["error"]["code"] == "E_BAD_ARGUMENTS" and out["persisted"] is False
    big = rig.ext.call("brain_enrichment_submit", req(proj, annotation={"summary": "x" * (ON.max_output_bytes + 1)}))
    assert big["error"]["code"] == "E_TOO_LARGE" and big["persisted"] is False
    assert len(rig.annotation.requests) == 2          # nothing malformed ever reached the service


# --------------------------------------------------------- brain_artifact_read
def test_artifact_read_shape_and_pagination_with_bound_cursor(rig):
    proj, chunks = load_source(rig.store, rig.w, "long", size=100)
    assert len(chunks) > C.PAGE_MAX_CHUNKS * 2
    annotate(rig.store, proj, chunks, chunk=True)
    seen, cursor, pages = [], None, 0
    while True:
        out = rig.ext.call("brain_artifact_read", {"source_ref": proj.source_ref, **({"cursor": cursor} if cursor else {})})
        assert out["ok"] is True
        if pages == 0:
            art = out["artifact"]
            assert art["source_ref"] == proj.source_ref and art["source_version"] == proj.source_version
            assert art["current_annotation_revision"] == 1 and out["annotation"]["tags"] == ["a"]
            assert out["coverage"] == {"annotated_chunks": 1, "total_chunks": len(chunks)}
            assert out["review"] == {"state": "unreviewed", "revision": None}
            assert "text" not in out["chunks"]["items"][0]
        assert out["chunks"]["total"] == len(chunks) and out["chunks"]["returned"] <= C.PAGE_MAX_CHUNKS
        seen += [c["chunk_ref"] for c in out["chunks"]["items"]]
        pages += 1
        cursor = out["chunks"]["next_cursor"]
        assert out["chunks"]["truncated"] is (cursor is not None)
        if cursor is None:
            break
    assert seen == [c.chunk_ref for c in chunks] and pages == -(-len(chunks) // C.PAGE_MAX_CHUNKS)
    payload = C.decode_cursor(out and rig.ext.call("brain_artifact_read", {"source_ref": proj.source_ref})["chunks"]["next_cursor"], "artifact")
    assert payload["s"] == proj.source_ref and payload["v"] == proj.source_version and payload["o"] == C.PAGE_MAX_CHUNKS


def test_artifact_cursor_is_bound_to_source_and_version(rig):
    proj, chunks = load_source(rig.store, rig.w, "long", size=100)
    other, _ = load_source(rig.store, rig.w, "fa_short")
    first = rig.ext.call("brain_artifact_read", {"source_ref": proj.source_ref})
    cur = first["chunks"]["next_cursor"]
    assert rig.ext.call("brain_artifact_read", {"source_ref": other.source_ref, "cursor": cur})["error"]["code"] == "E_BAD_CURSOR"
    assert rig.ext.call("brain_artifact_read", {"source_ref": proj.source_ref, "cursor": "!!!"})["error"]["code"] == "E_BAD_CURSOR"
    forged = C.encode_cursor("artifact", s=proj.source_ref, v=proj.source_version, o=10_000)
    assert rig.ext.call("brain_artifact_read", {"source_ref": proj.source_ref, "cursor": forged})["error"]["code"] == "E_BAD_CURSOR"
    wrong_kind = C.encode_cursor("pending", s=proj.source_ref, v=proj.source_version, o=1)
    assert rig.ext.call("brain_artifact_read", {"source_ref": proj.source_ref, "cursor": wrong_kind})["error"]["code"] == "E_BAD_CURSOR"
    new = make_projection(proj.source_ref, [("user-text", proj.source_ref, rig.w.texts["long"] + "edited\n", None)])
    rig.store.upsert_projection(new, naive_chunks(new, 100))
    out = rig.ext.call("brain_artifact_read", {"source_ref": proj.source_ref, "cursor": cur})
    assert out["error"]["code"] == "E_STALE_CURSOR"
    assert rig.ext.call("brain_artifact_read", {"source_ref": proj.source_ref})["ok"]       # restart works


def test_artifact_read_chunk_returns_exact_text_layer_and_neighbours(rig):
    proj, chunks = load_source(rig.store, rig.w, "crlf", size=25)
    assert len(chunks) >= 3 and "\r\n" in rig.w.texts["crlf"]
    annotate(rig.store, proj, chunks, chunk=True)
    mid = chunks[1]
    out = rig.ext.call("brain_artifact_read", {"source_ref": proj.source_ref, "chunk_ref": mid.chunk_ref})
    assert out["ok"] is True
    ch = out["chunk"]
    assert ch["text"] == rig.w.texts["crlf"][mid.start_char:mid.end_char] == mid.text
    assert (ch["start_char"], ch["end_char"], ch["chars"]) == (mid.start_char, mid.end_char, mid.end_char - mid.start_char)
    assert ch["prev_chunk_ref"] == chunks[0].chunk_ref and ch["next_chunk_ref"] == chunks[2].chunk_ref
    assert out["layer"]["layer_id"] == "user-text" and out["layer"]["text_sha256"] == C.sha256_text(rig.w.texts["crlf"])
    assert out["source"]["source_version"] == proj.source_version and out["source"]["tier"] == "capture"
    assert out["source"]["authority_level"] == 7
    assert out["annotation_for_chunk"] is None
    first = rig.ext.call("brain_artifact_read", {"source_ref": proj.source_ref, "chunk_ref": chunks[0].chunk_ref})
    assert first["annotation_for_chunk"] == {"chunk_ref": chunks[0].chunk_ref, "summary": "first piece"}
    assert first["chunk"]["prev_chunk_ref"] is None


def test_ingested_file_chunk_reports_derivative_tier_not_original(rig):
    proj, chunks = load_source(rig.store, rig.w, "held_ok", size=80)
    out = rig.ext.call("brain_artifact_read", {"source_ref": proj.source_ref, "chunk_ref": chunks[0].chunk_ref})
    assert out["source"]["tier"] == "derivative" and out["source"]["authority_level"] == 5
    assert out["source"]["source_tier"] == "source-record" and out["layer"]["text_ref"] == rig.w.refs["held_ok_text"]


def test_artifact_read_stale_other_source_unknown_and_bad_refs(rig):
    a, a_chunks = load_source(rig.store, rig.w, "fa_short", size=20)
    b, b_chunks = load_source(rig.store, rig.w, "dup_a", size=20)
    other = rig.ext.call("brain_artifact_read", {"source_ref": a.source_ref, "chunk_ref": b_chunks[0].chunk_ref})
    assert other["error"]["code"] == "E_BAD_REF" and "belong" in other["error"]["message"]
    new = make_projection(a.source_ref, [("user-text", a.source_ref, rig.w.texts["fa_short"] + "more\n", None)])
    rig.store.upsert_projection(new, naive_chunks(new, 20))
    stale = rig.ext.call("brain_artifact_read", {"source_ref": a.source_ref, "chunk_ref": a_chunks[0].chunk_ref})
    assert stale["error"]["code"] == "E_STALE_SOURCE" and "text" not in json.dumps(stale)
    fresh = rig.store.get_chunks(a.source_ref)[0]
    assert rig.ext.call("brain_artifact_read", {"source_ref": a.source_ref, "chunk_ref": fresh.chunk_ref})["ok"]
    ghost = rig.ext.call("brain_artifact_read", {"source_ref": a.source_ref, "chunk_ref": "chk:" + "0" * 32})
    assert ghost["error"]["code"] == "E_NOT_FOUND"
    assert rig.ext.call("brain_artifact_read", {"source_ref": "cap:cap-20990101-000000-abcd"})["error"]["code"] == "E_NOT_FOUND"
    for bad in ("03-objects/works/nil.md", "../x", "prop:abc", "hist:abc", "rec:", "doc:zzz", "", 5):
        assert rig.ext.call("brain_artifact_read", {"source_ref": bad})["error"]["code"] == "E_BAD_REF", bad
    for bad in ("chk:abc", "chk:" + "G" * 32, 7, ""):
        assert rig.ext.call("brain_artifact_read", {"source_ref": a.source_ref, "chunk_ref": bad})["error"]["code"] == "E_BAD_REF", bad
    mixed = rig.ext.call("brain_artifact_read", {"source_ref": a.source_ref, "chunk_ref": fresh.chunk_ref,
                                                 "cursor": C.encode_cursor("artifact", o=0)})
    assert mixed["error"]["code"] == "E_BAD_ARGUMENTS"
    assert rig.ext.call("brain_artifact_read", {})["error"]["code"] == "E_BAD_ARGUMENTS"


def test_source_drift_on_disk_is_never_served_as_current_evidence(enrichment_wiki):
    ref = enrichment_wiki.refs["fa_short"]
    reader = FakeReader()
    rig = Rig(enrichment_wiki, reader=reader)
    proj, chunks = load_source(rig.store, enrichment_wiki, "fa_short", size=20)
    reader.live[ref] = proj
    ok = rig.ext.call("brain_artifact_read", {"source_ref": ref, "chunk_ref": chunks[0].chunk_ref})
    assert ok["ok"] and ok["chunk"]["text"] == chunks[0].text and "notes" not in rig.ext.call("brain_artifact_read", {"source_ref": ref})
    reader.live[ref] = make_projection(ref, [("user-text", ref, "completely different text now\n", None)])
    drift = rig.ext.call("brain_artifact_read", {"source_ref": ref, "chunk_ref": chunks[0].chunk_ref})
    assert drift["error"]["code"] == "E_STALE_SOURCE"
    notes = rig.ext.call("brain_artifact_read", {"source_ref": ref})["notes"]
    assert "changed on disk" in notes[0]
    del reader.live[ref]
    gone = rig.ext.call("brain_artifact_read", {"source_ref": ref, "chunk_ref": chunks[0].chunk_ref})
    assert gone["error"]["code"] == "E_NOT_FOUND"
    assert "no longer exists" in rig.ext.call("brain_artifact_read", {"source_ref": ref})["notes"][0]


def test_integrity_mismatch_between_store_and_source_is_an_error_not_a_guess(enrichment_wiki):
    ref = enrichment_wiki.refs["fa_short"]
    reader = FakeReader()
    rig = Rig(enrichment_wiki, reader=reader)
    proj, chunks = load_source(rig.store, enrichment_wiki, "fa_short", size=20)
    reader.live[ref] = proj
    broken = C.Chunk(**{**chunks[0].__dict__, "text": "tampered store text"})
    rig.store.chunks[ref][0] = broken
    out = rig.ext.call("brain_artifact_read", {"source_ref": ref, "chunk_ref": broken.chunk_ref})
    assert out["error"]["code"] == "E_INTERNAL" and "tampered" not in json.dumps(out)


# ------------------------------------------------------------------- retrieve
def test_retrieve_delegates_with_defaults_and_adds_the_authority_note(rig):
    out = rig.ext.call("brain_retrieve", {"query": "  فیت پلن "})
    assert out["ok"] and out["path"] == "enrichment" and out["query_model_calls"] == 0
    assert out["authority_note"] == T.AUTHORITY_NOTE
    assert rig.retrieval.requests == [{"query": "فیت پلن", "scope": "all", "limit": 10, "budget_tokens": 6000, "cursor": None}]
    rig.ext.call("brain_retrieve", {"query": "x", "scope": "captures", "limit": 3, "budget_tokens": 800, "cursor": "abc"})
    assert rig.retrieval.requests[-1] == {"query": "x", "scope": "captures", "limit": 3, "budget_tokens": 800, "cursor": "abc"}
    for bad in ({"query": ""}, {"query": "  "}, {"query": 5}, {"query": "q", "scope": "everything"},
                {"query": "q", "limit": 0}, {"query": "q", "limit": 51}, {"query": "q", "limit": True},
                {"query": "q", "budget_tokens": 499}, {"query": "q", "budget_tokens": 32001},
                {"query": "q" * 1001}, {}):
        assert rig.ext.call("brain_retrieve", bad)["error"]["code"] == "E_BAD_ARGUMENTS", bad
    assert len(rig.retrieval.requests) == 2


def test_retrieve_engine_errors_other_than_unavailable_propagate(enrichment_wiki):
    rig = Rig(enrichment_wiki)
    rig.retrieval.error = C.EnrichmentError("E_STALE_CURSOR", "index generation changed")
    assert rig.ext.call("brain_retrieve", {"query": "q", "cursor": "abc"})["error"]["code"] == "E_STALE_CURSOR"
    rig.retrieval.error = RuntimeError(f"boom in {enrichment_wiki.root}")
    out = rig.ext.call("brain_retrieve", {"query": "q"})
    assert out["error"] == {"code": "E_INTERNAL", "message": "RuntimeError"}


def test_retrieval_disabled_falls_back_explicitly_to_the_base_search(enrichment_wiki):
    rig = Rig(enrichment_wiki, config=EnrichmentConfig(mode="on", retrieval=False))
    out = rig.ext.call("brain_retrieve", {"query": "Project Nil", "scope": "canonical"})
    assert out["ok"] is True and out["path"] in ("fallback-lexical", "fallback-exact")
    assert rig.retrieval.requests == []                                  # the engine was not consulted
    assert "BRAIN_ENRICHMENT_RETRIEVAL=0" in out["notes"][0]
    assert out["index"]["state"] == "missing" and out["index"]["fresh"] is False
    assert out["uses_candidate_annotations"] is False and out["query_model_calls"] == 0
    assert set(out["groups"]) == {"canonical"}
    hits = out["groups"]["canonical"]["results"]
    assert "rec:obj-nil" in [h["source_ref"] for h in hits]
    h = next(h for h in hits if h["source_ref"] == "rec:obj-nil")
    assert h["hit_level"] == "source" and h["chunk_ref"] is None and h["span"] is None
    assert h["inclusion_reason"]["paths"] == ["fallback"] and h["tier"] == "canonical-record"
    assert out["coverage"]["source_level_fallback_hits"] == out["groups"]["canonical"]["count"]
    assert any("brain_read" in n for n in out["notes"]) and out["authority_note"] == T.AUTHORITY_NOTE
    both = rig.ext.call("brain_retrieve", {"query": "فیت پلن"})
    assert set(both["groups"]) == {"canonical", "captures"}
    assert enrichment_wiki.refs["fa_short"] in [h["source_ref"] for h in both["groups"]["captures"]["results"]]


def test_retrieve_falls_back_when_the_engine_or_index_is_unavailable(enrichment_wiki, tmp_path):
    rig = Rig(enrichment_wiki)
    rig.retrieval.error = C.EnrichmentError("E_UNAVAILABLE", "index corrupt")
    out = rig.ext.call("brain_retrieve", {"query": "Project Nil", "scope": "canonical"})
    assert out["path"].startswith("fallback-") and "E_UNAVAILABLE" in out["notes"][0]
    rig.retrieval.error = sqlite3.DatabaseError("file is not a database")
    out = rig.ext.call("brain_retrieve", {"query": "Project Nil", "scope": "canonical"})
    assert out["path"].startswith("fallback-") and "DatabaseError" in out["notes"][0]
    cur = rig.ext.call("brain_retrieve", {"query": "Project Nil", "cursor": "abc"})
    assert cur["error"]["code"] == "E_STALE_CURSOR"
    none = Rig(enrichment_wiki, legacy=None, config=EnrichmentConfig(mode="on", retrieval=False))
    none.ext.s._svc["legacy"] = None
    none.ext.s.legacy = lambda: None
    assert none.ext.call("brain_retrieve", {"query": "q"})["error"]["code"] == "E_UNAVAILABLE"


class StubBackend:
    def __init__(self, lexical=None, exact=None):
        self.script = {"lexical": lexical, "exact": exact}
        self.calls = []

    def search(self, query, mode, scope, n):
        self.calls.append((mode, scope, n))
        got = self.script[mode]
        if isinstance(got, Exception):
            raise got
        return got


def hit(ref, snippet, rank=1):
    return {"ref": ref, "tier": "capture", "authority_level": 7, "snippet": snippet, "rank": rank}


def test_fallback_order_budget_and_missing_refs(enrichment_wiki):
    cfg = EnrichmentConfig(mode="on", retrieval=False)
    empty = {"canonical": {"results": []}, "captures": {"results": []}}
    be = StubBackend(lexical=empty, exact={"canonical": {"results": []}, "captures": {"results": [hit("cap:cap-1", "x" * 10)]}})
    out = Rig(enrichment_wiki, config=cfg, legacy=be).ext.call("brain_retrieve", {"query": "q"})
    assert [c[0] for c in be.calls] == ["lexical", "exact"] and out["path"] == "fallback-exact"
    assert any("lexical search returned no hits" in n for n in out["notes"])
    be = StubBackend(lexical=RuntimeError(f"qmd missing at {enrichment_wiki.root}"), exact=empty)
    out = Rig(enrichment_wiki, config=cfg, legacy=be).ext.call("brain_retrieve", {"query": "q"})
    assert out["path"] == "fallback-exact" and out["groups"]["canonical"]["count"] == 0
    assert str(enrichment_wiki.root) not in json.dumps(out)
    be = StubBackend(lexical=RuntimeError("a"), exact=RuntimeError("b"))
    out = Rig(enrichment_wiki, config=cfg, legacy=be).ext.call("brain_retrieve", {"query": "q"})
    assert out["error"]["code"] == "E_UNAVAILABLE"
    big = {"canonical": {"results": []},
           "captures": {"results": [hit("cap:cap-1", "a" * 1500, 1), hit("cap:cap-2", "b" * 1500, 2),
                                    {"ref": None, "snippet": "orphan"}]}}
    be = StubBackend(lexical=big)
    out = Rig(enrichment_wiki, config=cfg, legacy=be).ext.call("brain_retrieve", {"query": "q", "scope": "captures", "budget_tokens": 500})
    texts = [h["text"] for h in out["groups"]["captures"]["results"]]
    assert texts == ["a" * 1500] and out["truncated"] is True and out["truncation_reason"] == "budget_tokens"
    assert out["budget"]["estimated_tokens"] <= 500 and out["budget"]["truncated"] is True
    assert any("no public ref" in n for n in out["notes"])


# ------------------------------------------------- isolation, leaks, laziness
def test_errors_never_contain_paths_or_secrets_or_tracebacks(enrichment_wiki, monkeypatch):
    secret = "s3cr3t-value-1234567890"
    monkeypatch.setenv("BRAIN_OWNER_SECRET", secret)
    rig = Rig(enrichment_wiki)
    root = str(enrichment_wiki.root)
    load_source(rig.store, enrichment_wiki, "fa_short")

    def boom(*a, **k):
        raise C.EnrichmentError("E_NOT_FOUND", f"cannot open {root}/_enrichment/store-v1.sqlite3 with {secret}",
                                detail=f"{root}/x /etc/passwd C:\\Users\\a\\b")

    rig.store.get = boom
    out = rig.ext.call("brain_artifact_read", {"source_ref": enrichment_wiki.refs["fa_short"]})
    blob = json.dumps(out, ensure_ascii=False)
    assert out["error"]["code"] == "E_NOT_FOUND"
    for leak in (root, secret, "/etc/passwd", "C:\\Users", "/tmp/", "Traceback"):
        assert leak not in blob, leak
    rig.annotation.submit = lambda r: (_ for _ in ()).throw(ValueError(f"bad {root}"))
    proj = rig.store.projections[enrichment_wiki.refs["fa_short"]]
    out = rig.ext.call("brain_enrichment_submit", req(proj))
    assert out == {"ok": False, "error": {"code": "E_INTERNAL", "message": "ValueError"}, "persisted": False}


def test_server_only_keys_are_dropped_but_source_text_is_never_rewritten(enrichment_wiki):
    rig = Rig(enrichment_wiki)
    root = str(enrichment_wiki.root)
    proj, chunks = load_source(rig.store, enrichment_wiki, "fa_short", size=500)
    original = rig.store.pending

    def leaky(cursor, limit, retry_failed=False):
        page = original(cursor, limit, retry_failed)
        item = {**page.items[0], "anchor_path": f"{root}/01-inbox/x.md",
                "layers": [{**page.items[0]["layers"][0], "origin_path": f"{root}/y"}]}
        return C.PendingPage((item,), page.total, 1, False, None, page.discovery)

    rig.store.pending = leaky
    out = rig.ext.call("brain_enrichment_pending", {})
    assert not {"anchor_path", "origin_path"} & {k for k, _ in walk(out)}
    text_with_path = f"my notes live in {root}/notes and nowhere else\n"
    p2 = make_projection("cap:cap-20260101-000000-aaaa", [("user-text", "cap:cap-20260101-000000-aaaa", text_with_path, None)])
    rig.store.upsert_projection(p2, naive_chunks(p2, 500))
    got = rig.ext.call("brain_artifact_read", {"source_ref": p2.source_ref, "chunk_ref": rig.store.get_chunks(p2.source_ref)[0].chunk_ref})
    assert got["chunk"]["text"] == text_with_path                       # evidence is byte-exact


def test_construction_imports_nothing_and_real_modules_load_only_when_not_injected(enrichment_wiki):
    calls = []

    def importer(name):
        calls.append(name)
        raise ImportError(name)

    ext = build_extension(enrichment_wiki.root, ON, importer=importer)
    assert ext is not None and calls == []
    out = ext.call("brain_enrichment_pending", {})
    assert out["error"]["code"] == "E_UNAVAILABLE" and calls == ["store"]
    assert "No module" not in json.dumps(out) and str(enrichment_wiki.root) not in json.dumps(out)
    assert ext.call("brain_artifact_read", {"source_ref": enrichment_wiki.refs["fa_short"]})["error"]["code"] == "E_UNAVAILABLE"
    assert calls == ["store"]                                          # read tool: no store file, nothing created
    assert not (enrichment_wiki.root / "_enrichment").exists()
    calls.clear()
    rig = Rig(enrichment_wiki, importer=importer)
    proj, _ = load_source(rig.store, enrichment_wiki, "fa_short")
    for name, args in (("brain_enrichment_pending", {}), ("brain_enrichment_submit", req(proj)),
                       ("brain_artifact_read", {"source_ref": proj.source_ref}), ("brain_retrieve", {"query": "q"})):
        assert rig.ext.call(name, args)["ok"] is True, name
    assert set(calls) <= {"source"} and rig.index.calls == []     # only the optional, un-injected freshness reader
    assert not {"store", "annotation", "retrieval", "indexing"} & set(calls)


def test_newer_store_schema_isolates_the_feature_not_the_base_tools(enrichment_wiki):
    root = enrichment_wiki.root
    (root / "_enrichment").mkdir()
    con = sqlite3.connect(root / "_enrichment" / "store-v1.sqlite3")
    con.execute("PRAGMA user_version = 99")
    con.commit()
    con.close()
    calls = []
    ext = build_extension(root, ON, importer=lambda n: calls.append(n) or (_ for _ in ()).throw(ImportError(n)),
                          legacy_backend=WikiBackend(root))
    for name, args in (("brain_enrichment_pending", {}), ("brain_artifact_read", {"source_ref": enrichment_wiki.refs["fa_short"]})):
        out = ext.call(name, args)
        assert out["error"]["code"] == "E_SCHEMA_UNSUPPORTED", name
    out = ext.call("brain_enrichment_submit", {"source_ref": enrichment_wiki.refs["fa_short"], "source_version": "a" * 64,
                                               "expected_annotation_revision": 0, "idempotency_key": KEY,
                                               "annotation": {"tags": ["a"]}})
    assert out["error"]["code"] == "E_SCHEMA_UNSUPPORTED" and out["persisted"] is False
    ret = ext.call("brain_retrieve", {"query": "Project Nil", "scope": "canonical"})
    assert ret["ok"] and ret["path"].startswith("fallback-") and "E_SCHEMA_UNSUPPORTED" in ret["notes"][0]
    assert calls == []                                                 # refused before importing the store module
    base = BrainSurface(WikiBackend(root)).call("brain_read", {"ref": "rec:obj-nil"})
    assert base["ok"] is True


# ------------------------------------------- one ref, same text and version
def test_one_ref_gives_the_same_text_and_version_as_the_base_read(rig):
    base = BrainSurface(rig.legacy)
    w = rig.w
    ref = w.refs["fa_short"]
    read = base.call("brain_read", {"ref": ref})
    text = read["sections"]["User-supplied text"]
    assert text == w.texts["fa_short"]
    proj = make_projection(ref, [("user-text", ref, text, None)])
    rig.store.upsert_projection(proj, naive_chunks(proj, 30))
    art = rig.ext.call("brain_artifact_read", {"source_ref": ref})
    layer = art["artifact"]["layers"][0]
    assert layer["text_ref"] == ref and layer["text_sha256"] == C.sha256_text(text) and layer["chars"] == len(text)
    assert art["artifact"]["source_version"] == C.source_version(ref, [{
        "layer_id": "user-text", "text_ref": ref, "text_sha256": C.sha256_text(text),
        "origin": "fixture:user-text", "provenance_sha256": None}])
    rebuilt = ""
    for meta in art["chunks"]["items"]:
        got = rig.ext.call("brain_artifact_read", {"source_ref": ref, "chunk_ref": meta["chunk_ref"]})
        assert got["chunk"]["text"] == text[meta["start_char"]:meta["end_char"]]
        assert got["source"]["source_version"] == art["artifact"]["source_version"]
        rebuilt += got["chunk"]["text"]
    assert rebuilt == text and art["chunks"]["next_cursor"] is None
    # an ingested file: the base `doc:` read is the derivative the chunks cut
    proj2, chunks2 = load_source(rig.store, w, "held_ok", size=70)
    doc = base.call("brain_read", {"ref": w.refs["held_ok_text"]})
    assert doc["content"] == w.texts["held_ok_text"]
    parts = [rig.ext.call("brain_artifact_read", {"source_ref": proj2.source_ref, "chunk_ref": c.chunk_ref})["chunk"]["text"]
             for c in chunks2]
    assert "".join(parts) == doc["content"]
    assert rig.ext.call("brain_artifact_read", {"source_ref": proj2.source_ref})["artifact"]["layers"][0]["text_ref"] == w.refs["held_ok_text"]


def test_extension_calls_do_not_mutate_the_corpus(rig):
    before = corpus_hashes(rig.w.root)
    proj, chunks = load_source(rig.store, rig.w, "long", size=300)
    rig.ext.call("brain_enrichment_pending", {})
    rig.ext.call("brain_enrichment_submit", req(proj))
    rig.ext.call("brain_artifact_read", {"source_ref": proj.source_ref, "chunk_ref": chunks[0].chunk_ref})
    rig.ext.call("brain_retrieve", {"query": "نیل"})
    assert corpus_hashes(rig.w.root) == before
