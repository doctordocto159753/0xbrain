"""ExtensionSurface: the four enrichment tools (CONTRACT-E1 section 9).

    brain_enrichment_pending   technical work queue for the periodic annotator
    brain_enrichment_submit    candidate metadata revision (needs WRITES=1)
    brain_artifact_read        artifact / exact chunk text
    brain_retrieve             evidence-bearing retrieval + explicit fallback

The surface owns only the public contract: argument shapes, error mapping,
envelopes, authority labelling and the "never leak a path" rule. Semantics
belong to the services behind it (WP-A store, WP-B annotation, WP-C retrieval
/ index) which are injected, or lazily imported on first use.

Rules shared with `brain_surface.BrainSurface.call`:
  * `call` never raises. A rejection is {"ok": False, "error": {code, message,
    ...}} (+ "persisted": False for the write tool).
  * unknown tool / unknown argument -> E_UNKNOWN_ARGUMENT with `accepted`.
  * errors carry a code, a ref or stage; never a traceback, filesystem path or
    secret (`_Scrubber`).
  * success is claimed only from what a service actually returned.

Nothing here can review, approve or promote: that is the local human CLI
(`review.py` / `cli.py`), which this module deliberately never imports.
The seven base tools are not touched: `brain_surface.contract.TOOLS` stays
byte-identical, the adapter merges the two lists.
"""
from __future__ import annotations

import importlib
import inspect
import os
import re
import sqlite3
import sys
import threading
from pathlib import Path
from typing import Any, Callable

from . import contract as C
from . import tools_spec as T
from .config import EnrichmentConfig

AUTHORITY_NOTE = T.AUTHORITY_NOTE
WRITE_TOOLS = ("brain_enrichment_submit",)
SOURCE_REF_RE = re.compile(T.SOURCE_REF_PATTERN)
QUERY_MAX = 1000
CURSOR_MAX = 2048
RETRIEVE_SCOPES = ("canonical", "captures", "all")
# Keys that are server-side only (Layer.origin_path, SourceProjection.anchor_path
# ...). They are removed from every outgoing metadata structure, wherever a
# service accidentally left them.
_PRIVATE_KEYS = frozenset({"anchor_path", "origin_path", "storage_path", "abs_path", "root"})
# Fields whose value is source text or a verbatim quote: never rewritten.
_VERBATIM_KEYS = frozenset({"text", "quote", "snippet", "context"})
FALLBACK_REASON_CODES = ("E_UNAVAILABLE", "E_SCHEMA_UNSUPPORTED")

_SECRET_ENV = re.compile(r"(SECRET|TOKEN|PASSWORD|PASSPHRASE|API_?KEY|CREDENTIAL)", re.I)
_ABS_PATH = re.compile(r"(?<![\w:./-])(?:/[A-Za-z0-9._@+-]+){2,}|[A-Za-z]:\\[^\s'\"]+")


