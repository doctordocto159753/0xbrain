#!/usr/bin/env python3
"""Local enrichment CLI (CONTRACT-E1 sections 6, 7, 11).

    python -m brain_enrichment.cli [--root DIR] [--json] COMMAND ...     (cwd or PYTHONPATH = scripts/)
    python scripts/brain_enrichment/cli.py [--root DIR] [--json] COMMAND ...

    inspect SOURCE_REF [--chunk CHK] [--cursor C]   read-only artifact / exact chunk view
    scan [--max-rounds N] [--time-budget S] [--retry-failed]
                                                    bounded, resumable projection pass (sidecar only)
    index refresh|rebuild [--batch-limit N] [--max-batches N]
                                                    derived search index (sidecar only)
    pending [--limit N] [--after REF] [--failed]    read-only list of sources awaiting annotation
    status                                          enrichment state NEXT TO the legacy capture workflow
    verify [--no-freshness]                         read-only integrity + freshness report
    review SOURCE_REF --revision N --decision confirmed|rejected --note TEXT --actor NAME [--yes]
                                                    human decision on a candidate annotation (never promotes)
    export [--out FILE] [--with-text]               JSONL of the sidecar (read-only)

Everything except scan / index / review is read-only; scan and index write only
under `_enrichment/` and `_search/enrichment/`; review writes one store row and
one audit line. Nothing here writes the corpus or calls `brain_review promote`;
there is no approve/promote command by design.

Exit codes: 0 ok; 1 completed with findings (verify errors, scan/index not
finished); 2 usage / bad arguments; 3 refused or unavailable (E_DISABLED,
E_REVIEW_FORBIDDEN, E_UNAVAILABLE, E_SCHEMA_UNSUPPORTED); 4 conflict or stale
(E_CONFLICT, E_STALE_SOURCE, E_STALE_CURSOR, E_IDEMPOTENCY_CONFLICT);
5 not found / bad ref; 70 internal error.
"""
from __future__ import annotations

import argparse
import dataclasses
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

if __package__ in (None, ""):          # executed as a script: make `brain_enrichment` importable
    _HERE = Path(__file__).resolve().parent     # its own files (store.py, source.py ...) must not shadow top-level names
    sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != _HERE]
    sys.path.insert(0, str(_HERE.parent))

from brain_enrichment import contract as C  # noqa: E402
from brain_enrichment import review as RV  # noqa: E402
from brain_enrichment.config import EnrichmentConfig  # noqa: E402
from brain_enrichment.surface import (ExtensionSurface, Services, _Scrubber,  # noqa: E402
                                      annotation_body, revision_of)

DEFAULT_ROOT = Path(__file__).resolve().parents[2]
EXPORT_SCHEMA = "enrichment-export-1"
ANNOTATABLE_KINDS = ("capture-text", "capture-media", "ingested-file")
EXIT = {"E_BAD_ARGUMENTS": 2, "E_UNKNOWN_ARGUMENT": 2, "E_BAD_CURSOR": 2, "E_VALIDATION": 2,
        "E_TOO_LARGE": 2, "E_DISABLED": 3, "E_REVIEW_FORBIDDEN": 3, "E_UNAVAILABLE": 3,
        "E_SCHEMA_UNSUPPORTED": 3, "E_CONFLICT": 4, "E_STALE_SOURCE": 4, "E_STALE_CURSOR": 4,
        "E_IDEMPOTENCY_CONFLICT": 4, "E_NOT_FOUND": 5, "E_BAD_REF": 5, "E_INTERNAL": 70}
LEGACY_NOTE = ("These are the legacy capture-workflow statuses (received / needs-review / reviewed ...): "
               "the human intake queue of the original archive. They are NOT the enrichment backlog; "
               "see `enrichment` for that.")


class Ctx:
    def __init__(self, root: Path, config: EnrichmentConfig, services: Services, args, env, stdin, stdout):
        self.root, self.config, self.services, self.args = root, config, services, args
        self.env, self.stdin, self.stdout = env, stdin, stdout

    @property
    def surface(self) -> ExtensionSurface:
        return ExtensionSurface(self.root, self.config, self.services)


