"""brain_retrieve (CONTRACT-E1 section 8): evidence-bearing retrieval.

Direct chunk-text FTS and the candidate-metadata path run first; a bounded
graph walk adds neighbours; the lists are fused with weighted RRF (ranks only);
every candidate is re-checked against the CURRENT source version; the answer is
the exact chunk text under a token/char/byte budget. No model, no network, no
subprocess is ever used here. Retrieval rank, graph position and model
metadata never raise authority: a hit carries the tier/level of the text it
returns (an ingested file reports its DERIVATIVE, level 5).

Injected collaborators
----------------------
store         ProjectionStore: get_chunk, change_marker (+ list_current via index)
index         IndexBuilder-like: status(store) and open_reader()
legacy_search callable(query, mode, scope, n) -> {group: {"results": [...]}} in
              the shape of brain_surface.backend.WikiBackend.search; used ONLY
              for the explicit fallbacks and for sources lacking a projection
source_fresh  callable(source_ref, source_version) -> bool re-checking the disk
wiki_root     optional; only used to count sources on disk (coverage honesty)
"""
from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import contract as C
from .indexing import IndexUnavailable, tokens_of
from .tools_spec import AUTHORITY_NOTE

try:                                         # reuse the legacy stop-word lists when importable
    from search_lexical import _STOP_EN, _STOP_FA, _fa_stem  # type: ignore
except Exception:  # noqa: BLE001
    _STOP_EN = set("a an the of is are was were be been what which who how why when where do does did in on "
                   "at to for and or not that this with by from about it as".split())
    _STOP_FA = set("چیست چه چی چگونه چرا کجا کی کدام آیا است هست بود شد در از به با که را این آن و یا برای "
                   "درباره تا هم نیز".split())

    def _fa_stem(token: str) -> str:
        for suf in ("ترین", "های", "ها", "ات", "ان", "تر", "ین"):
            if token.endswith(suf) and len(token) - len(suf) >= 3:
                return token[: -len(suf)]
        return token

_STOP = {C.normal_form(w) for w in (*_STOP_EN, *_STOP_FA)}

# --------------------------------------------------------------- parameters
HIT_OVERHEAD_TOKENS = 30        # refs, labels and reason per hit are budgeted too
MIN_PARTIAL_TOKENS = 80         # a later hit is cut to a prefix only when this much budget remains
TEXT_POOL = 120                 # FTS rows read per group (strict + relaxed)
META_POOL = 60
PER_SOURCE_CAP = 3              # chunks of one source in one group's ordered list
SNIPPET_MAX_CHARS = 1500        # fallback snippets are bounded
FALLBACK_POOL_MAX = 50
REQUEST_KEYS = ("query", "scope", "limit", "budget_tokens", "cursor")
SCOPES = ("canonical", "captures", "all")
GROUP_ORDER = ("canonical", "captures")
REF_IN_QUERY = re.compile(r"(?<![A-Za-z0-9_-])(?:(?:rec|doc|cap):[A-Za-z0-9][A-Za-z0-9_-]{0,127}|chk:[0-9a-f]{32})")
BARE_ID = re.compile(r"(?<![A-Za-z0-9_:-])[A-Za-z0-9][A-Za-z0-9_-]{4,127}(?![A-Za-z0-9_-])")
CAPTURE_ID = re.compile(r"^cap-\d{8}-\d{6}-[0-9a-f]{4}$")
FIELD_PRIORITY = {"entity": 0, "alias": 1, "topic": 2, "tag": 3, "chunk_tag": 4, "chunk_summary": 5,
                  "summary": 6}
TIER_LEVEL = {"capture": 7, "candidate": 7, "derivative": 5, "source-record": 4, "canonical-record": 4}


@dataclass
class Cand:
    key: str
    group: str
    source_ref: str
    artifact_id: str | None = None
    chunk_ref: str | None = None
    source_version: str | None = None
    paths: list = field(default_factory=list)
    via: list = field(default_factory=list)
    matched: list = field(default_factory=list)
    ann: bool = False
    r_text: int | None = None
    r_meta: int | None = None
    r_legacy: int | None = None
    r_graph: int | None = None
    legacy: dict | None = None
    rank_direct: int | None = None
    score: float = 0.0

    @property
    def is_direct(self) -> bool:
        return bool(self.r_text or self.r_meta or self.r_legacy)

    def add_path(self, path: str, via=(), matched=()) -> None:
        if path not in self.paths:
            self.paths.append(path)
        for v in via:
            if v and v not in self.via:
                self.via.append(v)
        for m in matched:
            if m and m not in self.matched:
                self.matched.append(m)


class _Stale(Exception):
    """A candidate no longer matches the current source version."""


def _best(a, b):
    return b if a is None else a if b is None else min(a, b)