# ---------------------------------------------------------------- scrubbing
class _Scrubber:
    """Removes the server root, absolute-looking paths and secret-like env
    values from text that leaves the server (errors only; never evidence)."""

    def __init__(self, root: Path, env: dict | None = None):
        env = os.environ if env is None else env
        locs = {str(root)}
        try:
            locs.add(str(Path(root).resolve()))
        except OSError:
            pass
        self._locs = sorted((x for x in locs if len(x) > 1), key=len, reverse=True)
        self._secrets = sorted({v for k, v in env.items() if _SECRET_ENV.search(k)
                                and isinstance(v, str) and len(v) >= 8}, key=len, reverse=True)

    def text(self, value: Any) -> str:
        out = str(value)
        for loc in self._locs:
            out = out.replace(loc + os.sep, "").replace(loc, ".")
        for secret in self._secrets:
            out = out.replace(secret, "[redacted]")
        return _ABS_PATH.sub("[path]", out)

    def deep(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, dict):
            return {k: self.deep(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.deep(v) for v in obj]
        return obj


def _strip_private(obj: Any) -> Any:
    """Drop server-only keys from metadata. Values are never rewritten, so a
    chunk text that legitimately mentions a path stays byte-exact."""
    if isinstance(obj, dict):
        return {k: _strip_private(v) for k, v in obj.items() if k not in _PRIVATE_KEYS}
    if isinstance(obj, (list, tuple)):
        return [_strip_private(v) for v in obj]
    return obj


def _err(code: str, message: str, persisted: bool | None = None, **extra: Any) -> dict:
    out = {"ok": False, "error": {"code": code, "message": message, **extra}}
    if persisted is not None:           # write tool only: explicit "nothing stored"
        out["persisted"] = persisted
    return out


def _is_int(v: Any, lo: int, hi: int | None = None) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= lo and (hi is None or v <= hi)


# -------------------------------------------------------------- service access
# Single place that names the real WP-A/B/C components (AGENT-0 adjusts this
# table if a landed module uses other names). `_call_factory` passes only the
# keyword arguments a factory declares, from: root, config, store, index,
# legacy_backend, legacy_search, source_reader, create.
REAL_FACTORIES: dict[str, tuple[str, tuple[str, ...]]] = {
    "store": ("store", ("open_store", "ProjectionStoreSQLite", "SqliteProjectionStore", "SQLiteProjectionStore")),
    "annotation": ("annotation", ("build_annotation_service", "AnnotationService")),
    "retrieval": ("retrieval", ("build_retrieval_engine", "RetrievalEngine")),
    "index": ("indexing", ("build_index_builder", "IndexBuilder")),
    "source_reader": ("source", ("open_source_reader", "SourceReader", "FileSourceReader")),
}


def _call_factory(fn: Callable, available: dict) -> Any:
    kwargs = {}
    for name, p in inspect.signature(fn).parameters.items():
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        if name in available:
            kwargs[name] = available[name]
        elif p.default is p.empty:
            raise TypeError(f"factory needs unsupported parameter {name!r}")
    return fn(**kwargs)


def _default_importer(name: str):
    pkg = __package__ or "brain_enrichment"
    return importlib.import_module(f"{pkg}.{name}")


class Services:
    """Lazy, memoised access to the injected-or-real services. Failures are not
    cached (a store created later, or a module that lands later, is picked up)
    and never raise anything but EnrichmentError."""

    def __init__(self, root: Path, config: EnrichmentConfig, *, store=None, annotation=None,
                 retrieval=None, index=None, legacy_backend=None, source_reader=None,
                 importer: Callable[[str], Any] | None = None):
        self.root = Path(root)
        self.config = config
        self._importer = importer or _default_importer
        self._lock = threading.RLock()
        self._svc = {"store": store, "annotation": annotation, "retrieval": retrieval,
                     "index": index, "legacy": legacy_backend, "source_reader": source_reader}
        self._injected = {k for k, v in self._svc.items() if v is not None}
        self._reader_missing = False

    # -- generic builder
    def _build(self, kind: str, **available: Any) -> Any:
        module_name, candidates = REAL_FACTORIES[kind]
        try:
            module = self._importer(module_name)
        except ImportError:
            raise C.EnrichmentError("E_UNAVAILABLE", f"enrichment {kind} component is not installed") from None
        for cand in candidates:
            obj = getattr(module, cand, None)
            if obj is None:
                continue
            if inspect.isclass(obj) and getattr(obj, "__module__", None) != module.__name__:
                continue                       # a Protocol re-exported from contract, not an implementation
            try:
                return _call_factory(obj, {"root": self.root, "wiki_root": self.root,
                                           "index_path": self.root / C.INDEX_REL,
                                           "config": self.config, **available})
            except C.EnrichmentError:
                raise
            except sqlite3.Error as exc:
                raise C.EnrichmentError("E_UNAVAILABLE", f"enrichment {kind} could not be opened "
                                        f"({type(exc).__name__})") from None
            except Exception as exc:  # noqa: BLE001 - never leak a traceback or path
                raise C.EnrichmentError("E_UNAVAILABLE", f"enrichment {kind} failed to start "
                                        f"({type(exc).__name__})") from None
        raise C.EnrichmentError("E_UNAVAILABLE", f"enrichment {kind} component has no known factory")

    def _store_schema_guard(self) -> None:
        """A newer on-disk schema isolates the feature even if the store module
        forgets to check (contract section 6)."""
        path = self.root / C.STORE_REL
        if not path.is_file():
            return
        try:
            con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=2)
            try:
                version = con.execute("PRAGMA user_version").fetchone()[0]
            finally:
                con.close()
        except sqlite3.Error:
            return                              # the store module reports unreadable files itself
        if version > C.STORE_SCHEMA_VERSION:
            raise C.EnrichmentError("E_SCHEMA_UNSUPPORTED",
                                    f"enrichment store schema {version} is newer than supported "
                                    f"{C.STORE_SCHEMA_VERSION}; the enrichment feature is unavailable "
                                    "(the base tools are unaffected)")

    # -- services
    def store(self, create: bool = False):
        with self._lock:
            if self._svc["store"] is not None:
                return self._svc["store"]
            exists = (self.root / C.STORE_REL).is_file()
            if not exists and not create:
                raise C.EnrichmentError("E_UNAVAILABLE", "the enrichment store has not been created yet "
                                        "(brain_enrichment_pending or the CLI scan creates it)")
            self._store_schema_guard()
            self._svc["store"] = self._build("store", create=create)
            return self._svc["store"]

    def annotation(self):
        with self._lock:
            if self._svc["annotation"] is None:
                self._svc["annotation"] = self._build("annotation", store=self.store(create=False))
            return self._svc["annotation"]

    def index(self, create: bool = False):
        with self._lock:
            if self._svc["index"] is None:
                self._svc["index"] = self._build("index", store=self.store(create=create), create=create)
            return self._svc["index"]

    def legacy(self):
        """Base-surface backend (search/read). None when it cannot be built."""
        with self._lock:
            if self._svc["legacy"] is None:
                try:
                    scripts = str(Path(__file__).resolve().parents[1])
                    if scripts not in sys.path:
                        sys.path.insert(0, scripts)
                    from brain_surface.backend import WikiBackend  # noqa: PLC0415
                    self._svc["legacy"] = WikiBackend(self.root)
                except Exception:  # noqa: BLE001 - no base backend: fallback is simply unavailable
                    return None
            return self._svc["legacy"]

    def source_reader(self):
        """Freshness oracle (re-reads the source from disk). Optional."""
        with self._lock:
            if self._svc["source_reader"] is None and not self._reader_missing:
                try:
                    self._svc["source_reader"] = self._build("source_reader")
                except C.EnrichmentError:
                    self._reader_missing = True      # optional: do not retry the import on every read
                    return None
            return self._svc["source_reader"]

    def retrieval(self):
        with self._lock:
            if self._svc["retrieval"] is None:
                legacy = self.legacy()
                search = (lambda q, mode, scope, n: legacy.search(q, mode, scope, n)) if legacy else None
                self._svc["retrieval"] = self._build(
                    "retrieval", store=self.store(create=False),
                    index=self.index(), legacy_backend=legacy, legacy_search=search)
            return self._svc["retrieval"]

    def is_injected(self, kind: str) -> bool:
        return kind in self._injected