# ------------------------------------------------------------------ helpers
def _fail(code: str, message: str, **extra) -> C.EnrichmentError:
    return C.EnrichmentError(code, message, **extra)


def _unwrap(result: dict) -> dict:
    if result.get("ok"):
        return result
    err = result.get("error") or {}
    extra = {k: v for k, v in err.items() if k not in ("code", "message")}
    raise C.EnrichmentError(err.get("code", "E_INTERNAL"), err.get("message", "failed"), **extra)


def iter_current(store, page: int = 100):
    after = None
    while True:
        rows = store.list_current(after, page)
        if not rows:
            return
        yield from rows
        after = rows[-1]["projection"].source_ref
        if len(rows) < page:
            return


def _as_dict(obj) -> dict:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    return dict(vars(obj)) if hasattr(obj, "__dict__") else {"value": obj}


def _sizes(root: Path, rel: str) -> int:
    total = 0
    for suffix in ("", "-wal", "-shm", "-journal"):
        p = root / (rel + suffix)
        try:
            total += p.stat().st_size if p.is_file() else 0
        except OSError:
            pass
    return total


def store_schema_version(root: Path) -> int | None:
    path = root / C.STORE_REL
    if not path.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=2)
        try:
            return con.execute("PRAGMA user_version").fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error:
        return None


_FM_STATUS = re.compile(r"^status:\s*(.*?)\s*$", re.M)


def legacy_capture_status(root: Path) -> dict:
    """Counts of the capture workflow `status:` front-matter field. Reads only the
    head of each capture record; never writes."""
    counts: dict[str, int] = {}
    base = root / "01-inbox" / "captures"
    total = 0
    if base.is_dir():
        for p in sorted(base.rglob("*.md")):
            if ".quarantine" in p.parts or not p.is_file():
                continue
            try:
                with open(p, "r", encoding="utf-8", errors="replace", newline="") as fh:
                    head = fh.read(4000)
            except OSError:
                continue
            if not head.startswith("---"):
                continue
            block = head.split("\n---", 1)[0]
            m = _FM_STATUS.search(block)
            status = (m.group(1).strip("'\"") if m else "") or "unknown"
            counts[status] = counts.get(status, 0) + 1
            total += 1
    return {"total": total, "by_status": dict(sorted(counts.items())), "note": LEGACY_NOTE}


def _require_sidecar(ctx: Ctx, what: str) -> None:
    if not ctx.config.sidecar_enabled:
        raise _fail("E_DISABLED", f"{what} builds the enrichment sidecar: set BRAIN_ENRICHMENT=shadow or on")


# ----------------------------------------------------------------- commands
def cmd_inspect(ctx: Ctx) -> dict:
    a = {"source_ref": ctx.args.source_ref}
    if ctx.args.chunk:
        a["chunk_ref"] = ctx.args.chunk
    if ctx.args.cursor:
        a["cursor"] = ctx.args.cursor
    return _unwrap(ctx.surface.call("brain_artifact_read", a))


def cmd_pending(ctx: Ctx) -> dict:
    store = ctx.services.store(create=False)
    limit = ctx.args.limit
    items, after = [], ctx.args.after
    wanted = ("failed",) if ctx.args.failed else ("discovered", "projected", "stale")
    for row in iter_current(store):
        proj = row["projection"]
        if after and proj.source_ref <= after:
            continue
        if row["processing_state"] not in wanted:
            continue
        if not ctx.args.failed and (proj.source_kind not in ANNOTATABLE_KINDS or not proj.layers):
            continue
        view = store.get(proj.source_ref)
        items.append({"source_ref": proj.source_ref, "source_version": proj.source_version,
                      "source_kind": proj.source_kind, "processing_state": row["processing_state"],
                      "chunks": len(row["chunks"]), "coverage": dict(view.coverage),
                      "current_annotation_revision": revision_of(view),
                      "quality_flags": list(proj.quality_flags)})
        if len(items) > limit:
            break
    more = len(items) > limit
    items = items[:limit]
    return {"ok": True, "command": "pending", "items": items, "returned": len(items), "more": more,
            "next_after": items[-1]["source_ref"] if more and items else None,
            "note": "read-only listing of the sidecar; it does not refresh discovery (use scan) and is not "
                    "the MCP work queue"}


