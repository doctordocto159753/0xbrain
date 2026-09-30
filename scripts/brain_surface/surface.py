"""BrainSurface: the six frozen K3 tools over the integrated WikiBackend.

The surface owns only the public contract: argument shapes, the typed-ref
boundary, result envelopes and authority labelling. Semantics belong to the
owning subsystems behind WikiBackend (search: C, capture: D, proposals /
K9 / K5: E); the surface never re-validates what they validate.

Every method returns a plain dict and never raises: rejections are
{"ok": False, "error": {"code", "message", ...}}. Success is only ever
claimed from what the backend actually reported (persisted, commit_state).
"""
from __future__ import annotations

from . import contract as C
from . import refs as R
from .backend import BackendUnavailable, Rejected, WikiBackend

DEFAULT_N = 10
MAX_N = 50
MAX_CAPTURE_CHARS = 500_000          # internal safety bound, reported when hit
MAX_FIELD_CHARS = 20_000
WRITE_TOOLS = ("brain_capture", "brain_propose")


def _err(code, message, persisted=None, **extra):
    out = {"ok": False, "error": {"code": code, "message": message, **extra}}
    if persisted is not None:  # write tools only: explicit "nothing stored"
        out["persisted"] = persisted
    return out


def _bad_ref(ref):
    try:
        R.parse(ref)
    except R.BadRef as exc:
        return _err("E_BAD_REF", str(exc))
    return None