# ------------------------------------------------------------------- helpers
def revision_of(view: C.ArtifactView) -> int | None:
    """Current annotation revision of an ArtifactView (0 = none)."""
    proj = view.projection if isinstance(view.projection, dict) else {}
    rev = proj.get("current_annotation_revision")
    if _is_int(rev, 0):
        return rev
    ann = view.annotation
    if isinstance(ann, dict):
        for key in ("annotation_revision", "revision"):
            if _is_int(ann.get(key), 0):
                return ann[key]
    return 0 if ann is None else None


def annotation_body(annotation: Any) -> dict:
    """The candidate annotation object, whether the store hands back the bare
    object or a revision record wrapping it."""
    if not isinstance(annotation, dict):
        return {}
    inner = annotation.get("annotation")
    return inner if isinstance(inner, dict) else annotation


def _bad_ref(ref: Any) -> dict | None:
    if isinstance(ref, str) and SOURCE_REF_RE.fullmatch(ref):
        scripts = str(Path(__file__).resolve().parents[1])
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        try:
            from brain_surface import refs as R  # noqa: PLC0415
            R.parse(ref)                          # the one public grammar (doc: = 20 hex)
        except ImportError:
            return None
        except ValueError as exc:
            return _err("E_BAD_REF", str(exc))
        return None
    return _err("E_BAD_REF", "source_ref must be a typed ref 'rec:<id>', 'doc:<hash>' or "
                "'cap:<id>' taken from a prior tool result; paths and other kinds are not "
                "accepted here")


