"""BrainSurface: the six frozen K3 tools over an injected Backend.

Every method returns a plain dict and never raises: rejections are
{"ok": False, "error": {"code", "message", ...}}. Success is only ever
claimed from what the backend actually reported (persisted, commit_state).
"""
from __future__ import annotations

import base64
import hashlib
import json

from . import contract as C
from .backend import BackendUnavailable

DEFAULT_N = 10
MAX_N = 50
MAX_CAPTURE_CHARS = 500_000          # internal safety bound, reported when hit
DEFAULT_PAGE_LIMIT = 40              # items per section per page (paging, not a cap)
DEEP_HOPS = 3


def _err(code, message, persisted=None, **extra):
    out = {"ok": False, "error": {"code": code, "message": message, **extra}}
    if persisted is not None:  # write tools only: explicit "nothing stored"
        out["persisted"] = persisted
    return out


def _shape(msg):
    return _err("E_BACKEND_SHAPE", msg)


class BrainSurface:
    def __init__(self, backend, page_limit: int = DEFAULT_PAGE_LIMIT,
                 deep_hops: int = DEEP_HOPS, proposal_kinds: dict | None = None):
        self.b = backend
        self.page_limit = max(1, page_limit)
        self.deep_hops = max(2, deep_hops)
        self.kinds = (proposal_kinds if proposal_kinds is not None
                      else C.remote_proposal_kinds())

    # ------------------------------------------------------------ dispatch
    def tools(self):
        return C.TOOLS

    def call(self, name: str, arguments: dict | None) -> dict:
        if name not in C.TOOL_NAMES:
            return _err("E_UNKNOWN_TOOL", f"unknown tool: {name}")
        args = arguments if isinstance(arguments, dict) else {}
        allowed = set(next(t for t in C.TOOLS if t["name"] == name)
                      ["inputSchema"]["properties"])
        extra = sorted(set(args) - allowed)
        if extra:  # includes "path", "channel", "actor", ... by construction
            return _err("E_UNKNOWN_ARGUMENT",
                        f"argument(s) not accepted: {extra}", accepted=sorted(allowed))
        wp = False if name in ("brain_capture", "brain_propose") else None
        try:
            return getattr(self, name)(**args)
        except BackendUnavailable as exc:
            return _err("E_UNAVAILABLE", str(exc), persisted=wp)
        except KeyError as exc:
            return _err("E_NOT_FOUND", f"no such record: {exc.args[0]}")
        except TypeError as exc:
            return _err("E_BAD_ARGUMENTS", str(exc), persisted=wp)
        except Exception as exc:  # noqa: BLE001 - never leak a traceback
            return _err("E_INTERNAL", f"{type(exc).__name__}: {exc}",
                        persisted=wp)

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _bad_ref(ref):
        if not isinstance(ref, str) or not C.REF_RE.match(ref):
            return _err("E_BAD_REF",
                        "ref must be an opaque id like 'rec:<id>' or "
                        "'cap:<id>' from a prior tool result; paths are "
                        "never accepted")
        return None

    # ---------------------------------------------------------- brain_search
    def brain_search(self, query, mode="lexical", scope="canonical", n=None):
        if not isinstance(query, str) or not query.strip():
            return _err("E_BAD_ARGUMENTS", "query must be a non-empty string")
        if mode not in C.SEARCH_MODES:
            return _err("E_BAD_ARGUMENTS", f"mode must be one of {C.SEARCH_MODES}")
        if scope not in C.SEARCH_SCOPES:
            return _err("E_BAD_ARGUMENTS", f"scope must be one of {C.SEARCH_SCOPES}")
        if n is not None and (not isinstance(n, int) or isinstance(n, bool) or n < 1):
            return _err("E_BAD_ARGUMENTS", "n must be a positive integer")
        n_eff = min(n or DEFAULT_N, MAX_N)
        raw = self.b.search(query.strip(), mode, scope, n_eff)
        want = {"canonical": ("canonical", "all"), "captures": ("captures", "all")}
        groups, notes = {}, []
        for g, scopes in want.items():
            if scope not in scopes:
                continue
            hits = raw.get(g)
            if not isinstance(hits, list):
                return _shape(f"backend search result lacks group {g!r}")
            clean = []
            for h in hits:
                ref = h.get("ref")
                if self._bad_ref(ref):
                    notes.append(f"dropped a {g} hit without a valid ref")
                    continue
                is_cap = ref.startswith("cap:")
                if (g == "canonical") == is_cap:
                    # a capture may never surface in the canonical group
                    # (and vice versa): defence in depth for K3
                    notes.append(f"dropped {ref}: wrong group for {g}")
                    continue
                item = {"ref": ref, "zone": h.get("zone"),
                        "tier": "candidate" if is_cap else "canonical",
                        "authority_level": C.CANDIDATE_LEVEL if is_cap
                        else h.get("authority_level", C.LEVEL_BY_ZONE.get(
                            h.get("zone"), None)),
                        "title": h.get("title"), "snippet": h.get("snippet"),
                        "line": h.get("line"), "relevance": h.get("relevance")}
                clean.append({k: v for k, v in item.items() if v is not None})
            groups[g] = {"count": len(clean), "results": clean[:n_eff]}
        out = {"ok": True, "query": query.strip(), "mode": mode, "scope": scope,
               "n": n_eff, "groups": groups, "notes": notes,
               "authority_note": C.AUTHORITY_NOTE}
        if scope in ("captures", "all"):
            out["capture_note"] = C.CAPTURE_NOTE
        if n is not None and n > MAX_N:
            out["notes"].append(f"n clamped to safety bound {MAX_N}")
        if all(g["count"] == 0 for g in groups.values()):
            out["notes"].append(
                "no hits: lexical search does not match paraphrase; retry "
                "with exact terms/variants before concluding absence")
        return out

    # ------------------------------------------------------------ brain_read
    def brain_read(self, ref):
        bad = self._bad_ref(ref)
        if bad:
            return bad
        r = self.b.read(ref)
        for k in ("kind", "content", "authority_level"):
            if k not in r:
                return _shape(f"backend read result lacks {k!r}")
        out = {"ok": True, "ref": ref, "kind": r["kind"], "zone": r.get("zone"),
               "authority_level": r["authority_level"],
               "content": r["content"], "bytes": r.get("bytes"),
               "truncated": bool(r.get("truncated")),
               "metadata": r.get("metadata") or {},
               "authority_note": C.CAPTURE_NOTE if r["kind"] == "capture"
               else C.AUTHORITY_NOTE}
        media = r.get("media")
        if media:
            out["media"] = {"present": True, "kind": media.get("kind"),
                            "inspectable": False,
                            "note": ("media exists in the archive but is not "
                                     "delivered over this connector; do not "
                                     "describe or transcribe it")}
        if out["truncated"]:
            out["truncation_reason"] = r.get("truncation_reason",
                                             "server read-size safety bound")
        return out

    # --------------------------------------------------------- brain_capture
    def brain_capture(self, text, language_hint="unknown"):
        if not isinstance(text, str) or not text.strip():
            return _err("E_BAD_ARGUMENTS", "text must be non-empty", persisted=False)
        if language_hint not in C.LANG_HINTS:
            return _err("E_BAD_ARGUMENTS",
                        f"language_hint must be one of {C.LANG_HINTS}",
                        persisted=False)
        if len(text) > MAX_CAPTURE_CHARS:
            return _err("E_TOO_LARGE",
                        f"text exceeds the {MAX_CAPTURE_CHARS}-character "
                        "single-capture safety bound; split it into several "
                        "captures at natural boundaries", persisted=False)
        r = self.b.capture(text, language_hint)
        if not r.get("persisted"):
            return _err("E_NOT_PERSISTED", "backend did not confirm storage",
                        persisted=False)
        return {"ok": True, "ref": r["ref"], "status": r.get("status", "received"),
                "sha256": r.get("sha256"), "duplicate_of": r.get("duplicate_of"),
                "persisted": True,
                "commit_state": r.get("commit_state", "not_attempted"),
                "authority_note": C.CAPTURE_NOTE}

    # ------------------------------------------------ brain_reconcile_context
    def _sig(self, seed_ref, query, depth, sections, expand, snapshot):
        blob = json.dumps([seed_ref, query, depth, sections, expand, snapshot],
                          sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    def brain_reconcile_context(self, seed_ref=None, query=None, depth="focused",
                                cursor=None, sections=None, expand=None):
        if depth not in C.DEPTHS:
            return _err("E_BAD_ARGUMENTS", f"depth must be one of {C.DEPTHS}")
        if seed_ref is not None:
            bad = self._bad_ref(seed_ref)
            if bad:
                return bad
        if not seed_ref and not (isinstance(query, str) and query.strip()):
            return _err("E_BAD_ARGUMENTS", "provide seed_ref and/or query")
        sections = list(sections) if sections else None
        if sections is not None:
            unknown = sorted(set(sections) - set(C.RECONCILE_SECTIONS))
            if unknown:
                return _err("E_BAD_ARGUMENTS", f"unknown sections: {unknown}",
                            valid=list(C.RECONCILE_SECTIONS))
        expand = list(expand) if expand else []
        for e in expand:
            bad = self._bad_ref(e)
            if bad:
                return bad
        hops = 1 if depth == "focused" else self.deep_hops
        pkg = self.b.reconcile(seed_ref, query.strip() if query else None,
                               hops, expand)
        snap = pkg.get("snapshot")
        secs = pkg.get("sections")
        if snap is None or not isinstance(secs, dict):
            return _shape("backend reconcile result lacks snapshot/sections")
        sig = self._sig(seed_ref, query, depth, sections, expand, snap)
        page = 0
        if cursor:
            try:
                cur = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                page = int(cur["page"])
                if page < 0:
                    raise ValueError
            except Exception:  # noqa: BLE001
                return _err("E_BAD_CURSOR", "cursor is not a valid token")
            if cur.get("sig") != sig:
                return _err("E_STALE_CURSOR",
                            "cursor does not match these parameters or the "
                            "archive changed since it was issued; restart "
                            "without a cursor")
        names = sections or list(C.RECONCILE_SECTIONS)
        out_sections, truncation, more = {}, [], False
        lo, hi = page * self.page_limit, (page + 1) * self.page_limit
        for name in names:
            items = secs.get(name, [])
            for it in items:
                need = ("id", "authority_level", "reason")
                if any(k not in it for k in need) or (
                        "ref" not in it) or (it["ref"] is None and name != "unresolved"):
                    return _shape(f"item in {name!r} lacks id/ref/"
                                  "authority_level/reason")
            ordered = sorted(items, key=lambda i: (
                i["authority_level"] if i["authority_level"] is not None else 99,
                i.get("distance") if i.get("distance") is not None else 999,
                str(i["id"])))
            chunk = ordered[lo:hi]
            out_sections[name] = {"total": len(ordered), "returned": len(chunk),
                                  "offset": lo, "items": chunk}
            if hi < len(ordered):
                more = True
                truncation.append({
                    "section": name, "returned_through": min(hi, len(ordered)),
                    "total": len(ordered),
                    "reason": f"page size {self.page_limit} per section "
                              "(paging bound, not a relevance cut)"})
        next_cursor = None
        if more:
            next_cursor = base64.urlsafe_b64encode(json.dumps(
                {"v": 1, "page": page + 1, "sig": sig}).encode()).decode()
        return {"ok": True, "depth": depth, "seed": seed_ref, "query": query,
                "snapshot": snap, "page": page, "hops": hops,
                "sections": out_sections, "truncated": more,
                "truncation": truncation, "next_cursor": next_cursor,
                "verdict": None,
                "authority_note": C.AUTHORITY_NOTE + " No conflict, "
                "supersession or relation type has been decided here."}

    # ---------------------------------------------------------- brain_propose
    def brain_propose(self, kind, structured_fields, evidence_refs):
        spec = self.kinds.get(kind)
        if spec is None:
            return _err("E_PROPOSAL_REJECTED", f"unsupported kind {kind!r}",
                        reasons=[f"kind must be one of {sorted(self.kinds)}"],
                        persisted=False)
        reasons = []
        if not isinstance(structured_fields, dict):
            return _err("E_PROPOSAL_REJECTED", "structured_fields must be an object",
                        reasons=["structured_fields is not an object"],
                        persisted=False)
        for f in spec["required"]:
            v = structured_fields.get(f)
            if v is None or (isinstance(v, str) and not v.strip()):
                reasons.append(f"missing required field: {f}")
        allowed = set(spec["required"]) | {"note"}
        for f in sorted(set(structured_fields) - allowed):
            reasons.append(f"unknown field: {f}")
        to_enum = spec["extra"].get("to_tier_enum")
        if to_enum and structured_fields.get("to_tier") not in to_enum:
            reasons.append(f"to_tier must be one of {to_enum}")
        if not isinstance(evidence_refs, list) or not evidence_refs:
            reasons.append("evidence_refs must contain at least one item")
            evidence_refs = []
        passages, supporting = [], []
        for i, ev in enumerate(evidence_refs):
            if not isinstance(ev, dict):
                reasons.append(f"evidence_refs[{i}] is not an object")
                continue
            ref, quote = ev.get("ref"), ev.get("quote")
            if self._bad_ref(ref):
                reasons.append(f"evidence_refs[{i}].ref is not a valid ref")
                continue
            if not isinstance(quote, str) or len(quote) < 20:
                reasons.append(f"evidence_refs[{i}].quote must be >= 20 chars")
                continue
            res = self.b.resolve_evidence(ref, quote)
            if not res.get("ok"):
                reasons += [f"evidence_refs[{i}]: {e}" for e in
                            res.get("errors") or ["unverifiable"]]
            elif res.get("kind") == "rec":
                passages.append({"path": res["path"], "quote": quote})
            else:
                supporting.append({"ref": ref, "quote": quote})
        if not passages and not any("evidence_refs" in r for r in reasons):
            reasons.append("at least one canonical (rec:) evidence ref with a "
                           "verbatim quote is required; captures alone do not "
                           "count as evidence")
        if reasons:
            return _err("E_PROPOSAL_REJECTED",
                        "proposal rejected before storage; nothing was written",
                        reasons=reasons, persisted=False)
        body = {k: structured_fields[k] for k in structured_fields}
        body["source_passage"] = passages[0]
        if len(passages) > 1:
            body["additional_passages"] = passages[1:]
        if supporting:
            body["supporting_captures"] = supporting
        r = self.b.submit_proposal({"kind": kind, "authority_tier": "candidate",
                                    "status": "new", "body": body})
        if not r.get("persisted"):
            return _err("E_NOT_PERSISTED", "backend did not confirm storage",
                        persisted=False)
        return {"ok": True, "ref": f"prop:{r['proposal_id']}", "kind": kind,
                "status": "new", "authority_tier": "candidate",
                "persisted": True,
                "commit_state": r.get("commit_state", "not_attempted"),
                "note": ("Stored as a candidate for human adjudication; the "
                         "archive has not changed."),
                "authority_note": C.AUTHORITY_NOTE}

    # ----------------------------------------------------------- brain_status
    def brain_status(self):
        s = self.b.status()
        keys = ("snapshot", "counts", "pending_needs", "proposals",
                "validator", "search", "uncommitted")
        out = {"ok": True}
        for k in keys:
            out[k] = s.get(k)  # missing stays null, never invented
        out["missing"] = [k for k in keys if k not in s]
        out["degraded"] = bool(out["uncommitted"]) or bool(out["missing"]) or (
            (out["search"] or {}).get("state") in ("missing", "unavailable"))
        out["authority_note"] = C.AUTHORITY_NOTE
        return out
