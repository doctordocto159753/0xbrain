"""Characterization tests for scripts/capture/wiki_capture.py (Agent D, phase 1).

These pin the behavior of the capture core AS SHIPPED at baseline 9395ed9.
They were written before any core change. The three defects Agent D pinned as strict xfails (heading injection, CRLF,
unknown front-matter keys) are fixed; their tests are ordinary regressions.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from conftest import OGG, PDF, PNG

wc_err = pytest.importorskip("wiki_capture").CaptureError


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def code_of(exc: pytest.ExceptionInfo) -> str:
    return exc.value.code


# ------------------------------------------------------------- exact text

MULTILINGUAL = [
    "سلام دنیا",
    "متن با نیم‌فاصله: می‌روم، نمی‌دانم",
    "Mixed فارسی and English، با اعداد ۱۲۳ و 456",
    "ي ك (عربی) vs ی ک (فارسی)",          # Arabic vs Persian letter forms must not be unified
    "é vs é",                      # NFD vs NFC must not be normalized
    "rtl‏ mark and ‌ zwnj",
    "emoji 🧠 and 日本語",
    "  leading and trailing spaces  ",
    "tabs\tinside",
]


@pytest.mark.parametrize("text", MULTILINGUAL)
def test_text_capture_is_byte_exact_modulo_single_trailing_newline(cap, text):
    r = cap.capture_text(text, "mcp", "mixed")
    rec = cap.read_capture(r["id"])
    stored = rec["sections"]["User-supplied text"]
    assert stored == text.rstrip("\n") + "\n"
    assert r["sha256"] == sha(stored.encode("utf-8"))
    assert cap.validate_record(r["id"]) == []


def test_text_normalization_not_applied(cap):
    a = cap.capture_text("é", "mcp")
    b = cap.capture_text("é", "mcp")
    assert a["sha256"] != b["sha256"]
    assert b["duplicate_of"] is None


def test_text_trailing_newlines_collapse_to_one(cap):
    r = cap.capture_text("x\n\n\n", "mcp")
    assert cap.read_capture(r["id"])["sections"]["User-supplied text"] == "x\n"


def test_internal_blank_lines_preserved(cap):
    r = cap.capture_text("a\n\n\n\nb", "mcp")
    assert cap.read_capture(r["id"])["sections"]["User-supplied text"] == "a\n\n\n\nb\n"


def test_text_containing_frontmatter_fence_survives(cap):
    r = cap.capture_text("a\n---\nb\n", "mcp")
    assert cap.read_capture(r["id"])["sections"]["User-supplied text"] == "a\n---\nb\n"
    assert cap.validate_record(r["id"]) == []


def test_empty_and_whitespace_text_rejected(cap):
    for t in ("", "   ", "\n\n"):
        with pytest.raises(wc_err) as e:
            cap.capture_text(t, "mcp")
        assert code_of(e) == "E_EMPTY"
    assert list(cap.CAPTURES_ROOT.rglob("cap-*.md")) == []


def test_bad_channel_and_lang_rejected(cap):
    with pytest.raises(wc_err) as e:
        cap.capture_text("x", "carrier-pigeon")
    assert code_of(e) == "E_BAD_CHANNEL"
    with pytest.raises(wc_err) as e:
        cap.capture_text("x", "mcp", "klingon")
    assert code_of(e) == "E_BAD_LANG"


def test_new_text_record_fields(cap):
    r = cap.capture_text("hello", "mcp", "en")
    fm = cap.read_capture(r["id"])["front_matter"]
    assert fm["status"] == "received"
    assert fm["capture_kind"] == "text"
    assert fm["raw_media"] is None and fm["raw_media_available"] is False
    assert fm["transcription_state"] == "not-requested"
    assert fm["transcription_reviewed"] is False
    assert fm["quality_flags"] == [] and fm["promoted_to"] is None
    assert fm["schema_version"] == cap.CAPTURE_SCHEMA_VERSION
    assert cap.ID_RE.match(fm["id"])


def test_record_path_layout_is_year_month_sharded(cap):
    r = cap.capture_text("hello", "mcp")
    cid = r["id"]
    y, ym = cid[4:8], cid[4:10]
    assert (cap.CAPTURES_ROOT / y / ym / f"{cid}.md").is_file()


# ------------------------------------------------- former defects (fixed)
# DEFECT-1/2/3 were found by Agent D as strict xfails and fixed during
# integration (Agent H). Exhaustive mutation tests: tests/test_capture_integrity.py.

def test_text_with_section_header_line_roundtrips(cap):
    text = "before\n## Review notes\nafter\n"
    r = cap.capture_text(text, "mcp")
    assert cap.read_capture(r["id"])["sections"]["User-supplied text"] == text


def test_crlf_text_is_exact_and_validates(cap):
    text = "line1\r\nline2\r\n"
    r = cap.capture_text(text, "mcp")
    stored = cap.read_capture(r["id"])["sections"]["User-supplied text"]
    assert stored == text
    assert cap.validate_record(r["id"]) == []


def test_defect1_heading_line_is_escaped_on_disk(cap):
    text = "before\n## Review notes\nafter\n"
    r = cap.capture_text(text, "mcp")
    raw = cap.record_path(r["id"]).read_bytes().decode("utf-8")
    assert "body_encoding: escaped-headings-v1" in raw
    assert "before\n\\## Review notes\nafter" in raw


def test_defect1_rewrite_keeps_user_text(cap):
    text = "before\n## Review notes\nafter\n"
    r = cap.capture_text(text, "mcp")
    cap.set_state(r["id"], "processing", "alice", note="checked")
    got = cap.read_capture(r["id"])["sections"]
    assert got["User-supplied text"] == text
    assert "[alice] checked" in got["Review notes"]
    assert cap.validate_record(r["id"]) == []


def test_unknown_front_matter_key_survives_rewrite(cap):
    r = cap.capture_text("x", "mcp")
    p = cap.record_path(r["id"])
    p.write_text(p.read_text(encoding="utf-8").replace(
        "schema_version:", "future_field: keep\nschema_version:"), encoding="utf-8")
    cap.set_state(r["id"], "processing", "alice")
    assert "future_field: keep" in p.read_text(encoding="utf-8")


# ---------------------------------------------------------------- media

def test_media_capture_voice(cap, media):
    src = media("a.ogg", OGG)
    r = cap.capture_media(src, "voice", "telegram-hermes", "fa")
    fm = cap.read_capture(r["id"])["front_matter"]
    assert fm["capture_kind"] == "voice"
    assert fm["raw_media_available"] is True
    assert fm["transcription_state"] == "pending"
    mp = cap._resolve_media(fm["raw_media"])
    assert mp.read_bytes() == OGG
    assert Path(src).read_bytes() == OGG          # source untouched
    assert cap.validate_record(r["id"]) == []


def test_media_capture_image_and_file_states(cap, media):
    r = cap.capture_media(media("a.png", PNG), "image", "obsidian")
    assert cap.read_capture(r["id"])["front_matter"]["transcription_state"] == "not-requested"
    r2 = cap.capture_media(media("a.pdf", PDF), "file", "obsidian")
    assert cap.read_capture(r2["id"])["front_matter"]["capture_kind"] == "file"


def test_handwriting_and_drawing_accept_image_suffix(cap, media):
    for kind in ("handwriting", "drawing"):
        r = cap.capture_media(media(f"{kind}.png", PNG + kind.encode()), kind, "mcp")
        assert cap.validate_record(r["id"]) == []


def test_sha256_is_of_original_bytes(cap, media):
    r = cap.capture_media(media("a.ogg", OGG), "voice", "mcp")
    assert r["sha256"] == sha(OGG)
    assert r["bytes"] == len(OGG)
    assert cap.read_capture(r["id"])["front_matter"]["sha256"] == sha(OGG)


@pytest.mark.parametrize("name,data,kind,code", [
    ("a.exe", b"MZ" + b"\0" * 20, "file", "E_UNSUPPORTED_TYPE"),
    ("a.ogg", b"NOTOGG" + b"\0" * 20, "voice", "E_MIME_MISMATCH"),
    ("a.png", PNG, "voice", "E_MEDIA_MISMATCH"),
    ("a.ogg", b"", "voice", "E_EMPTY"),
])
def test_media_rejections(cap, media, name, data, kind, code):
    with pytest.raises(wc_err) as e:
        cap.capture_media(media(name, data), kind, "mcp")
    assert code_of(e) == code
    assert list(cap.CAPTURES_ROOT.rglob("cap-*")) == []   # nothing left behind


def test_text_kind_not_accepted_for_media(cap, media):
    with pytest.raises(wc_err) as e:
        cap.capture_media(media("a.ogg", OGG), "text", "mcp")
    assert code_of(e) == "E_BAD_KIND"


def test_missing_source_rejected(cap, tmp_path):
    with pytest.raises(wc_err) as e:
        cap.capture_media(str(tmp_path / "nope.ogg"), "voice", "mcp")
    assert code_of(e) == "E_NOT_FOUND"


def test_oversize_rejected(cap, media, monkeypatch):
    monkeypatch.setattr(cap, "MAX_BYTES", 10)
    with pytest.raises(wc_err) as e:
        cap.capture_media(media("a.ogg", OGG), "voice", "mcp")
    assert code_of(e) == "E_OVERSIZE"


@pytest.mark.skipif(os.name == "nt", reason="symlink privileges")
def test_symlink_source_rejected(cap, media, tmp_path):
    real = media("a.ogg", OGG)
    link = tmp_path / "link.ogg"
    link.symlink_to(real)
    with pytest.raises(wc_err) as e:
        cap.capture_media(str(link), "voice", "mcp")
    assert code_of(e) == "E_SYMLINK"


# ------------------------------------------------------------ duplicates

def test_duplicate_text_is_kept_and_linked_not_dropped(cap):
    a = cap.capture_text("same", "mcp")
    b = cap.capture_text("same", "mcp")
    assert b["duplicate_of"] == a["id"] and b["id"] != a["id"]
    assert cap.record_path(b["id"]).is_file()
    ev = cap.read_capture(b["id"])["sections"]["Provenance events"]
    assert "duplicate-of" in ev and a["id"] in ev


def test_duplicate_media_is_kept_and_linked(cap, media):
    a = cap.capture_media(media("a.ogg", OGG), "voice", "mcp")
    b = cap.capture_media(media("b.ogg", OGG), "voice", "telegram-hermes")
    assert b["duplicate_of"] == a["id"]
    assert cap._resolve_media(b["media"]).read_bytes() == OGG


def test_find_by_hash_first_match_deterministic(cap):
    a = cap.capture_text("x", "mcp")
    cap.capture_text("x", "mcp")
    assert cap.find_by_hash(a["sha256"]) is not None


# --------------------------------------------------------- atomic failure

def test_atomic_write_failure_leaves_no_partial_or_tmp(cap, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk gremlin")
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        cap.capture_text("hello", "mcp")
    leftovers = [p for p in cap.CAPTURES_ROOT.rglob("*") if p.is_file()]
    assert leftovers == []


def test_existing_record_survives_failed_rewrite(cap, monkeypatch):
    r = cap.capture_text("keep me", "mcp")
    p = cap.record_path(r["id"])
    before = p.read_bytes()

    def boom(*a, **k):
        raise OSError("disk gremlin")
    with monkeypatch.context() as m:
        m.setattr(os, "replace", boom)
        with pytest.raises(OSError):
            cap.set_state(r["id"], "processing", "alice")
    assert p.read_bytes() == before
    assert [q for q in p.parent.iterdir() if q.name.startswith(".tmp-")] == []


def test_media_rolled_back_when_record_write_fails(cap, media, monkeypatch):
    real_replace = os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:          # 1st = media, 2nd = record
            raise OSError("record write failed")
        return real_replace(src, dst)
    with monkeypatch.context() as m:
        m.setattr(os, "replace", flaky)
        with pytest.raises(OSError):
            cap.capture_media(media("a.ogg", OGG), "voice", "mcp")
    assert [p for p in cap.CAPTURES_ROOT.rglob("*") if p.is_file()] == []


# --------------------------------------------------------- state machine

VALID = [(a, b) for a, bs in __import__("wiki_capture").TRANSITIONS.items() for b in bs]


def _force_status(cap, cid, status):
    """Test helper: put a record in `status` with a coherent event path."""
    p, (fm, sections) = cap._load(cid)
    path = {"received": [], "processing": ["processing"],
            "transcribed": ["processing", "transcribed"],
            "described": ["processing", "described"],
            "needs-review": ["processing", "needs-review"],
            "processing-failed": ["processing", "processing-failed"],
            "reviewed": ["processing", "needs-review", "reviewed"],
            "parked": ["processing", "needs-review", "reviewed", "parked"],
            "promoted": ["processing", "needs-review", "reviewed", "promoted"]}[status]
    fm["status"] = status
    ev = sections["Provenance events"]
    for s in path:
        ev += cap._event_line(f"state:{s}", "ok", "actor:test") + "\n"
    sections["Provenance events"] = ev
    cap._atomic_write_bytes(p, cap.render_record(fm, sections).encode("utf-8"))


def test_every_documented_transition_is_reachable(cap):
    ok = 0
    for a, b in VALID:
        r = cap.capture_text(f"t-{a}-{b}", "mcp")
        if a == "received" and b == "reviewed":
            continue                       # duplicate-only, covered below
        _force_status(cap, r["id"], a)
        kw = {"target": "03-canonical/x.md"} if b == "promoted" else {}
        cap.set_state(r["id"], b, "alice", **kw)
        assert cap.read_capture(r["id"])["front_matter"]["status"] == b
        ok += 1
    assert ok == len(VALID) - 1


def test_invalid_transitions_rejected_and_state_unchanged(cap):
    states = list(cap.TRANSITIONS)
    for a in states:
        for b in states:
            if b in cap.TRANSITIONS[a]:
                continue
            r = cap.capture_text(f"i-{a}-{b}", "mcp")
            _force_status(cap, r["id"], a)
            with pytest.raises(wc_err) as e:
                cap.set_state(r["id"], b, "alice", target="x/y.md")
            assert code_of(e) == "E_BAD_TRANSITION"
            assert cap.read_capture(r["id"])["front_matter"]["status"] == a


def test_terminal_states_have_no_exits(cap):
    for t in cap.TERMINAL:
        assert cap.TRANSITIONS[t] == set()


def test_unknown_state_rejected(cap):
    r = cap.capture_text("x", "mcp")
    with pytest.raises(wc_err) as e:
        cap.set_state(r["id"], "bogus", "alice")
    assert code_of(e) == "E_BAD_STATE"


def test_actor_required(cap):
    r = cap.capture_text("x", "mcp")
    for actor in ("", "   "):
        with pytest.raises(wc_err) as e:
            cap.set_state(r["id"], "processing", actor)
        assert code_of(e) == "E_NO_ACTOR"
    assert cap.read_capture(r["id"])["front_matter"]["status"] == "received"


def test_received_to_reviewed_only_for_duplicates(cap):
    a = cap.capture_text("dupe", "mcp")
    b = cap.capture_text("dupe", "mcp")
    with pytest.raises(wc_err) as e:
        cap.set_state(a["id"], "reviewed", "alice")
    assert code_of(e) == "E_BAD_TRANSITION"
    cap.set_state(b["id"], "reviewed", "alice")
    assert cap.read_capture(b["id"])["front_matter"]["status"] == "reviewed"


def test_promotion_requires_safe_target(cap):
    r = cap.capture_text("x", "mcp")
    _force_status(cap, r["id"], "reviewed")
    with pytest.raises(wc_err) as e:
        cap.set_state(r["id"], "promoted", "alice")
    assert code_of(e) == "E_NO_TARGET"
    for bad in ("../x.md", "/etc/x", "a\\b"):
        with pytest.raises(wc_err) as e:
            cap.set_state(r["id"], "promoted", "alice", target=bad)
        assert code_of(e) == "E_TRAVERSAL"
    cap.set_state(r["id"], "promoted", "alice", target="03-canonical/x.md")
    assert cap.read_capture(r["id"])["front_matter"]["promoted_to"] == "03-canonical/x.md"


def test_state_change_appends_note_and_event_with_actor(cap):
    r = cap.capture_text("x", "mcp")
    cap.set_state(r["id"], "processing", "alice", note="starting")
    s = cap.read_capture(r["id"])["sections"]
    assert "[alice] starting" in s["Review notes"]
    assert "state:processing" in s["Provenance events"] and "actor:alice" in s["Provenance events"]
    assert cap.validate_record(r["id"]) == []


def test_original_text_untouched_by_state_changes(cap):
    r = cap.capture_text("سلام", "mcp", "fa")
    cap.set_state(r["id"], "processing", "alice")
    cap.set_state(r["id"], "needs-review", "alice")
    assert cap.read_capture(r["id"])["sections"]["User-supplied text"] == "سلام\n"
    assert cap.validate_record(r["id"]) == []


# ----------------------------------------------- transcript / description

def test_transcript_attaches_to_voice_only(cap, media):
    v = cap.capture_media(media("a.ogg", OGG), "voice", "mcp", "fa")
    cap.record_transcript(v["id"], "سلام دنیا", "manual", "v1")
    fm = cap.read_capture(v["id"])["front_matter"]
    s = cap.read_capture(v["id"])["sections"]
    assert s["Literal transcript or extraction"] == "سلام دنیا\n"
    assert fm["transcription_state"] == "complete"
    assert fm["transcription_method"] == "manual"
    assert fm["transcription_reviewed"] is False
    assert fm["status"] == "received"             # transcript does not move the state machine
    assert "transcribed" in s["Provenance events"]
    t = cap.capture_text("x", "mcp")
    with pytest.raises(wc_err) as e:
        cap.record_transcript(t["id"], "y", "manual", "v1")
    assert code_of(e) == "E_WRONG_KIND"


def test_transcript_empty_rejected_and_record_unchanged(cap, media):
    v = cap.capture_media(media("a.ogg", OGG), "voice", "mcp")
    before = cap.record_path(v["id"]).read_bytes()
    with pytest.raises(wc_err) as e:
        cap.record_transcript(v["id"], "  \n", "manual", "v1")
    assert code_of(e) == "E_EMPTY"
    assert cap.record_path(v["id"]).read_bytes() == before


def test_transcript_segments_sidecar_and_flags(cap, media):
    v = cap.capture_media(media("a.ogg", OGG), "voice", "mcp")
    segs = [{"start_ms": 0, "end_ms": 900, "text": "hi", "confidence": None}]
    cap.record_transcript(v["id"], "hi", "faster-whisper", "1.2.1", segments=segs,
                          quality_flags=["uncertain-names", "silence"])
    side = cap.record_path(v["id"]).with_suffix(".segments.json")
    assert json.loads(side.read_text(encoding="utf-8"))["segments"] == segs
    fm = cap.read_capture(v["id"])["front_matter"]
    assert fm["quality_flags"] == ["silence", "uncertain-names"]     # sorted union
    assert cap.validate_record(v["id"]) == []


def test_transcript_does_not_touch_media_or_hash(cap, media):
    v = cap.capture_media(media("a.ogg", OGG), "voice", "mcp")
    cap.record_transcript(v["id"], "x", "manual", "v1")
    fm = cap.read_capture(v["id"])["front_matter"]
    assert fm["sha256"] == sha(OGG)
    assert cap._resolve_media(fm["raw_media"]).read_bytes() == OGG


def test_description_keeps_literal_and_interpretation_separate(cap, media):
    i = cap.capture_media(media("a.png", PNG), "image", "mcp")
    cap.record_description(i["id"], "TOTAL 42", "A receipt on a table", "manual", "v1")
    s = cap.read_capture(i["id"])["sections"]
    assert s["Literal transcript or extraction"] == "TOTAL 42\n"
    assert s["Machine description"] == "A receipt on a table\n"
    assert "A receipt" not in s["Literal transcript or extraction"]
    assert "TOTAL" not in s["Machine description"]


def test_description_partial_update_leaves_other_section(cap, media):
    i = cap.capture_media(media("a.png", PNG), "image", "mcp")
    cap.record_description(i["id"], "TOTAL 42", "desc", "manual", "v1")
    cap.record_description(i["id"], None, "new desc", "manual", "v1")
    s = cap.read_capture(i["id"])["sections"]
    assert s["Literal transcript or extraction"] == "TOTAL 42\n"
    assert s["Machine description"] == "new desc\n"


def test_description_wrong_kind(cap, media):
    v = cap.capture_media(media("a.ogg", OGG), "voice", "mcp")
    with pytest.raises(wc_err) as e:
        cap.record_description(v["id"], "x", "y", "manual", "v1")
    assert code_of(e) == "E_WRONG_KIND"
    t = cap.capture_text("x", "mcp")
    with pytest.raises(wc_err) as e:
        cap.record_description(t["id"], "x", "y", "manual", "v1")
    assert code_of(e) == "E_WRONG_KIND"


def test_description_overwrites_previous_machine_text_but_logs_event(cap, media):
    """Characterization: prior description is replaced (only the event trail remains)."""
    i = cap.capture_media(media("a.png", PNG), "image", "mcp")
    cap.record_description(i["id"], "L", "first", "manual", "v1")
    cap.record_description(i["id"], "L", "second", "manual", "v1")
    s = cap.read_capture(i["id"])["sections"]
    assert s["Machine description"] == "second\n"
    assert s["Provenance events"].count("| described |") == 2


# ------------------------------------------------------- media integrity

def test_validate_detects_media_missing(cap, media):
    v = cap.capture_media(media("a.ogg", OGG), "voice", "mcp")
    cap._resolve_media(v["media"]).unlink()
    assert any(e.startswith("E_MEDIA_MISSING") for e in cap.validate_record(v["id"]))


def test_validate_detects_media_tamper(cap, media):
    v = cap.capture_media(media("a.ogg", OGG), "voice", "mcp")
    cap._resolve_media(v["media"]).write_bytes(OGG + b"x")
    assert any(e.startswith("E_HASH_MISMATCH") for e in cap.validate_record(v["id"]))


def test_validate_detects_text_tamper(cap):
    r = cap.capture_text("original", "mcp")
    p = cap.record_path(r["id"])
    p.write_text(p.read_text(encoding="utf-8").replace("original", "forged"), encoding="utf-8")
    assert any(e.startswith("E_HASH_MISMATCH") for e in cap.validate_record(r["id"]))


def test_validate_detects_missing_field_and_bad_schema(cap):
    r = cap.capture_text("x", "mcp")
    p = cap.record_path(r["id"])
    txt = p.read_text(encoding="utf-8")
    p.write_text(txt.replace("bytes:", "bytez:").replace(
        'schema_version: 1.0.0', 'schema_version: 9.9.9'), encoding="utf-8")
    errs = cap.validate_record(r["id"])
    assert any("E_MISSING_FIELD: bytes" in e for e in errs)
    assert any(e.startswith("E_BAD_SCHEMA") for e in errs)


def test_validate_detects_state_divergence(cap):
    r = cap.capture_text("x", "mcp")
    p = cap.record_path(r["id"])
    p.write_text(p.read_text(encoding="utf-8").replace("status: received", "status: reviewed", 1),
                 encoding="utf-8")
    assert any(e.startswith("E_STATE_DIVERGED") for e in cap.validate_record(r["id"]))


def test_validate_missing_record(cap):
    assert cap.validate_record("cap-20260101-000000-abcd")[0].startswith("E_NOT_FOUND")


# ------------------------------------------------------ orphan recovery

def test_orphan_media_is_adopted_as_processing_failed(cap, media):
    v = cap.capture_media(media("a.ogg", OGG), "voice", "mcp")
    rec = cap.record_path(v["id"])
    mp = cap._resolve_media(v["media"])
    rec.unlink()                                   # simulate crash between the two writes
    out = cap.recover_orphans()
    assert out["adopted"] == [v["id"]] and mp.is_file()
    fm = cap.read_capture(v["id"])["front_matter"]
    assert fm["status"] == "processing-failed"
    assert "recovered-orphan" in fm["quality_flags"]
    assert fm["sha256"] == sha(OGG)
    assert cap.validate_record(v["id"]) == []


def test_orphan_duplicate_is_quarantined_not_deleted(cap, media):
    a = cap.capture_media(media("a.ogg", OGG), "voice", "mcp")
    b = cap.capture_media(media("b.ogg", OGG), "voice", "mcp")
    cap.record_path(b["id"]).unlink()
    out = cap.recover_orphans()
    assert out["quarantined"] and out["quarantined"][0]["duplicate_of"] == a["id"]
    assert (cap.ORPHAN_QUARANTINE / cap._resolve_media(b["media"]).name).is_file()


def test_recovery_is_idempotent_and_ignores_tmp(cap, media):
    v = cap.capture_media(media("a.ogg", OGG), "voice", "mcp")
    (cap._resolve_media(v["media"]).parent / ".tmp-partial").write_bytes(b"junk")
    assert cap.recover_orphans() == {"ok": True, "adopted": [], "quarantined": []}
    cap.record_path(v["id"]).unlink()
    cap.recover_orphans()
    assert cap.recover_orphans() == {"ok": True, "adopted": [], "quarantined": []}


# ----------------------------------------------------------- traversal

@pytest.mark.parametrize("bad", ["../../etc/passwd", "cap-20260101-000000-zzzz",
                                 "cap-20260101-000000-abcd/../x", "", "CAP-1"])
def test_bad_ids_rejected(cap, bad):
    with pytest.raises(wc_err) as e:
        cap.read_capture(bad)
    assert code_of(e) in ("E_BAD_ID",)


def test_safe_within_rejects_escape(cap, tmp_path):
    with pytest.raises(wc_err) as e:
        cap._safe_within(tmp_path / "elsewhere.md", cap.CAPTURES_ROOT)
    assert code_of(e) == "E_TRAVERSAL"
    with pytest.raises(wc_err):
        cap._atomic_write_bytes(tmp_path / "elsewhere.md", b"x")
    assert not (tmp_path / "elsewhere.md").exists()


def test_validate_rejects_raw_media_traversal(cap):
    r = cap.capture_text("x", "mcp")
    p = cap.record_path(r["id"])
    p.write_text(p.read_text(encoding="utf-8").replace("raw_media: null", "raw_media: ../../secret.ogg"),
                 encoding="utf-8")
    assert any(e.startswith("E_TRAVERSAL") for e in cap.validate_record(r["id"]))


@pytest.mark.skipif(os.name == "nt", reason="symlink privileges")
def test_safe_within_resolves_symlink_escape(cap, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = cap.CAPTURES_ROOT / "escape"
    link.symlink_to(outside)
    with pytest.raises(wc_err) as e:
        cap._safe_within(link / "f.md", cap.CAPTURES_ROOT)
    assert code_of(e) == "E_TRAVERSAL"


# ---------------------------------------------------------- list / CLI

def test_list_filters_and_deterministic_order(cap, media):
    cap.capture_text("a", "mcp")
    cap.capture_media(media("a.ogg", OGG), "voice", "obsidian")
    all_rows = cap.list_captures()["captures"]
    assert [r["id"] for r in all_rows] == sorted(r["id"] for r in all_rows)
    assert cap.list_captures(kind="voice")["count"] == 1
    assert cap.list_captures(channel="mcp")["count"] == 1
    assert cap.list_captures(state="received")["count"] == 2
    assert cap.list_captures(state="reviewed")["count"] == 0


def test_cli_roundtrip(cap, capsys):
    assert cap.main(["--json", "capture-text", "--channel", "mcp", "--text", "سلام"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and cap.validate_record(out["id"]) == []
    assert cap.main(["set-state", out["id"], "processing", "--actor", ""]) == 1
    assert cap.main(["validate"]) == 0