# ----------------------------------------------------------------- surface
class ExtensionSurface:
    def __init__(self, root: Path, config: EnrichmentConfig, services: Services | None = None, *,
                 store=None, annotation=None, retrieval=None, index=None, legacy_backend=None,
                 source_reader=None, importer=None):
        self.root = Path(root)
        self.config = config
        self.s = services or Services(root, config, store=store, annotation=annotation,
                                      retrieval=retrieval, index=index, legacy_backend=legacy_backend,
                                      source_reader=source_reader, importer=importer)
        self._scrub = _Scrubber(self.root)
        self._schemas = {t["name"]: t["inputSchema"]["properties"] for t in T.EXTENSION_TOOLS}

    # ------------------------------------------------------------ dispatch
    def tools(self) -> list[dict]:
        return T.EXTENSION_TOOLS

    def call(self, name: str, arguments: dict | None) -> dict:
        wp = False if name in WRITE_TOOLS else None
        try:
            return self._dispatch(name, arguments, wp)
        except C.EnrichmentError as exc:
            return self._fail(exc, wp)
        except Exception as exc:  # noqa: BLE001 - never leak a traceback
            return _err("E_INTERNAL", type(exc).__name__, persisted=wp)

    def _fail(self, exc: C.EnrichmentError, wp: bool | None) -> dict:
        out = exc.to_public()
        if wp is not None:
            out["persisted"] = wp
        return self._scrub.deep(out)

    def _dispatch(self, name: Any, arguments: Any, wp: bool | None) -> dict:
        if name not in T.EXTENSION_TOOL_NAMES:
            return _err("E_UNKNOWN_ARGUMENT", f"unknown tool: {self._scrub.text(str(name)[:80])}",
                        accepted=list(T.EXTENSION_TOOL_NAMES))
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise C.EnrichmentError("E_BAD_ARGUMENTS", "arguments must be an object")
        allowed = set(self._schemas[name])
        extra = sorted(str(k) for k in set(arguments) - allowed)
        if extra:     # includes "path", "actor", "authority_level", "decision", ... by construction
            return _err("E_UNKNOWN_ARGUMENT", f"argument(s) not accepted: {extra}",
                        persisted=wp, accepted=sorted(allowed))
        out = getattr(self, name)(**arguments)
        out = _strip_private(out)
        if out.get("ok") and "authority_note" not in out:
            out["authority_note"] = AUTHORITY_NOTE
        return out

    # ----------------------------------------------- brain_enrichment_pending
    def brain_enrichment_pending(self, limit=None, cursor=None, retry_failed=False) -> dict:
        limit = self.config.batch_size if limit is None else limit
        if not _is_int(limit, 1, 5):
            raise C.EnrichmentError("E_BAD_ARGUMENTS", "limit must be an integer between 1 and 5")
        cursor = self._cursor(cursor)
        if not isinstance(retry_failed, bool):
            raise C.EnrichmentError("E_BAD_ARGUMENTS", "retry_failed must be a boolean")
        page = self.s.store(create=True).pending(cursor, limit, retry_failed)
        return {"ok": True, "items": list(page.items), "total": page.total,
                "returned": page.returned, "truncated": page.truncated,
                "next_cursor": page.next_cursor, "discovery": dict(page.discovery or {}),
                "authority_note": AUTHORITY_NOTE}

    # ------------------------------------------------ brain_enrichment_submit
    def brain_enrichment_submit(self, source_ref=None, source_version=None,
                                expected_annotation_revision=None, idempotency_key=None,
                                annotation=None) -> dict:
        if not self.config.writes_enabled:
            raise C.EnrichmentError("E_DISABLED", "brain_enrichment_submit is disabled on this "
                                    "server (it needs BRAIN_ENRICHMENT=on and BRAIN_ENRICHMENT_WRITES=1)")
        missing = [k for k, v in (("source_ref", source_ref), ("source_version", source_version),
                                  ("expected_annotation_revision", expected_annotation_revision),
                                  ("idempotency_key", idempotency_key), ("annotation", annotation))
                   if v is None]
        if missing:
            raise C.EnrichmentError("E_BAD_ARGUMENTS", f"missing required argument(s): {missing}")
        bad = _bad_ref(source_ref)
        if bad:
            return {**bad, "persisted": False}
        if not (isinstance(source_version, str) and C.SHA256_RE.fullmatch(source_version)):
            raise C.EnrichmentError("E_BAD_ARGUMENTS", "source_version must be the 64-hex value read from "
                                    "brain_enrichment_pending / brain_artifact_read")
        if not _is_int(expected_annotation_revision, 0):
            raise C.EnrichmentError("E_BAD_ARGUMENTS", "expected_annotation_revision must be an integer >= 0")
        if not (isinstance(idempotency_key, str) and C.IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key)):
            raise C.EnrichmentError("E_BAD_ARGUMENTS", "idempotency_key must be 8-128 characters "
                                    "[A-Za-z0-9._:-] starting with a letter or digit")
        if not isinstance(annotation, dict):
            raise C.EnrichmentError("E_BAD_ARGUMENTS", "annotation must be an object")
        try:
            size = len(C.canonical_json(annotation))
        except (TypeError, ValueError):
            raise C.EnrichmentError("E_BAD_ARGUMENTS", "annotation must be plain JSON") from None
        if size > self.config.max_output_bytes:
            raise C.EnrichmentError("E_TOO_LARGE", f"annotation is {size} bytes; the limit is "
                                    f"{self.config.max_output_bytes}")
        receipt = self.s.annotation().submit({
            "source_ref": source_ref, "source_version": source_version,
            "expected_annotation_revision": expected_annotation_revision,
            "idempotency_key": idempotency_key, "annotation": annotation})
        return {"ok": True, "persisted": True, "source_ref": receipt.source_ref,
                "source_version": receipt.source_version,
                "annotation_revision": receipt.annotation_revision,
                "idempotent_replay": receipt.idempotent_replay, "coverage": dict(receipt.coverage),
                "processing_state": receipt.processing_state, "index_dirty": receipt.index_dirty,
                "authority_note": AUTHORITY_NOTE}

    # ----------------------------------------------------- brain_artifact_read
    def brain_artifact_read(self, source_ref=None, chunk_ref=None, cursor=None) -> dict:
        if source_ref is None:
            raise C.EnrichmentError("E_BAD_ARGUMENTS", "source_ref is required")
        bad = _bad_ref(source_ref)
        if bad:
            return bad
        if chunk_ref is not None and not (isinstance(chunk_ref, str) and C.CHUNK_REF_RE.fullmatch(chunk_ref)):
            return _err("E_BAD_REF", "chunk_ref must look like 'chk:' + 32 hex, as returned by "
                        "brain_enrichment_pending or brain_retrieve")
        cursor = self._cursor(cursor)
        if chunk_ref is not None and cursor is not None:
            raise C.EnrichmentError("E_BAD_ARGUMENTS", "cursor pages the chunk list; do not send it with chunk_ref")
        store = self.s.store(create=False)
        view = store.get(source_ref)
        if chunk_ref is not None:
            return self._read_chunk(store, view, source_ref, chunk_ref)
        return self._read_artifact(store, view, source_ref, cursor)

    def _read_artifact(self, store, view: C.ArtifactView, source_ref: str, cursor: str | None) -> dict:
        proj = dict(view.projection)
        version = proj.get("source_version")
        chunks = store.get_chunks(source_ref, version)
        offset = 0
        if cursor is not None:
            cur = C.decode_cursor(cursor, "artifact")
            if cur.get("s") != source_ref:
                raise C.EnrichmentError("E_BAD_CURSOR", "cursor belongs to a different source")
            if cur.get("v") != version:
                raise C.EnrichmentError("E_STALE_CURSOR", "the source changed since this cursor was issued; "
                                        "restart without a cursor")
            offset = cur.get("o")
            if not _is_int(offset, 0, len(chunks)):
                raise C.EnrichmentError("E_BAD_CURSOR", "cursor offset is out of range")
        page = chunks[offset:offset + C.PAGE_MAX_CHUNKS]
        end = offset + len(page)
        next_cursor = (C.encode_cursor("artifact", s=source_ref, v=version, o=end)
                       if end < len(chunks) else None)
        proj["current_annotation_revision"] = revision_of(view)
        out = {"ok": True, "artifact": proj, "annotation": view.annotation,
               "coverage": dict(view.coverage),
               "review": dict(view.review or {"state": "unreviewed", "revision": None}),
               "chunks": {"items": [c.to_public(with_text=False) for c in page],
                          "total": len(chunks), "returned": len(page),
                          "truncated": end < len(chunks), "next_cursor": next_cursor},
               "authority_note": AUTHORITY_NOTE}
        notes = self._freshness_notes(source_ref, version)
        if notes:
            out["notes"] = notes
        return out

    def _freshness_notes(self, source_ref: str, version: str | None) -> list[str]:
        reader = self.s.source_reader()
        if reader is None:
            return []
        try:
            now = reader.load(source_ref).source_version
        except C.EnrichmentError as exc:
            if exc.code == "E_NOT_FOUND":
                return ["the source no longer exists on disk; this projection is historical"]
            return [f"source freshness could not be checked ({exc.code})"]
        except Exception:  # noqa: BLE001
            return ["source freshness could not be checked"]
        if now != version:
            return ["the source changed on disk after this projection was built; chunk refs listed here "
                    "are not current evidence until brain_enrichment_pending refreshes the projection"]
        return []

    def _read_chunk(self, store, view: C.ArtifactView, source_ref: str, chunk_ref: str) -> dict:
        chunk = store.get_chunk(chunk_ref)                      # E_NOT_FOUND | E_STALE_SOURCE
        if chunk.source_ref != source_ref:
            return _err("E_BAD_REF", "chunk_ref does not belong to source_ref")
        proj = view.projection
        if chunk.source_version != proj.get("source_version"):
            raise C.EnrichmentError("E_STALE_SOURCE", "chunk_ref belongs to an older version of this source; "
                                    "re-read the artifact to get current refs")
        text = chunk.text
        reader = self.s.source_reader()
        layer_pub = next((la for la in proj.get("layers", []) if la.get("layer_id") == chunk.layer_id), None)
        if reader is not None:
            live = reader.load(source_ref)                      # E_NOT_FOUND after deletion
            if live.source_version != chunk.source_version:
                raise C.EnrichmentError("E_STALE_SOURCE", "the source changed on disk since this chunk was "
                                        "projected; refresh with brain_enrichment_pending and re-read")
            layer = next((la for la in live.layers if la.layer_id == chunk.layer_id), None)
            if layer is None:
                raise C.EnrichmentError("E_INTERNAL", "chunk layer is missing from the live source")
            text = layer.text[chunk.start_char:chunk.end_char]
            if chunk.text and chunk.text != text:
                raise C.EnrichmentError("E_INTERNAL", "stored chunk text does not match the source (integrity)")
            layer_pub = layer.to_public()
        if layer_pub is None:
            raise C.EnrichmentError("E_INTERNAL", "chunk layer is missing from the projection")
        meta = chunk.to_public(with_text=True)
        meta["text"] = text
        entry = next((e for e in annotation_body(view.annotation).get("chunk_annotations", []) or []
                      if isinstance(e, dict) and e.get("chunk_ref") == chunk_ref), None)
        return {"ok": True, "chunk": meta, "layer": dict(layer_pub),
                "source": {"source_ref": source_ref, "source_version": chunk.source_version,
                           "tier": layer_pub.get("tier"), "authority_level": layer_pub.get("authority_level"),
                           "source_tier": proj.get("source_tier"),
                           "source_authority_level": proj.get("source_authority_level")},
                "annotation_for_chunk": entry, "authority_note": AUTHORITY_NOTE}

    # ------------------------------------------------------- brain_retrieve
    def brain_retrieve(self, query=None, scope="all", limit=C.DEFAULT_LIMIT,
                       budget_tokens=C.DEFAULT_BUDGET_TOKENS, cursor=None) -> dict:
        if not isinstance(query, str) or not query.strip():
            raise C.EnrichmentError("E_BAD_ARGUMENTS", "query must be a non-empty string")
        query = query.strip()
        if len(query) > QUERY_MAX:
            raise C.EnrichmentError("E_BAD_ARGUMENTS", f"query exceeds {QUERY_MAX} characters")
        if scope not in RETRIEVE_SCOPES:
            raise C.EnrichmentError("E_BAD_ARGUMENTS", f"scope must be one of {list(RETRIEVE_SCOPES)}")
        if not _is_int(limit, 1, C.MAX_LIMIT):
            raise C.EnrichmentError("E_BAD_ARGUMENTS", f"limit must be an integer between 1 and {C.MAX_LIMIT}")
        if not _is_int(budget_tokens, 500, C.MAX_BUDGET_TOKENS):
            raise C.EnrichmentError("E_BAD_ARGUMENTS", f"budget_tokens must be an integer between 500 and "
                                    f"{C.MAX_BUDGET_TOKENS}")
        cursor = self._cursor(cursor)
        request = {"query": query, "scope": scope, "limit": limit,
                   "budget_tokens": budget_tokens, "cursor": cursor}
        reason = None
        if not self.config.retrieval:
            reason = "enrichment retrieval is disabled (BRAIN_ENRICHMENT_RETRIEVAL=0)"
        else:
            try:
                result = self.s.retrieval().retrieve(request)
            except C.EnrichmentError as exc:
                if exc.code not in FALLBACK_REASON_CODES:
                    raise
                reason = f"enrichment retrieval unavailable ({exc.code})"
            except (sqlite3.Error, OSError) as exc:
                reason = f"enrichment index unreadable ({type(exc).__name__})"
            else:
                result = dict(result)
                result.setdefault("ok", True)
                result.setdefault("query_model_calls", 0)
                return result
        return self._fallback(request, reason)

    def _fallback(self, request: dict, reason: str) -> dict:
        """Explicit fallback to the base lexical search, then exact. Never silent:
        `path` says which ran and `notes` says why."""
        if request["cursor"] is not None:
            raise C.EnrichmentError("E_STALE_CURSOR", "this cursor belongs to the enrichment index path, "
                                    "which is not serving now; restart without a cursor")
        backend = self.s.legacy()
        if backend is None:
            raise C.EnrichmentError("E_UNAVAILABLE", f"{reason}; the base search is also unavailable")
        scope, query, limit = request["scope"], request["query"], request["limit"]
        notes = [reason + "; used the base search instead"]
        raw, path = None, None
        for mode, label in (("lexical", "fallback-lexical"), ("exact", "fallback-exact")):
            try:
                got = backend.search(query, mode, scope, limit)
            except Exception as exc:  # noqa: BLE001 - BackendUnavailable & friends
                notes.append(f"{mode} search unavailable ({type(exc).__name__})")
                continue
            raw, path = got, label
            if any((g or {}).get("results") for g in got.values()):
                break
            notes.append(f"{mode} search returned no hits")
        if raw is None:
            raise C.EnrichmentError("E_UNAVAILABLE", f"{reason}; the base lexical and exact searches failed")
        families = ("canonical", "captures") if scope == "all" else (scope,)
        groups, spent, truncated = {}, 0, False
        for fam in families:
            results = []
            for rank, h in enumerate((raw.get(fam) or {}).get("results", [])[:limit], start=1):
                ref = h.get("ref")
                text = h.get("snippet") or ""
                if not ref:
                    notes.append("a base hit has no public ref and cannot be opened remotely")
                    continue
                if truncated or not C.fits_budget(text, request["budget_tokens"] - spent):
                    truncated = True
                    continue
                spent += C.estimate_tokens(text)
                results.append({
                    "source_ref": ref, "text_ref": ref, "chunk_ref": None, "layer_id": None,
                    "span": None, "text": text, "hit_level": "source", "tier": h.get("tier"),
                    "authority_level": h.get("authority_level"), "annotation_revision": None,
                    "annotation_coverage": None,
                    "inclusion_reason": {"paths": ["fallback"], "via": [], "matched": []},
                    "rank": h.get("rank") if _is_int(h.get("rank"), 0) else rank,
                    "flags": [], "truncated": False, "neighbors": None})
            groups[fam] = {"count": len(results), "results": results}
        notes.append("fallback hits are base-search snippets at source level, not chunk text: open the "
                     "hit with brain_read before relying on it")
        index_file = self.root / C.INDEX_REL
        return {"ok": True, "query": query, "scope": scope, "limit": limit, "path": path,
                "query_model_calls": 0, "uses_candidate_annotations": False,
                "budget": {"budget_tokens": request["budget_tokens"], "estimated_tokens": spent,
                           "truncated": truncated},
                "index": {"generation": None, "state": "unavailable" if index_file.is_file() else "missing",
                          "lag": {"pending_artifacts": 0}, "fresh": False},
                "coverage": {"indexed_artifacts": 0, "total_artifacts": None,
                             "source_level_fallback_hits": sum(g["count"] for g in groups.values()),
                             "stale_dropped": 0},
                "groups": groups, "notes": notes, "truncated": truncated,
                "truncation_reason": "budget_tokens" if truncated else None, "next_cursor": None,
                "authority_note": AUTHORITY_NOTE}

    # ------------------------------------------------------------ utilities
    @staticmethod
    def _cursor(cursor: Any) -> str | None:
        if cursor is None:
            return None
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= CURSOR_MAX:
            raise C.EnrichmentError("E_BAD_CURSOR", "cursor must be the string returned as next_cursor")
        return cursor


