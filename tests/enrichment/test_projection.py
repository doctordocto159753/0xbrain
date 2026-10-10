"""SourceReader (CONTRACT-E1 section 2): real files in a throwaway git wiki,
no mocks of the thing under test. Layers, tiers, hashes, flags, failures and
the zero-source-mutation invariant."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

import wiki_capture as wc
from brain_enrichment import contract as C
from brain_enrichment.chunking import Chunker
from brain_enrichment.source import SourceReader, simple_front_matter
from brain_enrichment.store import open_store
from brain_surface import refs as R
from support import add_held_file, commit_all, corpus_hashes, record

POLICY = C.ChunkPolicy()


@pytest.fixture()
def w(enrichment_wiki):
    return enrichment_wiki


def rd(w) -> SourceReader:
    return SourceReader(w.root)


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def rewrite(root: Path, rel: str, fn) -> None:
    p = root / rel
    p.write_bytes(fn(p.read_bytes()))


def tree_state(root: Path) -> dict:
    """Every path outside .git/_enrichment/_search: (kind, sha256, mtime_ns)."""
    out = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if rel.startswith((".git/", "_enrichment", "_search")) or rel == ".git":
            continue
        if p.is_dir():
            out[rel] = ("dir", None, p.stat().st_mtime_ns)
        else:
            out[rel] = ("file", sha(p.read_bytes()), p.stat().st_mtime_ns)
    return out


def git_state(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): (sha(p.read_bytes()), p.stat().st_mtime_ns)
            for p in sorted((root / ".git").rglob("*")) if p.is_file()}


# --------------------------------------------------------------- listing
def test_listing_is_stable_complete_and_paged(w):
    r = rd(w)
    full = r.list_sources(None, 1000)
    assert full.next_cursor is None and full.total_known == len(full.refs)
    expected = {v for k, v in w.refs.items() if k not in ("held_ok_text", "proposal")}
    assert set(full.refs) == expected                    # derivative of a held file is NOT its own source
    assert list(full.refs) == sorted(full.refs)
    paged, cur = [], None
    while True:
        page = r.list_sources(cur, 3)
        paged += page.refs
        assert page.inventory_generation == full.inventory_generation
        cur = page.next_cursor
        if cur is None:
            break
    assert paged == list(full.refs)
    assert r.list_sources(None, 1000).refs == full.refs      # deterministic across scans
    assert not any(ref.startswith(("prop:", "hist:")) for ref in full.refs)


def test_capture_like_files_without_a_valid_id_are_reported_not_silently_lost(w):
    d = w.root / "01-inbox/captures/2026/202610"
    d.mkdir(parents=True, exist_ok=True)
    (d / "cap-notanid.md").write_text("---\nid: x\n---\n", encoding="utf-8")
    (d / "notes.md").write_text("ordinary file", encoding="utf-8")
    r = rd(w)
    refs = r.list_sources(None, 1000).refs
    assert len(refs) == 12 and r.unaddressable == ["01-inbox/captures/2026/202610/cap-notanid.md"]


def test_listing_rejects_bad_limits_and_cursors(w):
    r = rd(w)
    for bad in (0, -1, 1001, True, "5", None):
        with pytest.raises(C.EnrichmentError) as e:
            r.list_sources(None, bad)
        assert e.value.code == "E_BAD_ARGUMENTS"
    for cur in ("", "!!!", C.encode_cursor("pending", a="x"), "a" * 3000):
        with pytest.raises(C.EnrichmentError) as e:
            r.list_sources(cur, 5)
        assert e.value.code == "E_BAD_CURSOR"


def test_listing_resumes_with_a_fresh_reader_from_the_cursor(w):
    page = rd(w).list_sources(None, 4)
    rest = rd(w).list_sources(page.next_cursor, 1000)       # "restart": new instance, same cursor
    assert list(page.refs) + list(rest.refs) == list(rd(w).list_sources(None, 1000).refs)


# ------------------------------------------------------- text captures
@pytest.mark.parametrize("name", ["fa_short", "crlf", "escaped_headings", "long", "dup_a", "dup_b"])
def test_text_capture_projects_the_stored_user_text_only(w, name):
    p = rd(w).load(w.refs[name])
    assert (p.source_kind, p.source_tier, p.source_authority_level) == ("capture-text", "capture", 7)
    assert p.processing_state == "projected" and len(p.layers) == 1
    layer = p.layers[0]
    assert layer.text == w.texts[name]                     # bound to the STORED text
    assert (layer.layer_id, layer.text_ref, layer.literal, layer.reviewed) == ("user-text", w.refs[name], True, False)
    assert layer.text_sha256 == sha(layer.text.encode()) and layer.chars == len(layer.text)
    assert layer.bytes == len(layer.text.encode("utf-8")) and layer.tier == "capture" and layer.authority_level == 7
    for forbidden in ("Provenance events", "wiki_capture.py", "capture_kind", "schema_version", "Interpretation"):
        assert forbidden not in layer.text                  # front matter / events / interpretation are never user text
    assert p.origin_channel == "mcp" and p.original_sha256 is None
    assert p.artifact_id == C.artifact_id("zb-instance", w.refs[name])
    assert p.source_version == C.source_version(w.refs[name], [la.version_dict() for la in p.layers])
    assert p.storage_sha256 == sha((w.root / w.paths[name]).read_bytes()) != layer.text_sha256
    assert p.anchor_path == w.paths[name]
    assert layer.text_sha256 == wc.parse_record_text(wc.read_record_file(w.root / w.paths[name]))[0]["sha256"]


def test_crlf_zwnj_and_escaped_headings_survive_with_correct_spans(w):
    r = rd(w)
    crlf = r.load(w.refs["crlf"])
    assert "\r\n" in crlf.layers[0].text and "نیم‌فاصله" in crlf.layers[0].text and "crlf" in crlf.quality_flags
    esc = r.load(w.refs["escaped_headings"])
    assert esc.layers[0].text.startswith("## Not A Section\n") and "\\## already escaped" in esc.layers[0].text
    assert "crlf" not in esc.quality_flags
    for name in ("crlf", "escaped_headings", "fa_short", "long"):
        p = r.load(w.refs[name])
        for c in Chunker().build(p, POLICY):
            assert c.text == p.layers[0].text[c.start_char:c.end_char]
            assert c.source_version == p.source_version and c.text_ref == w.refs[name]
    long_p = r.load(w.refs["long"])
    assert len(Chunker().build(long_p, POLICY)) == 2


def test_duplicate_text_captures_are_two_artifacts(w):
    a, b = rd(w).load(w.refs["dup_a"]), rd(w).load(w.refs["dup_b"])
    assert a.layers[0].text_sha256 == b.layers[0].text_sha256
    assert a.source_ref != b.source_ref and a.artifact_id != b.artifact_id and a.source_version != b.source_version
    ca, cb = Chunker().build(a, POLICY), Chunker().build(b, POLICY)
    assert {c.chunk_ref for c in ca}.isdisjoint({c.chunk_ref for c in cb})


def test_origin_channel_comes_only_from_the_recorded_channel(w):
    # message text mentioning another channel never changes it
    r = wc.capture_text("forwarded from telegram: please remember this", "mcp", "en")
    p = rd(w).load(f"cap:{r['id']}")
    assert p.origin_channel == "mcp"
    rel = r["path"]
    rewrite(w.root, rel, lambda b: b.replace(b"capture_channel: mcp", b"capture_channel: some-unknown-channel"))
    assert rd(w).load(f"cap:{r['id']}").origin_channel is None
    media = rd(w).load(w.refs["voice_plain"])
    assert media.origin_channel == "filesystem"


def test_public_projection_never_leaks_paths_or_server_fields(w):
    for ref in rd(w).list_sources(None, 1000).refs:
        p = rd(w).load(ref)
        blob = json.dumps(p.to_public(), ensure_ascii=False)
        assert str(w.root) not in blob and "anchor_path" not in blob and "origin_path" not in blob
        assert ".md" not in blob.replace("timeline.md", "")  # no file names either


# ------------------------------------------------------------- media
def test_voice_without_transcript_is_no_text_media_only(w):
    p = rd(w).load(w.refs["voice_plain"])
    assert p.source_kind == "capture-media" and p.layers == () and p.processing_state == "no_text"
    assert {"no_text", "media_only"} <= set(p.quality_flags)
    assert any("media" in x for x in p.coverage_warnings)
    assert Chunker().build(p, POLICY) == []


def test_voice_with_unreviewed_transcript_has_an_unreviewed_transcript_layer(w):
    p = rd(w).load(w.refs["voice_transcribed"])
    assert [la.layer_id for la in p.layers] == ["transcript"]
    la = p.layers[0]
    assert la.text == w.texts["voice_transcribed"] and la.reviewed is False and la.literal is True
    assert la.text_ref == w.refs["voice_transcribed"] and la.tier == "capture" and la.authority_level == 7
    assert "unreviewed_transcript" in p.quality_flags and "no_text" not in p.quality_flags


def test_reviewed_transcript_and_mixed_capture_layers(w, tmp_path):
    src = tmp_path / "c.ogg"
    src.write_bytes(b"OggS" + b"\x02" * 60)
    r = wc.capture_media(str(src), "voice", "filesystem", "fa")
    wc.record_transcript(r["id"], "متن بازبینی‌شده\n", "human-fix", "1", reviewed=True)
    p = rd(w).load(f"cap:{r['id']}")
    assert p.layers[0].reviewed is True and "unreviewed_transcript" not in p.quality_flags
    # a mixed capture with user text AND a transcript: two layers, user-text first
    src2 = tmp_path / "d.ogg"
    src2.write_bytes(b"OggS" + b"\x03" * 60)
    m = wc.capture_media(str(src2), "mixed", "filesystem", "fa")
    rel = m["path"]

    def add_user_text(raw: bytes) -> bytes:
        t = raw.decode("utf-8")
        t = t.replace("## User-supplied text\n", "## User-supplied text\n\nنوشته کاربر\n", 1)
        t = t.replace("## Literal transcript or extraction\n", "## Literal transcript or extraction\n\nمتن صوتی\n", 1)
        return t.encode("utf-8")

    rewrite(w.root, rel, add_user_text)
    mixed = rd(w).load(f"cap:{m['id']}")
    assert [la.layer_id for la in mixed.layers] == ["user-text", "transcript"]
    assert mixed.layers[0].text == "نوشته کاربر\n" and mixed.layers[1].text == "متن صوتی\n"
    # description sections are excluded in R1 and the exclusion is reported
    rewrite(w.root, rel, lambda b: b.decode().replace("## Machine description\n", "## Machine description\n\nیک تصویر\n", 1).encode())
    again = rd(w).load(f"cap:{m['id']}")
    assert again.source_version == mixed.source_version
    assert any("description" in x for x in again.coverage_warnings)


# ------------------------------------------------------------- holdings
def test_held_file_is_one_artifact_anchored_at_the_record(w):
    p = rd(w).load(w.refs["held_ok"])
    assert w.refs["held_ok"].startswith("rec:") and p.source_ref == w.refs["held_ok"]
    assert (p.source_kind, p.source_tier, p.source_authority_level) == ("ingested-file", "source-record", 4)
    assert len(p.layers) == 1
    la = p.layers[0]
    assert la.layer_id == "derivative-text" and la.text_ref == w.refs["held_ok_text"] and la.text_ref.startswith("doc:")
    assert la.text_ref == "doc:" + R.doc_ident(w.paths["held_ok_derivative"])
    assert (la.tier, la.authority_level) == ("derivative", 5)
    assert la.text == w.texts["held_ok_text"] and la.literal is True and la.reviewed is False
    original = b"letter bytes\n"
    assert p.original_sha256 == sha(original) == la.provenance_sha256
    der_bytes = (w.root / w.paths["held_ok_derivative"]).read_bytes()
    assert p.storage_sha256 == sha(der_bytes)
    assert p.original_sha256 != p.storage_sha256 and p.original_sha256 != la.text_sha256
    # a UTF-8 derivative's text hash equals its storage hash by construction; neither is the original's
    assert la.text_sha256 == sha(der_bytes)
    assert not (set(p.quality_flags) & {"needs_ocr", "no_text", "metadata_only"})
    # the derivative is not ALSO listed as a source; the record text is never the layer text
    assert w.refs["held_ok_text"] not in rd(w).list_sources(None, 1000).refs
    assert "source-record" not in la.text and "holdings_tier" not in la.text


def test_held_without_derivative_and_needs_ocr_are_no_text_with_flags(w):
    nodoc = rd(w).load(w.refs["held_nodoc"])
    assert nodoc.layers == () and nodoc.processing_state == "no_text" and nodoc.source_kind == "ingested-file"
    assert {"no_text", "metadata_only"} <= set(nodoc.quality_flags)
    assert nodoc.original_sha256 == sha(b"%PDF-1.4 fixture")
    assert nodoc.storage_sha256 == sha((w.root / w.paths["held_nodoc"]).read_bytes())     # record file: no derivative
    ocr = rd(w).load(w.refs["held_ocr"])
    assert ocr.layers == () and ocr.processing_state == "no_text"
    assert {"needs_ocr", "no_text", "metadata_only"} <= set(ocr.quality_flags)
    assert any("needs_ocr" in x for x in ocr.coverage_warnings)


def test_needs_ocr_with_real_derivative_text_keeps_the_layer_and_the_flag(w):
    h = add_held_file(w.root, "zb-src-dddddddddddd", "partial.pdf", b"%PDF partial",
                      "# Extracted from partial.pdf\n\n> method: pymupdf-text | pages: 3 | no-text-layer pages (needs OCR): [2, 3]\n\n"
                      "## Page 1\n\nreal text on page one that is long enough to count as text.\n", "needs_ocr")
    p = rd(w).load("rec:zb-src-dddddddddddd")
    assert len(p.layers) == 1 and {"needs_ocr", "extraction_partial"} <= set(p.quality_flags)
    assert any("OCR" in x for x in p.coverage_warnings)
    assert all(c.quality_flags == p.quality_flags for c in Chunker().build(p, POLICY))


def test_xlsx_row_limit_is_flagged_and_warned(w):
    add_held_file(w.root, "zb-src-eeeeeeeeeeee", "sheet.xlsx", b"PK xlsx",
                  "# Extracted from sheet.xlsx\n\n> method: openpyxl | sheets: 1 | truncated: S1 (500 rows, kept 200)\n\n"
                  "## Sheet: S1\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n")
    p = rd(w).load("rec:zb-src-eeeeeeeeeeee")
    assert "row_limit" in p.quality_flags and any("truncated" in x for x in p.coverage_warnings)
    assert len(p.layers) == 1


def test_source_record_without_file_keys_is_metadata_only(w):
    (w.root / "02-sources/records").mkdir(parents=True, exist_ok=True)
    (w.root / "02-sources/records/zb-plain-record.md").write_text(
        "---\nid: zb-plain-record\ntype: source-record\ntitle: Plain\n---\n\n# Plain\n\nBody that is not file text.\n",
        encoding="utf-8")
    p = rd(w).load("rec:zb-plain-record")
    assert (p.source_kind, p.source_tier) == ("source-record", "source-record")
    assert p.layers == () and {"no_text", "metadata_only"} <= set(p.quality_flags) and p.processing_state == "no_text"


def test_orphan_derivative_and_canonical_record(w):
    rel = "02-sources/text/orphan--note.md"
    (w.root / rel).write_text("# Extracted from note.txt\n\n> method: txt | sha256: " + "b" * 64
                              + "\n\nOrphan body text خط فارسی.\n", encoding="utf-8")
    ref = "doc:" + R.doc_ident(rel)
    assert ref in rd(w).list_sources(None, 1000).refs
    o = rd(w).load(ref)
    assert (o.source_kind, o.source_tier, o.source_authority_level) == ("derivative-text", "derivative", 5)
    assert o.layers[0].layer_id == "derivative-text" and o.layers[0].text_ref == ref and o.original_sha256 is None
    c = rd(w).load(w.refs["canonical"])
    raw = (w.root / w.paths["canonical"]).read_text(encoding="utf-8")
    assert (c.source_kind, c.source_tier, c.source_authority_level) == ("canonical-record", "canonical-record", 4)
    assert c.layers[0].layer_id == "record-text" and c.layers[0].text == raw and c.layers[0].text_ref == "rec:obj-nil"
    assert raw.startswith("---\nid: obj-nil")            # whole file: offsets align with brain_read content
    chunks = Chunker().build(c, POLICY)
    assert chunks[0].start_char == 0 and chunks[-1].end_char == len(raw)


def test_record_without_front_matter_id_gets_a_doc_ref(w):
    rel = "04-notes/loose.md"
    (w.root / "04-notes").mkdir(exist_ok=True)
    (w.root / rel).write_text("# Loose note\n\nno front matter\n", encoding="utf-8")
    ref = "doc:" + R.doc_ident(rel)
    p = rd(w).load(ref)
    assert p.source_tier == "canonical-record" and p.source_authority_level == 6
    assert R.resolve(w.root, ref).path == rel              # the same public grammar brain_read uses


# -------------------------------------------------- failures / safety
def test_edit_changes_version_and_delete_is_not_found(w):
    r = rd(w)
    before = r.load(w.refs["canonical"])
    rewrite(w.root, w.paths["canonical"], lambda b: b + "\nمتن تازه.\n".encode())
    after = r.load(w.refs["canonical"])
    assert after.source_ref == before.source_ref and after.artifact_id == before.artifact_id
    assert after.source_version != before.source_version
    (w.root / w.paths["canonical"]).unlink()
    with pytest.raises(C.EnrichmentError) as e:
        r.load(w.refs["canonical"])
    assert e.value.code == "E_NOT_FOUND"
    with pytest.raises(C.EnrichmentError) as e:
        r.load("cap:cap-20990101-000000-ffff")
    assert e.value.code == "E_NOT_FOUND"


def test_new_sources_are_visible_to_the_next_listing_and_loadable_at_once(w):
    r = rd(w)
    assert len(r.list_sources(None, 1000).refs) == 12
    new = wc.capture_text("یادداشت تازه", "mcp", "fa")
    p = r.load(f"cap:{new['id']}")                      # load rescans on a miss
    assert p.layers[0].text == "یادداشت تازه\n"
    assert f"cap:{new['id']}" in r.list_sources(None, 1000).refs


def test_bad_refs_are_rejected_without_touching_the_filesystem(w):
    r = rd(w)
    for bad in ("", "cap:../etc/passwd", "/etc/passwd", "rec:", "cap:cap/../x", "prop:prop-fixture-1", "hist:HANDOFF",
                "chk:" + "a" * 32, None, 5):
        with pytest.raises(C.EnrichmentError) as e:
            r.load(bad)
        assert e.value.code == "E_BAD_REF"


def _capture_dir(w) -> Path:
    return w.root / "01-inbox/captures/2026/202610"


def test_unparseable_capture_raises_a_precise_validation_error(w):
    cid = "cap-20261010-999999-abcd"
    d = _capture_dir(w)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{cid}.md").write_text(f"---\nid: {cid}\ntype: capture\nnever closed\n", encoding="utf-8")
    with pytest.raises(C.EnrichmentError) as e:
        rd(w).load(f"cap:{cid}")
    assert e.value.code == "E_VALIDATION" and f"cap:{cid}" in e.value.message and str(w.root) not in e.value.message
    assert e.value.extra.get("source_ref") == f"cap:{cid}"
    assert f"cap:{cid}" in rd(w).list_sources(None, 1000).refs            # listed, so it can be recorded as failed


def test_ambiguous_wrong_identity_and_undecodable_captures_fail_loudly(w):
    d = _capture_dir(w)
    d.mkdir(parents=True, exist_ok=True)
    good = (w.root / w.paths["fa_short"]).read_text(encoding="utf-8")
    cid_amb, cid_mis, cid_bin, cid_kind = ("cap-20261010-999991-aaaa", "cap-20261010-999992-bbbb",
                                           "cap-20261010-999993-cccc", "cap-20261010-999994-dddd")
    dup = good.replace(w.refs["fa_short"][4:], cid_amb) + "\n## User-supplied text\n\nsecond copy\n"
    (d / f"{cid_amb}.md").write_text(dup, encoding="utf-8")
    (d / f"{cid_mis}.md").write_text(good, encoding="utf-8")                     # id in file != file name
    (d / f"{cid_bin}.md").write_bytes(b"---\nid: x\n---\n\xff\xfe\xfa")
    (d / f"{cid_kind}.md").write_text(good.replace(w.refs["fa_short"][4:], cid_kind)
                                      .replace("capture_kind: text", "capture_kind: hologram"), encoding="utf-8")
    for cid, needle in ((cid_amb, "ambiguous"), (cid_mis, "identity"), (cid_bin, "UTF-8"), (cid_kind, "capture_kind")):
        with pytest.raises(C.EnrichmentError) as e:
            rd(w).load(f"cap:{cid}")
        assert e.value.code == "E_VALIDATION" and needle in e.value.message and f"cap:{cid}" in e.value.message
        assert str(w.root) not in e.value.message


def test_symlinks_and_escapes_are_refused(w, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("# secret outside the wiki\n", encoding="utf-8")
    d = _capture_dir(w)
    d.mkdir(parents=True, exist_ok=True)
    cid = "cap-20261010-999995-eeee"
    os.symlink(w.root / w.paths["fa_short"], d / f"{cid}.md")
    with pytest.raises(C.EnrichmentError) as e:
        rd(w).load(f"cap:{cid}")
    assert e.value.code == "E_VALIDATION" and "symlink" in e.value.message
    # a record whose derivative is a symlink to a file outside the wiki
    h = add_held_file(w.root, "zb-src-ffffffffffff", "link.txt", b"x", "# Extracted\n\ntext\n")
    der = w.root / h["derivative"]
    der.unlink()
    os.symlink(outside, der)
    with pytest.raises(C.EnrichmentError) as e:
        rd(w).load("rec:zb-src-ffffffffffff")
    assert e.value.code == "E_VALIDATION" and "symlink" in e.value.message
    # a record pointing at a path outside the derivative zone
    add_held_file(w.root, "zb-src-999999999999", "esc.txt", b"y", "# Extracted\n\ntext\n")
    rewrite(w.root, "02-sources/records/zb-src-999999999999.md",
            lambda b: b.replace(b"extracted_text_path: 02-sources/text/", b"extracted_text_path: 03-objects/works/nil.md\n#"))
    with pytest.raises(C.EnrichmentError) as e:
        rd(w).load("rec:zb-src-999999999999")
    assert e.value.code == "E_VALIDATION" and "outside" in e.value.message
    # an ordinary note is still read normally
    (w.root / "04-notes").mkdir(exist_ok=True)
    (w.root / "04-notes/real.md").write_text("---\nid: n-real\n---\nbody\n", encoding="utf-8")
    p = rd(w).load("rec:n-real")
    assert p.layers[0].text.endswith("body\n")


def test_oversize_file_is_refused(w, monkeypatch):
    import brain_enrichment.source as S
    monkeypatch.setattr(S, "MAX_FILE_BYTES", 100)
    with pytest.raises(C.EnrichmentError) as e:
        rd(w).load(w.refs["long"])
    assert e.value.code == "E_TOO_LARGE"


def test_simple_front_matter_parser():
    fm = simple_front_matter('---\nid: a-1\ntitle: "Quoted: yes"\nextracted_text_path: \nflag: \'x\'\nnested:\n  k: v\n- item\n---\nbody')
    assert fm == {"id": "a-1", "title": "Quoted: yes", "extracted_text_path": "", "flag": "x", "nested": ""}
    assert simple_front_matter("no front matter") is None and simple_front_matter("---\nid: a\n") is None


# ----------------------------------------------- zero source mutation
def test_full_scan_projection_pending_annotation_and_ops_mutate_nothing(w, tmp_path):
    commit_all(w.root, "settled")
    before_tree, before_git = tree_state(w.root), git_state(w.root)
    before_hash = corpus_hashes(w.root)
    store = open_store(w.root)
    for _ in range(3):
        store.pending(None, 5)
    page = store.pending(None, 5)
    item = page.items[0]
    store.append_annotation({"source_ref": item["source_ref"], "source_version": item["source_version"],
                             "expected_annotation_revision": 0, "idempotency_key": "zero-mutation-1",
                             "annotation": {"summary": "x", "tags": ["t"]}})
    store.status()
    assert store.verify()["ok"]
    with open(tmp_path / "export.jsonl", "w", encoding="utf-8") as fh:
        store.export_jsonl(fh)
    store.backup(tmp_path / "backup.sqlite3")
    for ref in rd(w).list_sources(None, 1000).refs:
        try:
            rd(w).load(ref)
        except C.EnrichmentError:
            pass
    assert corpus_hashes(w.root) == before_hash
    assert tree_state(w.root) == before_tree               # no file, no directory, no mtime moved
    assert git_state(w.root) == before_git                 # .git untouched
    status = subprocess.run(["git", "status", "--porcelain"], cwd=w.root, capture_output=True, text=True, check=True)
    assert status.stdout == ""                              # _enrichment/ is ignored; nothing else changed
    assert all(p.suffix != ".md" for p in (w.root / "_enrichment").rglob("*"))
    assert sorted(p.name for p in (w.root / "_enrichment").iterdir() if p.is_file()) \
        <= ["store-v1.sqlite3", "store-v1.sqlite3-shm", "store-v1.sqlite3-wal"]
