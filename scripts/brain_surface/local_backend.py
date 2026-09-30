"""LocalBackend: adapter from the surface to the existing Living Wiki code.

Reuses (does not copy): wiki_capture (capture/read/list), wiki_mcp_server
(lexical + exact search, proposal append), evidence_audit (passage check),
context_pack / build_graph_index (neighborhood). It is an adapter, replaceable
by Agent A/C/D/E implementations behind the same Backend interface.

Not done here (owned elsewhere): auth, transport, Git lock/commit (K9),
capture states needs_* (K4). commit_state is therefore reported truthfully as
"not_attempted" and uncommitted paths come from `git status`.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import contract as C
from .backend import BackendUnavailable

SCRIPTS = Path(__file__).resolve().parents[1]
for p in (SCRIPTS, SCRIPTS / "capture"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import build_graph_index as bgi  # noqa: E402
import context_pack as cp  # noqa: E402
import evidence_audit as ea  # noqa: E402
import wiki_capture as cap  # noqa: E402
import wiki_mcp_server as wms  # noqa: E402

CANON = ea.CANONICAL_ZONES
MAX_READ = wms.MAX_READ_BYTES
ID_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class LocalBackend:
    def __init__(self, root: Path | None = None, search_fn=None,
                 run_validator: bool = False):
        self.root = Path(root or C.ROOT).resolve()
        self.search_fn = search_fn  # (query, n) -> [{"file","score",...}]
        self.run_validator = run_validator

    # ---------------------------------------------------------------- index
    def _records(self) -> dict[str, dict]:
        out = {}
        for zone in CANON:
            zd = self.root / zone
            if not zd.is_dir():
                continue
            for md in sorted(zd.rglob("*.md")):
                try:
                    fm = bgi.parse_frontmatter(md.read_text(encoding="utf-8",
                                                            errors="ignore"))
                except OSError:
                    continue
                rid = str(fm.get("id", "")).strip()
                if rid:
                    out[rid] = {"id": rid, "path": md, "zone": zone, "fm": fm}
        return out

    def _snapshot(self, recs) -> str:
        h = hashlib.sha256()
        for rid in sorted(recs):
            st = recs[rid]["path"].stat()
            h.update(f"{rid}|{st.st_size}|{st.st_mtime_ns}\n".encode())
        return h.hexdigest()[:16]

    def _hist_docs(self) -> dict[str, Path]:
        docs = {}
        for pat in ("07-genesis/*.md", "_captures/HANDOFF*.md"):
            for p in sorted(self.root.glob(pat)):
                docs[p.stem] = p
        return docs

    # --------------------------------------------------------------- search
    def _path_to_ref(self, file_hint: str, recs) -> str | None:
        stem = Path(str(file_hint).replace("qmd://wiki/", "")).stem.lower()
        for rid, r in recs.items():
            if r["path"].stem.lower() == stem:
                return f"rec:{rid}"
        return None

    def _canonical_lexical(self, query, n, recs):
        if self.search_fn is not None:
            hits = self.search_fn(query, n)
        else:
            payload = json.loads(wms.tool_wiki_search({"query": query, "n": n}))
            if "error" in payload:
                raise BackendUnavailable(f"lexical search: {payload['error']}")
            hits = payload.get("results") or []
        out = []
        for h in hits:
            ref = self._path_to_ref(h.get("file", ""), recs)
            if not ref:
                continue
            r = recs[ref[4:]]
            out.append({"ref": ref, "zone": r["zone"],
                        "title": r["fm"].get("title"),
                        "snippet": str(h.get("snippet") or h.get("title") or "")[:200],
                        "relevance": h.get("score")})
        return out

    def _canonical_exact(self, query, n, recs):
        payload = json.loads(wms.tool_wiki_exact({"text": query, "limit": n}))
        by_path = {r["path"].relative_to(self.root).as_posix(): r
                   for r in recs.values()}
        out = []
        for m in payload.get("matches", []):
            r = by_path.get(m["file"])
            if r:
                out.append({"ref": f"rec:{r['id']}", "zone": r["zone"],
                            "title": r["fm"].get("title"), "line": m["line"],
                            "snippet": m["context"]})
        return out

    def _captures(self, query, mode, n):
        q = query.casefold()
        toks = q.split()
        out = []
        for p, fm in cap._scan_records():
            try:
                _, sections = cap.parse_record_text(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            blob = "\n".join(sections.values())
            low = blob.casefold()
            ok = (q in low) if mode == "exact" else all(t in low for t in toks)
            if ok:
                i = low.find(toks[0] if mode == "lexical" and toks else q)
                out.append({"ref": f"cap:{fm.get('id')}", "zone": "01-inbox/captures",
                            "snippet": blob[max(0, i - 60):i + 140]})
            if len(out) >= n:
                break
        return out

    def search(self, query, mode, scope, n):
        recs = self._records()
        out = {}
        if scope in ("canonical", "all"):
            out["canonical"] = (self._canonical_exact(query, n, recs)
                                if mode == "exact"
                                else self._canonical_lexical(query, n, recs))
        if scope in ("captures", "all"):
            out["captures"] = self._captures(query, mode, n)
        return out

    # ----------------------------------------------------------------- read
    def read(self, ref):
        kind, _, ident = ref.partition(":")
        if not ID_FILE_RE.match(ident):
            raise KeyError(ref)
        if kind == "rec":
            r = self._records().get(ident)
            if not r:
                raise KeyError(ref)
            return self._file_result(ref, "record", r["path"], r["zone"],
                                    C.LEVEL_BY_ZONE[r["zone"]])
        if kind == "hist":
            p = self._hist_docs().get(ident)
            if not p:
                raise KeyError(ref)
            zone = "07-genesis" if p.parent.name == "07-genesis" else "_captures"
            return self._file_result(ref, "history", p, zone, C.LEVEL_BY_ZONE[zone])
        if kind == "cap":
            try:
                r = cap.read_capture(ident)
            except cap.CaptureError as e:
                if e.code in ("E_NOT_FOUND", "E_BAD_ID"):
                    raise KeyError(ref)
                raise
            fm = r["front_matter"]
            text = "\n\n".join(f"## {k}\n{v}" for k, v in r["sections"].items() if v)
            out = {"ref": ref, "kind": "capture", "zone": "01-inbox/captures",
                   "authority_level": C.CANDIDATE_LEVEL, "content": text,
                   "bytes": len(text.encode()), "truncated": False,
                   "metadata": {k: fm.get(k) for k in (
                       "status", "capture_kind", "captured_at", "language_hint",
                       "transcription_state", "quality_flags", "duplicate_of")}}
            if fm.get("raw_media"):
                out["media"] = {"present": True, "kind": fm.get("capture_kind")}
            return out
        if kind == "prop":
            for rec in self._proposals():
                if rec.get("id") == ident:
                    text = json.dumps(rec, ensure_ascii=False, indent=1)
                    return {"ref": ref, "kind": "proposal", "zone": "_proposals",
                            "authority_level": C.CANDIDATE_LEVEL, "content": text,
                            "bytes": len(text.encode()), "truncated": False,
                            "metadata": {"status": rec.get("status")}}
        raise KeyError(ref)

    @staticmethod
    def _file_result(ref, kind, path, zone, level):
        body = path.read_text(encoding="utf-8", errors="replace")
        return {"ref": ref, "kind": kind, "zone": zone, "authority_level": level,
                "content": body[:MAX_READ], "bytes": len(body),
                "truncated": len(body) > MAX_READ}

    # -------------------------------------------------------------- capture
    def capture(self, text, language_hint):
        try:
            r = cap.capture_text(text, C.CAPTURE_CHANNEL, language_hint)
        except cap.CaptureError as e:
            raise BackendUnavailable(f"{e.code}: {e.message}")
        return {"ref": f"cap:{r['id']}", "status": r["status"],
                "sha256": r["sha256"],
                "duplicate_of": f"cap:{r['duplicate_of']}" if r.get("duplicate_of") else None,
                "persisted": True, "commit_state": "not_attempted"}

    # ------------------------------------------------------------ reconcile
    def _graph(self, recs):
        db = self.root / "_search" / "graph.db"
        newest = max((r["path"].stat().st_mtime for r in recs.values()), default=0)
        if not db.exists() or db.stat().st_mtime < newest:
            bgi.build(self.root)
        return cp.sqlite3_connect(db)

    def _proposals(self):
        p = self.root / "_proposals" / "proposals.jsonl"
        out = []
        if p.exists():
            for line in p.read_text(encoding="utf-8-sig").splitlines():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return out

    def reconcile(self, seed_ref, query, hops, expand):
        recs = self._records()
        snap = self._snapshot(recs)
        seed = None
        if seed_ref:
            if not seed_ref.startswith("rec:") or seed_ref[4:] not in recs:
                raise KeyError(seed_ref)
            seed = seed_ref[4:]
        starts = ([seed] if seed else []) + [
            e[4:] for e in expand if e.startswith("rec:") and e[4:] in recs]
        dist: dict[str, tuple] = {s: (0, "seed" if s == seed else "expand") for s in starts}
        con = self._graph(recs) if recs else None
        if con and starts:
            for s in starts:
                for nid, reason, d in cp._neighborhood(con, s, hops):
                    if nid in recs and (nid not in dist or int(d) < dist[nid][0]):
                        dist[nid] = (int(d), reason)
        if query:
            for h in (self._canonical_lexical(query, 12, recs)
                      if (self.search_fn or shutil.which("qmd"))
                      else self._canonical_exact(query, 12, recs)):
                rid = h["ref"][4:]
                dist.setdefault(rid, (None, f"lexical: {query}"))
        secs: dict[str, list] = {n: [] for n in C.RECONCILE_SECTIONS}
        zone_sec = {"02-sources": "source_records", "05-claims": "claims",
                    "06-relations": "relations"}
        for rid, (d, reason) in dist.items():
            r = recs[rid]
            status = " ".join(str(r["fm"].get(k) or "") for k in
                              ("status", "relation_status")).strip()
            name = ("superseded" if re.search(r"supersed|retired", status, re.I)
                    else zone_sec.get(r["zone"], "canonical_records"))
            secs[name].append({"id": rid, "ref": f"rec:{rid}",
                               "authority_level": C.LEVEL_BY_ZONE[r["zone"]],
                               "reason": reason, "distance": d,
                               "title": r["fm"].get("title"), "status": status or None})
        ids = set(dist)
        if con:
            rows = con.execute("SELECT src_id, target FROM edges WHERE "
                               "resolved_id IS NULL").fetchall()
            for src, tgt in sorted(set(rows)):
                if src in ids:
                    secs["unresolved"].append({
                        "id": f"{src}->{tgt}", "ref": None, "authority_level": None,
                        "reason": f"unresolved link from {src}", "distance": None})
        for rec in self._proposals():
            if rec.get("status") in (None, "new", "audited") and any(
                    i in json.dumps(rec.get("body", ""), ensure_ascii=False)
                    for i in ids):
                secs["open_proposals"].append({
                    "id": rec.get("id"), "ref": f"prop:{rec.get('id')}",
                    "authority_level": C.CANDIDATE_LEVEL, "reason": "mentions included record",
                    "distance": None, "status": rec.get("status")})
        needles = ids | ({query} if query else set())
        for stem, p in self._hist_docs().items():
            text = p.read_text(encoding="utf-8", errors="ignore")
            if any(nd and nd in text for nd in needles):
                z = "07-genesis" if p.parent.name == "07-genesis" else "_captures"
                secs["chronology"].append({
                    "id": stem, "ref": f"hist:{stem}",
                    "authority_level": C.LEVEL_BY_ZONE[z],
                    "reason": "mentions included record or query", "distance": None})
        for p, fm in cap._scan_records():
            try:
                _, sections = cap.parse_record_text(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            blob = "\n".join(sections.values())
            hit = next((nd for nd in needles if nd and nd.casefold() in blob.casefold()), None)
            if hit:
                secs["captures"].append({
                    "id": fm.get("id"), "ref": f"cap:{fm.get('id')}",
                    "authority_level": C.CANDIDATE_LEVEL,
                    "reason": f"mentions {hit!r}", "distance": None,
                    "status": fm.get("status")})
        return {"snapshot": snap, "seed": seed_ref, "sections": secs}

    # ------------------------------------------------------------- evidence
    def resolve_evidence(self, ref, quote):
        kind, _, ident = ref.partition(":")
        if kind == "rec":
            r = self._records().get(ident)
            if not r:
                return {"ok": False, "errors": [f"unknown ref: {ref}"]}
            rel = r["path"].relative_to(self.root).as_posix()
            errs = ea._check_passage(self.root, {"path": rel, "quote": quote})
            return {"ok": not errs, "kind": "rec", "path": rel, "errors": errs}
        if kind == "cap":
            try:
                got = cap.read_capture(ident)
            except cap.CaptureError:
                return {"ok": False, "errors": [f"unknown ref: {ref}"]}
            ok = quote in "\n".join(got["sections"].values())
            return {"ok": ok, "kind": "cap", "path": None,
                    "errors": [] if ok else [f"quote NOT found verbatim in {ref}"]}
        return {"ok": False, "errors": [f"ref kind not usable as evidence: {ref}"]}

    def submit_proposal(self, record):
        out = json.loads(wms.tool_wiki_propose({
            "kind": record["kind"],
            "body": json.dumps(record["body"], ensure_ascii=False)}))
        if not out.get("accepted"):
            raise BackendUnavailable(out.get("error", "proposal not accepted"))
        return {"proposal_id": out["proposal_id"], "persisted": True,
                "commit_state": "not_attempted"}

    # --------------------------------------------------------------- status
    def _git_uncommitted(self):
        git = shutil.which("git")
        if not git:
            return None
        p = subprocess.run([git, "status", "--porcelain", "--",
                            "01-inbox/captures", "_proposals"], cwd=self.root,
                           text=True, capture_output=True, timeout=20)
        if p.returncode != 0:
            return None
        return [ln[3:] for ln in p.stdout.splitlines() if ln.strip()]

    def status(self):
        recs = self._records()
        by_zone: dict[str, int] = {}
        for r in recs.values():
            by_zone[r["zone"]] = by_zone.get(r["zone"], 0) + 1
        caps = cap.list_captures()["captures"]
        by_state: dict[str, int] = {}
        needs = []
        for c in caps:
            by_state[c["status"]] = by_state.get(c["status"], 0) + 1
            fm = cap.read_capture(c["id"])["front_matter"]
            flags = [f for f in (fm.get("quality_flags") or [])
                     if str(f).startswith("needs_")]
            if fm.get("capture_kind") not in (None, "text") and \
                    fm.get("transcription_state") not in ("complete",):
                flags.append("needs_transcription" if fm["capture_kind"] == "voice"
                             else "needs_description")
            if flags:
                needs.append({"ref": f"cap:{c['id']}", "needs": sorted(set(flags))})
        props: dict[str, int] = {}
        for rec in self._proposals():
            s = rec.get("status") or "new"
            props[s] = props.get(s, 0) + 1
        validator = {"state": "not_checked"}
        if self.run_validator:
            p = subprocess.run([sys.executable, "scripts/validate_repo.py", "--full"],
                               cwd=self.root, text=True, capture_output=True,
                               timeout=300)
            validator = {"state": "pass" if p.returncode == 0 else "fail",
                         "detail": (p.stdout.strip().splitlines() or [""])[-1][:200]}
        db = self.root / "_search" / "graph.db"
        search = {"qmd": "available" if shutil.which("qmd") else "missing",
                  "state": "ok" if shutil.which("qmd") or self.search_fn else "missing",
                  "graph_index": "present" if db.exists() else "absent"}
        return {"snapshot": self._snapshot(recs), "counts": {
                    "canonical_records": len(recs), "by_zone": by_zone,
                    "captures": len(caps), "captures_by_state": by_state},
                "pending_needs": needs, "proposals": props,
                "validator": validator, "search": search,
                "uncommitted": self._git_uncommitted()}
