"""WP-C retrieval: brain_retrieve behaviour end to end (CONTRACT-E1 section 8)."""
from __future__ import annotations

import json
import math
import socket
import statistics
import subprocess
import time
import urllib.request

import pytest

from brain_enrichment import contract as C
from brain_enrichment import indexing as IX
from brain_enrichment import retrieval as RT
from brain_enrichment.config import EnrichmentConfig
from brain_enrichment.indexing import IndexBuilder
from brain_enrichment.retrieval import RetrievalEngine
from brain_enrichment.tools_spec import AUTHORITY_NOTE
from retrieval_support import Corpus, DerivedStore
from support import corpus_hashes

CFG = EnrichmentConfig(mode="on", retrieval=True)
ENVELOPE_KEYS = {"ok", "query", "scope", "limit", "path", "query_model_calls", "uses_candidate_annotations",
                 "budget", "index", "coverage", "groups", "notes", "truncated", "truncation_reason",
                 "next_cursor", "authority_note"}
HIT_KEYS = {"source_ref", "text_ref", "chunk_ref", "layer_id", "span", "text", "hit_level", "tier",
            "authority_level", "annotation_revision", "annotation_coverage", "inclusion_reason", "rank",
            "flags", "truncated", "neighbors"}


class Legacy:
    """Stand-in for WikiBackend.search (shape: {group: {"results": [...]}})."""

    def __init__(self, lexical=None, exact=None, lexical_error=False):
        self.by_mode = {"lexical": lexical or {}, "exact": exact or {}}
        self.lexical_error = lexical_error
        self.calls = []

    def __call__(self, query, mode, scope, n):
        self.calls.append((query, mode, scope, n))
        if mode == "lexical" and self.lexical_error:
            raise RuntimeError("qmd not installed")
        return self.by_mode[mode]


def legacy_hit(ref, snippet="snippet text", tier="capture", level=7):
    return {"ref": ref, "tier": tier, "authority_level": level, "snippet": snippet, "rank": 1}


def make(corpus, tmp_path, *, legacy=None, wiki_root=None, source_fresh=None, config=CFG, refresh=True):
    ib = IndexBuilder(corpus.store, tmp_path / "_search/enrichment/search-v1.sqlite3", wiki_root=wiki_root)
    if refresh:
        ib.refresh(10_000)
    eng = RetrievalEngine(corpus.store, ib, config=config, legacy_search=legacy, source_fresh=source_fresh,
                          wiki_root=wiki_root)
    return eng, ib


def refs(out, group):
    return [h["source_ref"] for h in out["groups"][group]["results"]]


def all_hits(out):
    return [h for g in out["groups"].values() for h in g["results"]]


def fixture_corpus(w, **kw):
    c = Corpus(DerivedStore(), **kw)
    for name in ("fa_short", "crlf", "dup_a", "dup_b", "escaped_headings"):
        c.capture(w.texts[name], w.refs[name])
    c.ingested(w.refs["held_ok"][4:], w.texts["held_ok_text"], doc=w.refs["held_ok_text"])
    c.canonical("obj-nil", (w.root / w.paths["canonical"]).read_text(encoding="utf-8"))
    return c


# ------------------------------------------------------- acceptance: finds
def test_fit_plan_capture_is_found_with_no_ref_and_no_annotation(enrichment_wiki, tmp_path):
    w = enrichment_wiki
    eng, _ = make(fixture_corpus(w), tmp_path)
    out = eng.retrieve({"query": "فیت پلن"})
    assert set(out) == ENVELOPE_KEYS and out["ok"] is True
    assert out["path"] == "enrichment" and out["query_model_calls"] == 0
    assert out["uses_candidate_annotations"] is False and out["authority_note"] == AUTHORITY_NOTE
    top = out["groups"]["captures"]["results"][0]
    assert set(top) == HIT_KEYS
    assert top["source_ref"] == w.refs["fa_short"] and top["text"] == w.texts["fa_short"]
    assert top["inclusion_reason"]["paths"] == ["direct_text"] and top["annotation_revision"] is None
    assert top["hit_level"] == "chunk" and top["tier"] == "capture" and top["authority_level"] == 7
    assert top["span"] == {"start_char": 0, "end_char": len(w.texts["fa_short"])}
    assert out["index"] == {"generation": 1, "state": "ok", "lag": {"pending_artifacts": 0}, "fresh": True}
    assert out["groups"]["canonical"]["results"] == []
    assert out["next_cursor"] is None and out["truncated"] is False
    # orthographic variants of the query find the same text (Arabic yeh, ASCII digits)
    assert refs(eng.retrieve({"query": "فيت پلن"}), "captures")[0] == w.refs["fa_short"]
    assert refs(eng.retrieve({"query": "پیاده‌روی ۳۰ دقیقه"}), "captures")[0] == w.refs["fa_short"]
    assert refs(eng.retrieve({"query": "پیاده روی 30 دقیقه"}), "captures")[0] == w.refs["fa_short"]


