"""Derived graph (CONTRACT-E1 section 7): node/edge builders and bounded expansion.

Pure functions of a store item plus read-only lookups. Nothing here writes to
the store, the corpus or `_search/graph.db`.

Edge kinds (see the contract table)
-----------------------------------
structural  contains / next_chunk / derived_from       deterministic projection
registered  wikilink / cites_ref (+ graph.db bridge)   existing corpus
semantic    about / mentions / part_of_project / related_to   annotation, CANDIDATE
reading     supports / contradicts                            annotation, CANDIDATE reading

Invariants kept here
--------------------
* Entities are per-source nodes (`C.entity_id`). Similar names never merge; a
  cross-source join happens at query time through shared normal forms.
* Semantic/reading edges come only from a non-rejected annotation revision of
  the CURRENT source version and carry provenance (`candidate: true`), the
  source_version, the annotation_revision and the edge_kind.
* Topology carries no authority and no relevance: `expand` never ranks by
  degree, never starts from the densest hub and caps fan-out per node.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import contract as C

# kinds the retrieval walk may traverse. Structural edges are bookkeeping
# (contains / next_chunk): walking them would turn every artifact into a hub.
TRAVERSE_KINDS = ("registered", "semantic", "reading")
FANOUT_CAP = 10         # neighbours taken per expanded node (deterministic order)
HUB_DEGREE = 24         # a non-seed node with more traversable edges is a leaf
MAX_CITES = 50          # cites_ref edges per artifact
ALSO_VIA_MAX = 3        # extra inclusion paths remembered per node

WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")
IDENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
IDENT_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9][A-Za-z0-9_-]{4,127}(?![A-Za-z0-9_-])")
DOC_TOKEN = re.compile(r"\bdoc:([0-9a-f]{20})\b")
CAPTURE_ID_RE = re.compile(r"^cap-\d{8}-\d{6}-[0-9a-f]{4}$")
PUBLIC_REF_RE = re.compile(r"^(rec|doc|cap):[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


# ---------------------------------------------------------------- fragments
@dataclass
class GraphFragment:
    nodes: list[dict] = field(default_factory=list)
    edges: list[dict] = field(default_factory=list)


def node_row(node_id: str, kind: str, artifact_id: str | None, source_ref: str | None,
             label: str | None = None, normal: str | None = None, extra: dict | None = None) -> dict:
    return {"node_id": node_id, "kind": kind, "artifact_id": artifact_id,
            "source_ref": source_ref, "label": label, "normal": normal, "extra": extra or {}}


def edge_kind_for(relation: str) -> str:
    return "reading" if relation in C.READING_RELATIONS else "semantic"


# ------------------------------------------------------ annotation -> derived
def derive_from_annotation(source_ref: str, annotation: dict) -> dict:
    """Fallback when the store does not hand back its server-computed `derived`
    block: the same shape, computed deterministically from the validated
    annotation. Spans are unknown here (None); chunk refs are kept."""
    entities = []
    for e in annotation.get("entities") or []:
        name = e.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        normal = C.normal_form(name)
        mention = e.get("mention") or {}
        entities.append({"entity_id": C.entity_id(source_ref, normal), "name": name, "normal": normal,
                         "kind": e.get("kind", "other"), "aliases": list(e.get("aliases") or []),
                         "mention": {"chunk_ref": mention.get("chunk_ref"), "start_char": None,
                                     "end_char": None, "occurrences": None}})
    links = []
    for lk in annotation.get("links") or []:
        rel, tgt = lk.get("relation"), lk.get("target") or {}
        if rel not in C.LINK_RELATIONS:
            continue
        if "entity" in tgt:
            dst = C.entity_id(source_ref, C.normal_form(str(tgt["entity"])))
        elif "entity_id" in tgt:
            dst = tgt["entity_id"]
        elif "source_ref" in tgt:
            dst = tgt["source_ref"]
        else:
            continue
        links.append({"relation": rel, "edge_kind": edge_kind_for(rel), "src": None, "dst": dst,
                      "evidence": [{"chunk_ref": ev.get("chunk_ref"), "start_char": None,
                                    "end_char": None, "occurrences": None}
                                   for ev in lk.get("evidence") or []]})
    return {"entities": entities, "links": links}


def derived_of(item: dict) -> dict:
    """The store's `derived` block when present (item or annotation), else the
    deterministic recomputation from the annotation. A store that composes
    page-by-page revisions hands back `effective_derived`, which wins."""
    if isinstance(item.get("effective_derived"), dict):
        return item["effective_derived"]
    d = item.get("derived")
    if not isinstance(d, dict):
        ann = item.get("annotation") or {}
        d = ann.get("derived") if isinstance(ann.get("derived"), dict) else None
    if isinstance(d, dict):
        return d
    return derive_from_annotation(item["projection"].source_ref, item.get("annotation") or {})


def usable_annotation(item: dict) -> dict | None:
    """The current annotation only when it may be indexed: not rejected, not
    bound to another source version. Rejected/stale revisions are never
    indexed as current candidate metadata. When the store composed the
    revisions of the current version (`effective_annotation`, rejected ones
    already left out), that composition is the annotation."""
    if "effective_annotation" in item:
        eff = item["effective_annotation"]
        return eff if isinstance(eff, dict) and eff else None
    ann = item.get("annotation")
    if not isinstance(ann, dict) or not ann:
        return None
    if item.get("review_state") == "rejected":
        return None
    sv = ann.get("source_version") or item.get("annotation_source_version")
    if sv and sv != item["projection"].source_version:
        return None
    return ann


# ------------------------------------------------------------------- builder
class GraphBuilder:
    """Builds the graph fragment of one store item.

    `known_refs` are the public refs of every current projection (set by the
    index builder before a batch). With a `wiki_root`, an unknown record/capture
    target is additionally resolved through `brain_surface.refs` (read-only)
    and `_search/graph.db` is bridged read-only."""

    def __init__(self, wiki_root=None, known_refs=(), graph_db=None):
        self.wiki_root = Path(wiki_root) if wiki_root else None
        if graph_db is not None:
            self.graph_db = Path(graph_db)
        else:
            self.graph_db = (self.wiki_root / "_search" / "graph.db") if self.wiki_root else None
        self.known_refs = set(known_refs)
        self._graph_ids: set[str] | None = None
        self._resolved: dict[str, bool] = {}

    # -- lifecycle -----------------------------------------------------
    def reset(self, known_refs=()) -> None:
        self.known_refs = set(known_refs)
        self._graph_ids = None
        self._resolved = {}

    def stamp(self) -> str:
        """Cheap fingerprint of the bridged graph.db so a rebuilt graph.db
        makes canonical-record artifacts dirty."""
        try:
            if self.graph_db and self.graph_db.is_file():
                st = self.graph_db.stat()
                return f"{st.st_mtime_ns}:{st.st_size}"
        except OSError:
            pass
        return ""

    # -- read-only lookups ----------------------------------------------
    def _ro_connect(self):
        if not self.graph_db or not self.graph_db.is_file():
            return None
        try:
            return sqlite3.connect(f"file:{self.graph_db.as_posix()}?mode=ro", uri=True, timeout=2)
        except sqlite3.Error:
            return None

    def graph_ids(self) -> set[str]:
        if self._graph_ids is None:
            ids: set[str] = set()
            con = self._ro_connect()
            if con is not None:
                try:
                    ids = {str(r[0]) for r in con.execute("SELECT id FROM nodes")}
                except sqlite3.Error:
                    ids = set()
                finally:
                    con.close()
            self._graph_ids = ids
        return self._graph_ids

    def _refs_module(self):
        try:
            from brain_surface import refs as R  # noqa: PLC0415 - optional, read-only
            return R
        except Exception:  # noqa: BLE001 - corpus tooling absent: resolve by known refs only
            return None

    def resolves(self, ref: str) -> bool:
        """Does the public ref name an existing record/capture? known refs and
        graph.db ids first, then the public ref resolver (read-only)."""
        if ref in self.known_refs:
            return True
        if ref in self._resolved:
            return self._resolved[ref]
        ok = False
        kind, _, ident = ref.partition(":")
        if kind == "rec" and ident in self.graph_ids():
            ok = True
        elif self.wiki_root is not None:
            R = self._refs_module()
            if R is not None:
                try:
                    R.resolve(self.wiki_root, ref)
                    ok = True
                except Exception:  # noqa: BLE001 - BadRef/KeyError/anything: not resolvable
                    ok = False
        self._resolved[ref] = ok
        return ok

    def _ident_map(self) -> dict[str, str]:
        m: dict[str, str] = {}
        for ref in self.known_refs:
            kind, _, ident = ref.partition(":")
            if kind in ("rec", "cap") and len(ident) >= 5 and "-" in ident:
                m.setdefault(ident, ref)
        for ident in self.graph_ids():
            if len(ident) >= 5 and "-" in ident and IDENT_RE.fullmatch(ident):
                m.setdefault(ident, "rec:" + ident)
        return m

    # -- build -----------------------------------------------------------
    def build(self, item: dict) -> GraphFragment:
        proj, chunks = item["projection"], list(item.get("chunks") or [])
        art = "art:" + proj.artifact_id
        frag = GraphFragment()
        have_nodes: set[str] = set()
        seen_edges: set[tuple] = set()
        chunk_refs = {c.chunk_ref for c in chunks}

        def add_node(row: dict) -> None:
            if row["node_id"] not in have_nodes:
                have_nodes.add(row["node_id"])
                frag.nodes.append(row)

        def add_edge(src: str, dst: str, kind: str, label: str, *, revision: int | None = None,
                     provenance: dict | None = None) -> None:
            key = (src, dst, kind, label)
            if src == dst or key in seen_edges:
                return
            seen_edges.add(key)
            frag.edges.append({"src": src, "dst": dst, "edge_kind": kind, "label": label,
                               "artifact_id": proj.artifact_id, "source_ref": proj.source_ref,
                               "source_version": proj.source_version, "annotation_revision": revision,
                               "provenance": provenance or {"origin": "projection"}})

        add_node(node_row(art, "artifact", proj.artifact_id, proj.source_ref))

        # ---- structural (deterministic)
        for c in chunks:
            add_node(node_row(c.chunk_ref, "chunk", proj.artifact_id, proj.source_ref))
            add_edge(art, c.chunk_ref, "structural", "contains")
            if c.next_chunk_ref:
                add_edge(c.chunk_ref, c.next_chunk_ref, "structural", "next_chunk")
        for layer in proj.layers:
            if layer.tier == "derivative" and layer.text_ref != proj.source_ref:
                add_node(node_row(layer.text_ref, "ref", proj.artifact_id, layer.text_ref))
                add_edge(art, layer.text_ref, "structural", "derived_from",
                         provenance={"origin": "projection", "layer_id": layer.layer_id})

        # ---- registered (existing corpus)
        self._registered(proj, add_node, add_edge)

        # ---- semantic / reading (candidate, from a usable annotation)
        ann = usable_annotation(item)
        if ann is not None:
            self._annotation_edges(item, art, chunk_refs, add_node, add_edge)
        return frag

    # -- registered edges ------------------------------------------------
    def _registered(self, proj, add_node, add_edge) -> None:
        art = "art:" + proj.artifact_id
        own = {proj.source_ref} | {la.text_ref for la in proj.layers}
        have: set[tuple] = set()

        def link(dst: str, label: str, prov: dict) -> bool:
            if dst in own or (dst, label) in have:
                return False
            have.add((dst, label))
            add_node(node_row(dst, "ref", proj.artifact_id, dst))
            add_edge(art, dst, "registered", label, provenance=prov)
            return True

        if proj.source_kind == "canonical-record":
            for layer in proj.layers:
                for target in WIKILINK.findall(layer.text):
                    t = target.strip()
                    if IDENT_RE.fullmatch(t) and self.resolves("rec:" + t):
                        link("rec:" + t, "wikilink", {"origin": "layer-text", "target": t})
        cites = 0
        idents = self._ident_map()
        for layer in proj.layers:
            for m in IDENT_TOKEN.finditer(layer.text):
                if cites >= MAX_CITES:
                    break
                tok = m.group(0)
                ref = idents.get(tok)
                if ref is None and CAPTURE_ID_RE.fullmatch(tok) and self.resolves("cap:" + tok):
                    ref = "cap:" + tok
                if ref and link(ref, "cites_ref", {"origin": "layer-text", "token": tok}):
                    cites += 1
            for m in DOC_TOKEN.finditer(layer.text):
                ref = "doc:" + m.group(1)
                if cites < MAX_CITES and ref in self.known_refs and link(
                        ref, "cites_ref", {"origin": "layer-text", "token": m.group(1)}):
                    cites += 1
        # read-only bridge to the existing graph index (tables/build untouched)
        if proj.source_kind == "canonical-record" and proj.source_ref.startswith("rec:"):
            con = self._ro_connect()
            if con is not None:
                try:
                    rows = con.execute(
                        "SELECT field, target_kind, resolved_id FROM edges "
                        "WHERE src_id=? AND resolved_id IS NOT NULL ORDER BY field, resolved_id",
                        (proj.source_ref[4:],)).fetchall()
                except sqlite3.Error:
                    rows = []
                finally:
                    con.close()
                for fld, tkind, rid in rows:
                    if IDENT_RE.fullmatch(str(rid)):
                        link("rec:" + str(rid), "wikilink" if tkind == "link" else "cites_ref",
                             {"origin": "graph.db", "field": fld, "target_kind": tkind})

    # -- candidate edges ---------------------------------------------------
    def _annotation_edges(self, item, art, chunk_refs, add_node, add_edge) -> None:
        proj = item["projection"]
        rev = item.get("effective_revision", item.get("annotation_revision"))
        derived = derived_of(item)

        def rev_of(entry) -> int | None:
            r = entry.get("revision")        # composed entries keep the revision they came from
            return r if isinstance(r, int) and not isinstance(r, bool) else rev

        def valid_chunk(ref) -> bool:
            return isinstance(ref, str) and ref in chunk_refs

        def evidence_of(raw) -> list[dict]:
            out = []
            for ev in raw or []:
                if isinstance(ev, dict) and valid_chunk(ev.get("chunk_ref")):
                    out.append({"chunk_ref": ev["chunk_ref"], "start_char": ev.get("start_char"),
                                "end_char": ev.get("end_char"), "occurrences": ev.get("occurrences")})
            return out

        def prov(extra: dict, r=None) -> dict:
            return {"origin": "annotation", "candidate": True, "annotation_revision": rev if r is None else r,
                    **extra}

        def endpoint(raw) -> tuple[str | None, list[dict]]:
            """Normalise a derived src/dst. Chunk endpoints collapse to the
            artifact node, keeping the chunk as evidence."""
            if not raw:
                return art, []
            if C.ENTITY_ID_RE.fullmatch(raw):
                return raw, []
            if raw.startswith("art:"):
                return raw, []
            if C.CHUNK_REF_RE.fullmatch(raw):
                return (art, [{"chunk_ref": raw, "start_char": None, "end_char": None,
                               "occurrences": None}]) if valid_chunk(raw) else (None, [])
            if PUBLIC_REF_RE.fullmatch(raw):
                return (art if raw == proj.source_ref else raw), []
            return None, []

        for e in derived.get("entities") or []:
            eid = e.get("entity_id")
            name = e.get("name")
            if not (isinstance(eid, str) and C.ENTITY_ID_RE.fullmatch(eid) and isinstance(name, str)):
                continue
            normal = e.get("normal") or C.normal_form(name)
            aliases = [a for a in (e.get("aliases") or []) if isinstance(a, str)]
            add_node(node_row(eid, "entity", proj.artifact_id, proj.source_ref, name, normal,
                              {"kind": e.get("kind"), "aliases": aliases}))
            m = e.get("mention") or {}
            ev = evidence_of([m]) if m else []
            add_edge(art, eid, "semantic", "mentions", revision=rev_of(e),
                     provenance=prov({"edge_kind": "semantic", "evidence": ev}, rev_of(e)))
        for lk in derived.get("links") or []:
            rel = lk.get("relation")
            if rel not in C.LINK_RELATIONS:
                continue
            kind = edge_kind_for(rel)               # recomputed: never trusted from the caller
            src, sev = endpoint(lk.get("src"))
            dst, dev = endpoint(lk.get("dst"))
            if src is None or dst is None:
                continue
            evidence = evidence_of(lk.get("evidence")) or sev or dev
            if not dst.startswith(("art:", "ent:")):
                add_node(node_row(dst, "ref", proj.artifact_id, dst))
            add_edge(src, dst, kind, rel, revision=rev_of(lk),
                     provenance=prov({"edge_kind": kind, "evidence": evidence,
                                      "reading": kind == "reading"}, rev_of(lk)))


# -------------------------------------------------------------------- expand
@dataclass
class Expansion:
    nodes: list[dict] = field(default_factory=list)   # insertion = traversal order
    truncated: bool = False
    hub_capped: list[str] = field(default_factory=list)
    hub_leaf: list[str] = field(default_factory=list)


def describe_step(step: dict) -> str:
    return (f"{step['edge_kind']}:{step['label']} "
            f"({'out' if step['direction'] == 'out' else 'in'})")


def expand(neighbors, seeds, *, max_depth: int = C.GRAPH_MAX_DEPTH,
           max_nodes: int = C.GRAPH_MAX_NODES, fanout_cap: int = FANOUT_CAP,
           hub_degree: int = HUB_DEGREE) -> Expansion:
    """Bounded breadth-first walk from the given seeds.

    `neighbors(node) -> [{"other","edge_kind","label","direction","owner",
    "annotation_revision","evidence"}]` yields traversable edges of a node.
    Seeds are taken in the order given (<= GRAPH_MAX_SEEDS): the caller derives
    them from metadata/entity matches or explicit refs, never from degree.
    Every returned node carries its depth, the seed it descends from and the
    list of steps that reached it (`via`) - the inclusion reason."""
    depth_cap = max(0, min(int(max_depth), C.GRAPH_MAX_DEPTH))
    node_cap = max(1, min(int(max_nodes), C.GRAPH_MAX_NODES))
    out = Expansion()
    visited: dict[str, dict] = {}
    frontier: list[str] = []
    for s in list(dict.fromkeys(seeds))[:C.GRAPH_MAX_SEEDS]:
        if len(visited) >= node_cap:
            out.truncated = True
            break
        rec = {"node": s, "depth": 0, "seed": s, "via": [], "also_via": [], "reason": "seed"}
        visited[s] = rec
        out.nodes.append(rec)
        frontier.append(s)
    for depth in range(1, depth_cap + 1):
        nxt: list[str] = []
        for node in frontier:
            parent = visited[node]
            edges = sorted(neighbors(node), key=lambda e: (e["label"], e["edge_kind"], e["other"],
                                                           e["direction"]))
            degree = len(edges)
            if degree > hub_degree and parent["depth"] > 0:
                out.hub_leaf.append(node)             # reached, but not walked through
                continue
            if degree > fanout_cap:
                out.hub_capped.append(node)
                edges = edges[:fanout_cap]
            for e in edges:
                other = e["other"]
                step = {"from": node, "to": other, **{k: e[k] for k in
                        ("edge_kind", "label", "direction", "owner", "annotation_revision", "evidence")}}
                if other in visited:
                    seen = visited[other]
                    if seen["depth"] and len(seen["also_via"]) < ALSO_VIA_MAX \
                            and seen["via"][-1]["from"] != node:
                        seen["also_via"].append(parent["via"] + [step])
                    continue
                if len(visited) >= node_cap:
                    out.truncated = True
                    continue
                rec = {"node": other, "depth": depth, "seed": parent["seed"],
                       "via": parent["via"] + [step], "also_via": [],
                       "reason": f"{depth}-hop from {parent['seed']} via " +
                                 " > ".join(describe_step(x) for x in parent["via"] + [step])}
                visited[other] = rec
                out.nodes.append(rec)
                nxt.append(other)
        frontier = nxt
        if not frontier:
            break
    return out