def cmd_scan(ctx: Ctx) -> dict:
    _require_sidecar(ctx, "scan")
    store = ctx.services.store(create=True)
    start, first, last, first_pass, stopped, rounds = time.monotonic(), None, None, None, "max_rounds", 0
    for rounds in range(1, ctx.args.max_rounds + 1):
        page = store.pending(None, 1, ctx.args.retry_failed)
        d = dict(page.discovery or {})
        first = first if first is not None else d
        last = d
        first_pass = d.get("pass") if first_pass is None else first_pass
        if d.get("pass") is not None and first_pass is not None and d.get("pass") != first_pass:
            stopped = "pass_complete"
            break
        if not any(int(d.get(k) or 0) for k in ("scanned", "projected", "failed", "no_text")):
            stopped = "idle"
            break
        if time.monotonic() - start >= ctx.args.time_budget:
            stopped = "time_budget"
            break
    complete = stopped in ("pass_complete", "idle")
    return {"ok": True, "command": "scan", "rounds": rounds, "stopped_because": stopped,
            "complete": complete, "first_discovery": first, "discovery": last,
            "resume": None if complete else "run scan again: the inventory cursor persists in the store",
            "note": "scan writes only the enrichment sidecar; no corpus file is touched"}


def cmd_index(ctx: Ctx) -> dict:
    _require_sidecar(ctx, "index")
    idx = ctx.services.index(create=True)
    receipts, finished = [], False
    if ctx.args.action == "rebuild":
        if not hasattr(idx, "rebuild"):
            raise _fail("E_UNAVAILABLE", "this index builder does not support a full rebuild")
        receipts.append(_as_dict(idx.rebuild()))
        finished = True
    else:
        for _ in range(ctx.args.max_batches):
            r = idx.refresh(ctx.args.batch_limit)
            receipts.append(_as_dict(r))
            if int(getattr(r, "artifacts_pending", 0) or 0) == 0:
                finished = True
                break
    return {"ok": True, "command": "index", "action": ctx.args.action, "batches": len(receipts),
            "finished": finished, "last": receipts[-1] if receipts else None,
            "note": "the index is derived and rebuildable; the store is not touched"}


