"""AnnotationService (WP-B): deterministic, model-free validation of a CANDIDATE
metadata submission (CONTRACT-E1 section 5), then one `append_annotation` call
on the ProjectionStore.

What this module is: a gate. It proves that every quote and mention is a byte
exact substring of a chunk of THIS source at THIS source_version, that every
ref resolves, and that no server-owned field was supplied. It does NOT prove
that a summary, tag, entity reading or `supports`/`contradicts` link is true:
passing validation means "the quoted words exist there", nothing more. Model
output stays candidate tier (authority level 7).

Order (fixed; the first failing stage decides the error):
  0. writes enabled                                 -> E_DISABLED
  1. size (`config.max_output_bytes`)               -> E_TOO_LARGE
  2. structure + request-level checks (hand-written, no jsonschema at
     runtime; parity with schemas/annotation.schema.json is a test)  -> E_VALIDATION
  3. store lookups: source_version current          -> E_STALE_SOURCE / E_NOT_FOUND
  4. semantic checks (chunk membership, exact spans, link targets) -> E_VALIDATION
  5. build the server `derived` block, `store.append_annotation(request+derived)`
     (store errors E_CONFLICT / E_STALE_SOURCE / E_IDEMPOTENCY_CONFLICT /
     E_NOT_FOUND propagate unchanged).

All reasons of a stage are collected (field + ref + fixed code; never raw
caller text, never a path) and nothing is written when any stage fails.

Offsets recorded in `derived` are zero-based code-point offsets into the
decoded LAYER text (chunk.start_char + offset inside the chunk), so they line up
with `brain_read` content; `occurrences` counts non-overlapping occurrences of
the exact string inside that chunk and the span is the first one.

Constructor injection only: the store, the config, an optional
`resolve_source(ref) -> bool` (default: `brain_surface.refs.resolve` against the
injected `root`; with neither, source_ref link targets fail closed) and a clock.
Nothing here imports a model SDK, touches the corpus or reads the environment.
"""
from __future__ import annotations

import copy
import re
from datetime import datetime, timezone
from typing import Any, Callable

from .config import EnrichmentConfig
from .contract import (CHUNK_REF_RE, ENTITY_ID_RE, ENTITY_KINDS, IDEMPOTENCY_KEY_RE, LIMITS,
                       LINK_RELATIONS, MESSAGE_ROLES, READING_RELATIONS, SHA256_RE,
                       AnnotationReceipt, EnrichmentError, canonical_json, entity_id, normal_form)

MAX_REASONS = 50
_SOURCE_REF_RE = re.compile(r"^(rec|doc|cap):[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9_]{1,40}$")

REQUEST_KEYS = ("source_ref", "source_version", "expected_annotation_revision",
                "idempotency_key", "annotation")
ANNOTATION_KEYS = ("summary", "topics", "tags", "categories", "message_role", "entities", "links",
                   "chunk_annotations", "uncertainty", "producer_reported")
ENTITY_KEYS = ("name", "kind", "aliases", "mention")
MENTION_KEYS = ("chunk_ref", "text")
LINK_KEYS = ("relation", "target", "evidence", "note")
EVIDENCE_KEYS = ("chunk_ref", "quote")
CHUNK_ANN_KEYS = ("chunk_ref", "summary", "topics", "tags")
PRODUCER_KEYS = ("harness", "model", "prompt_version")
TARGET_KEYS = ("source_ref", "entity", "entity_id")
CHUNK_ANN_LIMITS = {"topics": 2}