def test_fa_en_alias_is_found_through_entity_aliases(enrichment_wiki, tmp_path):
    w = enrichment_wiki
    c = fixture_corpus(w)
    c.annotate(w.refs["fa_short"], {"entities": [{"name": "فیت پلن", "kind": "project", "aliases": ["fit plan"],
                                                  "mention": {"chunk_ref": "FIRST", "text": "فیت پلن"}}]})
    eng, _ = make(c, tmp_path)
    out = eng.retrieve({"query": "fit plan"})
    hit = out["groups"]["captures"]["results"][0]
    assert hit["source_ref"] == w.refs["fa_short"]
    assert hit["text"] == w.texts["fa_short"]                       # the exact source text, not the alias
    assert hit["inclusion_reason"]["paths"] == ["metadata"] and hit["annotation_revision"] == 1
    assert hit["inclusion_reason"]["via"] and hit["inclusion_reason"]["via"][0].startswith("ent:")
    assert out["uses_candidate_annotations"] is True
    assert any("candidate" in n for n in out["notes"])
    # and the other direction: the Persian name finds the capture by text AND by metadata (one hit, both paths)
    out2 = eng.retrieve({"query": "فیت پلن"})
    hits = [h for h in out2["groups"]["captures"]["results"] if h["source_ref"] == w.refs["fa_short"]]
    assert len(hits) == 1 and hits[0]["inclusion_reason"]["paths"] == ["direct_text", "metadata"]


def test_without_the_annotation_the_alias_query_finds_nothing_and_says_nothing_false(enrichment_wiki, tmp_path):
    w = enrichment_wiki
    eng, _ = make(fixture_corpus(w), tmp_path)
    out = eng.retrieve({"query": "fit plan"})
    assert refs(out, "captures") == [] and out["uses_candidate_annotations"] is False


def test_the_registered_graph_alone_is_not_candidate_annotation_use(tmp_path):
    c = Corpus(DerivedStore())
    c.canonical("obj-nil", "# Nil\n\nThe second novel.\n")
    a = c.capture("My note that quotes obj-nil in passing.")
    eng, _ = make(c, tmp_path)
    out = eng.retrieve({"query": "obj-nil"})
    assert "rec:obj-nil" in refs(out, "canonical")                    # explicit ref route
    assert a.source_ref in refs(out, "captures")
    paths = {h["source_ref"]: h["inclusion_reason"]["paths"] for h in all_hits(out)}
    assert "graph" in paths[a.source_ref] or "direct_text" in paths[a.source_ref]
    assert out["uses_candidate_annotations"] is False


# -------------------------------------------------------- graph behaviour
def orchid_hub_corpus(n_unrelated=30):
    c = Corpus(DerivedStore())
    direct = [c.capture(f"orchid care tip number {i}: water on sundays and mist the roots") for i in range(4)]
    hub = c.capture("Orchid care schedule for the whole spring season.")
    c.annotate(hub.source_ref, {"entities": [{"name": "Orchid care", "kind": "concept",
                                              "mention": {"chunk_ref": "FIRST", "text": "Orchid care"}}]})
    hub_ent = C.entity_id(hub.source_ref, C.normal_form("Orchid care"))
    others = []
    for i in range(n_unrelated):
        o = c.capture(f"unrelated diary page {i} about trains and weather {i * 7}")
        c.annotate(o.source_ref, {"links": [{"relation": "related_to", "target": {"entity_id": hub_ent},
                                             "evidence": [{"chunk_ref": "FIRST", "quote": f"diary page {i} about"}]}]})
        others.append(o)
    return c, direct, hub, others


def test_a_hub_with_many_unrelated_edges_does_not_dominate(tmp_path):
    c, direct, hub, others = orchid_hub_corpus()
    eng, _ = make(c, tmp_path)
    out = eng.retrieve({"query": "orchid care", "limit": 10})
    got = refs(out, "captures")
    direct_refs = {d.source_ref for d in direct} | {hub.source_ref}
    assert direct_refs <= set(got)                                     # every direct match is present
    unrelated = [r for r in got if r in {o.source_ref for o in others}]
    assert 0 < len(unrelated) <= 5 and len(unrelated) < len(others)      # <= 50% of capacity, far below the hub's 30
    assert len(unrelated) <= 10 // 2
    for h in out["groups"]["captures"]["results"]:
        if h["source_ref"] in {o.source_ref for o in others}:
            assert h["inclusion_reason"]["paths"] == ["graph"]
            assert h["inclusion_reason"]["via"]                           # explained
            assert h["authority_level"] == 7 and h["tier"] == "capture"   # topology never raises authority
    assert any("hub guard" in n for n in out["notes"])


