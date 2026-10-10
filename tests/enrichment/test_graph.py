"""WP-C graph: node/edge builders and bounded expansion (CONTRACT-E1 section 7)."""
from __future__ import annotations

import hashlib

import build_graph_index as bgi

from brain_enrichment import contract as C
from brain_enrichment import graph as G
from brain_enrichment.indexing import IndexBuilder
from retrieval_support import Corpus, DerivedStore
from support import record

FIT = "فیت پلن: هر روز ۳۰ دقیقه پیاده‌روی و دو بار در هفته تمرین قدرتی انجام بدهم."


def item_of(corpus, source_ref):
    return next(it for it in corpus.store.list_current(None, 1000) if it["projection"].source_ref == source_ref)


def build(corpus, ref, **kw):
    gb = G.GraphBuilder(known_refs=set(corpus.proj), **kw)
    return gb.build(item_of(corpus, ref))


def edges_of(frag, kind=None, label=None):
    return [e for e in frag.edges if (kind is None or e["edge_kind"] == kind)
            and (label is None or e["label"] == label)]


# -------------------------------------------------------------- structural
def test_structural_edges_are_deterministic_and_cover_the_chunks():
    c = Corpus(DerivedStore(), chunk_size=40)
    p = c.capture(FIT)
    f1, f2 = build(c, p.source_ref), build(c, p.source_ref)
    assert f1.edges == f2.edges and f1.nodes == f2.nodes
    chunks = c.store.chunks[p.source_ref]
    assert len(chunks) > 1
    contains = edges_of(f1, "structural", "contains")
    assert [e["dst"] for e in contains] == [ch.chunk_ref for ch in chunks]
    assert all(e["src"] == "art:" + p.artifact_id for e in contains)
    nxt = edges_of(f1, "structural", "next_chunk")
    assert [(e["src"], e["dst"]) for e in nxt] == [(a.chunk_ref, b.chunk_ref) for a, b in zip(chunks, chunks[1:])]
    for e in f1.edges:                                          # every edge carries its provenance fields
        assert e["source_version"] == p.source_version and e["source_ref"] == p.source_ref
        assert e["annotation_revision"] is None and e["provenance"]["origin"] == "projection"


def test_derived_from_links_the_holding_to_its_derivative_text():
    c = Corpus(DerivedStore())
    p = c.ingested("src-aaaaaaaaaaaa", "The ferry to Kolan left every Thursday.\n", doc="doc:" + "a" * 20)
    f = build(c, p.source_ref)
    (e,) = edges_of(f, "structural", "derived_from")
    assert (e["src"], e["dst"]) == ("art:" + p.artifact_id, "doc:" + "a" * 20)


# -------------------------------------------------------------- registered
def test_wikilinks_in_canonical_records_resolve_only_to_existing_records():
    c = Corpus(DerivedStore())
    c.canonical("obj-a", "# A\n\nSee [[obj-b]] and [[obj-missing|alias]] and [[obj-b#part]].\n")
    c.canonical("obj-b", "# B\n\nbody\n")
    f = build(c, "rec:obj-a")
    links = edges_of(f, "registered", "wikilink")
    assert [(e["src"], e["dst"]) for e in links] == [("art:" + c.proj["rec:obj-a"].artifact_id, "rec:obj-b")]
    assert links[0]["provenance"]["origin"] == "layer-text"
    assert not any(e["dst"] == "rec:obj-missing" for e in f.edges)           # nothing is invented


def test_cites_ref_needs_an_explicit_existing_id_verbatim_in_the_layer_text():
    c = Corpus(DerivedStore())
    c.canonical("obj-nil", "# Nil\n\nbody\n")
    t = c.capture("As recorded in obj-nil and also zzz-unknown-id, and the id itself is not a word.")
    f = build(c, t.source_ref)
    cites = edges_of(f, "registered", "cites_ref")
    assert [e["dst"] for e in cites] == ["rec:obj-nil"]
    assert cites[0]["provenance"]["token"] == "obj-nil"
    other = c.capture("a capture that mentions nothing")
    assert not edges_of(build(c, other.source_ref), "registered")