def cmd_status(ctx: Ctx) -> dict:
    cfg, root = ctx.config, ctx.root
    degraded: list[str] = []
    schema = store_schema_version(root)
    store_info = {"state": "missing", "schema_version": schema, "supported_schema_version": C.STORE_SCHEMA_VERSION,
                  "bytes": _sizes(root, C.STORE_REL)}
    states = {s: 0 for s in C.PROCESSING_STATES}
    reviews = {s: 0 for s in C.REVIEW_STATES}
    sources = {"total": 0, "by_kind": {}}
    cov = {"annotated_chunks": 0, "total_chunks": 0}
    awaiting = skipped = 0
    store = None
    if schema is not None and schema > C.STORE_SCHEMA_VERSION:
        store_info["state"] = "unsupported"
        degraded.append("enrichment store schema is newer than this code supports")
    elif schema is not None or ctx.services.is_injected("store"):
        try:
            store = ctx.services.store(create=False)
            store_info["state"] = "ok"
        except C.EnrichmentError as exc:
            store_info.update({"state": "unavailable", "reason": exc.code})
            degraded.append(f"enrichment store unavailable ({exc.code})")
    if store is not None:
        for row in iter_current(store):
            proj = row["projection"]
            states[row["processing_state"]] = states.get(row["processing_state"], 0) + 1
            sources["total"] += 1
            sources["by_kind"][proj.source_kind] = sources["by_kind"].get(proj.source_kind, 0) + 1
            n_chunks = len(row["chunks"])
            cov["total_chunks"] += n_chunks
            if row["annotation_revision"]:
                view = store.get(proj.source_ref)
                cov["annotated_chunks"] += int(view.coverage.get("annotated_chunks", 0))
                reviews[row.get("review_state", "unreviewed")] = reviews.get(row.get("review_state", "unreviewed"), 0) + 1
            if row["processing_state"] in ("discovered", "projected", "stale"):
                if proj.source_kind in ANNOTATABLE_KINDS and proj.layers:
                    awaiting += 1
                else:
                    skipped += 1
        try:
            store_info["change_marker"] = store.change_marker()
        except Exception:  # noqa: BLE001
            store_info["change_marker"] = None
        if hasattr(store, "status"):
            try:
                reported = store.status()
                if isinstance(reported, dict):
                    store_info["reported"] = reported
            except Exception:  # noqa: BLE001
                pass
    index = {"state": "present" if (root / C.INDEX_REL).is_file() else "missing",
             "bytes": _sizes(root, C.INDEX_REL), "generation": None, "lag": None}
    idx = ctx.services._svc.get("index")  # noqa: SLF001 - only an already-built/injected index is consulted
    if idx is None and (root / C.INDEX_REL).is_file():
        try:
            idx = ctx.services.index(create=False)
        except C.EnrichmentError:
            idx = None
    if idx is not None and hasattr(idx, "status"):
        try:
            index.update({k: v for k, v in dict(idx.status()).items() if k in ("generation", "state", "lag", "fresh")})
        except Exception:  # noqa: BLE001
            index["state"] = "unreadable"
    if index["state"] in ("missing", "unreadable") and cfg.extension_enabled:
        degraded.append(f"enrichment index {index['state']} (retrieval falls back to the base search)")
    return {
        "ok": True, "command": "status", "root": ".",
        "enrichment": {
            "mode": cfg.mode, "writes_enabled": cfg.writes_enabled, "retrieval_enabled": cfg.retrieval,
            "config_warnings": list(cfg.warnings), "store": store_info, "sources": sources,
            "processing_state": states, "review_state_of_current_annotations": reviews,
            "backlog": {"awaiting_annotation": awaiting, "failed": states.get("failed", 0),
                        "no_text": states.get("no_text", 0), "projected_but_not_annotatable": skipped},
            "chunk_coverage": cov, "index": index,
            "sidecar_bytes": store_info["bytes"] + index["bytes"]},
        "legacy_capture_workflow": legacy_capture_status(root),
        "degraded": bool(degraded), "degraded_reasons": degraded}