def test_graph_on_or_off_never_hides_direct_text(tmp_path):
    c, direct, hub, others = orchid_hub_corpus()
    eng, _ = make(c, tmp_path)

    def pages(limit):
        seen, cursor = [], None
        for _ in range(40):
            out = eng.retrieve({"query": "orchid care", "limit": limit, **({"cursor": cursor} if cursor else {})})
            seen += out["groups"]["captures"]["results"]
            cursor = out["next_cursor"]
            if not cursor:
                return seen
        raise AssertionError("cursor never ended")

    eng.graph_enabled = False
    off = pages(6)
    assert off and all(h["inclusion_reason"]["paths"] != ["graph"] for h in off)
    eng.graph_enabled = True
    first_on = eng.retrieve({"query": "orchid care", "limit": 6})["groups"]["captures"]["results"]
    on = pages(6)
    assert {h["source_ref"] for h in off} <= {h["source_ref"] for h in on}      # nothing direct is lost
    top_direct = off[0]["source_ref"]
    assert top_direct in [h["source_ref"] for h in first_on]                      # best direct hit is on page 1
    direct_in_first = [h for h in first_on if set(h["inclusion_reason"]["paths"]) & {"direct_text", "metadata"}]
    assert len(direct_in_first) >= math.ceil(0.5 * 6)                              # >= 50% reserved for direct


def test_reserve_direct_share_unit():
    mk = lambda key, direct: RT.Cand(key, "captures", key, r_text=1 if direct else None, r_graph=None if direct else 1)  # noqa: E731
    ordered = [mk("g1", False), mk("g2", False), mk("g3", False), mk("d1", True), mk("d2", True), mk("d3", True)]
    out = RT.RetrievalEngine._reserve_direct(ordered, 4)
    assert [c.key for c in out[:4]].count("d1") + [c.key for c in out[:4]].count("d2") >= 2
    assert sorted(c.key for c in out) == sorted(c.key for c in ordered)           # nothing lost, only reordered
    only_graph = [mk("g1", False), mk("g2", False)]
    assert [c.key for c in RT.RetrievalEngine._reserve_direct(only_graph, 2)] == ["g1", "g2"]


def test_same_name_entities_in_different_sources_are_not_merged(tmp_path):
    c = Corpus(DerivedStore())
    a = c.capture("My grandmother came from Kolan, a ferry town on the coast.")
    b = c.capture("Kolan is also what we named the neighbour's dog.")
    n = c.capture("Nothing about that name here, only trains.")
    for p, kind in ((a, "place"), (b, "other")):
        c.annotate(p.source_ref, {"entities": [{"name": "Kolan", "kind": kind,
                                                "mention": {"chunk_ref": "FIRST", "text": "Kolan"}}]})
    eng, ib = make(c, tmp_path)
    out = eng.retrieve({"query": "Kolan"})
    got = refs(out, "captures")
    assert set(got) == {a.source_ref, b.source_ref} and n.source_ref not in got
    via = {h["source_ref"]: [v for v in h["inclusion_reason"]["via"] if v.startswith("ent:")]
           for h in out["groups"]["captures"]["results"]}
    ea, eb = C.entity_id(a.source_ref, "kolan"), C.entity_id(b.source_ref, "kolan")
    assert ea != eb and via[a.source_ref] == [ea] and via[b.source_ref] == [eb]    # each hit names ITS entity
    assert any("never merged" in n for n in out["notes"])
    with ib.open_reader() as rd:                                                    # no edge or path joins them
        assert all(x["other"] not in (eb, "art:" + b.artifact_id) for x in rd.neighbors(ea))
        assert all(x["other"] not in (ea, "art:" + a.artifact_id) for x in rd.neighbors(eb))


def test_supports_and_contradicts_are_only_candidate_reading_edges(tmp_path):
    c = Corpus(DerivedStore())
    a = c.capture("Plan A says the budget is fixed for the whole year, no changes.")
    b = c.capture("Plan B claims the allowance can grow every quarter if needed.")
    c.annotate(a.source_ref, {
        "summary": "Plan A contradicts Plan B about flexibility",
        "entities": [{"name": "Budget", "kind": "concept", "mention": {"chunk_ref": "FIRST", "text": "budget"}}],
        "links": [{"relation": "contradicts", "target": {"source_ref": b.source_ref},
                   "evidence": [{"chunk_ref": "FIRST", "quote": "budget is fixed for the whole year"}]}]})
    eng, ib = make(c, tmp_path)
    out = eng.retrieve({"query": "Budget"})
    by = {h["source_ref"]: h for h in all_hits(out)}
    hb = by[b.source_ref]
    assert hb["inclusion_reason"]["paths"] == ["graph"]
    assert "reading:contradicts" in hb["inclusion_reason"]["matched"]            # labelled as a reading edge
    assert hb["text"] == "Plan B claims the allowance can grow every quarter if needed."   # B's own source text
    assert hb["authority_level"] == 7 and hb["annotation_revision"] is None and out["uses_candidate_annotations"]
    assert "Plan A contradicts" not in json.dumps(out, ensure_ascii=False)         # a summary is never returned
    with ib.open_reader() as rd:
        (e,) = [x for x in rd.neighbors("art:" + a.artifact_id) if x["label"] == "contradicts"]
        assert e["edge_kind"] == "reading" and e["candidate"] and e["annotation_revision"] == 1