# Names a caller may never set. They are rejected wherever they appear (request
# top level or any nested object) with a dedicated reason code, ahead of the
# generic "unknown key" reason, so a forged authority claim is visible as such.
SERVER_OWNED_FIELDS = frozenset({
    "path", "paths", "file", "filepath", "file_path", "filename", "root", "directory", "dir", "uri", "url",
    "authority", "authority_level", "source_authority_level", "tier", "source_tier", "level",
    "actor", "owner", "user", "author", "reviewed_by", "reviewer", "identity", "verified",
    "canonical", "canonical_status", "canonical_promotion", "promote", "promotion", "approve",
    "approved", "accepted", "status", "review_state", "review", "reviewed",
    "revision", "annotation_revision", "received_at", "request_digest", "artifact_id",
    "derived", "entity_ids", "coverage", "processing_state",
    "command", "cmd", "shell", "script", "exec", "eval", "run", "args", "argv", "env",
})


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Reasons:
    """Collector of validation reasons. A reason is {field, code[, ref]}: a
    field path, a fixed machine code and optionally a ref. Never caller text."""

    def __init__(self) -> None:
        self.items: list[dict] = []
        self.total = 0

    def add(self, field: str, code: str, ref: str | None = None) -> None:
        self.total += 1
        if len(self.items) < MAX_REASONS:
            item = {"field": field, "code": code}
            if ref is not None:
                item["ref"] = ref
            self.items.append(item)

    def __bool__(self) -> bool:
        return self.total > 0

    def error(self) -> EnrichmentError:
        extra: dict[str, Any] = {"reasons": list(self.items)}
        if self.total > len(self.items):
            extra["reasons_omitted"] = self.total - len(self.items)
        return EnrichmentError("E_VALIDATION", "annotation rejected; nothing was stored", **extra)


# ----------------------------------------------------------- structure checks
def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _text(v: Any, path: str, lo: int, hi: int, r: _Reasons) -> bool:
    """A string of lo..hi code points without NUL. Reasons never echo the value."""
    if not isinstance(v, str):
        r.add(path, "not_a_string")
        return False
    if len(v) < lo:
        r.add(path, "too_short")
        return False
    if len(v) > hi:
        r.add(path, "too_long")
        return False
    if "\x00" in v:
        r.add(path, "nul_character")
        return False
    return True


def _array(v: Any, path: str, max_items: int, r: _Reasons, min_items: int = 0) -> bool:
    if not isinstance(v, list):
        r.add(path, "not_an_array")
        return False
    if len(v) > max_items:
        r.add(path, "too_many_items")
        return False
    if len(v) < min_items:
        r.add(path, "too_few_items")
        return False
    return True


def _object(v: Any, path: str, allowed: tuple[str, ...], r: _Reasons) -> bool:
    if not isinstance(v, dict):
        r.add(path, "not_an_object")
        return False
    ok = True
    for key in v:
        if key in allowed:
            continue
        ok = False
        label = key if isinstance(key, str) and _SAFE_KEY_RE.fullmatch(key) else "<invalid-key>"
        code = "server_owned_field" if isinstance(key, str) and key.strip().lower() in SERVER_OWNED_FIELDS \
            else "unknown_key"
        r.add(f"{path}.{label}" if path else label, code)
    return ok


def _string_list(v: Any, path: str, max_items: int, item_len: int, r: _Reasons) -> None:
    if _array(v, path, max_items, r):
        for i, s in enumerate(v):
            _text(s, f"{path}[{i}]", 1, item_len, r)


def _chunk_ref_field(v: Any, path: str, r: _Reasons) -> bool:
    if not isinstance(v, str) or not CHUNK_REF_RE.fullmatch(v):
        r.add(path, "bad_chunk_ref_format")
        return False
    return True