class BrainSurface:
    def __init__(self, backend=None):
        self.b = backend if backend is not None else WikiBackend()

    # ------------------------------------------------------------ dispatch
    def tools(self):
        return C.TOOLS

    def call(self, name: str, arguments: dict | None) -> dict:
        if name not in C.TOOL_NAMES:
            return _err("E_UNKNOWN_TOOL", f"unknown tool: {name}")
        args = arguments if isinstance(arguments, dict) else {}
        allowed = set(next(t for t in C.TOOLS if t["name"] == name)["inputSchema"]["properties"])
        extra = sorted(set(args) - allowed)
        wp = False if name in WRITE_TOOLS else None
        if extra:  # includes "path", "channel", "actor", ... by construction
            return _err("E_UNKNOWN_ARGUMENT", f"argument(s) not accepted: {extra}",
                        persisted=wp, accepted=sorted(allowed))
        try:
            return getattr(self, name)(**args)
        except Rejected as exc:
            extra_fields = dict(exc.extra)
            persisted = extra_fields.pop("persisted", wp)
            return _err(exc.code, exc.message, persisted=persisted, **extra_fields)
        except R.BadRef as exc:
            return _err("E_BAD_REF", str(exc), persisted=wp)
        except BackendUnavailable as exc:
            return _err("E_UNAVAILABLE", str(exc), persisted=wp)
        except KeyError as exc:
            return _err("E_NOT_FOUND", f"no such item: {exc.args[0]}", persisted=wp)
        except Exception as exc:  # noqa: BLE001 - never leak a traceback
            return _err("E_INTERNAL", type(exc).__name__, persisted=wp)

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
        want = ("canonical", "captures") if scope == "all" else (scope,)
        groups, notes = {}, []
        for g in want:
            grp = raw.get(g) or {"results": []}
            clean = []
            for h in grp["results"]:
                ref = h.get("ref")
                if ref is not None and _bad_ref(ref):
                    ref = None
                is_cap = bool(ref) and ref.startswith("cap:")
                if ref and (g == "canonical") == is_cap:
                    notes.append(f"dropped {ref}: wrong group for {g}")   # K3 defence in depth
                    continue
                if ref is None:
                    notes.append("a hit has no public ref and cannot be opened remotely")
                clean.append(h)
            groups[g] = {k: v for k, v in grp.items() if k != "results"}
            groups[g].update({"count": len(clean), "results": clean[:n_eff],
                              "authority_note": C.CAPTURE_NOTE if g == "captures"
                              else C.SCOPE_NOTE})
        out = {"ok": True, "query": query.strip(), "mode": mode, "scope": scope, "n": n_eff,
               "model_free": True, "groups": groups, "notes": notes,
               "authority_note": C.AUTHORITY_NOTE}
        if n is not None and n > MAX_N:
            notes.append(f"n clamped to {MAX_N}")
        if all(g["count"] == 0 for g in groups.values()):
            notes.append("no hits: lexical search does not match paraphrase; retry with the "
                         "exact terms, spelling variants, the other language or mode=exact "
                         "before concluding absence")
        return out

    # ------------------------------------------------------------ brain_read
    def brain_read(self, ref):
        bad = _bad_ref(ref)
        if bad:
            return bad
        r = self.b.read(ref)
        out = {"ok": True, **r,
               "authority_note": C.CAPTURE_NOTE if r["kind"] == "capture" else C.AUTHORITY_NOTE}
        if r.get("media"):
            out["media"] = {**r["media"], "inspectable": False,
                            "note": ("media exists in the archive but is not delivered over "
                                     "this connector; do not describe or transcribe it")}
        return out

    # --------------------------------------------------------- brain_capture
    def brain_capture(self, text, language_hint="unknown"):
        if not isinstance(text, str) or not text.strip():
            return _err("E_BAD_ARGUMENTS", "text must be non-empty", persisted=False)
        if language_hint not in C.LANG_HINTS:
            return _err("E_BAD_ARGUMENTS", f"language_hint must be one of {C.LANG_HINTS}",
                        persisted=False)
        if len(text) > MAX_CAPTURE_CHARS:
            return _err("E_TOO_LARGE",
                        f"text exceeds the {MAX_CAPTURE_CHARS}-character single-capture "
                        "safety bound; split it at natural boundaries", persisted=False)
        r = self.b.capture(text, language_hint)
        return {"ok": True, **r, "authority_note": C.CAPTURE_NOTE}

    # ------------------------------------------------ brain_reconcile_context
    def brain_reconcile_context(self, seed_ref=None, query=None, depth="focused",
                                cursor=None, sections=None, expand=None):
        if depth not in C.DEPTHS:
            return _err("E_BAD_ARGUMENTS", f"depth must be one of {C.DEPTHS}")
        if seed_ref is not None:
            bad = _bad_ref(seed_ref)
            if bad:
                return bad
        if query is not None and not isinstance(query, str):
            return _err("E_BAD_ARGUMENTS", "query must be a string")
        query = query.strip() if query and query.strip() else None
        if not seed_ref and not query:
            return _err("E_BAD_ARGUMENTS", "provide seed_ref and/or query")
        if cursor is not None and not isinstance(cursor, str):
            return _err("E_BAD_CURSOR", "cursor must be the string from next_cursor")
        if sections is not None:
            if not isinstance(sections, list) or not all(isinstance(s, str) for s in sections):
                return _err("E_BAD_ARGUMENTS", "sections must be a list of section names")
            unknown = sorted(set(sections) - set(C.RECONCILE_SECTIONS))
            if unknown:
                return _err("E_BAD_ARGUMENTS", f"unknown sections: {unknown}",
                            valid=list(C.RECONCILE_SECTIONS))
        expand = expand if expand is not None else []
        if not isinstance(expand, list):
            return _err("E_BAD_ARGUMENTS", "expand must be a list of refs")
        for e in expand:
            bad = _bad_ref(e)
            if bad:
                return bad
        pkg = self.b.reconcile(seed_ref, query, depth, cursor or None, sections, expand)
        return {"ok": True, "depth": depth, "query": query, **pkg, "verdict": None,
                "authority_note": C.AUTHORITY_NOTE + " No conflict, supersession or "
                "relation type has been decided here."}

    # ---------------------------------------------------------- brain_propose
    def brain_propose(self, kind, structured_fields, evidence_refs):
        reasons = []
        kinds = C.remote_proposal_kinds()
        if kind not in kinds:
            return _err("E_PROPOSAL_REJECTED", f"unsupported kind {kind!r}",
                        reasons=[f"kind must be one of {sorted(kinds)}"], persisted=False)
        if not isinstance(structured_fields, dict):
            return _err("E_PROPOSAL_REJECTED", "structured_fields must be an object",
                        reasons=["structured_fields is not an object"], persisted=False)
        if "source_passage" in structured_fields:
            reasons.append("source_passage is derived from evidence_refs; do not send it")
        for k, v in structured_fields.items():
            if not isinstance(v, str) or len(v) > MAX_FIELD_CHARS:
                reasons.append(f"field {k!r} must be a string of at most {MAX_FIELD_CHARS} chars")
        if not isinstance(evidence_refs, list) or not evidence_refs:
            reasons.append("evidence_refs must contain at least one item")
            evidence_refs = []
        for i, ev in enumerate(evidence_refs):
            if not isinstance(ev, dict) or set(ev) - {"ref", "quote"} or "ref" not in ev:
                reasons.append(f"evidence_refs[{i}] must be an object {{ref, quote?}}")
            elif _bad_ref(ev["ref"]):
                reasons.append(f"evidence_refs[{i}].ref is not a valid ref (paths are never accepted)")
            elif "quote" in ev and not isinstance(ev["quote"], str):
                reasons.append(f"evidence_refs[{i}].quote must be a string")
        if reasons:
            return _err("E_PROPOSAL_REJECTED",
                        "proposal rejected before storage; nothing was written",
                        reasons=reasons, persisted=False)
        r = self.b.propose(kind, structured_fields, evidence_refs)
        return {"ok": True, "ref": f"prop:{r['proposal_id']}", "kind": kind, "status": "new",
                "authority_tier": "candidate", "persisted": True,
                **{k: v for k, v in r.items() if k not in ("proposal_id", "persisted")},
                "note": ("Stored as a candidate for human adjudication; the archive has "
                         "not changed."),
                "authority_note": C.AUTHORITY_NOTE}

    # ----------------------------------------------------------- brain_status
    def brain_status(self):
        s = self.b.status()
        degraded = []
        if not s["uncommitted"]["clean"]:
            degraded.append("uncommitted noncanonical writes")
        if s["search"]["state"] != "ok":
            degraded.append(f"search index {s['search']['state']}")
        if s["validation"].get("state") != "pass":
            degraded.append("validation not passing")
        return {"ok": True, **s, "degraded": bool(degraded), "degraded_reasons": degraded,
                "authority_note": C.AUTHORITY_NOTE}