def test_a_summary_is_side_metadata_only_never_text_or_quote(tmp_path):
    c = Corpus(DerivedStore())
    a = c.capture("Short unrelated body about trains.")
    summary = "A quokka themed reflection that the body never says"
    c.annotate(a.source_ref, {"summary": summary, "tags": ["quokka"]})
    eng, _ = make(c, tmp_path)
    out = eng.retrieve({"query": "quokka reflection"})
    (hit,) = out["groups"]["captures"]["results"]
    assert hit["source_ref"] == a.source_ref and hit["inclusion_reason"]["paths"] == ["metadata"]
    assert hit["text"] == "Short unrelated body about trains."
    assert summary not in json.dumps(out, ensure_ascii=False)
    assert all(summary not in m for m in hit["inclusion_reason"]["matched"])


def test_one_hit_per_chunk_with_every_route_listed(tmp_path):
    c = Corpus(DerivedStore())
    a = c.capture("orchid care schedule: water on sundays")
    d = c.capture("orchid care tips from the greenhouse book")
    c.annotate(a.source_ref, {"entities": [{"name": "orchid care", "kind": "concept",
                                            "mention": {"chunk_ref": "FIRST", "text": "orchid care"}}]})
    c.annotate(d.source_ref, {"links": [{"relation": "related_to",
                                         "target": {"entity_id": C.entity_id(a.source_ref, "orchid care")},
                                         "evidence": [{"chunk_ref": "FIRST", "quote": "orchid care tips"}]}]})
    eng, _ = make(c, tmp_path)
    out = eng.retrieve({"query": "orchid care"})
    by = {}
    for h in out["groups"]["captures"]["results"]:
        by.setdefault(h["source_ref"], []).append(h)
    assert all(len(v) == 1 for v in by.values())
    assert by[a.source_ref][0]["inclusion_reason"]["paths"] == ["direct_text", "metadata"]
    assert set(by[d.source_ref][0]["inclusion_reason"]["paths"]) == {"direct_text", "graph"}


# ------------------------------------------------------ scope and authority
def test_scope_all_keeps_authority_groups_separate_and_labels_levels(tmp_path):
    c = Corpus(DerivedStore())
    c.capture("ferry timetable note: the ferry leaves at dawn")
    c.canonical("obj-ferry", "# Ferry lines\n\nThe coastal ferry runs in summer only.\n")
    c.ingested("src-aaaaaaaaaaaa", "The ferry to Kolan left every Thursday before dawn.\n", flags=("needs_ocr",))
    eng, _ = make(c, tmp_path)
    out = eng.retrieve({"query": "ferry"})
    assert list(out["groups"]) == ["canonical", "captures"]
    can, cap = out["groups"]["canonical"]["results"], out["groups"]["captures"]["results"]
    assert len(cap) == 1 and cap[0]["tier"] == "capture" and cap[0]["authority_level"] == 7
    levels = {h["source_ref"]: (h["tier"], h["authority_level"]) for h in can}
    assert levels == {"rec:obj-ferry": ("canonical-record", 4),
                      "rec:src-aaaaaaaaaaaa": ("derivative", 5)}                   # derivative tier, never the original's
    assert [h["rank"] for h in can] == [1, 2] and [h["rank"] for h in cap] == [1]   # per-group ranking, no merged list
    ocr = next(h for h in can if h["source_ref"] == "rec:src-aaaaaaaaaaaa")
    assert "needs_ocr" in ocr["flags"] and ocr["text_ref"].startswith("doc:")
    assert out["groups"]["canonical"]["count"] == 2 and out["groups"]["captures"]["count"] == 1
    only_c = eng.retrieve({"query": "ferry", "scope": "captures"})
    assert list(only_c["groups"]) == ["captures"]
    only_k = eng.retrieve({"query": "ferry", "scope": "canonical"})
    assert list(only_k["groups"]) == ["canonical"] and len(only_k["groups"]["canonical"]["results"]) == 2


def test_long_source_hits_are_exact_chunks_with_neighbours(enrichment_wiki, tmp_path):
    w = enrichment_wiki
    c = Corpus(DerivedStore())
    c.capture(w.texts["long"], w.refs["long"], size=1500)
    eng, _ = make(c, tmp_path)
    out = eng.retrieve({"query": "Nil manuscript timeline", "budget_tokens": 32000})
    hits = out["groups"]["captures"]["results"]
    assert 1 <= len(hits) <= RT.PER_SOURCE_CAP
    chunks = {ch.chunk_ref: ch for ch in c.store.chunks[w.refs["long"]]}
    for h in hits:
        s, e = h["span"]["start_char"], h["span"]["end_char"]
        assert h["text"] == w.texts["long"][s:e] == chunks[h["chunk_ref"]].text
        assert h["neighbors"] == {"prev_chunk_ref": chunks[h["chunk_ref"]].prev_chunk_ref,
                                  "next_chunk_ref": chunks[h["chunk_ref"]].next_chunk_ref}
    assert any("per-source cap" in n for n in out["notes"]) or len(hits) < RT.PER_SOURCE_CAP


# ---------------------------------------------------------------- budget
def corpus_of(n, sentence, reps):
    c = Corpus(DerivedStore())
    for i in range(n):
        c.capture(f"{i}: " + sentence * reps)
    return c