def _check_structure(ann: Any, r: _Reasons) -> None:
    """Hand-written mirror of schemas/annotation.schema.json (the test-suite
    proves parity). Collects every problem; never raises."""
    if not _object(ann, "annotation", ANNOTATION_KEYS, r) and not isinstance(ann, dict):
        return
    if not ann:
        r.add("annotation", "empty")
    if "summary" in ann:
        _text(ann["summary"], "annotation.summary", 1, LIMITS["summary"], r)
    if "topics" in ann:
        _string_list(ann["topics"], "annotation.topics", LIMITS["topics"], LIMITS["topic_len"], r)
    if "tags" in ann:
        _string_list(ann["tags"], "annotation.tags", LIMITS["tags"], LIMITS["tag_len"], r)
    if "categories" in ann:
        _string_list(ann["categories"], "annotation.categories", LIMITS["categories"], LIMITS["category_len"], r)
    if "message_role" in ann and ann["message_role"] not in MESSAGE_ROLES:
        r.add("annotation.message_role", "bad_enum")
    if "uncertainty" in ann:
        _string_list(ann["uncertainty"], "annotation.uncertainty", LIMITS["uncertainty"],
                     LIMITS["uncertainty_len"], r)

    if "entities" in ann and _array(ann["entities"], "annotation.entities", LIMITS["entities"], r):
        for i, e in enumerate(ann["entities"]):
            p = f"annotation.entities[{i}]"
            if not _object(e, p, ENTITY_KEYS, r) and not isinstance(e, dict):
                continue
            for req in ("name", "kind", "mention"):
                if req not in e:
                    r.add(f"{p}.{req}", "missing")
            if "name" in e:
                _text(e["name"], f"{p}.name", 1, LIMITS["entity_name_len"], r)
            if "kind" in e and e["kind"] not in ENTITY_KINDS:
                r.add(f"{p}.kind", "bad_enum")
            if "aliases" in e and _array(e["aliases"], f"{p}.aliases", LIMITS["aliases"], r):
                for j, a in enumerate(e["aliases"]):
                    _text(a, f"{p}.aliases[{j}]", 1, LIMITS["entity_name_len"], r)
            if "mention" in e:
                m = e["mention"]
                if _object(m, f"{p}.mention", MENTION_KEYS, r) or isinstance(m, dict):
                    for req in MENTION_KEYS:
                        if req not in m:
                            r.add(f"{p}.mention.{req}", "missing")
                    if "chunk_ref" in m:
                        _chunk_ref_field(m["chunk_ref"], f"{p}.mention.chunk_ref", r)
                    if "text" in m:
                        _text(m["text"], f"{p}.mention.text", 1, LIMITS["mention_len"], r)

    if "links" in ann and _array(ann["links"], "annotation.links", LIMITS["links"], r):
        for i, link in enumerate(ann["links"]):
            p = f"annotation.links[{i}]"
            if not _object(link, p, LINK_KEYS, r) and not isinstance(link, dict):
                continue
            for req in ("relation", "target", "evidence"):
                if req not in link:
                    r.add(f"{p}.{req}", "missing")
            if "relation" in link and link["relation"] not in LINK_RELATIONS:
                r.add(f"{p}.relation", "bad_enum")
            if "note" in link:
                _text(link["note"], f"{p}.note", 0, LIMITS["note_len"], r)
            if "target" in link:
                t = link["target"]
                if _object(t, f"{p}.target", TARGET_KEYS, r) or isinstance(t, dict):
                    present = [k for k in TARGET_KEYS if k in t]
                    if len(present) != 1:
                        r.add(f"{p}.target", "exactly_one_target_kind_required")
                    elif present[0] == "source_ref":
                        if not isinstance(t["source_ref"], str) or not _SOURCE_REF_RE.fullmatch(t["source_ref"]):
                            r.add(f"{p}.target.source_ref", "bad_source_ref_format")
                    elif present[0] == "entity":
                        _text(t["entity"], f"{p}.target.entity", 1, LIMITS["entity_name_len"], r)
                    elif not isinstance(t["entity_id"], str) or not ENTITY_ID_RE.fullmatch(t["entity_id"]):
                        r.add(f"{p}.target.entity_id", "bad_entity_id_format")
            if "evidence" in link and _array(link["evidence"], f"{p}.evidence", 3, r, min_items=1):
                for j, ev in enumerate(link["evidence"]):
                    q = f"{p}.evidence[{j}]"
                    if not _object(ev, q, EVIDENCE_KEYS, r) and not isinstance(ev, dict):
                        continue
                    for req in EVIDENCE_KEYS:
                        if req not in ev:
                            r.add(f"{q}.{req}", "missing")
                    if "chunk_ref" in ev:
                        _chunk_ref_field(ev["chunk_ref"], f"{q}.chunk_ref", r)
                    if "quote" in ev:
                        _text(ev["quote"], f"{q}.quote", LIMITS["quote_min"], LIMITS["quote_max"], r)

    if "chunk_annotations" in ann and _array(ann["chunk_annotations"], "annotation.chunk_annotations", 20, r):
        for i, ca in enumerate(ann["chunk_annotations"]):
            p = f"annotation.chunk_annotations[{i}]"
            if not _object(ca, p, CHUNK_ANN_KEYS, r) and not isinstance(ca, dict):
                continue
            if "chunk_ref" not in ca:
                r.add(f"{p}.chunk_ref", "missing")
            else:
                _chunk_ref_field(ca["chunk_ref"], f"{p}.chunk_ref", r)
            if "summary" in ca:
                _text(ca["summary"], f"{p}.summary", 1, LIMITS["chunk_summary"], r)
            if "topics" in ca:
                _string_list(ca["topics"], f"{p}.topics", CHUNK_ANN_LIMITS["topics"], LIMITS["topic_len"], r)
            if "tags" in ca:
                _string_list(ca["tags"], f"{p}.tags", LIMITS["chunk_tags"], LIMITS["tag_len"], r)

    if "producer_reported" in ann:
        pr = ann["producer_reported"]
        p = "annotation.producer_reported"
        if _object(pr, p, PRODUCER_KEYS, r) or isinstance(pr, dict):
            for k in PRODUCER_KEYS:
                if k in pr and _text(pr[k], f"{p}.{k}", 0, LIMITS["producer_field_len"], r):
                    if any(ord(c) < 32 or ord(c) == 127 for c in pr[k]):
                        r.add(f"{p}.{k}", "control_character")