def test_cites_ref_resolves_captures_through_the_public_ref_resolver(enrichment_wiki):
    w = enrichment_wiki
    cap_id = w.refs["fa_short"][4:]
    c = Corpus(DerivedStore())
    t = c.capture(f"see {cap_id} and cap-20990101-000000-ffff (does not exist)")
    gb = G.GraphBuilder(wiki_root=w.root, known_refs={t.source_ref})
    f = gb.build(item_of(c, t.source_ref))
    assert [e["dst"] for e in edges_of(f, "registered", "cites_ref")] == [w.refs["fa_short"]]


def test_graph_db_bridge_is_read_only_and_resolves_what_text_parsing_cannot(enrichment_wiki):
    w = enrichment_wiki
    record(w.root, "03-objects/works/bbbb-note.md", "obj-bbbbbbbbbb", "B", "target")
    record(w.root, "03-objects/works/aaaa.md", "obj-aaaaaaaaaa", "A", "links [[bbbb-note]] here")
    bgi.build(w.root)
    db = w.root / "_search/graph.db"
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    c = Corpus(DerivedStore())
    c.canonical("obj-aaaaaaaaaa", (w.root / "03-objects/works/aaaa.md").read_text(encoding="utf-8"))
    gb = G.GraphBuilder(wiki_root=w.root, known_refs={"rec:obj-aaaaaaaaaa"})
    f = gb.build(item_of(c, "rec:obj-aaaaaaaaaa"))
    bridged = [e for e in f.edges if e["provenance"].get("origin") == "graph.db"]
    assert [(e["label"], e["dst"]) for e in bridged] == [("wikilink", "rec:obj-bbbbbbbbbb")]
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before             # tables and bytes untouched
    assert gb.stamp() and G.GraphBuilder(wiki_root=w.root.parent / "nowhere").stamp() == ""


# ------------------------------------------------- semantic and reading edges
def annotated_pair():
    c = Corpus(DerivedStore())
    a = c.capture("Plan A says the budget is fixed for the whole year, no changes.")
    b = c.capture("Plan B claims the budget can grow every quarter if needed.")
    rev = c.annotate(a.source_ref, {
        "entities": [{"name": "Budget", "kind": "concept", "aliases": ["بودجه"],
                      "mention": {"chunk_ref": "FIRST", "text": "budget"}}],
        "links": [{"relation": "about", "target": {"entity": "Budget"},
                   "evidence": [{"chunk_ref": "FIRST", "quote": "budget is fixed"}]},
                  {"relation": "contradicts", "target": {"source_ref": b.source_ref},
                   "evidence": [{"chunk_ref": "FIRST", "quote": "budget is fixed for the whole year"}]},
                  {"relation": "supports", "target": {"source_ref": b.source_ref},
                   "evidence": [{"chunk_ref": "FIRST", "quote": "no changes"}]}]})
    return c, a, b, rev


def test_semantic_and_reading_edges_are_candidates_with_full_provenance():
    c, a, b, rev = annotated_pair()
    f = build(c, a.source_ref)
    sem = edges_of(f, "semantic")
    assert {e["label"] for e in sem} == {"mentions", "about"}
    reading = edges_of(f, "reading")
    assert {e["label"] for e in reading} == {"contradicts", "supports"}
    for e in sem + reading:
        assert e["annotation_revision"] == rev and e["source_version"] == a.source_version
        assert e["provenance"]["origin"] == "annotation" and e["provenance"]["candidate"] is True
        assert e["provenance"]["edge_kind"] == e["edge_kind"]
        assert e["provenance"]["evidence"] and e["provenance"]["evidence"][0]["chunk_ref"] == c.store.chunks[a.source_ref][0].chunk_ref
    assert all(e["provenance"]["reading"] for e in reading)
    for e in reading:
        assert e["dst"] == b.source_ref                         # a ref node; reading never creates authority
    # no quote text and no summary is stored on any edge
    assert "budget is fixed" not in repr([e["provenance"] for e in f.edges])