def test_tiny_budget_gives_partial_coverage_and_a_working_cursor(tmp_path):
    c = corpus_of(12, "این جمله دربارهٔ پروژه نیل است. ", 60)      # ~900 estimated tokens per chunk
    eng, _ = make(c, tmp_path)
    seen, partial_seen, cursor = [], 0, None
    first = None
    for _ in range(30):
        out = eng.retrieve({"query": "پروژه نیل", "budget_tokens": 500, "limit": 10,
                            **({"cursor": cursor} if cursor else {})})
        first = first or out
        assert out["budget"]["estimated_tokens"] <= 500 and out["budget"]["budget_tokens"] == 500
        for h in out["groups"]["captures"]["results"]:
            stored = c.store.get_chunk(h["chunk_ref"])
            assert h["text"] == stored.text[:len(h["text"])]                    # a prefix of the exact text
            assert C.fits_budget(h["text"], 500)
            assert h["span"]["end_char"] - h["span"]["start_char"] == len(h["text"])
            partial_seen += h["truncated"]
            seen.append(h["source_ref"])
        cursor = out["next_cursor"]
        if not cursor:
            break
    assert first["truncated"] is True and first["truncation_reason"] == "budget"
    assert first["budget"]["truncated"] is True and first["next_cursor"]       # not a full claim
    assert len(first["groups"]["captures"]["results"]) < 12
    assert partial_seen >= 1
    assert len(seen) == len(set(seen)) == 12                                   # paging reaches every source exactly once


def test_persian_costs_more_budget_than_english_for_the_same_characters(tmp_path):
    fa = corpus_of(10, "این جمله دربارهٔ پروژه نیل است. ", 30)
    en = corpus_of(10, "Notes about the Nil project plan. ", 30)
    assert abs(len(fa.store.chunks[next(iter(fa.proj))][0].text) -
               len(en.store.chunks[next(iter(en.proj))][0].text)) < 150
    efa, _ = make(fa, tmp_path / "fa")
    een, _ = make(en, tmp_path / "en")
    ofa = efa.retrieve({"query": "نیل", "budget_tokens": 1500})
    oen = een.retrieve({"query": "Nil", "budget_tokens": 1500})
    n_fa = len([h for h in ofa["groups"]["captures"]["results"] if not h["truncated"]])
    n_en = len([h for h in oen["groups"]["captures"]["results"] if not h["truncated"]])
    assert 0 < n_fa < n_en
    for out in (ofa, oen):
        assert out["budget"]["estimated_tokens"] <= 1500
        total = sum(C.estimate_tokens(h["text"]) for h in all_hits(out))
        assert total <= out["budget"]["estimated_tokens"]                        # overhead is budgeted on top


def test_budget_uses_the_contract_ceilings(monkeypatch, tmp_path):
    c = corpus_of(3, "word " * 100, 1)
    eng, _ = make(c, tmp_path)
    calls = []
    real = C.fits_budget
    monkeypatch.setattr(C, "fits_budget", lambda t, b: calls.append(b) or real(t, b))
    out = eng.retrieve({"query": "word", "budget_tokens": 500})
    assert calls and out["budget"]["estimated_tokens"] <= 500
    cut = RT.RetrievalEngine._prefix_that_fits("x" * 5000, 100)
    assert real(cut, 100) and len(cut) == 400 and not real("x" * 401, 100)       # 4 chars per budgeted token


# ------------------------------------------------------- freshness / cursors
def test_changed_or_deleted_sources_are_dropped_not_served(tmp_path):
    c = Corpus(DerivedStore())
    a = c.capture("orchid care schedule for spring")
    b = c.capture("orchid care notes for summer")
    d = c.capture("orchid care leaflet for autumn")
    eng, ib = make(c, tmp_path)
    assert len(refs(eng.retrieve({"query": "orchid care"}), "captures")) == 3
    c.replace_text(a.source_ref, "completely new text about roses")              # index not refreshed yet
    out = eng.retrieve({"query": "orchid care"})
    assert set(refs(out, "captures")) == {b.source_ref, d.source_ref}
    assert out["coverage"]["stale_dropped"] == 1
    c.delete(b.source_ref)
    out = eng.retrieve({"query": "orchid care"})
    assert refs(out, "captures") == [d.source_ref] and out["coverage"]["stale_dropped"] == 2
    assert out["index"]["state"] == "lagging" and out["index"]["lag"]["pending_artifacts"] >= 1
    ib.refresh(100)
    out = eng.retrieve({"query": "roses"})
    assert refs(out, "captures") == [a.source_ref] and out["coverage"]["stale_dropped"] == 0
    assert out["index"]["state"] == "ok"


def test_an_injected_disk_check_can_veto_a_candidate(tmp_path):
    c = Corpus(DerivedStore())
    a = c.capture("orchid care schedule")
    b = c.capture("orchid care notes")
    eng, _ = make(c, tmp_path, source_fresh=lambda ref, ver: ref != a.source_ref)
    out = eng.retrieve({"query": "orchid care"})
    assert refs(out, "captures") == [b.source_ref] and out["coverage"]["stale_dropped"] == 1