def _check_request_level(req: Any, r: _Reasons) -> None:
    if not _object(req, "request", REQUEST_KEYS, r) and not isinstance(req, dict):
        return
    for k in REQUEST_KEYS:
        if k not in req:
            r.add(f"request.{k}", "missing")
    sref = req.get("source_ref")
    if "source_ref" in req and (not isinstance(sref, str) or not _SOURCE_REF_RE.fullmatch(sref)):
        r.add("request.source_ref", "bad_source_ref_format")
    sver = req.get("source_version")
    if "source_version" in req and (not isinstance(sver, str) or not SHA256_RE.fullmatch(sver)):
        r.add("request.source_version", "bad_source_version_format")
    exp = req.get("expected_annotation_revision")
    if "expected_annotation_revision" in req and (not _is_int(exp) or exp < 0):
        r.add("request.expected_annotation_revision", "not_a_non_negative_integer")
    key = req.get("idempotency_key")
    if "idempotency_key" in req and (not isinstance(key, str) or not IDEMPOTENCY_KEY_RE.fullmatch(key)):
        r.add("request.idempotency_key", "bad_idempotency_key_format")


# ------------------------------------------------------------ semantic checks
def _span(chunk, needle: str) -> dict:
    """First exact occurrence of `needle` in chunk.text, as layer-absolute
    code-point offsets, plus the non-overlapping occurrence count."""
    local = chunk.text.find(needle)
    return {"start_char": chunk.start_char + local, "end_char": chunk.start_char + local + len(needle),
            "occurrences": chunk.text.count(needle)}