# ----------------------------------------------------------------- factories
def build_extension(root, config: EnrichmentConfig | None, *, store=None, annotation=None,
                    retrieval=None, index=None, legacy_backend=None, source_reader=None,
                    importer=None) -> ExtensionSurface | None:
    """The extension for this wiki, or None when BRAIN_ENRICHMENT is not `on`.

    Construction imports nothing and touches no file: the real store /
    annotation / retrieval / indexing modules are imported (and the store is
    opened) on the first call that needs them, only for services that were not
    injected. A failure there answers that call with an error envelope and
    never affects the seven base tools.
    """
    if config is None or not config.extension_enabled:
        return None
    return ExtensionSurface(Path(root), config, store=store, annotation=annotation,
                            retrieval=retrieval, index=index, legacy_backend=legacy_backend,
                            source_reader=source_reader, importer=importer)


def tool_specs(extension: ExtensionSurface) -> list[tuple[str, str, dict, Callable[[dict], dict]]]:
    """Plain (name, description, input_schema, handler) tuples for the four
    tools; AGENT-0 converts them to remote_mcp ToolSpec in the adapter."""
    def bind(tool_name: str) -> Callable[[dict], dict]:
        return lambda args: extension.call(tool_name, args)

    return [(t["name"], t["description"], t["inputSchema"], bind(t["name"])) for t in T.EXTENSION_TOOLS]