def test_cursor_binds_query_scope_freshness_and_generation(tmp_path):
    c = Corpus(DerivedStore())
    for i in range(8):
        c.capture(f"orchid care note {i}")
    eng, ib = make(c, tmp_path)
    first = eng.retrieve({"query": "orchid care", "limit": 3})
    cur = first["next_cursor"]
    assert cur and len(first["groups"]["captures"]["results"]) == 3
    second = eng.retrieve({"query": "orchid care", "limit": 3, "cursor": cur})
    assert not set(refs(first, "captures")) & set(refs(second, "captures"))
    assert [h["rank"] for h in second["groups"]["captures"]["results"]] == [4, 5, 6]
    for bad, kw in (({"query": "other words", "limit": 3}, "q"), ({"query": "orchid care", "scope": "captures"}, "s")):
        with pytest.raises(C.EnrichmentError) as e:
            eng.retrieve({**bad, "cursor": cur})
        assert e.value.code == "E_BAD_CURSOR"
    for junk in ("!!!", "a" * 100, C.encode_cursor("pending", x=1)):
        with pytest.raises(C.EnrichmentError) as e:
            eng.retrieve({"query": "orchid care", "cursor": junk})
        assert e.value.code == "E_BAD_CURSOR"
    # the sources change: the freshness token moves
    c.capture("orchid care note added later")
    with pytest.raises(C.EnrichmentError) as e:
        eng.retrieve({"query": "orchid care", "limit": 3, "cursor": cur})
    assert e.value.code == "E_STALE_CURSOR"


def test_generation_change_invalidates_the_cursor(tmp_path):
    c = Corpus(DerivedStore())
    for i in range(8):
        c.capture(f"orchid care note {i}")
    eng, ib = make(c, tmp_path)
    cur = eng.retrieve({"query": "orchid care", "limit": 3})["next_cursor"]
    assert eng.retrieve({"query": "orchid care", "limit": 3, "cursor": cur})["ok"]
    ib.rebuild_full()                                                        # generation 2, store untouched
    with pytest.raises(C.EnrichmentError) as e:
        eng.retrieve({"query": "orchid care", "limit": 3, "cursor": cur})
    assert e.value.code == "E_STALE_CURSOR"
    fresh = eng.retrieve({"query": "orchid care", "limit": 3})                # restart works, on generation 2
    assert fresh["index"]["generation"] == 2 and fresh["next_cursor"]


# --------------------------------------------------------------- fallback
def test_fts5_unavailable_falls_back_to_lexical_explicitly(enrichment_wiki, tmp_path, monkeypatch):
    w = enrichment_wiki
    c = fixture_corpus(w)
    legacy = Legacy(lexical={"captures": {"results": [legacy_hit(w.refs["fa_short"], "فیت پلن ...")]},
                             "canonical": {"results": []}})
    eng, ib = make(c, tmp_path, legacy=legacy)
    monkeypatch.setattr(IX, "fts5_available", lambda: False)
    out = eng.retrieve({"query": "فیت پلن", "limit": 5})
    assert out["path"] == "fallback-lexical" and out["index"]["state"] == "unavailable"
    assert out["index"]["generation"] is None and out["query_model_calls"] == 0
    assert any("fallback" in n for n in out["notes"])
    (hit,) = out["groups"]["captures"]["results"]
    assert hit["hit_level"] == "source" and hit["chunk_ref"] is None and hit["span"] is None
    assert hit["inclusion_reason"]["paths"] == ["fallback"] and hit["source_ref"] == w.refs["fa_short"]
    assert out["coverage"]["source_level_fallback_hits"] == 1
    assert legacy.calls == [("فیت پلن", "lexical", "all", 5)]


def test_lexical_failure_or_emptiness_falls_through_to_exact(tmp_path):
    c = Corpus(DerivedStore())
    c.capture("hello")
    exact = {"captures": {"results": [legacy_hit("cap:cap-20261001-000001-0001", "ctx")]}, "canonical": {"results": []}}
    for legacy in (Legacy(exact=exact, lexical_error=True), Legacy(exact=exact, lexical={"captures": {"results": []}})):
        eng, _ = make(c, tmp_path, legacy=legacy, refresh=False)               # no index at all
        out = eng.retrieve({"query": "hello"})
        assert out["path"] == "fallback-exact" and out["index"]["state"] == "missing"
        assert [m for _, m, _, _ in legacy.calls] == ["lexical", "exact"]
        assert out["groups"]["captures"]["count"] == 1
        assert any("exact" in n for n in out["notes"])