def cmd_verify(ctx: Ctx) -> dict:
    store = ctx.services.store(create=False)
    reader = ctx.services.source_reader() if ctx.args.freshness else None
    instance = C.instance_id(ctx.root)
    errors, warnings, stale, missing = [], [], [], []
    n_sources = n_chunks = n_ann = 0

    def err(code, ref, detail):
        errors.append({"code": code, "source_ref": ref, "detail": detail})

    for row in iter_current(store):
        proj, chunks = row["projection"], row["chunks"]
        ref = proj.source_ref
        n_sources += 1
        n_chunks += len(chunks)
        if C.artifact_id(instance, ref) != proj.artifact_id:
            err("artifact_id", ref, "artifact_id does not match the instance and source_ref")
        if C.source_version(ref, [la.version_dict() for la in proj.layers]) != proj.source_version:
            err("source_version", ref, "source_version does not match the stored layers")
        by_layer: dict[str, list] = {}
        for ch in sorted(chunks, key=lambda c: c.index):
            by_layer.setdefault(ch.layer_id, []).append(ch)
        known = {ch.chunk_ref for ch in chunks}
        for layer in proj.layers:
            lc = by_layer.get(layer.layer_id, [])
            pos = 0
            for i, ch in enumerate(lc):
                if ch.start_char != pos:
                    err("chunk_partition", ref, f"layer {layer.layer_id}: chunk {ch.chunk_ref} starts at "
                                                f"{ch.start_char}, expected {pos}")
                pos = ch.end_char
                dg = C.chunk_digest(ref, proj.source_version, layer.layer_id, ch.start_char, ch.end_char)
                if ch.chunk_digest != dg or ch.chunk_ref != C.chunk_ref(dg):
                    err("chunk_ref", ref, f"chunk {ch.chunk_ref} does not match its digest")
                if ch.text and len(ch.text) != ch.chars:
                    err("chunk_text", ref, f"chunk {ch.chunk_ref} text length differs from its offsets")
                want_prev = lc[i - 1].chunk_ref if i else None
                want_next = lc[i + 1].chunk_ref if i + 1 < len(lc) else None
                if ch.prev_chunk_ref not in (want_prev, None) or ch.next_chunk_ref not in (want_next, None):
                    err("chunk_links", ref, f"chunk {ch.chunk_ref} neighbours are inconsistent")
                for link in (ch.prev_chunk_ref, ch.next_chunk_ref):
                    if link and link not in known:
                        err("chunk_links", ref, f"chunk {ch.chunk_ref} links to an unknown chunk")
            if pos != layer.chars:
                err("chunk_partition", ref, f"layer {layer.layer_id}: chunks end at {pos}, layer has "
                                            f"{layer.chars} characters")
        body = annotation_body(row.get("annotation"))
        if body:
            n_ann += 1
            refs = [e.get("chunk_ref") for e in body.get("chunk_annotations", []) or []]
            refs += [(e.get("mention") or {}).get("chunk_ref") for e in body.get("entities", []) or []]
            refs += [ev.get("chunk_ref") for lk in body.get("links", []) or [] for ev in lk.get("evidence", []) or []]
            for r in refs:
                if r and r not in known:
                    err("annotation_chunk_ref", ref, f"annotation cites {r}, which is not a chunk of the "
                                                    "current version")
        if reader is not None:
            try:
                live = reader.load(ref).source_version
            except C.EnrichmentError as exc:
                (missing if exc.code == "E_NOT_FOUND" else warnings).append(
                    ref if exc.code == "E_NOT_FOUND" else f"{ref}: could not re-read ({exc.code})")
                continue
            if live != proj.source_version:
                stale.append(ref)
    if reader is None and ctx.args.freshness:
        warnings.append("source freshness not checked: no source reader available")
    store_verify = None
    if hasattr(store, "verify"):
        try:
            res = store.verify()
            store_verify = res if isinstance(res, dict) else {"ok": bool(res) if res is not None else True}
            if store_verify.get("ok") is False:
                err("store_verify", None, str(store_verify.get("errors") or store_verify.get("detail") or "store reported a problem")[:300])
        except C.EnrichmentError as exc:
            err("store_verify", None, f"{exc.code}: {exc.message}")
        except Exception as exc:  # noqa: BLE001
            err("store_verify", None, type(exc).__name__)
    return {"ok": not errors, "command": "verify",
            "checked": {"sources": n_sources, "chunks": n_chunks, "annotated_sources": n_ann},
            "errors": errors, "warnings": warnings, "stale_sources": stale, "missing_sources": missing,
            "store_verify": store_verify,
            "note": "stale/missing sources are expected after a source edit or deletion and are refreshed by "
                    "scan; they are not corruption"}


def cmd_review(ctx: Ctx) -> dict:
    a = ctx.args
    human = RV.resolve_human_context(a.actor, env=ctx.env, stdin=ctx.stdin, stdout=ctx.stdout, yes=a.yes)
    store = ctx.services.store(create=False)
    confirm = None
    if not a.yes:
        def confirm(prompt: str) -> str:
            ctx.stdout.write(prompt)
            ctx.stdout.flush()
            return ctx.stdin.readline()
    out = RV.record_decision(store, ctx.root, source_ref=a.source_ref, annotation_revision=a.revision,
                             decision=a.decision, note=a.note, human_context=human,
                             source_version=a.source_version, confirm=confirm)
    return {**out, "command": "review"}


def _export_lines(ctx: Ctx):
    store = ctx.services.store(create=False)
    yield {"type": "header", "schema": EXPORT_SCHEMA, "contract": "CONTRACT-E1",
           "store_schema_version": store_schema_version(ctx.root), "with_text": ctx.args.with_text,
           "scope": "current-state"}
    for row in iter_current(store):
        proj, chunks = row["projection"], row["chunks"]
        pub = proj.to_public()
        pub["current_annotation_revision"] = row["annotation_revision"]
        yield {"type": "artifact", **pub, "processing_state": row["processing_state"],
               "review_state": row.get("review_state", "unreviewed")}
        for ch in chunks:
            yield {"type": "chunk", **ch.to_public(with_text=ctx.args.with_text)}
        if row["annotation_revision"]:
            yield {"type": "annotation", "source_ref": proj.source_ref, "source_version": proj.source_version,
                   "annotation_revision": row["annotation_revision"], "annotation": row["annotation"]}


