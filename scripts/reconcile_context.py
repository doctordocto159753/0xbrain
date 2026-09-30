#!/usr/bin/env python3
"""K5 reconciliation context: `brain_reconcile_context(...)`.

Assembles the material a reviewer (or the calling model) needs to reconcile a
seed record or query against the archive. Deterministic, read-only, no model
call, no semantic verdict. It REUSES existing machinery:

  build_graph_index   rebuildable SQLite graph (nodes/edges) in _search/
  context_pack        BFS neighborhood (_neighborhood) over resolved edges
  evidence_audit      proposal queue loader
  reconcile_runner    freeze/drift state of the materials register
  wiki_capture        capture record parser

depth="focused"  seed + direct (1-hop) neighborhood.
depth="deep"     multi-hop neighborhood (until exhausted, safety-capped).

Sections (fixed order, deterministic within each):
  canonical_records claims relations source_records captures superseded
  open_proposals unresolved chronology   (frozen K5 names)

Paging: `total` / `returned` / `next_cursor`. A cursor is bound to a
fingerprint of the current result set; if the corpus changed it is refused
(`cursor_stale`) rather than silently skipping or repeating items.
`truncated` + `reason` are set ONLY when a safety limit cut something
(hop ceiling, per-record content ceiling); ordinary paging is not truncation.
There is no product-level record or token cap. Inclusion reasons and depth
are navigation aids, never evidence.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "capture"))
import build_graph_index as bgi  # noqa: E402
import context_pack as cp  # noqa: E402
import evidence_audit as ea  # noqa: E402
import reconcile_runner as rr  # noqa: E402

try:  # capture parsing is optional; captures are simply skipped if unavailable
    import wiki_capture as _cap  # noqa: E402
except Exception:  # noqa: BLE001
    _cap = None

SECTION_ORDER = ("canonical_records", "claims", "relations", "source_records", "captures",
                 "superseded", "open_proposals", "unresolved", "chronology")
ZONE_SECTION = {"02-sources": "source_records", "03-objects": "canonical_records",
                "04-notes": "canonical_records", "05-claims": "claims",
                "06-relations": "relations"}
SUPERSEDED_STATUSES = {"superseded", "retired", "deprecated", "withdrawn"}
UNRESOLVED_STATUSES = {"unresolved", "open", "disputed", "contested"}
CAPTURES_REL = Path("01-inbox/captures")
HANDOFF_GLOBS = ("07-genesis/**/*.md", "_captures/HANDOFF*.md")
DEFAULT_DEEP_PAGE = 50
MAX_HOPS_SAFETY = 8            # safety ceiling, reported via truncated/reason
MAX_CONTENT_BYTES = 400_000    # per expanded record, same ceiling as brain_read
EXCERPT_CHARS = 600
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
AUTHORITY_NOTE = ("Navigation aid only. Inclusion reasons, hop depth and ordering are "
                  "not evidence; no server-side semantic verdict is produced.")


class ContextError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ------------------------------------------------------------------ helpers

def _canonical_mtime(root: Path) -> float:
    latest = 0.0
    for zone in bgi.CANONICAL_ZONES:
        d = root / zone
        if d.is_dir():
            for p in d.rglob("*.md"):
                latest = max(latest, p.stat().st_mtime)
    return latest


def ensure_graph(root: Path) -> Path:
    """(Re)build the graph index when missing or older than the corpus."""
    db = root / "_search" / "graph.db"
    if not db.exists() or db.stat().st_mtime < _canonical_mtime(root):
        bgi.build(root)
    return db


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _read(root: Path, rel: str) -> str:
    try:
        return (root / rel).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _strip_frontmatter(text: str) -> str:
    m = bgi.FM_BLOCK.match(text)
    return text[m.end():] if m else text


def _scan_captures(root: Path) -> list[dict]:
    out = []
    cdir = root / CAPTURES_REL
    if not cdir.is_dir():
        return out
    for p in sorted(cdir.rglob("cap-*.md")):
        with open(p, encoding="utf-8", errors="ignore", newline="") as fh:
            text = fh.read()
        fm = {}
        if _cap is not None:
            try:
                fm, _ = _cap.parse_record_text(text)
            except Exception:  # noqa: BLE001
                fm = {}
        out.append({"id": p.stem, "path": p.relative_to(root).as_posix(),
                    "status": fm.get("status"), "kind": fm.get("capture_kind"),
                    "text": text})
    return out


def _handoffs(root: Path) -> list[dict]:
    seen, out = set(), []
    for pattern in HANDOFF_GLOBS:
        for p in sorted(root.glob(pattern)):
            rel = p.relative_to(root).as_posix()
            if rel in seen or not p.is_file():
                continue
            seen.add(rel)
            m = DATE_RE.search(p.name)
            out.append({"path": rel, "date": m.group(1) if m else "",
                        "text": p.read_text(encoding="utf-8", errors="ignore")})
    out.sort(key=lambda h: (h["date"], h["path"]))
    return out


def exact_search(root: Path, query: str) -> list[str]:
    """Default deterministic query: exact substring over canonical zones."""
    hits = []
    for zone in bgi.CANONICAL_ZONES:
        d = root / zone
        if d.is_dir():
            for p in sorted(d.rglob("*.md")):
                if query in p.read_text(encoding="utf-8", errors="ignore"):
                    hits.append(p.relative_to(root).as_posix())
    return sorted(hits)


def _node_by_ref(con, ref: str):
    row = con.execute("SELECT id, path, zone, title, kind, status FROM nodes WHERE id=?",
                      (ref,)).fetchone()
    if row is None:
        row = con.execute("SELECT id, path, zone, title, kind, status FROM nodes WHERE path=?",
                          (ref.replace("\\", "/"),)).fetchone()
    return row


def _node_by_path_hit(con, hit: str):
    f = str(hit.get("file", "")) if isinstance(hit, dict) else str(hit)
    f = f.replace("qmd://wiki/", "").replace("\\", "/")
    row = con.execute("SELECT id, path, zone, title, kind, status FROM nodes WHERE path=?",
                      (f,)).fetchone()
    if row is None:
        name = Path(f).name
        row = con.execute(
            "SELECT id, path, zone, title, kind, status FROM nodes WHERE path LIKE ? "
            "ORDER BY path LIMIT 1", (f"%/{name}",)).fetchone() if name else None
    return row


# --------------------------------------------------------------- collection

def _collect_nodes(con, root: Path, seed_ref, query, depth, search_fn,
                   captures: list[dict]) -> tuple[dict, dict, list[str]]:
    """Returns (nodes{id: info}, seed_info, truncation_reasons)."""
    reasons: list[str] = []
    roots: dict[str, dict] = {}      # id -> {"reason": str}
    seed_info: dict = {"ref": seed_ref, "kind": None, "resolved": False}

    if seed_ref:
        row = _node_by_ref(con, seed_ref)
        if row is not None:
            roots[row[0]] = {"reason": "seed", "depth": 0}
            seed_info.update({"kind": "record", "resolved": True, "id": row[0], "path": row[1]})
        else:
            cap = next((c for c in captures if c["id"] == seed_ref), None)
            if cap is None:
                raise ContextError("seed_not_found", f"seed_ref does not resolve: {seed_ref!r}")
            seed_info.update({"kind": "capture", "resolved": True, "id": cap["id"],
                              "path": cap["path"]})
            for (nid,) in con.execute("SELECT id FROM nodes ORDER BY id"):
                if nid in cap["text"]:
                    roots[nid] = {"reason": f"cited by capture {cap['id']}", "depth": 1}
    if query:
        if search_fn is not None:
            hits = search_fn(query, 50) or []
            rows = [_node_by_path_hit(con, h) for h in hits]
        else:
            rows = [_node_by_path_hit(con, h) for h in exact_search(root, query)]
        for r in rows:
            if r is not None and r[0] not in roots:
                roots[r[0]] = {"reason": f"query: {query}", "depth": 0 if not seed_ref else 1}

    hops = 1 if depth == "focused" else MAX_HOPS_SAFETY + 1
    nodes: dict[str, dict] = {}
    for rid in sorted(roots):
        info = dict(roots[rid])
        nodes[rid] = info
    for rid in sorted(roots):
        base = roots[rid]["depth"]
        for nid, reason, d in cp._neighborhood(con, rid, hops):
            dd = base + int(d)
            if dd > (1 if depth == "focused" else MAX_HOPS_SAFETY):
                if depth == "deep" and "max_hops_safety_limit" not in reasons:
                    reasons.append("max_hops_safety_limit")
                continue
            cur = nodes.get(nid)
            if cur is None or dd < cur["depth"]:
                nodes[nid] = {"reason": reason if base == 0 else f"{reason} (from {rid})",
                              "depth": dd}
    return nodes, seed_info, reasons


def _rows(con, ids: list[str]) -> dict[str, tuple]:
    out = {}
    for i in ids:
        r = con.execute("SELECT id, path, zone, title, kind, status FROM nodes WHERE id=?",
                        (i,)).fetchone()
        if r:
            out[i] = r
    return out


def _item(section, ref, path, title, kind, status, depth, reason, text, expand_all,
          expand: set, **extra) -> tuple[dict, bool]:
    body = _strip_frontmatter(text)
    content_truncated = False
    it = {"section": section, "id": ref, "ref": ref, "path": path, "title": title or "",
          "kind": kind or "", "status": status or "", "depth": depth, "reason": reason,
          "authority_level": ea.authority_level(path)}
    if expand_all or ref in expand or path in expand:
        raw = body.encode("utf-8", errors="replace")
        if len(raw) > MAX_CONTENT_BYTES:
            it["content"] = raw[:MAX_CONTENT_BYTES].decode("utf-8", errors="ignore")
            it["content_truncated"] = True
            content_truncated = True
        else:
            it["content"] = body
            it["content_truncated"] = False
    else:
        it["excerpt"] = body.strip()[:EXCERPT_CHARS]
    it.update(extra)
    it["_sha"] = _sha(text)
    return it, content_truncated


def build_items(root: Path, con, nodes: dict, seed_info: dict, query, captures,
                expand_all: bool, expand: set) -> tuple[list[dict], list[str]]:
    items: list[dict] = []
    reasons: list[str] = []
    rows = _rows(con, sorted(nodes))
    included_ids = set(rows)
    tokens = set(included_ids)
    if seed_info.get("id"):
        tokens.add(seed_info["id"])
    tokens |= {r[1] for r in rows.values()}

    def rank(x):   # K5: authority level, then graph distance, then id
        return (x["authority_level"], x["depth"], x["ref"])

    rec_items: dict[str, list[dict]] = {s: [] for s in SECTION_ORDER}
    for nid, info in nodes.items():
        r = rows.get(nid)
        if r is None:
            continue
        _, path, zone, title, kind, status = r
        sec = "superseded" if (status or "").lower() in SUPERSEDED_STATUSES \
            else ZONE_SECTION.get(zone, "canonical_records")
        it, trunc = _item(sec, nid, path, title, kind, status, info["depth"],
                          info["reason"], _read(root, path), expand_all, expand)
        if trunc and "content_byte_limit" not in reasons:
            reasons.append("content_byte_limit")
        rec_items[sec].append(it)

    # captures (noncanonical): mention an included id/path, the query, or are the seed
    for c in captures:
        why = None
        if seed_info.get("kind") == "capture" and c["id"] == seed_info.get("id"):
            why = "seed"
        elif any(t and t in c["text"] for t in tokens):
            why = "mentions an included record"
        elif query and query in c["text"]:
            why = f"query: {query}"
        if why:
            it, trunc = _item("captures", c["id"], c["path"], "", c["kind"], c["status"],
                              None, why, c["text"], expand_all, expand)
            if trunc and "content_byte_limit" not in reasons:
                reasons.append("content_byte_limit")
            rec_items["captures"].append(it)

    # open proposals (candidate queue) that reference an included id/path
    for prop in ea._load_proposals(root):
        if prop.get("status") not in (None, "new", "audited"):
            continue
        blob = json.dumps(prop.get("body"), ensure_ascii=False, sort_keys=True) \
            if not isinstance(prop.get("body"), str) else prop["body"]
        hit = sorted(t for t in tokens if t and t in blob)
        if hit or (query and query in blob):
            pid = str(prop.get("id"))
            rec_items["open_proposals"].append({
                "section": "open_proposals", "id": pid, "ref": pid,
                "path": ea.QUEUE_REL.as_posix(), "authority_level": ea.CANDIDATE_LEVEL,
                "title": "", "kind": prop.get("kind") or "", "status": prop.get("status") or "",
                "depth": None,
                "reason": f"references {hit[0]}" if hit else f"query: {query}",
                "body": prop.get("body"), "_sha": _sha(blob)})

    # unresolved: dangling edges from included nodes + unresolved-status records
    if included_ids:
        marks = ",".join("?" * len(included_ids))
        for src, field, target, tkind in con.execute(
                f"SELECT src_id, field, target, target_kind FROM edges "
                f"WHERE resolved_id IS NULL AND src_id IN ({marks}) "
                f"ORDER BY src_id, field, target", tuple(sorted(included_ids))):
            rec_items["unresolved"].append({
                "section": "unresolved", "id": f"{src}::{field}::{target}",
                "ref": f"{src}::{field}::{target}", "authority_level": ea.authority_level(rows[src][1]),
                "path": rows[src][1], "title": "", "kind": "dangling-edge",
                "status": "unresolved", "depth": nodes[src]["depth"],
                "reason": f"{tkind} target does not resolve", "src_id": src,
                "field": field, "target": target, "_sha": _sha(f"{src}{field}{target}")})
    for nid, r in rows.items():
        if (r[5] or "").lower() in UNRESOLVED_STATUSES:
            rec_items["unresolved"].append({
                "section": "unresolved", "id": f"{nid}::status", "ref": f"{nid}::status",
                "path": r[1], "authority_level": ea.authority_level(r[1]),
                "title": r[3] or "", "kind": "status", "status": r[5],
                "depth": nodes[nid]["depth"], "reason": f"record status is {r[5]!r}",
                "_sha": _sha(f"{nid}{r[5]}")})

    # chronology / genesis / handoffs: full timeline, flagged when related
    for h in _handoffs(root):
        related = any(t and t in h["text"] for t in tokens) or bool(query and query in h["text"])
        it, trunc = _item("chronology", h["path"], h["path"], "", "handoff", "", None,
                          "related to included records" if related else "timeline",
                          h["text"], expand_all, expand, date=h["date"], related=related)
        if trunc and "content_byte_limit" not in reasons:
            reasons.append("content_byte_limit")
        rec_items["chronology"].append(it)

    keyers = {
        "canonical_records": rank, "claims": rank, "relations": rank, "source_records": rank,
        "superseded": rank,
        "captures": lambda x: (x["ref"],),
        "open_proposals": lambda x: (x["ref"],),
        "unresolved": lambda x: (x["path"], x["ref"]),
        "chronology": lambda x: (x.get("date", ""), x["path"]),
    }
    for sec in SECTION_ORDER:
        items += sorted(rec_items[sec], key=keyers[sec])
    return items, reasons


# ------------------------------------------------------------------- paging

def _fingerprint(args_sig: dict, items: list[dict]) -> str:
    payload = json.dumps([args_sig, [[i["section"], i["ref"], i["_sha"]] for i in items]],
                         ensure_ascii=False, sort_keys=True)
    return _sha(payload)[:24]


def _encode_cursor(offset: int, fp: str) -> str:
    return base64.urlsafe_b64encode(json.dumps({"o": offset, "f": fp}).encode()).decode()


def _decode_cursor(cur: str) -> tuple[int, str]:
    try:
        d = json.loads(base64.urlsafe_b64decode(cur.encode()))
        return int(d["o"]), str(d["f"])
    except Exception as exc:  # noqa: BLE001
        raise ContextError("bad_cursor", "cursor is not valid") from exc


# ---------------------------------------------------------------- public API

def reconcile_context(root: Path, seed_ref: str | None = None, query: str | None = None,
                      depth: str = "focused", cursor: str | None = None,
                      sections: list[str] | None = None, expand: list[str] | None = None,
                      page_size: int | None = None, search_fn=None) -> dict:
    """See module docstring. Raises ContextError on invalid input."""
    root = Path(root)
    if depth not in ("focused", "deep"):
        raise ContextError("bad_depth", "depth must be 'focused' or 'deep'")
    if not seed_ref and not query:
        raise ContextError("no_anchor", "provide seed_ref and/or query")
    if sections is not None:
        bad = [s for s in sections if s not in SECTION_ORDER]
        if bad:
            raise ContextError("bad_sections", f"unknown section(s) {bad}; allowed {list(SECTION_ORDER)}")
    if page_size is not None and (not isinstance(page_size, int) or page_size < 1):
        raise ContextError("bad_page_size", "page_size must be a positive integer")
    expand = list(expand or [])
    expand_all = "*" in expand
    wanted = [s for s in SECTION_ORDER if sections is None or s in sections]

    con = cp.sqlite3_connect(ensure_graph(root))
    try:
        captures = _scan_captures(root)
        nodes, seed_info, reasons = _collect_nodes(con, root, seed_ref, query, depth,
                                                   search_fn, captures)
        items, item_reasons = build_items(root, con, nodes, seed_info, query, captures,
                                          expand_all, set(expand))
    finally:
        con.close()
    for r in item_reasons:
        if r not in reasons:
            reasons.append(r)

    items = [i for i in items if i["section"] in wanted]
    section_totals = {s: sum(1 for i in items if i["section"] == s) for s in wanted}
    args_sig = {"seed": seed_ref, "query": query, "depth": depth, "sections": wanted,
                "expand": sorted(expand)}
    fp = _fingerprint(args_sig, items)
    offset = 0
    if cursor:
        offset, cfp = _decode_cursor(cursor)
        if cfp != fp:
            raise ContextError("cursor_stale", "corpus or arguments changed since the cursor "
                                               "was issued; restart without a cursor")
        if offset < 0 or offset > len(items):
            raise ContextError("bad_cursor", "cursor offset out of range")
    size = page_size if page_size else (None if depth == "focused" else DEFAULT_DEEP_PAGE)
    page = items[offset:] if size is None else items[offset:offset + size]
    nxt = offset + len(page)
    next_cursor = _encode_cursor(nxt, fp) if nxt < len(items) else None
    for it in page:
        it.pop("_sha", None)

    state = rr._load_state(root)
    try:
        drift = None if not state else \
            rr.rows_signature(rr.manifest_rows(root)) != state.get("rows_signature")
    except (OSError, ValueError):
        drift = None
    return {
        "ok": True, "depth": depth, "seed": seed_info, "query": query,
        "sections": wanted, "section_totals": section_totals,
        "total": len(items), "returned": len(page), "offset": offset,
        "next_cursor": next_cursor,
        "truncated": bool(reasons), "reason": ",".join(reasons) or None,
        "items": page,
        "meta": {"materials_changed_since_freeze": drift,
                 "last_freeze": (state or {}).get("last_run_id"),
                 "verdict": None, "authority_note": AUTHORITY_NOTE},
    }


def brain_reconcile_context(root: Path, **kwargs) -> dict:
    """Exception-free wrapper for the MCP adapter."""
    try:
        return reconcile_context(root, **kwargs)
    except ContextError as e:
        return {"ok": False, "error": {"code": e.code, "message": e.message}}


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=None)
    ap.add_argument("--seed")
    ap.add_argument("--query")
    ap.add_argument("--depth", default="focused", choices=["focused", "deep"])
    ap.add_argument("--cursor")
    ap.add_argument("--sections", nargs="*")
    ap.add_argument("--expand", nargs="*")
    ap.add_argument("--page-size", type=int)
    a = ap.parse_args(argv)
    root = Path(a.root).resolve() if a.root else Path(__file__).resolve().parents[1]
    res = brain_reconcile_context(root, seed_ref=a.seed, query=a.query, depth=a.depth,
                                  cursor=a.cursor, sections=a.sections, expand=a.expand,
                                  page_size=a.page_size)
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0 if res.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main())
