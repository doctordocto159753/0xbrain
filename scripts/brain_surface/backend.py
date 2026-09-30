"""Backend interface consumed by the surface, plus an in-memory fake.

A real backend (see local_backend.py, or Agent A/C/D/E's own) must return the
shapes documented per method. The surface validates them; a backend that
breaks the shape yields E_BACKEND_SHAPE instead of a silently wrong answer.
"""
from __future__ import annotations

import hashlib
from typing import Protocol


class BackendUnavailable(Exception):
    """A dependency (search index, git, store) cannot serve the call."""


class Backend(Protocol):
    def search(self, query: str, mode: str, scope: str, n: int) -> dict:
        """-> {"canonical": [hit], "captures": [hit]} (only requested scopes).
        hit: {ref, title?, zone, snippet?, line?, relevance?}"""

    def read(self, ref: str) -> dict:
        """-> {ref, kind, zone, authority_level, content, bytes, truncated,
        metadata?, media?}. Raise KeyError for an unknown ref."""

    def capture(self, text: str, language_hint: str) -> dict:
        """-> {ref, status, sha256, duplicate_of?, persisted, commit_state}
        commit_state in committed|uncommitted|not_attempted."""

    def reconcile(self, seed_ref: str | None, query: str | None,
                  hops: int, expand: list[str]) -> dict:
        """-> {snapshot, seed?, sections: {name: [item]}} where item has
        id, ref, authority_level, reason, distance?, title?, status?,
        excerpt?. Unordered is fine; the surface orders."""

    def resolve_evidence(self, ref: str, quote: str) -> dict:
        """-> {ok, path?, errors[], kind}  Verifies the ref is a canonical
        (rec) or capture (cap) record and the quote occurs verbatim."""

    def submit_proposal(self, record: dict) -> dict:
        """-> {proposal_id, persisted, commit_state}. record is the
        validated candidate body; the backend owns storage and commit."""

    def status(self) -> dict:
        """-> see BrainSurface.brain_status for the expected keys."""