def cmd_export(ctx: Ctx) -> dict:
    dest = ctx.args.out
    if dest in (None, "-"):
        # spool first: a failure half-way must produce a clean error, never a truncated export
        with tempfile.SpooledTemporaryFile(max_size=8 << 20, mode="w+", encoding="utf-8", newline="\n") as spool:
            for line in _export_lines(ctx):
                spool.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n")
            spool.seek(0)
            for chunk in iter(lambda: spool.read(1 << 16), ""):
                ctx.stdout.write(chunk)
        return {"ok": True, "command": "export", "streamed": True}
    path = Path(dest).expanduser().resolve()
    try:
        rel = path.relative_to(ctx.root.resolve())
    except ValueError:
        rel = None
    if rel is not None and rel.parts[:2] != (C.ENRICHMENT_DIR, "exports"):
        raise _fail("E_BAD_ARGUMENTS", f"--out inside the wiki must be under {C.ENRICHMENT_DIR}/exports/")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    n = 0
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            for line in _export_lines(ctx):
                fh.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n")
                n += 1
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    return {"ok": True, "command": "export", "lines": n, "out": path.name}


# ------------------------------------------------------------------- output
def render_text(res: dict) -> str:
    cmd = res.get("command")
    if cmd == "status":
        e, lg = res["enrichment"], res["legacy_capture_workflow"]
        st = e["store"]
        lines = [f"ENRICHMENT (new sidecar)  mode={e['mode']} writes={e['writes_enabled']} "
                 f"retrieval={e['retrieval_enabled']}",
                 f"  store: {st['state']}  schema={st['schema_version']}/{st['supported_schema_version']}  "
                 f"bytes={st['bytes']}",
                 f"  sources: {e['sources']['total']}  by processing_state: {e['processing_state']}",
                 f"  backlog: awaiting_annotation={e['backlog']['awaiting_annotation']} "
                 f"failed={e['backlog']['failed']} no_text={e['backlog']['no_text']}",
                 f"  chunk coverage: {e['chunk_coverage']['annotated_chunks']}/{e['chunk_coverage']['total_chunks']}",
                 f"  index: {e['index']['state']} generation={e['index']['generation']} lag={e['index']['lag']} "
                 f"bytes={e['index']['bytes']}",
                 f"  sidecar_bytes: {e['sidecar_bytes']}",
                 f"LEGACY CAPTURE WORKFLOW (human intake queue, not enrichment)  total={lg['total']}",
                 f"  by status: {lg['by_status']}"]
        if res["degraded"]:
            lines.append("DEGRADED: " + "; ".join(res["degraded_reasons"]))
        return "\n".join(lines)
    if cmd == "verify":
        lines = [f"verify: {'OK' if res['ok'] else 'ERRORS'}  sources={res['checked']['sources']} "
                 f"chunks={res['checked']['chunks']} annotated={res['checked']['annotated_sources']}"]
        lines += [f"  ERROR {e['code']} {e['source_ref']}: {e['detail']}" for e in res["errors"]]
        lines += [f"  stale: {r}" for r in res["stale_sources"]] + [f"  missing: {r}" for r in res["missing_sources"]]
        lines += [f"  warning: {w}" for w in res["warnings"]]
        return "\n".join(lines)
    if cmd == "review":
        return (f"REVIEW RECORDED  {res['source_ref']} revision {res['annotation_revision']}: {res['decision']}\n"
                f"  reviewed_by={res['reviewed_by']} at {res['reviewed_at']}\n"
                f"  canonical_promotion=false promoted=false\n  {res['message']}")
    if cmd == "scan":
        return (f"scan: rounds={res['rounds']} stopped_because={res['stopped_because']} "
                f"complete={res['complete']}\n  discovery={res['discovery']}")
    if cmd == "pending":
        lines = [f"awaiting annotation: {res['returned']}" + (" (more)" if res["more"] else "")]
        lines += [f"  {i['source_ref']}  {i['source_kind']}  state={i['processing_state']}  "
                  f"chunks={i['chunks']}  coverage={i['coverage']}" for i in res["items"]]
        return "\n".join(lines)
    return json.dumps(res, ensure_ascii=False, indent=2)