def test_edge_kind_is_recomputed_not_trusted_from_the_caller():
    c = Corpus(DerivedStore())
    a = c.capture("Some text that supports a claim made elsewhere in the notes.")
    b = c.capture("The claim itself.")
    ann = {"links": [{"relation": "supports", "target": {"source_ref": b.source_ref},
                      "evidence": [{"chunk_ref": "FIRST", "quote": "supports a claim"}]}]}
    derived = {"entities": [], "links": [{"relation": "supports", "edge_kind": "semantic", "src": None,
                                           "dst": b.source_ref, "evidence": [{"chunk_ref": "FIRST"}]}]}
    c.annotate(a.source_ref, ann, derived=derived)
    (e,) = edges_of(build(c, a.source_ref), "reading")
    assert e["edge_kind"] == "reading" and e["label"] == "supports"


def test_server_derived_block_and_local_recomputation_agree():
    c, a, b, _ = annotated_pair()
    plain = build(c, a.source_ref)
    item = item_of(c, a.source_ref)
    ann = item["annotation"]
    derived = G.derive_from_annotation(a.source_ref, ann)
    assert derived["entities"][0]["entity_id"] == C.entity_id(a.source_ref, C.normal_form("Budget"))
    item2 = {**item, "derived": derived}
    again = G.GraphBuilder(known_refs=set(c.proj)).build(item2)
    key = lambda f: sorted((e["src"], e["dst"], e["label"], e["edge_kind"]) for e in f.edges)  # noqa: E731
    assert key(plain) == key(again)


def test_rejected_or_stale_annotation_creates_no_candidate_edges():
    c, a, b, rev = annotated_pair()
    item = item_of(c, a.source_ref)
    gb = G.GraphBuilder(known_refs=set(c.proj))
    assert edges_of(gb.build({**item, "review_state": "rejected"}), "semantic") == []
    assert edges_of(gb.build({**item, "review_state": "rejected"}), "reading") == []
    stale = {**item, "annotation": {**item["annotation"], "source_version": "0" * 64}}
    assert edges_of(gb.build(stale), "reading") == []
    assert edges_of(gb.build(item), "reading")                  # control: the same item is fine when usable


def test_entities_are_per_source_and_never_merged_by_similar_name():
    c = Corpus(DerivedStore())
    a = c.capture("Kolan is the ferry town my grandmother came from.")
    b = c.capture("Kolan is also the name of my neighbour's dog.")
    for p, kind in ((a, "place"), (b, "other")):
        c.annotate(p.source_ref, {"entities": [{"name": "Kolan", "kind": kind, "mention": {"chunk_ref": "FIRST", "text": "Kolan"}}]})
    fa, fb = build(c, a.source_ref), build(c, b.source_ref)
    ea = [n for n in fa.nodes if n["kind"] == "entity"]
    eb = [n for n in fb.nodes if n["kind"] == "entity"]
    assert len(ea) == len(eb) == 1
    assert ea[0]["node_id"] != eb[0]["node_id"] and ea[0]["normal"] == eb[0]["normal"] == "kolan"
    assert ea[0]["node_id"] == C.entity_id(a.source_ref, "kolan")
    assert not any({e["src"], e["dst"]} == {ea[0]["node_id"], eb[0]["node_id"]} for e in fa.edges + fb.edges)


# ----------------------------------------------------------------- expansion
def make_neighbors(adj):
    def neighbors(node):
        return [{"other": o, "edge_kind": "semantic", "label": lbl, "direction": "out", "owner": "own",
                 "annotation_revision": 1, "evidence": []} for o, lbl in adj.get(node, [])]
    return neighbors