class FakeBackend:
    """Deterministic in-memory backend for fixtures and tests."""

    def __init__(self):
        self.records: dict[str, dict] = {}     # id -> record dict
        self.captures: dict[str, dict] = {}
        self.proposals: list[dict] = []
        self.snapshot = "fake-snap-1"
        self.unavailable: set[str] = set()     # method names that raise
        self.commit_state = "committed"
        self.calls: list[tuple] = []

    # -- fixture helpers
    def add_record(self, rid, title, text, zone="03-objects", level=4,
                   edges=(), status="active", section=None):
        self.records[rid] = {"id": rid, "title": title, "text": text,
                             "zone": zone, "level": level, "edges": list(edges),
                             "status": status, "section": section}

    def add_capture(self, cid, text, media=None, needs=None):
        self.captures[cid] = {"id": cid, "text": text, "media": media,
                              "needs": needs or []}

    def _guard(self, name):
        self.calls.append((name,))
        if name in self.unavailable:
            raise BackendUnavailable(f"{name} unavailable")

    # -- Backend
    def search(self, query, mode, scope, n):
        self._guard("search")
        q = query.casefold()
        toks = q.split()

        def hit(text):
            t = text.casefold()
            return (q in t) if mode == "exact" else all(x in t for x in toks)
        out = {}
        if scope in ("canonical", "all"):
            out["canonical"] = [
                {"ref": f"rec:{r['id']}", "title": r["title"], "zone": r["zone"],
                 "snippet": r["text"][:120], "relevance": 1.0}
                for r in sorted(self.records.values(), key=lambda r: r["id"])
                if hit(r["title"] + " " + r["text"])][:n]
        if scope in ("captures", "all"):
            out["captures"] = [
                {"ref": f"cap:{c['id']}", "zone": "01-inbox/captures",
                 "snippet": c["text"][:120]}
                for c in sorted(self.captures.values(), key=lambda c: c["id"])
                if hit(c["text"])][:n]
        return out

    def read(self, ref):
        self._guard("read")
        kind, _, ident = ref.partition(":")
        if kind == "rec" and ident in self.records:
            r = self.records[ident]
            return {"ref": ref, "kind": "record", "zone": r["zone"],
                    "authority_level": r["level"], "content": r["text"],
                    "bytes": len(r["text"].encode()), "truncated": False,
                    "metadata": {"title": r["title"], "status": r["status"]}}
        if kind == "cap" and ident in self.captures:
            c = self.captures[ident]
            media = None
            if c["media"]:
                media = {"present": True, "kind": c["media"],
                         "inspectable": False}
            return {"ref": ref, "kind": "capture", "zone": "01-inbox/captures",
                    "authority_level": 7, "content": c["text"],
                    "bytes": len(c["text"].encode()), "truncated": False,
                    "metadata": {"needs": c["needs"]}, "media": media}
        raise KeyError(ref)

    def capture(self, text, language_hint):
        self._guard("capture")
        digest = hashlib.sha256(text.encode()).hexdigest()
        dup = next((k for k, c in self.captures.items()
                    if hashlib.sha256(c["text"].encode()).hexdigest() == digest),
                   None)
        cid = f"cap-fake-{len(self.captures) + 1:04d}"
        self.add_capture(cid, text)
        return {"ref": f"cap:{cid}", "status": "received", "sha256": digest,
                "duplicate_of": f"cap:{dup}" if dup else None,
                "persisted": True, "commit_state": self.commit_state}

    def reconcile(self, seed_ref, query, hops, expand):
        self._guard("reconcile")
        sections: dict[str, list] = {}
        seen = set()
        frontier = []
        seed_id = seed_ref.split(":", 1)[1] if seed_ref else None
        if seed_id and seed_id not in self.records:
            raise KeyError(seed_ref)
        if seed_id:
            frontier = [seed_id]
            seen.add(seed_id)
        starts = frontier + [e.split(":", 1)[1] for e in expand
                             if e.split(":", 1)[1] in self.records]
        dist = {s: 0 for s in starts}
        frontier = list(starts)
        seen |= set(starts)
        for d in range(1, hops + 1):
            nxt = []
            for rid in frontier:
                nbrs = set(self.records[rid]["edges"]) | {
                    o for o, r in self.records.items() if rid in r["edges"]}
                for o in sorted(nbrs):
                    if o in self.records and o not in seen:
                        seen.add(o); dist[o] = d; nxt.append(o)
            frontier = nxt
        if query:
            for h in self.search(query, "lexical", "canonical", 50)["canonical"]:
                rid = h["ref"].split(":", 1)[1]
                if rid not in seen:
                    seen.add(rid); dist[rid] = None
        for rid in seen:
            r = self.records[rid]
            zone_sec = {"02-sources": "source_records", "05-claims": "claims",
                        "06-relations": "relations"}
            name = r["section"] or ("superseded" if r["status"] == "superseded"
                                    else zone_sec.get(r["zone"], "canonical_records"))
            sections.setdefault(name, []).append({
                "id": rid, "ref": f"rec:{rid}", "authority_level": r["level"],
                "reason": "seed" if rid == seed_id else (
                    f"{dist[rid]}-hop" if dist.get(rid) else f"lexical: {query}"),
                "distance": dist.get(rid), "title": r["title"],
                "status": r["status"]})
        return {"snapshot": self.snapshot, "seed": seed_ref,
                "sections": sections}

    def resolve_evidence(self, ref, quote):
        self._guard("resolve_evidence")
        kind, _, ident = ref.partition(":")
        if kind == "rec" and ident in self.records:
            r = self.records[ident]
            ok = quote in r["text"]
            return {"ok": ok, "kind": "rec", "path": f"{r['zone']}/{ident}.md",
                    "errors": [] if ok else
                    [f"quote NOT found verbatim in {ref}"]}
        if kind == "cap" and ident in self.captures:
            ok = quote in self.captures[ident]["text"]
            return {"ok": ok, "kind": "cap", "path": None,
                    "errors": [] if ok else
                    [f"quote NOT found verbatim in {ref}"]}
        return {"ok": False, "kind": None, "path": None,
                "errors": [f"unknown ref: {ref}"]}

    def submit_proposal(self, record):
        self._guard("submit_proposal")
        pid = f"prop-fake-{len(self.proposals) + 1:04d}"
        self.proposals.append({"id": pid, **record})
        return {"proposal_id": pid, "persisted": True,
                "commit_state": self.commit_state}

    def status(self):
        self._guard("status")
        return {
            "snapshot": self.snapshot,
            "counts": {"canonical_records": len(self.records),
                       "captures": len(self.captures),
                       "proposals": len(self.proposals)},
            "pending_needs": [
                {"ref": f"cap:{c['id']}", "needs": c["needs"]}
                for c in self.captures.values() if c["needs"]],
            "proposals": {"new": len(self.proposals)},
            "validator": {"state": "not_checked"},
            "search": {"state": "fake"},
            "uncommitted": []}