COMMANDS = {"inspect": cmd_inspect, "pending": cmd_pending, "scan": cmd_scan, "index": cmd_index,
            "status": cmd_status, "verify": cmd_verify, "review": cmd_review, "export": cmd_export}


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=argparse.SUPPRESS, help="wiki root (default: this repository)")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="machine-readable output")
    ap = argparse.ArgumentParser(prog="brain_enrichment.cli", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter, parents=[common])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, **kw):
        return sub.add_parser(name, parents=[common], **kw)

    s = add("inspect"); s.add_argument("source_ref"); s.add_argument("--chunk"); s.add_argument("--cursor")
    s = add("scan"); s.add_argument("--max-rounds", type=int, default=50)
    s.add_argument("--time-budget", type=float, default=20.0); s.add_argument("--retry-failed", action="store_true")
    s = add("index"); s.add_argument("action", choices=("refresh", "rebuild"))
    s.add_argument("--batch-limit", type=int, default=C.INDEX_REFRESH_BATCH)
    s.add_argument("--max-batches", type=int, default=20)
    s = add("pending"); s.add_argument("--limit", type=int, default=20); s.add_argument("--after")
    s.add_argument("--failed", action="store_true")
    add("status")
    s = add("verify"); s.add_argument("--freshness", action=argparse.BooleanOptionalAction, default=True)
    s = add("review"); s.add_argument("source_ref"); s.add_argument("--revision", type=int, required=True)
    s.add_argument("--decision", choices=RV.DECISIONS, required=True)
    s.add_argument("--note", required=True); s.add_argument("--actor", required=True)
    s.add_argument("--source-version"); s.add_argument("--yes", action="store_true")
    s = add("export"); s.add_argument("--out"); s.add_argument("--with-text", action="store_true")
    return ap


def main(argv: list[str] | None = None, *, services: Services | None = None, env: dict | None = None,
         stdin=None, stdout=None, stderr=None) -> int:
    env = dict(os.environ) if env is None else env
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    args = build_parser().parse_args(argv)
    as_json = getattr(args, "json", False)
    root = Path(getattr(args, "root", None) or DEFAULT_ROOT).resolve()
    config = EnrichmentConfig.from_env(env)

    def emit_error(exc: C.EnrichmentError) -> int:
        pub = _Scrubber(root, env).deep(exc.to_public())
        if as_json:
            stdout.write(json.dumps({**pub, "command": args.cmd}, ensure_ascii=False) + "\n")
        else:
            e = pub["error"]
            extra = {k: v for k, v in e.items() if k not in ("code", "message")}
            stderr.write(f"ERROR {e['code']}: {e['message']}" + (f" {extra}" if extra else "") + "\n")
        return EXIT.get(exc.code, 70)

    if not root.is_dir():
        return emit_error(C.EnrichmentError("E_BAD_ARGUMENTS", "--root is not a directory"))
    if args.cmd == "review" and env.get("BRAIN_REMOTE_SESSION"):   # before anything is built or opened
        return emit_error(C.EnrichmentError("E_REVIEW_FORBIDDEN", "review is forbidden in a remote session"))
    services = services or Services(root, config)
    ctx = Ctx(root, config, services, args, env, stdin, stdout)
    try:
        res = COMMANDS[args.cmd](ctx)
    except C.EnrichmentError as exc:
        return emit_error(exc)
    except Exception as exc:  # noqa: BLE001 - never a traceback to the operator's JSON consumer
        return emit_error(C.EnrichmentError("E_INTERNAL", type(exc).__name__))
    if res.get("streamed"):
        return 0
    stdout.write((json.dumps(res, ensure_ascii=False) if as_json else render_text(res)) + "\n")
    if args.cmd == "verify" and not res["ok"]:
        return 1
    if args.cmd in ("scan", "index") and not (res.get("complete") or res.get("finished")):
        return 1
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError, io.UnsupportedOperation):
            pass
    sys.exit(main())