def test_other_fallback_triggers_are_explicit(tmp_path):
    c = Corpus(DerivedStore())
    c.capture("hello world")
    res = {"captures": {"results": [legacy_hit("cap:cap-20261001-000001-0001")]}, "canonical": {"results": []}}
    # retrieval switched off
    eng, ib = make(c, tmp_path, legacy=Legacy(lexical=res), config=EnrichmentConfig(mode="on", retrieval=False))
    out = eng.retrieve({"query": "hello"})
    assert out["path"] == "fallback-lexical" and any("RETRIEVAL is off" in n for n in out["notes"])
    # corrupt index
    ib.index_path.write_bytes(b"garbage" * 500)
    eng2 = RetrievalEngine(c.store, ib, config=CFG, legacy_search=Legacy(lexical=res))
    out = eng2.retrieve({"query": "hello"})
    assert out["path"] == "fallback-lexical" and out["index"]["state"] == "unavailable"
    # no base search injected: an explicit error, never an empty success
    eng3 = RetrievalEngine(c.store, ib, config=CFG)
    with pytest.raises(C.EnrichmentError) as e:
        eng3.retrieve({"query": "hello"})
    assert e.value.code == "E_UNAVAILABLE"
    # a cursor cannot be continued on a fallback path
    with pytest.raises(C.EnrichmentError) as e:
        eng2.retrieve({"query": "hello", "cursor": C.encode_cursor("retrieve", q="x")})
    assert e.value.code == "E_STALE_CURSOR"


def test_incomplete_backfill_adds_flagged_source_level_hits(enrichment_wiki, tmp_path):
    w = enrichment_wiki
    c = Corpus(DerivedStore())
    c.capture(w.texts["fa_short"], w.refs["fa_short"])                      # only ONE source is projected
    legacy = Legacy(lexical={"captures": {"results": [
        legacy_hit(w.refs["fa_short"], "covered by the index"),
        legacy_hit(w.refs["crlf"], "line one ... (not projected yet)")]}, "canonical": {"results": []}})
    eng, _ = make(c, tmp_path, legacy=legacy, wiki_root=w.root)
    out = eng.retrieve({"query": "line one فیت پلن"})
    assert out["path"] == "enrichment"
    assert out["index"]["state"] == "lagging" and out["index"]["lag"]["pending_artifacts"] > 0 and not out["index"]["fresh"]
    cov = out["coverage"]
    assert cov["source_level_fallback_hits"] == 1 and cov["indexed_artifacts"] == 1
    assert cov["total_artifacts"] > cov["indexed_artifacts"]
    by = {h["source_ref"]: h for h in out["groups"]["captures"]["results"]}
    src = by[w.refs["crlf"]]
    assert src["hit_level"] == "source" and src["chunk_ref"] is None and src["span"] is None
    assert src["inclusion_reason"]["paths"] == ["fallback"] and src["text"] == "line one ... (not projected yet)"
    assert by[w.refs["fa_short"]]["hit_level"] == "chunk"                       # the indexed one is NOT duplicated
    assert sum(1 for h in out["groups"]["captures"]["results"] if h["source_ref"] == w.refs["fa_short"]) == 1
    assert any("source-level" in n for n in out["notes"]) and any("lagging" in n for n in out["notes"])


def test_a_complete_index_never_calls_the_base_search(tmp_path):
    c = Corpus(DerivedStore())
    c.capture("hello world")
    legacy = Legacy()
    eng, _ = make(c, tmp_path, legacy=legacy)
    eng.retrieve({"query": "hello"})
    assert legacy.calls == []


# ------------------------------------------------------------- rejected
def test_rejected_review_revision_is_excluded_from_candidate_retrieval(tmp_path):
    c = Corpus(DerivedStore())
    a = c.capture("Short body about trains and weather.")
    rev = c.annotate(a.source_ref, {"tags": ["quokka"], "entities": [
        {"name": "Zorblax", "kind": "concept", "mention": {"chunk_ref": "FIRST", "text": "trains"}}]})
    eng, ib = make(c, tmp_path)
    assert refs(eng.retrieve({"query": "quokka"}), "captures") == [a.source_ref]
    c.store.review_state_of[(a.source_ref, rev)] = "rejected"
    assert ib.status(c.store)["state"] == "lagging"                            # the review moved the fingerprint
    ib.refresh(10)
    out = eng.retrieve({"query": "quokka"})
    assert refs(out, "captures") == [] and out["uses_candidate_annotations"] is False
    out = eng.retrieve({"query": "trains weather"})                             # the text path is untouched
    assert refs(out, "captures") == [a.source_ref]
    assert out["groups"]["captures"]["results"][0]["annotation_revision"] is None
    assert out["uses_candidate_annotations"] is False


# --------------------------------------------------------- input contract
@pytest.mark.parametrize("req,code", [
    ({}, "E_BAD_ARGUMENTS"), ({"query": ""}, "E_BAD_ARGUMENTS"), ({"query": "   "}, "E_BAD_ARGUMENTS"),
    ({"query": "x" * 1001}, "E_BAD_ARGUMENTS"), ({"query": 5}, "E_BAD_ARGUMENTS"),
    ({"query": "a", "scope": "everything"}, "E_BAD_ARGUMENTS"),
    ({"query": "a", "limit": 0}, "E_BAD_ARGUMENTS"), ({"query": "a", "limit": 51}, "E_BAD_ARGUMENTS"),
    ({"query": "a", "limit": True}, "E_BAD_ARGUMENTS"), ({"query": "a", "limit": "3"}, "E_BAD_ARGUMENTS"),
    ({"query": "a", "budget_tokens": 499}, "E_BAD_ARGUMENTS"),
    ({"query": "a", "budget_tokens": 32001}, "E_BAD_ARGUMENTS"),
    ({"query": "a", "cursor": ""}, "E_BAD_ARGUMENTS"), ({"query": "a", "cursor": 7}, "E_BAD_ARGUMENTS"),
    ({"query": "a", "path": "../x"}, "E_UNKNOWN_ARGUMENT"), ({"query": "a", "model": "x"}, "E_UNKNOWN_ARGUMENT"),
])
def test_request_validation(req, code, tmp_path):
    c = Corpus(DerivedStore())
    c.capture("a")
    eng, _ = make(c, tmp_path)
    with pytest.raises(C.EnrichmentError) as e:
        eng.retrieve(req)
    assert e.value.code == code
    with pytest.raises(C.EnrichmentError):
        eng.retrieve("not a dict")


