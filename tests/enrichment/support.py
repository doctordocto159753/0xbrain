"""Test support for the enrichment layer (AGENT-0): wiki seeding and an
in-memory FakeProjectionStore that implements the CONTRACT-E1 store
semantics (CAS, idempotency, stale-source) so B/C/D can develop before the
real SQLite store (WP-A) lands. The fake is a test double, not a reference
implementation of persistence."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from brain_enrichment import contract as C

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
for _p in (_SCRIPTS, _SCRIPTS / "capture"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import wiki_capture as wc  # noqa: E402
from brain_surface import ingest as ING  # noqa: E402

FA_SHORT = "فیت پلن: هر روز ۳۰ دقیقه پیاده‌روی و دو بار در هفته تمرین قدرتی انجام بدهم."
CRLF_TEXT = "line one\r\nخط دوم با نیم‌فاصله\r\n\r\nparagraph after blank\r\n"
ESCAPED_HEADINGS = "## Not A Section\nbody under fake heading\n\\## already escaped\nend"
DUP_TEXT = "Same words twice, two separate captures."


def long_text(paragraphs: int = 30) -> str:
    """~9-10k code points, headings + paragraphs + a table, Persian and English."""
    out = ["# طرح کلی پروژه نیل\n"]
    for i in range(paragraphs):
        if i % 8 == 0:
            out.append(f"\n## بخش {i // 8 + 1}\n")
        out.append(f"\nپاراگراف {i + 1}: " + ("این یک جملهٔ آزمایشی دربارهٔ پروژه نیل و برنامهٔ نوشتن است. " * 3)
                   + "Second sentence in English about the Nil manuscript timeline.\n")
    out.append("\n| name | value |\n| --- | --- |\n| a | 1 |\n| b | 2 |\n")
    return "".join(out)


@dataclass
class SeededWiki:
    root: Path
    refs: dict = field(default_factory=dict)       # name -> public ref
    texts: dict = field(default_factory=dict)      # name -> stored literal text
    paths: dict = field(default_factory=dict)      # name -> repo-relative path


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def add_held_file(root: Path, rid: str, name: str, original: bytes, derivative: str | None,
                  extraction: str = "complete") -> dict:
    """Write a held original + source record (+ derivative) exactly as
    brain_ingest_file would, without running the converter."""
    sha = hashlib.sha256(original).hexdigest()
    stem = ING.safe_stem(name)
    ext = Path(name).suffix.lower()
    orig_rel = f"{ING.ORIGINALS_DIR}/{rid}--{stem}{ext}"
    rec_rel = f"{ING.RECORDS_DIR}/{rid}.md"
    der_rel = f"{ING.TEXT_DIR}/{rid}--{stem}.md" if derivative is not None else None
    for rel, data in ((orig_rel, original),):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    if der_rel:
        (root / der_rel).parent.mkdir(parents=True, exist_ok=True)
        (root / der_rel).write_text(derivative, encoding="utf-8", newline="")
    (root / rec_rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rec_rel).write_text(ING._record(
        rid, name, "txt", "text/plain", sha, len(original), orig_rel, der_rel, extraction,
        None if extraction == "complete" else "fixture", None, None, "2026-10-01T00:00:00Z"),
        encoding="utf-8", newline="")
    return {"rid": rid, "sha": sha, "orig": orig_rel, "record": rec_rel, "derivative": der_rel}


def commit_all(root: Path, msg: str = "fixture") -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "--allow-empty", "-m", msg)


def record(root: Path, rel: str, rid: str, title: str, body: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\nid: {rid}\ntype: object\ntitle: {title}\nstatus: active\n---\n\n# {title}\n\n{body}\n",
                 encoding="utf-8")
    return p


def seed_wiki(root: Path) -> SeededWiki:
    from brain_surface import refs as R

    w = SeededWiki(root)
    (root / "00-system/registers/INSTANCE.json").write_text(json.dumps(
        {"id": "zb-instance", "name": "Fixture", "record_prefix": "zb"}) + "\n", encoding="utf-8")
    channel = "mcp"

    def text_capture(name: str, text: str, lang: str = "unknown") -> None:
        r = wc.capture_text(text, channel, lang)
        w.refs[name] = f"cap:{r['id']}"
        w.paths[name] = r["path"]
        w.texts[name] = text.rstrip("\n") + "\n"

    text_capture("fa_short", FA_SHORT, "fa")
    text_capture("crlf", CRLF_TEXT)
    text_capture("escaped_headings", ESCAPED_HEADINGS)
    text_capture("long", long_text(), "mixed")
    text_capture("dup_a", DUP_TEXT, "en")
    text_capture("dup_b", DUP_TEXT, "en")

    src = root.parent / "media-src"
    src.mkdir(exist_ok=True)
    (src / "a.ogg").write_bytes(b"OggS" + b"\x00" * 60)
    (src / "b.ogg").write_bytes(b"OggS" + b"\x01" * 60)
    for name, f in (("voice_plain", "a.ogg"), ("voice_transcribed", "b.ogg")):
        r = wc.capture_media(str(src / f), "voice", "filesystem", "fa")
        w.refs[name], w.paths[name] = f"cap:{r['id']}", r["path"]
    tr = "این متن پیاده‌شدهٔ صدا است درباره پروژه نیل.\n"
    wc.record_transcript(w.refs["voice_transcribed"][4:], tr, "fixture-stt", "1", reviewed=False)
    w.texts["voice_transcribed"] = tr

    der = "# Extracted from letter.txt\n\n> method: fixture | sha256: " + "a" * 64 + "\n\nThe ferry to Kolan left every Thursday before dawn.\nنامهٔ کلان دربارهٔ پروژه نیل.\n"
    h = add_held_file(root, "zb-src-aaaaaaaaaaaa", "letter.txt", b"letter bytes\n", der)
    w.refs["held_ok"] = f"rec:{h['rid']}"
    w.refs["held_ok_text"] = R.public_ref_for_path(root, h["derivative"])
    w.paths["held_ok"], w.paths["held_ok_derivative"] = h["record"], h["derivative"]
    w.texts["held_ok_text"] = der
    w.paths["held_ok_original"] = h["orig"]
    h2 = add_held_file(root, "zb-src-bbbbbbbbbbbb", "scan.pdf", b"%PDF-1.4 fixture", None, "preserved_only")
    w.refs["held_nodoc"] = f"rec:{h2['rid']}"
    w.paths["held_nodoc"] = h2["record"]
    h3 = add_held_file(root, "zb-src-cccccccccccc", "scanned.txt", b"scan bytes", "# Extracted from scanned.txt\n\n> method: fixture\n\n", "needs_ocr")
    w.refs["held_ocr"] = f"rec:{h3['rid']}"
    w.paths["held_ocr"] = h3["record"]

    record(root, "03-objects/works/nil.md", "obj-nil", "Project Nil",
           "Project Nil is the working title of the second novel. نیل نام کاری رمان دوم است.")
    w.refs["canonical"] = "rec:obj-nil"
    w.paths["canonical"] = "03-objects/works/nil.md"
    (root / "_proposals").mkdir(exist_ok=True)
    with open(root / "_proposals/proposals.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"id": "prop-fixture-1", "received": "2026-10-01T00:00:00Z",
                             "kind": "note", "authority_tier": "candidate", "status": "new",
                             "body": "unregistered proposal"}, ensure_ascii=False) + "\n")
    w.refs["proposal"] = "prop:prop-fixture-1"
    commit_all(root, "enrichment fixture corpus")
    return w


def corpus_hashes(root: Path) -> dict[str, str]:
    """sha256 of every tracked-or-untracked file outside .git/_enrichment/_search:
    the 'zero source mutation' witness."""
    out = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if p.is_file() and not rel.startswith((".git/", "_enrichment/", "_search/")):
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


# --------------------------------------------------------------- projection
def make_projection(source_ref: str, layers: list[tuple], *, instance: str = "zb-instance",
                    kind: str = "capture-text", tier: str = "capture", level: int = 7,
                    flags: tuple = ()) -> C.SourceProjection:
    """layers: (layer_id, text_ref, text, extra) tuples; extra may set
    literal/reviewed/origin/provenance_sha256/tier/authority_level."""
    built = []
    for layer_id, text_ref, text, extra in layers:
        extra = dict(extra or {})
        built.append(C.Layer(layer_id=layer_id, text_ref=text_ref, text_sha256=C.sha256_text(text),
                             chars=len(text), bytes=len(text.encode("utf-8")),
                             literal=extra.pop("literal", True), reviewed=extra.pop("reviewed", False),
                             text=text, origin=extra.pop("origin", f"fixture:{layer_id}"),
                             provenance_sha256=extra.pop("provenance_sha256", None),
                             tier=extra.pop("tier", tier), authority_level=extra.pop("authority_level", level)))
    ver = C.source_version(source_ref, [la.version_dict() for la in built])
    return C.SourceProjection(
        artifact_id=C.artifact_id(instance, source_ref), source_ref=source_ref, source_version=ver,
        source_kind=kind, source_tier=tier, source_authority_level=level, layers=tuple(built),
        storage_sha256=C.sha256_text("storage:" + source_ref), quality_flags=flags,
        processing_state="projected" if built else "no_text")


def naive_chunks(proj: C.SourceProjection, size: int = 40) -> list[C.Chunk]:
    """TEST-ONLY fixed-width chunker (not the CONTRACT-E1 algorithm): enough
    to exercise stores, annotation validation and retrieval against chunks."""
    chunks: list[C.Chunk] = []
    for layer in proj.layers:
        text, pos = layer.text, 0
        while pos < len(text) or (pos == 0 and not text):
            end = min(len(text), pos + size)
            dg = C.chunk_digest(proj.source_ref, proj.source_version, layer.layer_id, pos, end)
            chunks.append(C.Chunk(C.chunk_ref(dg), dg, proj.source_ref, proj.source_version,
                                  layer.text_ref, layer.layer_id, len(chunks), pos, end, text[pos:end]))
            pos = end
            if not text:
                break
    return [C.Chunk(**{**c.__dict__, "prev_chunk_ref": chunks[i - 1].chunk_ref if i else None,
                       "next_chunk_ref": chunks[i + 1].chunk_ref if i + 1 < len(chunks) else None})
            for i, c in enumerate(chunks)]


# ---------------------------------------------------------------- fake store
class FakeProjectionStore:
    """In-memory CONTRACT-E1 store semantics: append-only revisions, CAS on
    expected_annotation_revision, idempotency receipts, stale-source refusal."""

    def __init__(self):
        self.projections: dict[str, C.SourceProjection] = {}
        self.chunks: dict[str, list[C.Chunk]] = {}
        self.revisions: dict[str, list[dict]] = {}       # source_ref -> [{revision, source_version, annotation}]
        self.receipts: dict[tuple, tuple] = {}           # (source_ref, key) -> (digest, receipt)
        self.reviews: list[dict] = []

    def upsert_projection(self, projection, chunks):
        prev = self.projections.get(projection.source_ref)
        state = "created" if prev is None else "unchanged" if prev.source_version == projection.source_version else "new_version"
        if not projection.layers:
            state = "no_text"
        self.projections[projection.source_ref] = projection
        self.chunks[projection.source_ref] = list(chunks)
        return C.ProjectionReceipt(projection.source_ref, projection.artifact_id, projection.source_version,
                                   len(chunks), state, prev.source_version if prev else None)

    def _current_rev(self, source_ref, version=None):
        revs = [r for r in self.revisions.get(source_ref, []) if version is None or r["source_version"] == version]
        return revs[-1] if revs else None

    def get(self, source_ref, source_version=None):
        proj = self.projections.get(source_ref)
        if proj is None:
            raise C.EnrichmentError("E_NOT_FOUND", f"no projection for {source_ref}")
        if source_version and source_version != proj.source_version:
            raise C.EnrichmentError("E_STALE_SOURCE", "source_version is not current")
        cur = self._current_rev(source_ref, proj.source_version)
        chunks = self.chunks.get(source_ref, [])
        annotated = {c["chunk_ref"] for r in self.revisions.get(source_ref, [])
                     if r["source_version"] == proj.source_version
                     for c in r["annotation"].get("chunk_annotations", [])}
        return C.ArtifactView(projection=proj.to_public(), annotation=cur["annotation"] if cur else None,
                              coverage={"annotated_chunks": len(annotated), "total_chunks": len(chunks)},
                              review={"state": "unreviewed", "revision": None}, chunk_count=len(chunks))

    def append_annotation(self, request):
        ref, key = request["source_ref"], request["idempotency_key"]
        digest = C.request_digest(request)
        seen = self.receipts.get((ref, key))
        if seen:
            if seen[0] != digest:
                raise C.EnrichmentError("E_IDEMPOTENCY_CONFLICT", "idempotency_key reused with a different payload")
            return C.AnnotationReceipt(**{**seen[1].__dict__, "idempotent_replay": True})
        proj = self.projections.get(ref)
        if proj is None:
            raise C.EnrichmentError("E_NOT_FOUND", f"no projection for {ref}")
        if request["source_version"] != proj.source_version:
            raise C.EnrichmentError("E_STALE_SOURCE", "source changed since it was read")
        cur = self._current_rev(ref)
        cur_n = cur["revision"] if cur else 0
        if request["expected_annotation_revision"] != cur_n:
            raise C.EnrichmentError("E_CONFLICT", "annotation revision moved", current_revision=cur_n)
        rev = {"revision": cur_n + 1, "source_version": proj.source_version,
               "annotation": request["annotation"], "derived": request.get("derived")}
        self.revisions.setdefault(ref, []).append(rev)
        view = self.get(ref)
        receipt = C.AnnotationReceipt(ref, proj.source_version, rev["revision"], False, view.coverage, "annotated")
        self.receipts[(ref, key)] = (digest, receipt)
        return receipt

    def record_review(self, request, human_context):
        if not human_context or not human_context.get("actor") or human_context.get("remote"):
            raise C.EnrichmentError("E_REVIEW_FORBIDDEN", "review needs a local human context")
        self.reviews.append({**request, "by": human_context["actor"]})
        return C.ReviewReceipt(request["source_ref"], request["annotation_revision"], request["decision"],
                               human_context["actor"], "2026-10-10T00:00:00Z")

    # -- read helpers (Protocol parity)
    def get_chunks(self, source_ref, source_version=None):
        proj = self.projections.get(source_ref)
        if proj is None:
            raise C.EnrichmentError("E_NOT_FOUND", f"no projection for {source_ref}")
        if source_version and source_version != proj.source_version:
            raise C.EnrichmentError("E_STALE_SOURCE", "source_version is not current")
        return list(self.chunks.get(source_ref, []))

    def get_chunk(self, chunk_ref):
        for chs in self.chunks.values():
            for c in chs:
                if c.chunk_ref == chunk_ref:
                    return c
        raise C.EnrichmentError("E_NOT_FOUND", f"no such chunk {chunk_ref}")

    def entity_exists(self, entity_id):
        return any(e.get("entity_id") == entity_id for revs in self.revisions.values() for r in revs
                   for e in (r.get("derived") or {}).get("entities", []))

    def list_current(self, after, limit):
        refs = sorted(r for r in self.projections if after is None or r > after)[:limit]
        out = []
        for r in refs:
            p = self.projections[r]
            cur = self._current_rev(r, p.source_version)
            out.append({"projection": p, "chunks": list(self.chunks.get(r, [])),
                        "annotation": cur["annotation"] if cur else None,
                        "annotation_revision": cur["revision"] if cur else None,
                        "review_state": "unreviewed",
                        "processing_state": "annotated" if cur else p.processing_state})
        return out

    def change_marker(self):
        return len(self.projections) + sum(len(v) for v in self.revisions.values()) + len(self.reviews)

    def pending(self, cursor, limit, retry_failed=False):
        todo = [r for r, p in self.projections.items() if p.layers and self._current_rev(r, p.source_version) is None]
        items = tuple({"source_ref": r, "source_version": self.projections[r].source_version} for r in todo[:limit])
        return C.PendingPage(items, len(todo), len(items), len(todo) > len(items), None)