# ---------------------------------------------------------------- the engine
class RetrievalEngine:
    def __init__(self, store, index, *, config, legacy_search=None, source_fresh=None, wiki_root=None):
        self.store = store
        self.index = index
        self.config = config
        self.legacy_search = legacy_search
        self.source_fresh = source_fresh
        self.wiki_root = Path(wiki_root) if wiki_root else None
        self.graph_enabled = True           # tests flip it to prove the graph never hides direct text

    # ---------------------------------------------------------- validation
    @staticmethod
    def _validate(request) -> dict:
        if not isinstance(request, dict):
            raise C.EnrichmentError("E_BAD_ARGUMENTS", "arguments must be an object")
        unknown = sorted(k for k in request if k not in REQUEST_KEYS)
        if unknown:
            raise C.EnrichmentError("E_UNKNOWN_ARGUMENT", "unknown argument(s)", arguments=unknown)
        q = request.get("query")
        if not isinstance(q, str) or not q.strip():
            raise C.EnrichmentError("E_BAD_ARGUMENTS", "query must be a non-empty string")
        if len(q) > 1000:
            raise C.EnrichmentError("E_BAD_ARGUMENTS", "query is longer than 1000 characters")
        scope = request.get("scope", "all")
        if scope not in SCOPES:
            raise C.EnrichmentError("E_BAD_ARGUMENTS", f"scope must be one of {list(SCOPES)}")

        def integer(name, default, lo, hi):
            v = request.get(name, default)
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                raise C.EnrichmentError("E_BAD_ARGUMENTS", f"{name} must be an integer in [{lo},{hi}]")
            return v

        cursor = request.get("cursor")
        if cursor is not None and (not isinstance(cursor, str) or not 1 <= len(cursor) <= 2048):
            raise C.EnrichmentError("E_BAD_ARGUMENTS", "cursor must be a string of 1..2048 characters")
        return {"query": q.strip(), "scope": scope,
                "limit": integer("limit", C.DEFAULT_LIMIT, 1, C.MAX_LIMIT),
                "budget": integer("budget_tokens", C.DEFAULT_BUDGET_TOKENS, 500, C.MAX_BUDGET_TOKENS),
                "cursor": cursor}

    # ------------------------------------------------------------- terms
    @staticmethod
    def _terms(query: str) -> dict:
        toks = tokens_of(query)
        content = [t for t in toks if t not in _STOP] or toks
        content = list(dict.fromkeys(content))
        return {"all": toks, "content": content, "norm": C.normal_form(query)}

    @staticmethod
    def _and(tokens) -> str | None:
        return " AND ".join(f'"{t}"' for t in tokens) if tokens else None

    @staticmethod
    def _or(tokens) -> str | None:
        parts = []
        for t in tokens:
            parts.append(f'"{t}"')
            stem = _fa_stem(t)
            if stem != t and len(stem) >= 3:
                parts.append(f'"{stem}" *')
        return " OR ".join(parts) if parts else None

    @staticmethod
    def _matched_tokens(tokens, norm_text: str) -> list[str]:
        have = set(re.findall(r"[^\W_]+", norm_text))
        out = []
        for t in tokens:
            stem = _fa_stem(t)
            if t in have or (stem != t and len(stem) >= 3 and any(h.startswith(stem) for h in have)):
                out.append(t)
        return out

    # ------------------------------------------------------------ retrieve
    def retrieve(self, request: dict) -> dict:
        args = self._validate(request)
        if not getattr(self.config, "retrieval", False):
            return self._fallback(args, None, "BRAIN_ENRICHMENT_RETRIEVAL is off")
        status = self.index.status(self.store)
        if status["state"] in ("missing", "unavailable"):
            return self._fallback(args, status, f"index {status['state']}"
                                  + (f" ({status['reason']})" if status.get("reason") else ""))
        try:
            reader = self.index.open_reader()
        except IndexUnavailable as exc:
            return self._fallback(args, {**status, "state": exc.state, "generation": None}, f"index {exc.state}")
        try:
            try:
                return self._enriched(args, reader, status)
            except sqlite3.DatabaseError:
                return self._fallback(args, {**status, "state": "unavailable", "generation": None},
                                      "index unreadable (corrupt)")
        finally:
            reader.close()

    # -------------------------------------------------------------- cursor
    def _check_cursor(self, args, generation, marker) -> dict | None:
        if not args["cursor"]:
            return None
        cur = C.decode_cursor(args["cursor"], "retrieve")
        if cur.get("q") != C.sha256_text(args["query"]) or cur.get("s") != args["scope"]:
            raise C.EnrichmentError("E_BAD_CURSOR", "cursor belongs to a different query or scope")
        if cur.get("g") != generation or cur.get("f") != marker:
            raise C.EnrichmentError("E_STALE_CURSOR",
                                    "the index or the sources changed since this cursor was issued; "
                                    "restart the query without a cursor")
        if not isinstance(cur.get("o"), dict) or not isinstance(cur.get("w"), int):
            raise C.EnrichmentError("E_BAD_CURSOR", "cursor is malformed")
        return cur

    def _make_cursor(self, args, generation, marker, pos: dict, window: int, supplemented: bool = False) -> str:
        extra = {"x": 1} if supplemented else {}          # later pages rebuild the same (supplemented) order
        return C.encode_cursor("retrieve", q=C.sha256_text(args["query"]), s=args["scope"], g=generation,
                               f=marker, o=pos, w=window, **extra)

    # ----------------------------------------------------- enrichment path
    def _enriched(self, args, reader, status) -> dict:
        query, scope, limit, budget = args["query"], args["scope"], args["limit"], args["budget"]
        marker = self._marker()
        cur = self._check_cursor(args, reader.generation, marker)
        window = cur["w"] if cur else limit
        groups = [g for g in GROUP_ORDER if scope in ("all", g)]
        terms = self._terms(query)
        notes: list[str] = []
        counts = reader.counts()

        text = {g: self._text_hits(reader, g, terms) for g in groups}
        meta = {g: self._meta_hits(reader, g, terms) for g in groups}
        explicit = self._explicit(reader, query, groups)

        cands: dict[str, dict[str, Cand]] = {g: {} for g in groups}
        rep_cache: dict = {}

        def rep_chunk(artifact_id: str, group: str) -> str | None:
            """The representative chunk of an artifact-level (metadata/graph) match:
            the best direct-text chunk, else the best in-artifact text match, else
            the first chunk. Never a chunk invented from a summary."""
            if artifact_id in rep_cache:
                return rep_cache[artifact_id]
            best = None
            for c in cands[group].values():
                if c.artifact_id == artifact_id and c.r_text and (best is None or c.r_text < best.r_text):
                    best = c
            ref = best.chunk_ref if best else None
            if ref is None:
                m = self._or(terms["content"])
                rows = reader.search_chunks(m, 1, artifact_id=artifact_id) if m else []
                ref = rows[0]["chunk_ref"] if rows else reader.first_chunk(artifact_id)
            rep_cache[artifact_id] = ref
            return ref

        def cand_for(chunk_ref: str, art: dict, group: str) -> Cand | None:
            if chunk_ref is None:
                return None
            c = cands[group].get(chunk_ref)
            if c is None:
                c = Cand(chunk_ref, group, art["source_ref"], art["artifact_id"], chunk_ref,
                         art["source_version"])
                cands[group][chunk_ref] = c
            return c

        # direct text route
        for g in groups:
            for i, row in enumerate(text[g], 1):
                art = reader.artifact_by_id(row["artifact_id"])
                c = cand_for(row["chunk_ref"], art, g)
                c.r_text = _best(c.r_text, i)
                c.add_path("direct_text", matched=row["matched"])
        # metadata route: explicit refs lead, then entity/alias/topic/tag/summary matches
        r = 0
        for g in groups:
            for e in explicit[g]:
                r += 1
                art = e["artifact"]
                chk = e["chunk_ref"] or rep_chunk(art["artifact_id"], g)
                c = cand_for(chk, art, g)
                if c:
                    c.r_meta = _best(c.r_meta, r)
                    c.add_path("metadata", via=[art["source_ref"]], matched=[f"ref:{e['ref']}"])
        for g in groups:
            for i, m in enumerate(meta[g], 1):
                art = reader.artifact_by_id(m["artifact_id"])
                chk = m["chunk_ref"] if m["chunk_ref"] and reader.chunk_info(m["chunk_ref"]) else None
                chk = chk or rep_chunk(m["artifact_id"], g)
                c = cand_for(chk, art, g)
                if c:
                    c.r_meta = _best(c.r_meta, i)
                    c.ann = True
                    c.add_path("metadata", via=m["via"], matched=m["matched"])

        # graph route (seeds: explicit refs, metadata/entity matches; never a hub)
        graph_notes: list[str] = []
        if self.graph_enabled:
            self._graph(reader, groups, explicit, meta, cands, cand_for, rep_chunk, graph_notes)
        notes.extend(graph_notes)
        ambiguous = self._ambiguous_entities(meta)
        if ambiguous:
            notes.append(f"{ambiguous} distinct per-source entities match the query; they are "
                         "listed per source, never merged")

        # sources the index does not (yet) cover -> source-level base-search candidates
        coverage_total, pending = self._coverage(status, counts)
        fallback_hits = 0
        if pending > 0 and self.legacy_search is not None:
            fallback_hits = self._legacy_supplement(args, groups, cands, reader, notes)
        elif pending > 0:
            notes.append("index coverage is incomplete and no base search is available to cover the gap")

        # fuse, order, page
        supplemented = bool(cur and cur.get("x") == 1 and pending == 0 and self.legacy_search is not None)
        if supplemented:                                # page 1 added stale/empty-result base hits: same order
            fallback_hits = self._legacy_supplement(args, groups, cands, reader, notes, allow_stale=True)
        ordered = {g: self._order(list(cands[g].values()), window, notes) for g in groups}
        pos = {g: (cur["o"].get(g, 0) if cur else 0) for g in groups}
        page = self._assemble(args, groups, ordered, pos, reader)
        empty = not any(page["hits"][g] for g in groups)
        if (not cur and pending == 0 and self.legacy_search is not None and (page["stale"] or empty)):
            # A source edited after it was indexed (a promotion, a transcript added to a voice capture)
            # is dropped as stale, and its NEW text is not in the index until the next discovery pass.
            # The base search sees the disk: ask it, keep only refs the index does not cover or covers
            # with a superseded version. Never an empty "not found" while the corpus says otherwise.
            fallback_hits = self._legacy_supplement(args, groups, cands, reader, notes, allow_stale=True)
            if fallback_hits:
                supplemented = True
                ordered = {g: self._order(list(cands[g].values()), window, notes) for g in groups}
                page = self._assemble(args, groups, ordered, {g: 0 for g in groups}, reader)
        has_more = any(page["pos"][g] < len(ordered[g]) for g in groups)
        budget_stop = page["budget_stop"]
        reason = "budget" if budget_stop else ("limit" if has_more else None)
        used_ann = any(h["_ann"] for g in groups for h in page["hits"][g])
        if used_ann:
            notes.append("candidate (model-produced, unreviewed) metadata or its edges contributed to this "
                         "result; hit text is exact source text")
        state = "lagging" if pending > 0 else status["state"]
        if state == "lagging":
            notes.append(f"index is lagging: {pending} artifact(s) not yet current; source-level fallback hits "
                         "cover sources without a current indexed projection")
        if page["stale"]:
            notes.append(f"{page['stale']} indexed chunk(s) were dropped because their source changed on disk after "
                         "it was indexed; the next discovery pass (brain_enrichment_pending or the CLI scan) "
                         "re-projects it, base-search hits cover its current text meanwhile")
        return self._envelope(
            args, path="enrichment", used_ann=used_ann, page=page, groups=groups, notes=notes,
            index={"generation": reader.generation, "state": state,
                   "lag": {"pending_artifacts": pending},
                   "fresh": pending == 0 and state == "ok" and not page["stale"]},
            coverage={"indexed_artifacts": counts["artifacts"], "total_artifacts": coverage_total,
                      "source_level_fallback_hits": fallback_hits, "stale_dropped": page["stale"]},
            truncated=bool(reason), reason=reason,
            next_cursor=(self._make_cursor(args, reader.generation, marker, page["pos"], window, supplemented)
                         if has_more else None))

    # ----------------------------------------------------------- routes
    def _marker(self):
        try:
            return self.store.change_marker()
        except Exception:  # noqa: BLE001
            return None

    def _text_hits(self, reader, group, terms) -> list[dict]:
        out, seen = [], set()
        strict = self._and(terms["content"])
        for row in (reader.search_chunks(strict, TEXT_POOL, group) if strict else []):
            row["matched"] = self._matched_tokens(terms["content"], row["norm"])
            out.append(row)
            seen.add(row["chunk_ref"])
        relaxed = self._or(terms["content"]) if len(terms["content"]) > 1 or not out else None
        if relaxed and len(out) < TEXT_POOL:
            need = max(1, math.ceil(0.5 * len(terms["content"])))
            for row in reader.search_chunks(relaxed, TEXT_POOL, group):
                if row["chunk_ref"] in seen:
                    continue
                row["matched"] = self._matched_tokens(terms["content"], row["norm"])
                if len(row["matched"]) >= need:
                    out.append(row)
                    seen.add(row["chunk_ref"])
        return out[:TEXT_POOL]

    def _meta_hits(self, reader, group, terms) -> list[dict]:
        """Candidate-metadata matches, merged per (artifact, chunk). Ordered by
        match quality, then field priority, then BM25 - ranks only."""
        rows: list[tuple] = []
        content = terms["content"]
        strict = self._and(content)
        seen_rows = set()
        for row in (reader.search_meta(strict, META_POOL, group) if strict else []):
            rows.append((1, row))
            seen_rows.add((row["artifact_id"], row["chunk_ref"], row["field"], row["node_id"], row["norm"]))
        if not rows or len(content) > 1:
            relaxed = self._or(content)
            need = max(1, math.ceil(0.5 * len(content)))
            for row in (reader.search_meta(relaxed, META_POOL, group) if relaxed else []):
                key = (row["artifact_id"], row["chunk_ref"], row["field"], row["node_id"], row["norm"])
                if key in seen_rows:
                    continue
                if len(self._matched_tokens(content, row["norm"])) >= need:
                    rows.append((2, row))
        merged: dict[tuple, dict] = {}
        order = []
        for quality, row in rows:
            toks = re.findall(r"[^\W_]+", row["norm"])
            if toks == terms["all"]:
                quality = 0                              # the row IS the query
            matched = self._matched_tokens(content, row["norm"])
            k = (row["artifact_id"], row["chunk_ref"])
            sort_key = (quality, FIELD_PRIORITY.get(row["field"], 9), row["score"])
            m = merged.get(k)
            via = [row["node_id"]] if row["node_id"] else []
            label = f"{row['field']}:{' '.join(matched)}"      # the QUERY's tokens, never the stored text
            if m is None:
                m = {"artifact_id": row["artifact_id"], "chunk_ref": row["chunk_ref"], "key": sort_key,
                     "via": [], "matched": [], "nodes": [], "fields": []}
                merged[k] = m
                order.append(m)
            m["key"] = min(m["key"], sort_key)
            m["via"] += [v for v in via if v not in m["via"]]
            m["nodes"] += [(row["field"], row["node_id"])] if row["node_id"] else []
            m["fields"].append(row["field"])
            if label not in m["matched"]:
                m["matched"].append(label)
        order.sort(key=lambda m: (m["key"], m["artifact_id"], m["chunk_ref"] or ""))
        return order

    def _explicit(self, reader, query, groups) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {g: [] for g in groups}
        seen = set()
        found = [m.group(0) for m in REF_IN_QUERY.finditer(query)]
        for tok in BARE_ID.findall(query):
            if "-" in tok and tok not in found:
                found.append("cap:" + tok if CAPTURE_ID.fullmatch(tok) else "rec:" + tok)
        for ref in found:
            if ref in seen:
                continue
            seen.add(ref)
            if ref.startswith("chk:"):
                info = reader.chunk_info(ref)
                art = reader.artifact_by_id(info["artifact_id"]) if info else None
                chunk = ref if info else None
            else:
                art, chunk = reader.artifact_by_ref(ref), None
            if art and art["grp"] in out:
                out[art["grp"]].append({"ref": ref, "artifact": art, "chunk_ref": chunk})
        return out

    @staticmethod
    def _ambiguous_entities(meta) -> int:
        """Distinct per-source entity nodes matched by the query (>1 = the same
        name in different sources; they are never merged)."""
        nodes = {node for g in meta for m in meta[g] for field_, node in m["nodes"]
                 if field_ in ("entity", "alias")}
        return len(nodes) if len(nodes) > 1 else 0

    def _graph(self, reader, groups, explicit, meta, cands, cand_for, rep_chunk, notes) -> None:
        seeds: list[str] = []
        seed_ann: dict[str, bool] = {}

        def add(node, ann):
            if node not in seed_ann:
                seed_ann[node] = ann
                seeds.append(node)

        for g in groups:
            for e in explicit[g]:
                add("art:" + e["artifact"]["artifact_id"], False)
        ranked = sorted(((i, gi, m) for gi, g in enumerate(groups) for i, m in enumerate(meta[g])),
                        key=lambda t: (t[0], t[1]))
        for _i, _gi, m in ranked:
            for _field, node in m["nodes"]:
                add(node, True)
            add("art:" + m["artifact_id"], True)
        if not seeds:
            return
        exp = reader.expand(seeds[:C.GRAPH_MAX_SEEDS])
        if exp.truncated:
            notes.append(f"graph expansion stopped at {C.GRAPH_MAX_NODES} nodes")
        if exp.hub_capped or exp.hub_leaf:
            notes.append(f"hub guard applied to {len(set(exp.hub_capped) | set(exp.hub_leaf))} node(s): "
                         f"fan-out is capped; degree is never a relevance signal")
        rank = {g: 0 for g in groups}
        for rec in exp.nodes:
            node = rec["node"]
            if rec["depth"] == 0 or not node.startswith("art:"):
                continue
            art = reader.artifact_by_id(node[4:])
            if not art or art["grp"] not in rank:
                continue
            g = art["grp"]
            last = rec["via"][-1]
            chk = None
            for ev in last["evidence"]:
                info = reader.chunk_info(ev)
                if info and info["artifact_id"] == art["artifact_id"] and last["owner"] == art["artifact_id"]:
                    chk = ev
                    break
            chk = chk or rep_chunk(art["artifact_id"], g)
            c = cand_for(chk, art, g)
            if c is None:
                continue
            rank[g] += 1
            c.r_graph = _best(c.r_graph, rank[g])
            ann = seed_ann.get(rec["seed"], False) or any(s["edge_kind"] in ("semantic", "reading")
                                                          for s in rec["via"])
            c.ann = c.ann or ann
            via = [self._public(reader, rec["seed"])] + [self._public(reader, s["to"]) for s in rec["via"][:-1]]
            c.add_path("graph", via=via, matched=[f"{s['edge_kind']}:{s['label']}" for s in rec["via"]])

    @staticmethod
    def _public(reader, node: str) -> str:
        if node.startswith("art:"):
            art = reader.artifact_by_id(node[4:])
            return art["source_ref"] if art else node
        return node

    # --------------------------------------------------------- coverage
    def _disk_total(self) -> int | None:
        if self.wiki_root is None:
            return None
        root = self.wiki_root
        try:
            def md(rel, recursive=True):
                d = root / rel
                if not d.is_dir():
                    return []
                it = d.rglob("*.md") if recursive else d.glob("*.md")
                return [p for p in it if p.is_file() and ".quarantine" not in p.parts]

            caps = [p for p in md("01-inbox/captures") if CAPTURE_ID.fullmatch(p.stem)]
            recs = md("02-sources/records", False)
            rec_ids = {p.stem for p in recs}
            texts = [p for p in md("02-sources/text", False) if p.name.split("--", 1)[0] not in rec_ids]
            canon = [p for z in ("03-objects", "04-notes", "05-claims", "06-relations") for p in md(z)]
            return len(caps) + len(recs) + len(texts) + len(canon)
        except OSError:
            return None

    def _coverage(self, status, counts) -> tuple[int, int]:
        store_total = status.get("store_artifacts", counts["artifacts"])
        disk = self._disk_total()
        total = max(store_total, disk or 0)
        pending = status["lag"]["pending_artifacts"] + max(0, (disk or 0) - store_total)
        return total, pending

    # ------------------------------------------------------ legacy bridge
    def _call_legacy(self, query, mode, scope, n):
        return self.legacy_search(query, mode, scope, n)

    @staticmethod
    def _legacy_flat(res, group) -> list[dict]:
        g = (res or {}).get(group) or {}
        return [h for h in g.get("results", []) if isinstance(h, dict) and h.get("ref")]

    def _stale_on_disk(self, art: dict) -> bool:
        """The indexed version of this artifact is no longer the one on disk (one fresh read)."""
        try:
            if self.source_fresh is not None:
                return not self.source_fresh(art["source_ref"], art["source_version"])
            reader = getattr(self.store, "reader", None)
            if reader is None:
                return False
            return reader.load(art["source_ref"]).source_version != art["source_version"]
        except Exception:  # noqa: BLE001 - gone/unreadable: the base hit itself says what is on disk
            return True

    def _legacy_supplement(self, args, groups, cands, reader, notes, allow_stale: bool = False) -> int:
        n = min(FALLBACK_POOL_MAX, max(args["limit"], 10))
        res, mode = None, "lexical"
        try:
            res = self._call_legacy(args["query"], "lexical", args["scope"], n)
        except Exception:  # noqa: BLE001 - base search unavailable (e.g. no qmd): try exact, say so
            notes.append("base lexical search unavailable; used exact search for uncovered sources")
        if res is None or not any(self._legacy_flat(res, g) for g in groups):
            try:
                res, mode = self._call_legacy(args["query"], "exact", args["scope"], n), "exact"
            except Exception:  # noqa: BLE001
                notes.append("base search unavailable; uncovered sources could not be searched")
                return 0
        added = 0
        for g in groups:
            r = 0
            for h in self._legacy_flat(res, g):
                ref = h["ref"]
                lookup = getattr(reader, "artifact_by_text_ref", reader.artifact_by_ref)
                art = lookup(ref)                  # a held file's derivative doc: is its rec: holding
                if art is not None and not (allow_stale and self._stale_on_disk(art)):
                    continue                       # covered by the index: its chunk routes already see it
                if "src:" + ref in cands[g]:
                    continue
                r += 1
                c = Cand("src:" + ref, g, ref, legacy=h)
                c.r_legacy = r
                c.add_path("fallback", via=[ref])
                cands[g].setdefault(c.key, c)
                added += 1
        if added:
            notes.append(f"{added} source-level hit(s) from the base {mode} search for sources without a "
                         "current indexed projection (no chunk_ref; open the source to quote it)")
        return added

    # ----------------------------------------------------------- ordering
    def _order(self, cands: list[Cand], window: int, notes: list[str]) -> list[Cand]:
        k, wd, wg = C.RRF_K, C.RRF_WEIGHTS["direct"], C.RRF_WEIGHTS["graph"]
        direct = [c for c in cands if c.is_direct]
        for c in direct:
            c.score = sum(1.0 / (k + r) for r in (c.r_text, c.r_meta, c.r_legacy) if r)
        direct.sort(key=lambda c: (-c.score, min(r for r in (c.r_text, c.r_meta, c.r_legacy) if r), c.key))
        for i, c in enumerate(direct, 1):
            c.rank_direct = i
        graph = sorted((c for c in cands if c.r_graph), key=lambda c: (c.r_graph, c.key))
        rank_graph = {c.key: i for i, c in enumerate(graph, 1)}
        for c in cands:
            c.score = (wd / (k + c.rank_direct) if c.rank_direct else 0.0) + \
                      (wg / (k + rank_graph[c.key]) if c.key in rank_graph else 0.0)
        ordered = sorted(cands, key=lambda c: (-c.score, 0 if c.rank_direct else 1, c.rank_direct or 0, c.key))
        kept, per_source, capped = [], {}, 0
        for c in ordered:
            n = per_source.get(c.source_ref, 0)
            if n >= PER_SOURCE_CAP:
                capped += 1
                continue
            per_source[c.source_ref] = n + 1
            kept.append(c)
        if capped and "per-source cap" not in " ".join(notes):
            notes.append(f"per-source cap: {capped} further chunk(s) of already represented sources omitted "
                         f"(max {PER_SOURCE_CAP} per source and group)")
        return self._reserve_direct(kept, max(1, window))

    @staticmethod
    def _reserve_direct(ordered: list[Cand], window: int) -> list[Cand]:
        """Every window of `window` results holds >= DIRECT_MIN_SHARE direct
        candidates when enough exist: the graph can enrich but never crowd out
        direct text matches."""
        share = math.ceil(C.DIRECT_MIN_SHARE * window)
        index = {c.key: i for i, c in enumerate(ordered)}
        remaining, out = list(ordered), []
        while remaining:
            win = remaining[:window]
            rest_direct = [c for c in remaining[window:] if c.is_direct]
            have = sum(1 for c in win if c.is_direct)
            need = min(share, have + len(rest_direct))
            if have < need:
                add = rest_direct[: need - have]
                drop = [c for c in reversed(win) if not c.is_direct][: len(add)]
                dropped = {c.key for c in drop}
                win = sorted([c for c in win if c.key not in dropped] + add, key=lambda c: index[c.key])
            out.extend(win)
            taken = {c.key for c in win}
            remaining = [c for c in remaining if c.key not in taken]
        return out

    # ------------------------------------------------------------ paging
    def _assemble(self, args, groups, ordered, pos, reader) -> dict:
        limit, budget = args["limit"], args["budget"]
        pos = dict(pos)
        hits: dict[str, list[dict]] = {g: [] for g in groups}
        remaining = budget
        stale = 0
        budget_stop = False
        progressed = True
        while progressed and not budget_stop:
            progressed = False
            for g in groups:
                if len(hits[g]) >= limit or pos[g] >= len(ordered[g]):
                    continue
                cand = ordered[g][pos[g]]
                try:
                    hit = self._materialize(cand, reader, pos[g] + 1)
                except _Stale:
                    pos[g] += 1
                    stale += 1
                    progressed = True
                    continue
                text = hit["text"]
                room = remaining - HIT_OVERHEAD_TOKENS
                if room > 0 and C.fits_budget(text, room):
                    cost = C.estimate_tokens(text) + HIT_OVERHEAD_TOKENS
                elif sum(len(v) for v in hits.values()) == 0 or remaining >= MIN_PARTIAL_TOKENS + HIT_OVERHEAD_TOKENS:
                    cut = self._prefix_that_fits(text, max(room, 1))
                    if not cut:
                        budget_stop = True
                        break
                    hit["text"], hit["truncated"] = cut, True
                    if hit["span"]:
                        hit["span"] = {"start_char": hit["span"]["start_char"],
                                       "end_char": hit["span"]["start_char"] + len(cut)}
                    cost = C.estimate_tokens(cut) + HIT_OVERHEAD_TOKENS
                    budget_stop = True                      # the budget is spent after a partial hit
                else:
                    budget_stop = True
                    break
                remaining -= cost
                hits[g].append(hit)
                pos[g] += 1
                progressed = True
                if budget_stop:
                    break
        spent = budget - max(remaining, 0)
        return {"hits": hits, "pos": pos, "stale": stale, "budget_stop": budget_stop,
                "estimated": spent, "text_truncated": any(h["truncated"] for v in hits.values() for h in v)}

    @staticmethod
    def _prefix_that_fits(text: str, room_tokens: int) -> str:
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if C.fits_budget(text[:mid], room_tokens):
                lo = mid
            else:
                hi = mid - 1
        cut = text[:lo]
        if lo < len(text):
            ws = max(cut.rfind(" "), cut.rfind("\n"))
            if ws > 0.8 * lo:
                cut = cut[:ws]
        return cut

    def _materialize(self, cand: Cand, reader, rank: int) -> dict:
        if cand.legacy is not None:
            return self._source_hit(cand, rank)
        try:
            chunk = self.store.get_chunk(cand.chunk_ref)
        except C.EnrichmentError:
            raise _Stale from None
        if chunk.source_ref != cand.source_ref or chunk.source_version != cand.source_version:
            raise _Stale
        if self.source_fresh is not None:
            try:
                fresh = bool(self.source_fresh(cand.source_ref, cand.source_version))
            except Exception:  # noqa: BLE001 - cannot prove freshness -> do not serve as current
                fresh = False
            if not fresh:
                raise _Stale
        art = reader.artifact_by_id(cand.artifact_id)
        layer = next((la for la in art["layers"] if la["layer_id"] == chunk.layer_id), None)
        tier = layer["tier"] if layer else art["source_tier"]
        level = layer["authority_level"] if layer else art["authority_level"]
        used = art["annotation_revision"]
        flags = list(dict.fromkeys([*chunk.quality_flags, *art["quality_flags"]]))
        return {
            "source_ref": chunk.source_ref, "text_ref": chunk.text_ref, "chunk_ref": chunk.chunk_ref,
            "layer_id": chunk.layer_id,
            "span": {"start_char": chunk.start_char, "end_char": chunk.end_char},
            "text": chunk.text, "hit_level": "chunk", "tier": tier, "authority_level": level,
            "annotation_revision": used,
            "annotation_coverage": ({"annotated_chunks": art["annotated_chunks"],
                                     "total_chunks": art["chunk_count"]} if used is not None else None),
            "inclusion_reason": {"paths": list(cand.paths), "via": list(cand.via), "matched": list(cand.matched)},
            "rank": rank, "flags": flags, "truncated": False,
            "neighbors": {"prev_chunk_ref": chunk.prev_chunk_ref, "next_chunk_ref": chunk.next_chunk_ref},
            "_ann": cand.ann}

    @staticmethod
    def _source_hit(cand: Cand, rank: int) -> dict:
        h = cand.legacy
        text = str(h.get("snippet") or h.get("context") or h.get("title") or "")[:SNIPPET_MAX_CHARS]
        level = h.get("authority_level")
        return {"source_ref": cand.source_ref, "text_ref": cand.source_ref, "chunk_ref": None, "layer_id": None,
                "span": None, "text": text, "hit_level": "source", "tier": h.get("tier"),
                "authority_level": level if isinstance(level, int) else TIER_LEVEL.get(h.get("tier"), 7),
                "annotation_revision": None, "annotation_coverage": None,
                "inclusion_reason": {"paths": ["fallback"], "via": [], "matched": []},
                "rank": rank, "flags": [], "truncated": False, "neighbors": None, "_ann": False}

    # ----------------------------------------------------------- envelope
    def _envelope(self, args, *, path, used_ann, page, groups, notes, index, coverage, truncated, reason,
                  next_cursor) -> dict:
        out_groups = {}
        for g in groups:
            results = [{k: v for k, v in h.items() if not k.startswith("_")} for h in page["hits"][g]]
            out_groups[g] = {"count": len(results), "results": results}
        truncated = bool(truncated or page["text_truncated"])
        if page["text_truncated"] and not reason:
            reason = "budget"
        return {"ok": True, "query": args["query"], "scope": args["scope"], "limit": args["limit"],
                "path": path, "query_model_calls": 0, "uses_candidate_annotations": bool(used_ann),
                "budget": {"budget_tokens": args["budget"], "estimated_tokens": page["estimated"],
                           "truncated": bool(page["budget_stop"] or page["text_truncated"])},
                "index": index, "coverage": coverage, "groups": out_groups, "notes": notes,
                "truncated": truncated, "truncation_reason": reason, "next_cursor": next_cursor,
                "authority_note": AUTHORITY_NOTE}

    # ------------------------------------------------------------ fallback
    def _fallback(self, args, status, why: str) -> dict:
        if args["cursor"]:
            C.decode_cursor(args["cursor"], "retrieve")        # malformed -> E_BAD_CURSOR
            raise C.EnrichmentError("E_STALE_CURSOR", "the enrichment index no longer serves this cursor; "
                                    "restart the query without a cursor")
        if self.legacy_search is None:
            raise C.EnrichmentError("E_UNAVAILABLE", f"enrichment retrieval is unavailable ({why}) and no base "
                                    "search is configured")
        groups = [g for g in GROUP_ORDER if args["scope"] in ("all", g)]
        notes = [f"fallback to the base search: {why}"]
        n = min(args["limit"], FALLBACK_POOL_MAX)
        res, path = None, "fallback-lexical"
        try:
            res = self._call_legacy(args["query"], "lexical", args["scope"], n)
        except Exception:  # noqa: BLE001
            notes.append("base lexical search is unavailable; exact search was used")
        if res is None or not any(self._legacy_flat(res, g) for g in groups):
            if res is not None:
                notes.append("lexical search found nothing; exact search was also run")
            try:
                res, path = self._call_legacy(args["query"], "exact", args["scope"], n), "fallback-exact"
            except Exception:  # noqa: BLE001
                raise C.EnrichmentError("E_UNAVAILABLE", f"enrichment retrieval is unavailable ({why}) and the "
                                        "base search failed") from None
        ordered, count = {}, 0
        for g in groups:
            lst = []
            for i, h in enumerate(self._legacy_flat(res, g), 1):
                c = Cand("src:" + h["ref"], g, h["ref"], legacy=h)
                c.r_legacy = i
                lst.append(c)
            ordered[g] = lst[:n]
            count += len(ordered[g])
        notes.append("fallback hits are source-level snippets without chunk_ref; open the source to quote it")
        page = self._assemble(args, groups, ordered, {g: 0 for g in groups}, None)
        has_more = any(page["pos"][g] < len(ordered[g]) for g in groups)
        reason = "budget" if page["budget_stop"] else None
        st = status or {}
        return self._envelope(
            args, path=path, used_ann=False, page=page, groups=groups, notes=notes,
            index={"generation": st.get("generation"), "state": (st.get("state") or "unavailable"),
                   "lag": st.get("lag") or {"pending_artifacts": 0}, "fresh": False},
            coverage={"indexed_artifacts": st.get("indexed_artifacts", 0),
                      "total_artifacts": max(st.get("store_artifacts", 0), self._disk_total() or 0),
                      "source_level_fallback_hits": count, "stale_dropped": 0},
            truncated=bool(reason or has_more), reason=reason or ("limit" if has_more else None), next_cursor=None)