def test_boundary_values_are_accepted(tmp_path):
    c = Corpus(DerivedStore())
    c.capture("alpha beta gamma")
    eng, _ = make(c, tmp_path)
    out = eng.retrieve({"query": "alpha", "limit": 50, "budget_tokens": 32000, "scope": "captures"})
    assert out["limit"] == 50 and out["budget"]["budget_tokens"] == 32000 and refs(out, "captures")
    assert eng.retrieve({"query": "alpha", "limit": 1, "budget_tokens": 500})["ok"]


# -------------------------------------------------- hard invariants
def test_no_model_network_or_subprocess_in_build_or_query(enrichment_wiki, tmp_path, monkeypatch):
    w = enrichment_wiki
    c = fixture_corpus(w)
    c.annotate(w.refs["fa_short"], {"entities": [{"name": "فیت پلن", "kind": "project", "aliases": ["fit plan"],
                                                  "mention": {"chunk_ref": "FIRST", "text": "فیت پلن"}}]})

    def boom(*a, **k):
        raise AssertionError("forbidden external call")

    for target, name in ((socket.socket, "connect"), (socket, "create_connection"), (socket, "getaddrinfo"),
                         (urllib.request, "urlopen"), (subprocess, "Popen"), (subprocess, "run")):
        monkeypatch.setattr(target, name, boom)
    legacy = Legacy(lexical={"captures": {"results": [legacy_hit(w.refs["crlf"])]}, "canonical": {"results": []}})
    eng, ib = make(c, tmp_path, legacy=legacy, wiki_root=w.root)
    for q in ("فیت پلن", "fit plan", "Nil", "obj-nil", "ferry to Kolan", "zebra quantum"):
        out = eng.retrieve({"query": q})
        assert out["query_model_calls"] == 0
    ib.rebuild_full()
    monkeypatch.setattr(IX, "fts5_available", lambda: False)
    assert eng.retrieve({"query": "x"})["query_model_calls"] == 0               # fallback path too


def test_retrieval_never_writes_the_corpus_and_never_rebuilds_inside_a_query(enrichment_wiki, tmp_path, monkeypatch):
    w = enrichment_wiki
    before = corpus_hashes(w.root)
    c = fixture_corpus(w)
    eng, ib = make(c, w.root, wiki_root=w.root)
    written = []
    monkeypatch.setattr(IndexBuilder, "_write_artifact", lambda *a, **k: written.append(1))
    monkeypatch.setattr(IndexBuilder, "rebuild_full", lambda *a, **k: written.append("full"))
    monkeypatch.setattr(IndexBuilder, "refresh", lambda *a, **k: written.append("refresh"))
    for q in ("فیت پلن", "Nil", "ferry", "nothing matches this at all"):
        eng.retrieve({"query": q})
    assert written == []
    assert corpus_hashes(w.root) == before


def test_p95_latency_on_a_few_hundred_sources(tmp_path):
    c = Corpus(DerivedStore())
    words = "garden train ferry orchid budget novel plan letter market river".split()
    for i in range(300):
        c.capture(f"entry {i}: " + " ".join(words[(i + k) % 10] for k in range(8)) + f" id{i}")
    for i in range(5):
        c.canonical(f"obj-{i}-aaaaa", f"# R{i}\n\nOrchid ferry budget record {i}\n")
    eng, ib = make(c, tmp_path)
    times = []
    for i in range(60):
        t0 = time.perf_counter()
        eng.retrieve({"query": f"{words[i % 10]} {words[(i + 3) % 10]}"})
        times.append(time.perf_counter() - t0)
    times.sort()
    p95 = times[int(len(times) * 0.95) - 1]
    assert p95 < 0.5, f"p95 {p95:.3f}s"
    assert statistics.median(times) < 0.25


def test_the_benchmark_has_r1_at_least_as_good_as_the_exact_baseline():
    import bench_retrieval as B
    out = B.run_benchmark()
    r = {x["system"]: x for x in out["results"]}
    assert r["r1"]["recall@10"] >= r["legacy-exact"]["recall@10"]
    assert r["r1"]["mrr"] >= r["legacy-exact"]["mrr"]
    assert r["r1-direct"]["recall@10"] >= r["legacy-exact"]["recall@10"]
    assert r["r1"]["recall@10"] >= r["r1-direct"]["recall@10"]
    assert all(x["negative_false_positives"] == 0 for x in out["results"])
