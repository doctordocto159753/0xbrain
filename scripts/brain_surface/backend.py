"""WikiBackend: the one production backend behind the K3 surface.

Every operation delegates to the subsystem that owns it; nothing here
re-implements search, capture, proposal validation or reconciliation:

  search      Agent C  search_lexical.search (model-free QMD BM25 + exact)
  read        refs.resolve (public ref boundary) + wiki_capture.read_capture
  capture     Agent D  wiki_capture.capture_text, channel fixed server-side,
              inside Agent E's K9 git_safety.locked_write_commit
  propose     Agent E  brain_proposals.submit_proposal (validate_submission)
  reconcile   Agent E  reconcile_context.reconcile_context (K5)
  status      counts, capture needs (Agent D), proposals, search freshness
              (Agent C), last-known validation, git_safety.uncommitted_state

This module translates between the public typed-ref grammar and the internal
ids/paths each subsystem uses. Nothing it returns contains an absolute path,
an environment value or authentication state.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import refs as R

_SCRIPTS = Path(__file__).resolve().parents[1]
for _p in (_SCRIPTS, _SCRIPTS / "capture"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import brain_proposals as bp  # noqa: E402
import evidence_audit as ea  # noqa: E402
import git_safety as gs  # noqa: E402
import reconcile_context as rc  # noqa: E402
import search_lexical as sl  # noqa: E402
import wiki_capture as cap  # noqa: E402

CAPTURE_CHANNEL = "mcp"          # fixed server-side; never caller-supplied
MAX_READ_BYTES = 400_000         # same bound as the legacy wiki_read / K5 expand
VALIDATION_CACHE = "brain-validation.json"
VALIDATION_TIMEOUT = 600


class BackendUnavailable(Exception):
    """A dependency (search index, git, store) cannot serve the call."""


class Rejected(Exception):
    """The request is well-formed but refused by the owning validator."""

    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


def scrub(text, root: Path) -> str:
    """Remove server filesystem locations from text that leaves the server."""
    text = str(text or "")
    for loc in {str(root), str(root.resolve())}:
        text = text.replace(loc + os.sep, "").replace(loc, ".")
    return text


def _commit_state(res: dict, root: Path) -> dict:
    """K9 result -> public commit report (no filesystem internals)."""
    if res.get("committed"):
        return {"commit_state": "committed", "commit": res.get("commit")}
    return {"commit_state": "uncommitted", "commit": None,
            "commit_stage": res.get("stage"),
            "commit_error": scrub(res.get("error"), root)[-300:],
            "note": ("Stored durably but not yet committed to Git. Nothing was "
                     "rolled back; brain_status lists it until the owner "
                     "flushes it (brain_review.py flush).")}


_INDEX_LOCK = threading.Lock()


class WikiBackend:
    def __init__(self, root: Path | None = None, *, page_size: int | None = None,
                 validation_timeout: int = VALIDATION_TIMEOUT):
        self.root = Path(root or _SCRIPTS.parent).resolve()
        self.page_size = page_size
        self.validation_timeout = validation_timeout
        cap_root = Path(cap.REPO_ROOT).resolve()
        if cap_root != self.root:
            # wiki_capture is rooted at import time; a mismatch would write
            # captures into a different tree than the one being committed.
            raise BackendUnavailable(
                "capture core is rooted elsewhere; run the server from the wiki checkout")

    # --------------------------------------------------------------- search
    def ensure_index(self) -> dict:
        """Bring the rebuildable BM25 index up to date before a lexical query:
        register collections when the index is missing (fresh install,
        restore) and `qmd update` when files changed (e.g. a new capture).
        Model-free by construction (search_lexical guards every qmd call)."""
        with _INDEX_LOCK:
            fr = sl.index_freshness(self.root)
            if not fr["index_present"]:
                sl.configure(self.root)
                return {"action": "configured"}
            if fr["stale"]:
                sl.refresh(self.root)
                return {"action": "refreshed"}
            return {"action": "none"}

    def search(self, query: str, mode: str, scope: str, n: int) -> dict:
        if mode == "lexical" and shutil.which("qmd") is None:
            raise BackendUnavailable("lexical search index unavailable (qmd not installed); "
                                     "mode='exact' still works")
        try:
            if mode == "lexical":
                self.ensure_index()
            raw = sl.search(query, mode=mode, scope=scope, n=n, root=self.root)
        except sl.ModelFreeViolation as exc:          # never degrade into a model path
            raise BackendUnavailable(f"search refused: {exc}") from exc
        except sl.SearchError as exc:
            raise BackendUnavailable(f"search failed: {exc}") from exc
        out: dict = {}
        for group, g in raw["groups"].items():
            hits = []
            for h in g.get("results", []):
                ref = R.public_ref_for_path(self.root, h["path"], h.get("id"))
                item = {"ref": ref, "tier": h.get("tier"), "zone": R.zone_of(h["path"]),
                        "authority_level": ea.authority_level(h["path"]),
                        "title": h.get("title"), "snippet": h.get("snippet") or h.get("context"),
                        "rank": h.get("rank")}
                if mode == "exact":
                    item.update({"line": h.get("line"), "lines": h.get("lines"),
                                 "hit_count": h.get("hit_count")})
                hits.append({k: v for k, v in item.items() if v is not None})
            meta = {k: g[k] for k in ("strategy", "attempts", "truncated", "matched_files")
                    if k in g}
            if "tiers" in g:
                meta["tiers"] = g["tiers"]
            out[group] = {"results": hits, **meta}
        return out

    # ----------------------------------------------------------------- read
    def read(self, ref: str) -> dict:
        res = R.resolve(self.root, ref)              # BadRef / KeyError
        base = {"ref": ref, "zone": res.zone, "tier": res.tier,
                "authority_level": res.authority_level}
        if res.kind == "cap":
            got = cap.read_capture(res.ident)
            fm = got["front_matter"]
            meta = {k: fm.get(k) for k in (
                "status", "capture_kind", "captured_at", "capture_channel", "language_hint",
                "sha256", "duplicate_of", "transcription_state", "transcription_method",
                "transcription_reviewed", "quality_flags", "derivative_methods",
                "derivative_tier")}
            meta["interpretation_needs"] = cap.interpretation_gaps(fm, got["sections"])
            out = {**base, "kind": "capture", "sections": got["sections"],
                   "metadata": meta}
            text = json.dumps(got["sections"], ensure_ascii=False)
            out["bytes"] = len(text.encode("utf-8"))
            out["truncated"] = False
            if fm.get("raw_media"):
                out["media"] = {"present": True, "kind": fm.get("capture_kind"),
                                "inspectable": False}
            return out
        if res.kind == "prop":
            rec = next(r for r in ea._load_proposals(self.root) if r.get("id") == res.ident)
            return {**base, "kind": "proposal", "content": rec,
                    "metadata": {"status": rec.get("status"), "kind": rec.get("kind")},
                    "truncated": False}
        raw = (self.root / res.path).read_bytes()
        text = raw[:MAX_READ_BYTES].decode("utf-8", errors="ignore")
        kind = {"rec": "record", "doc": "document", "hist": "history"}[res.kind]
        return {**base, "kind": kind, "content": text, "bytes": len(raw),
                "truncated": len(raw) > MAX_READ_BYTES,
                **({"truncation_reason": f"server read bound {MAX_READ_BYTES} bytes"}
                   if len(raw) > MAX_READ_BYTES else {})}

    # -------------------------------------------------------------- capture
    def capture(self, text: str, language_hint: str) -> dict:
        box: dict = {}

        def write():                                 # runs under the K9 lock
            box["r"] = cap.capture_text(text, CAPTURE_CHANNEL, language_hint)
            return [box["r"]["path"]]

        def validate():
            return cap.validate_record(box["r"]["id"])

        try:
            res = gs.locked_write_commit(self.root, write, "brain: capture (mcp)", validate)
        except cap.CaptureError as exc:              # rejected before anything was stored
            raise Rejected(exc.code, exc.message, persisted=False) from exc
        except gs.GovernanceError as exc:            # refused before any write (lock busy)
            raise BackendUnavailable(str(exc)) from exc
        r = box["r"]
        if not (self.root / r["path"]).is_file():
            raise BackendUnavailable("capture write could not be confirmed on disk")
        if res.get("committed"):
            res = {**res, "commit": res.get("commit") or self._head()}
        return {"ref": f"cap:{r['id']}", "status": r["status"], "sha256": r["sha256"],
                "bytes": r["bytes"], "channel": CAPTURE_CHANNEL,
                "duplicate_of": f"cap:{r['duplicate_of']}" if r.get("duplicate_of") else None,
                "persisted": True, **_commit_state(res, self.root)}

    # ------------------------------------------------------------ proposals
    def propose(self, kind: str, structured_fields: dict, evidence_refs: list) -> dict:
        """Map public refs to E's internal refs, then E validates and queues."""
        reasons: list[str] = []
        internal: list = []
        passage = None
        for i, ev in enumerate(evidence_refs):
            ref, quote = ev.get("ref"), ev.get("quote")
            try:
                res = R.resolve(self.root, ref)
            except R.BadRef:
                reasons.append(f"evidence_refs[{i}].ref is not a valid ref")
                continue
            except KeyError:
                reasons.append(f"evidence_refs[{i}]: {ref} does not resolve")
                continue
            if res.kind not in ("rec", "doc", "cap"):
                reasons.append(f"evidence_refs[{i}]: {res.kind}: refs are not evidence "
                               "(use rec:, doc: or cap:)")
                continue
            iref = res.path if res.kind == "doc" else res.ident
            internal.append({"ref": iref, "quote": quote} if quote else iref)
            if passage is None and quote and res.kind in ("rec", "doc"):
                passage = {"path": res.path, "quote": quote}
        if reasons:
            raise Rejected("E_PROPOSAL_REJECTED",
                           "proposal rejected before storage; nothing was written",
                           reasons=reasons, persisted=False)
        fields = dict(structured_fields)
        if passage is not None:
            fields["source_passage"] = passage
        out = bp.submit_proposal(self.root, kind, fields, internal)
        if not out.get("accepted"):
            raise Rejected("E_PROPOSAL_REJECTED",
                           "proposal rejected before storage; nothing was written",
                           reasons=out.get("errors") or ["rejected"], persisted=False)
        return {"proposal_id": out["proposal_id"], "persisted": True,
                **_commit_state({"committed": out.get("committed"),
                                 "commit": out.get("commit") or (self._head() if out.get("committed") else None),
                                 "stage": None if out.get("committed") else "commit",
                                 "error": out.get("commit_error")}, self.root)}

    # ------------------------------------------------------------ reconcile
    def _internal_anchor(self, ref: str) -> str:
        res = R.resolve(self.root, ref)
        if res.kind == "rec" or res.kind == "cap":
            return res.ident
        if res.kind in ("doc", "hist"):
            return res.path
        raise Rejected("E_BAD_ARGUMENTS", f"{res.kind}: refs cannot anchor reconciliation")

    def _search_fn(self):
        if shutil.which("qmd") is None:
            return None                              # E falls back to exact substring

        def fn(query, n):
            self.ensure_index()
            raw = sl.search(query, mode="lexical", scope="canonical", n=min(int(n), 100),
                            root=self.root)
            return [{"file": h["path"]} for h in raw["groups"]["canonical"]["results"]]
        return fn

    def reconcile(self, seed_ref, query, depth, cursor, sections, expand) -> dict:
        seed = self._internal_anchor(seed_ref) if seed_ref else None
        exp = [self._internal_anchor(e) for e in expand]
        res = rc.brain_reconcile_context(
            self.root, seed_ref=seed, query=query, depth=depth, cursor=cursor,
            sections=sections, expand=exp, page_size=self.page_size,
            search_fn=self._search_fn())
        if not res.get("ok"):
            err = res.get("error") or {}
            code = {"seed_not_found": "E_NOT_FOUND", "cursor_stale": "E_STALE_CURSOR",
                    "bad_cursor": "E_BAD_CURSOR"}.get(err.get("code"), "E_BAD_ARGUMENTS")
            raise Rejected(code, err.get("message", "reconciliation refused"))
        grouped: dict[str, dict] = {s: {"total": res["section_totals"][s], "returned": 0,
                                        "items": []} for s in res["sections"]}
        for it in res["items"]:
            sec = it["section"]
            if sec == "captures":
                pub = R.public_ref_for_id(it["ref"])
            elif sec == "open_proposals":
                pub = f"prop:{it['ref']}" if R.IDENT_RE.fullmatch(str(it["ref"])) else None
            elif sec == "unresolved":
                pub = None
            else:
                pub = R.public_ref_for_path(self.root, it["path"],
                                            None if sec == "chronology" else it["ref"])
            item = {k: v for k, v in it.items() if k not in ("path", "section", "ref")}
            item["ref"] = pub
            if "depth" in item:
                item["distance"] = item.pop("depth")
            grouped[sec]["items"].append(item)
            grouped[sec]["returned"] += 1
        seed_info = res.get("seed") or {}
        return {"seed": {"ref": seed_ref, "kind": seed_info.get("kind"),
                         "resolved": seed_info.get("resolved", False)} if seed_ref else None,
                "sections": grouped, "total": res["total"], "returned": res["returned"],
                "offset": res["offset"], "next_cursor": res["next_cursor"],
                "truncated": res["truncated"], "truncation_reason": res["reason"],
                "materials_changed_since_freeze": res["meta"].get("materials_changed_since_freeze"),
                "last_freeze": res["meta"].get("last_freeze")}

    # --------------------------------------------------------------- status
    def _git(self, *args: str) -> str | None:
        p = subprocess.run(["git", "-c", "core.quotepath=off", *args], cwd=self.root,
                           text=True, capture_output=True, timeout=60)
        return p.stdout.strip() if p.returncode == 0 else None

    def _head(self) -> str | None:
        return self._git("rev-parse", "HEAD")

    def _validation(self) -> dict:
        """Last-known validation, recomputed when HEAD or the working tree
        changed since it was cached (cache lives in the git dir)."""
        head = self._head()
        dirty = self._git("status", "--porcelain=v1", "-uall") or ""
        key = hashlib.sha256(f"{head}\n{dirty}".encode()).hexdigest()
        try:
            cache_file = gs.git_dir(self.root) / VALIDATION_CACHE
        except gs.GovernanceError:
            cache_file = None
        if cache_file and cache_file.is_file():
            try:
                cached = json.loads(cache_file.read_text(encoding="utf-8"))
                if cached.get("key") == key:
                    return {**cached["result"], "cached": True}
            except (OSError, ValueError, KeyError):
                pass
        result = {"checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "head": head}
        for name, cmd in (("repo", [sys.executable, "scripts/validate_repo.py", "--full"]),
                          ("content_release", [sys.executable, "scripts/validate_content_release.py"])):
            try:
                p = subprocess.run(cmd, cwd=self.root, text=True, capture_output=True,
                                   timeout=self.validation_timeout,
                                   env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
                lines = (p.stdout.strip() or p.stderr.strip()).splitlines()
                result[name] = {"state": "pass" if p.returncode == 0 else "fail",
                                "summary": (lines[-1] if lines else "")[:200]}
            except subprocess.TimeoutExpired:
                result[name] = {"state": "timeout", "summary": ""}
        result["state"] = "pass" if all(result[n]["state"] == "pass"
                                        for n in ("repo", "content_release")) else "fail"
        if cache_file:
            try:
                gs.durable_replace(cache_file, json.dumps({"key": key, "result": result}))
            except OSError:
                pass
        return {**result, "cached": False}

    def status(self) -> dict:
        state_file = self.root / "00-system/registers/CORPUS_STATE.json"
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        mi = self.root / "00-system/registers/MATERIALS_INDEX.jsonl"
        registered = sum(1 for ln in mi.read_text(encoding="utf-8-sig").splitlines()
                         if ln.strip()) if mi.is_file() else 0
        originals = self.root / "_originals"
        held = sum(1 for p in originals.rglob("*") if p.is_file()
                   and p.name not in (".gitkeep", "README.md")) if originals.is_dir() else 0
        by_zone: dict[str, int] = {}
        for rel in R._canonical_files(self.root):
            z = R.zone_of(rel)
            by_zone[z] = by_zone.get(z, 0) + 1
        caps = cap.list_captures()["captures"]
        by_state: dict[str, int] = {}
        needs = []
        for c in caps:
            by_state[c["status"]] = by_state.get(c["status"], 0) + 1
            if c["interpretation_needs"] or c["status"] in ("received", "needs-review"):
                needs.append({"ref": f"cap:{c['id']}", "status": c["status"],
                              "kind": c["kind"], "needs": c["interpretation_needs"]})
        props: dict[str, int] = {}
        for rec in ea._load_proposals(self.root):
            s = rec.get("status") or "new"
            props[s] = props.get(s, 0) + 1
        search = {"engine": "qmd-bm25+exact", "model_free": True,
                  "qmd": "available" if shutil.which("qmd") else "missing"}
        if search["qmd"] == "available":
            try:
                fr = sl.index_freshness(self.root)
                search.update({"index_present": fr["index_present"], "stale": fr["stale"],
                               "indexed_files": fr["indexed_files"], "disk_files": fr["disk_files"]})
            except (sl.SearchError, OSError, subprocess.SubprocessError) as exc:
                search.update({"index_present": None, "error": str(exc)[:200]})
        search["state"] = ("unavailable" if search["qmd"] == "missing"
                           else "stale" if search.get("stale") else
                           "ok" if search.get("index_present") else "missing")
        unc = gs.uncommitted_state(self.root)
        canonical_dirty = self._git("status", "--porcelain=v1", "-uall", "--",
                                    *ea.CANONICAL_ZONES, "_originals")
        return {
            "snapshot": state.get("id"),
            "corpus_snapshot_sha256": state.get("corpus_snapshot_sha256"),
            "counts": {"source_material_count": registered,
                       "registered_state_count": state.get("source_material_count"),
                       "held_artifact_count": held,
                       "held_state_count": state.get("held_artifact_count"),
                       "records_by_zone": by_zone, "captures": len(caps),
                       "captures_by_state": by_state},
            "captures_pending": needs,
            "proposals": {"by_status": props,
                          "pending": sum(v for k, v in props.items()
                                         if k in ("new", "audited"))},
            "search": search,
            "validation": self._validation(),
            "uncommitted": {"count": unc["uncommitted_count"], "files": unc["uncommitted"],
                            "last_failure": json.loads(scrub(json.dumps(unc["last_failure"]),
                                                             self.root)) if unc["last_failure"] else None,
                            "clean": unc["clean"]},
            "git": {"head": self._head(), "branch": self._git("rev-parse", "--abbrev-ref", "HEAD"),
                    "canonical_uncommitted": len((canonical_dirty or "").splitlines())},
        }