class AnnotationService:
    """`AnnotationService(store, *, config, resolve_source=None, now=None, root=None)`.

    store           a ProjectionStore (get_chunks, get_chunk, entity_exists, append_annotation)
    config          EnrichmentConfig (writes_enabled, max_output_bytes)
    resolve_source  ref -> bool, True when the public ref names an existing source.
                    Default: brain_surface.refs.resolve against `root`; with neither
                    a resolver nor a root, source_ref link targets are rejected.
    now             () -> ISO-8601 string, recorded as derived.validated_at
    """

    def __init__(self, store, *, config: EnrichmentConfig,
                 resolve_source: Callable[[str], bool] | None = None,
                 now: Callable[[], str] | None = None, root=None):
        self._store = store
        self._config = config
        self._now = now or _utc_now
        if resolve_source is None and root is not None:
            resolve_source = self._public_resolver(root)
        self._resolve_source = resolve_source

    @staticmethod
    def _public_resolver(root) -> Callable[[str], bool]:
        from pathlib import Path

        from brain_surface import refs as R   # local import: keeps this module importable without the surface

        root = Path(root)

        def resolve(ref: str) -> bool:
            try:
                R.resolve(root, ref)
                return True
            except (R.BadRef, KeyError):
                return False
        return resolve

    # ------------------------------------------------------------------ submit
    def submit(self, request: dict) -> AnnotationReceipt:
        if not self._config.writes_enabled:
            raise EnrichmentError("E_DISABLED", "enrichment writes are disabled (BRAIN_ENRICHMENT=on and "
                                  "BRAIN_ENRICHMENT_WRITES=1 are both required)")
        # 1. size ---------------------------------------------------------
        try:
            size = len(canonical_json(request))
        except (TypeError, ValueError, OverflowError, RecursionError):
            r = _Reasons()
            r.add("request", "not_json_serialisable_utf8")
            raise r.error() from None
        if size > self._config.max_output_bytes:
            raise EnrichmentError("E_TOO_LARGE", "annotation request exceeds the configured output limit",
                                  max_output_bytes=self._config.max_output_bytes)
        # 2. structure + request level -------------------------------------
        r = _Reasons()
        _check_request_level(request, r)
        if isinstance(request, dict) and "annotation" in request:
            _check_structure(request["annotation"], r)
        if r:
            raise r.error()
        source_ref, source_version = request["source_ref"], request["source_version"]
        ann = request["annotation"]
        # 3. the version the caller read must still be current --------------
        chunks = self._store.get_chunks(source_ref, source_version)     # E_STALE_SOURCE / E_NOT_FOUND
        by_ref = {c.chunk_ref: c for c in chunks
                  if c.source_ref == source_ref and c.source_version == source_version}
        # 4. semantic ------------------------------------------------------
        derived = self._semantic(source_ref, source_version, ann, by_ref, r)
        if r:
            raise r.error()
        # 5. store ---------------------------------------------------------
        derived["validated_at"] = self._now()
        return self._store.append_annotation({**copy.deepcopy(request), "derived": derived})

    # ---------------------------------------------------------------- semantic
    def _chunk(self, ref: str, field: str, source_ref: str, source_version: str, by_ref: dict,
               cache: dict, r: _Reasons):
        """Chunk of THIS source/version or None (reason recorded). A chunk of a
        superseded version raises E_STALE_SOURCE: the caller must re-read."""
        if ref in by_ref:
            return by_ref[ref]
        if ref not in cache:
            try:
                other = self._store.get_chunk(ref)
            except EnrichmentError as e:
                if e.code != "E_NOT_FOUND":      # E_STALE_SOURCE and anything else propagate
                    raise
                other = None
            cache[ref] = other
        other = cache[ref]
        if other is None:
            r.add(field, "unknown_chunk_ref", ref)
        elif other.source_ref != source_ref:
            r.add(field, "chunk_of_another_source", ref)
        elif other.source_version != source_version:
            raise EnrichmentError("E_STALE_SOURCE", "chunk_ref belongs to a superseded source_version",
                                  ref=ref)
        else:       # same source and version yet absent from the listing: the store disagrees with itself
            r.add(field, "unknown_chunk_ref", ref)
        return None

    def _semantic(self, source_ref: str, source_version: str, ann: dict, by_ref: dict, r: _Reasons) -> dict:
        cache: dict = {}
        look = lambda ref, field: self._chunk(ref, field, source_ref, source_version, by_ref, cache, r)  # noqa: E731

        def exact(chunk, needle: str, field: str, ref: str):
            """Exact substring check; returns span dict or None."""
            if not needle.strip():
                r.add(field, "blank_text", ref)
                return None
            if needle not in chunk.text:
                r.add(field, "not_an_exact_substring_of_chunk", ref)
                return None
            return _span(chunk, needle)

        d_entities: list[dict] = []
        normals: dict[str, str] = {}              # normal form -> entity_id (declared in this annotation)
        for i, e in enumerate(ann.get("entities", [])):
            p = f"annotation.entities[{i}]"
            normal = normal_form(e["name"])
            if not normal:
                r.add(f"{p}.name", "empty_normal_form")
                continue
            if normal in normals:
                r.add(f"{p}.name", "duplicate_entity_in_annotation")
                continue
            eid = entity_id(source_ref, normal)
            normals[normal] = eid
            m = e["mention"]
            chunk = look(m["chunk_ref"], f"{p}.mention.chunk_ref")
            span = exact(chunk, m["text"], f"{p}.mention.text", m["chunk_ref"]) if chunk else None
            if span:
                d_entities.append({"entity_id": eid, "name": e["name"], "normal": normal, "kind": e["kind"],
                                   "aliases": list(e.get("aliases", [])),
                                   "mention": {"chunk_ref": m["chunk_ref"], **span}})

        seen_chunks: set[str] = set()
        for i, ca in enumerate(ann.get("chunk_annotations", [])):
            p = f"annotation.chunk_annotations[{i}]"
            look(ca["chunk_ref"], f"{p}.chunk_ref")
            if ca["chunk_ref"] in seen_chunks:
                r.add(f"{p}.chunk_ref", "duplicate_chunk_annotation", ca["chunk_ref"])
            seen_chunks.add(ca["chunk_ref"])

        d_links: list[dict] = []
        for i, link in enumerate(ann.get("links", [])):
            p = f"annotation.links[{i}]"
            relation, target = link["relation"], link["target"]
            dst = None
            if "source_ref" in target:
                tref = target["source_ref"]
                if tref == source_ref:
                    r.add(f"{p}.target.source_ref", "self_link", tref)
                elif not self._resolves(tref):
                    r.add(f"{p}.target.source_ref", "unresolvable_source_ref", tref)
                else:
                    dst = tref
            elif "entity" in target:
                dst = normals.get(normal_form(target["entity"]))
                if dst is None:
                    r.add(f"{p}.target.entity", "entity_not_declared_in_this_annotation")
            else:
                eid = target["entity_id"]
                if not self._store.entity_exists(eid):
                    r.add(f"{p}.target.entity_id", "unknown_entity_id", eid)
                else:
                    dst = eid
            d_evidence = []
            for j, ev in enumerate(link["evidence"]):
                q = f"{p}.evidence[{j}]"
                chunk = look(ev["chunk_ref"], f"{q}.chunk_ref")
                span = exact(chunk, ev["quote"], f"{q}.quote", ev["chunk_ref"]) if chunk else None
                if span:
                    d_evidence.append({"chunk_ref": ev["chunk_ref"], **span})
            if dst is not None and len(d_evidence) == len(link["evidence"]):
                d_links.append({"relation": relation,
                                "edge_kind": "reading" if relation in READING_RELATIONS else "semantic",
                                "src": source_ref, "dst": dst, "evidence": d_evidence})
        return {"entities": d_entities, "links": d_links}

    def _resolves(self, ref: str) -> bool:
        if self._resolve_source is None:
            return False                       # fail closed: cannot verify a target
        try:
            return bool(self._resolve_source(ref))
        except Exception:                      # noqa: BLE001 - a resolver fault is "not verified"
            return False