def test_expand_is_bounded_in_depth_and_nodes_and_explains_every_node():
    adj = {"s": [("a", "x"), ("b", "x")], "a": [("c", "x")], "c": [("d", "x")], "b": [("e", "x")]}
    ex = G.expand(make_neighbors(adj), ["s"])
    got = {r["node"]: r["depth"] for r in ex.nodes}
    assert got == {"s": 0, "a": 1, "b": 1, "c": 2, "e": 2}      # depth capped at 2 (d is 3 hops away)
    for r in ex.nodes[1:]:
        assert r["via"] and r["reason"] and r["seed"] == "s"
    wide = {"s": [(f"n{i:02d}", "x") for i in range(9)]}
    wide.update({f"n{i:02d}": [(f"m{i:02d}{j}", "x") for j in range(9)] for i in range(9)})
    ex = G.expand(make_neighbors(wide), ["s"], max_nodes=1000)       # asking for more is clamped to the contract
    assert len(ex.nodes) <= C.GRAPH_MAX_NODES and ex.truncated
    ex = G.expand(make_neighbors(wide), ["s"], max_depth=9)
    assert max(r["depth"] for r in ex.nodes) <= C.GRAPH_MAX_DEPTH


def test_seeds_are_capped_and_taken_in_the_order_given_never_by_degree():
    adj = {f"seed{i}": [(f"leaf{i}", "x")] for i in range(12)}
    adj["seed11"] = [(f"big{j}", "x") for j in range(5)]            # the densest node is the LAST seed
    ex = G.expand(make_neighbors(adj), [f"seed{i}" for i in range(12)])
    seeds = [r["node"] for r in ex.nodes if r["depth"] == 0]
    assert seeds == [f"seed{i}" for i in range(C.GRAPH_MAX_SEEDS)]    # first 8, degree is not consulted


def test_hub_guard_caps_fanout_and_does_not_walk_through_non_seed_hubs():
    adj = {"seed": [("hub", "x"), ("friend", "x")],
           "hub": [(f"stranger{i:02d}", "x") for i in range(60)],
           "friend": [("friend2", "x")]}
    ex = G.expand(make_neighbors(adj), ["seed"])
    nodes = {r["node"] for r in ex.nodes}
    assert "hub" in nodes and "friend2" in nodes                      # the hub is reachable ...
    assert not any(n.startswith("stranger") for n in nodes)          # ... but not walked through
    assert ex.hub_leaf == ["hub"]
    ex = G.expand(make_neighbors({"hubseed": [(f"n{i:02d}", "x") for i in range(60)]}), ["hubseed"])
    assert len(ex.nodes) == 1 + G.FANOUT_CAP and ex.hub_capped == ["hubseed"]   # a seed hub is capped, deterministic
    again = G.expand(make_neighbors({"hubseed": [(f"n{i:02d}", "x") for i in range(60)]}), ["hubseed"])
    assert [r["node"] for r in ex.nodes] == [r["node"] for r in again.nodes]


def test_expand_remembers_other_inclusion_paths():
    adj = {"s1": [("t", "x")], "s2": [("t", "y")]}
    ex = G.expand(make_neighbors(adj), ["s1", "s2"])
    t = next(r for r in ex.nodes if r["node"] == "t")
    assert t["seed"] == "s1" and t["also_via"] and t["also_via"][0][-1]["label"] == "y"


def test_index_neighbors_follow_reading_edges_both_ways_and_ref_equals_artifact(tmp_path):
    c, a, b, _ = annotated_pair()
    ib = IndexBuilder(c.store, tmp_path / "i.sqlite3")
    ib.refresh(10)
    with ib.open_reader() as rd:
        out = rd.neighbors("art:" + a.artifact_id)
        assert {(n["other"], n["label"]) for n in out if n["edge_kind"] == "reading"} == {
            ("art:" + b.artifact_id, "contradicts"), ("art:" + b.artifact_id, "supports")}
        back = rd.neighbors("art:" + b.artifact_id)
        assert {(n["other"], n["direction"]) for n in back} == {("art:" + a.artifact_id, "in")}
        assert all(n["candidate"] for n in out if n["edge_kind"] in ("reading", "semantic"))
        ex = rd.expand(["art:" + a.artifact_id])
        reached = next(r for r in ex.nodes if r["node"] == "art:" + b.artifact_id)
        assert reached["depth"] == 1 and "reading:contradicts" in reached["reason"]
        # structural bookkeeping edges are not traversable
        assert not any(n["label"] in G.C.STRUCTURAL_EDGES for n in out)
