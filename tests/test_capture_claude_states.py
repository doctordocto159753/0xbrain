"""Phase 2 tests (Agent D): additive interpretation-gap metadata and
Claude-produced candidate derivatives. Legacy compatibility is tested first."""
from __future__ import annotations

import importlib.util
import json
import re
import sys

import pytest

from conftest import OGG, PNG, PDF

wc_err = pytest.importorskip("wiki_capture").CaptureError


def code_of(exc):
    return exc.value.code


def _strip_k4(cap, cid):
    """Rewrite a fresh record into the exact pre-K4 (baseline) on-disk shape."""
    p = cap.record_path(cid)
    txt = p.read_text(encoding="utf-8")
    txt = re.sub(r"^(interpretation_needs|derivative_methods|derivative_tier):.*\n", "",
                 txt, flags=re.M)
    p.write_text(txt, encoding="utf-8")
    return p


# ------------------------------------------------- legacy compatibility

def test_legacy_record_without_new_fields_validates(cap, media):
    r = cap.capture_media(media("a.ogg", OGG), "voice", "mcp")
    _strip_k4(cap, r["id"])
    assert cap.validate_record(r["id"]) == []


def test_legacy_record_rewrite_adds_no_new_fields_or_sections(cap, media):
    r = cap.capture_media(media("a.ogg", OGG), "voice", "mcp")
    p = _strip_k4(cap, r["id"])
    cap.set_state(r["id"], "processing", "alice")
    txt = p.read_text(encoding="utf-8")
    assert "interpretation_needs" not in txt and "derivative_" not in txt
    assert "Interpretation (candidate)" not in txt
    assert cap.validate_record(r["id"]) == []


def test_render_is_byte_stable_for_legacy_shape(cap):
    r = cap.capture_text("سلام", "mcp", "fa")
    p = _strip_k4(cap, r["id"])
    fm, sections = cap.parse_record_text(p.read_text(encoding="utf-8"))
    assert cap.render_record(fm, sections) == p.read_text(encoding="utf-8")


def test_legacy_gaps_are_computed_not_stored(cap, media):
    r = cap.capture_media(media("a.ogg", OGG), "voice", "mcp")
    _strip_k4(cap, r["id"])
    row = cap.list_captures(needs="needs_transcription")["captures"]
    assert [x["id"] for x in row] == [r["id"]]
    cap.record_transcript(r["id"], "hello", "manual", "v1")
    assert cap.list_captures(needs="needs_transcription")["count"] == 0


def test_schema_version_unchanged(cap):
    r = cap.capture_text("x", "mcp")
    assert cap.read_capture(r["id"])["front_matter"]["schema_version"] == "1.0.0"


# ------------------------------------------------------- gap metadata

@pytest.mark.parametrize("name,data,kind,expected", [
    ("a.ogg", OGG, "voice", ["needs_transcription"]),
    ("a.png", PNG, "image", ["needs_description"]),
    ("a.png", PNG, "drawing", ["needs_description"]),
    ("a.png", PNG, "handwriting", ["needs_transcription", "needs_description"]),
    ("a.pdf", PDF, "file", []),
])
def test_initial_gaps_by_kind(cap, media, name, data, kind, expected):
    r = cap.capture_media(media(name, data), kind, "mcp")
    fm = cap.read_capture(r["id"])["front_matter"]
    assert fm["interpretation_needs"] == expected
    assert cap.validate_record(r["id"]) == []


def test_text_capture_has_no_gaps(cap):
    r = cap.capture_text("x", "mcp")
    assert cap.read_capture(r["id"])["front_matter"]["interpretation_needs"] == []


def test_manual_transcript_clears_transcription_gap_and_records_method(cap, media):
    r = cap.capture_media(media("a.ogg", OGG), "voice", "mcp")
    cap.record_transcript(r["id"], "hi", "manual", "v1")
    fm = cap.read_capture(r["id"])["front_matter"]
    assert fm["interpretation_needs"] == []
    assert fm["derivative_methods"] == ["literal:manual"]
    assert fm["derivative_tier"] == "candidate"


def test_description_and_literal_clear_their_own_gaps(cap, media):
    r = cap.capture_media(media("a.png", PNG), "handwriting", "mcp")
    cap.record_description(r["id"], "LITERAL", None, "manual", "v1")
    assert cap.read_capture(r["id"])["front_matter"]["interpretation_needs"] == ["needs_description"]
    cap.record_description(r["id"], None, "DESC", "manual", "v1")
    fm = cap.read_capture(r["id"])["front_matter"]
    assert fm["interpretation_needs"] == []
    assert fm["derivative_methods"] == ["description:manual", "literal:manual"]


def test_list_needs_filter_and_bad_value(cap, media):
    cap.capture_text("t", "mcp")
    v = cap.capture_media(media("a.ogg", OGG), "voice", "mcp")
    i = cap.capture_media(media("a.png", PNG), "image", "mcp")
    assert [x["id"] for x in cap.list_captures(needs="needs_transcription")["captures"]] == [v["id"]]
    assert [x["id"] for x in cap.list_captures(needs="needs_description")["captures"]] == [i["id"]]
    assert cap.list_captures(needs="needs_interpretation")["count"] == 0
    with pytest.raises(wc_err) as e:
        cap.list_captures(needs="bogus")
    assert code_of(e) == "E_BAD_NEEDS"


def test_request_interpretation_flow(cap):
    r = cap.capture_text("a rough idea", "mcp")
    with pytest.raises(wc_err) as e:
        cap.request_interpretation(r["id"], " ")
    assert code_of(e) == "E_NO_ACTOR"
    out = cap.request_interpretation(r["id"], "alice")
    assert out["interpretation_needs"] == ["needs_interpretation"]
    assert cap.list_captures(needs="needs_interpretation")["count"] == 1
    assert cap.read_capture(r["id"])["front_matter"]["status"] == "received"   # state untouched
    cap.record_claude_derivative(r["id"], "interpretation", "Likely about X.")
    assert cap.list_captures(needs="needs_interpretation")["count"] == 0
    assert cap.validate_record(r["id"]) == []
    with pytest.raises(wc_err) as e:
        cap.request_interpretation(r["id"], "alice")
    assert code_of(e) == "E_LAYER_EXISTS"


# --------------------------------------------- Claude candidate derivative

def test_claude_transcript_is_candidate_unreviewed_and_state_neutral(cap, media):
    v = cap.capture_media(media("a.ogg", OGG), "voice", "mcp", "fa")
    out = cap.record_claude_derivative(v["id"], "literal", "سلام دنیا", producer_ref="session-1")
    assert out["method"] == "claude" and out["tier"] == "candidate"
    assert out["human_reviewed"] is False
    rec = cap.read_capture(v["id"])
    fm, s = rec["front_matter"], rec["sections"]
    assert s["Literal transcript or extraction"] == "سلام دنیا\n"
    assert fm["transcription_state"] == "complete"
    assert fm["transcription_method"] == "claude"
    assert fm["transcription_reviewed"] is False
    assert fm["derivative_methods"] == ["literal:claude"]
    assert fm["derivative_tier"] == "candidate"
    assert fm["status"] == "received"
    assert fm["interpretation_needs"] == []
    assert "derivative:literal" in s["Provenance events"]
    assert "method:claude" in s["Provenance events"] and "ref:session-1" in s["Provenance events"]
    assert cap.validate_record(v["id"]) == []


def test_layers_stay_in_separate_sections(cap, media):
    i = cap.capture_media(media("a.png", PNG), "image", "mcp")
    cap.record_claude_derivative(i["id"], "literal", "INVOICE 42")
    cap.record_claude_derivative(i["id"], "description", "A scanned invoice")
    cap.record_claude_derivative(i["id"], "interpretation", "Probably a Q3 expense")
    s = cap.read_capture(i["id"])["sections"]
    assert s["Literal transcript or extraction"] == "INVOICE 42\n"
    assert s["Machine description"] == "A scanned invoice\n"
    assert s["Interpretation (candidate)"] == "Probably a Q3 expense\n"
    for a, b in [("Literal transcript or extraction", "Interpretation (candidate)"),
                 ("Machine description", "Interpretation (candidate)"),
                 ("Literal transcript or extraction", "Machine description")]:
        assert s[a] not in s[b]
    fm = cap.read_capture(i["id"])["front_matter"]
    assert fm["derivative_methods"] == ["description:claude", "interpretation:claude", "literal:claude"]
    assert cap.validate_record(i["id"]) == []


def test_claude_derivative_is_write_once(cap, media):
    v = cap.capture_media(media("a.ogg", OGG), "voice", "mcp")
    cap.record_claude_derivative(v["id"], "literal", "first")
    with pytest.raises(wc_err) as e:
        cap.record_claude_derivative(v["id"], "literal", "second")
    assert code_of(e) == "E_LAYER_EXISTS"
    assert cap.read_capture(v["id"])["sections"]["Literal transcript or extraction"] == "first\n"


def test_claude_never_overwrites_human_text(cap, media):
    v = cap.capture_media(media("a.ogg", OGG), "voice", "mcp")
    cap.record_transcript(v["id"], "human words", "manual", "v1", reviewed=True)
    before = cap.record_path(v["id"]).read_bytes()
    with pytest.raises(wc_err) as e:
        cap.record_claude_derivative(v["id"], "literal", "claude words")
    assert code_of(e) == "E_LAYER_EXISTS"
    assert cap.record_path(v["id"]).read_bytes() == before


@pytest.mark.parametrize("adapter", ["claude", "Claude", " CLAUDE "])
def test_legacy_writers_cannot_launder_claude_output(cap, media, adapter):
    v = cap.capture_media(media("a.ogg", OGG), "voice", "mcp")
    with pytest.raises(wc_err) as e:
        cap.record_transcript(v["id"], "x", adapter, "v1", reviewed=True)
    assert code_of(e) == "E_USE_CLAUDE_DERIVATIVE"
    i = cap.capture_media(media("a.png", PNG), "image", "mcp")
    with pytest.raises(wc_err) as e:
        cap.record_description(i["id"], "x", "y", adapter, "v1")
    assert code_of(e) == "E_USE_CLAUDE_DERIVATIVE"


def test_claude_has_no_path_to_human_review_or_promotion(cap, media):
    v = cap.capture_media(media("a.ogg", OGG), "voice", "mcp")
    cap.record_claude_derivative(v["id"], "literal", "x")
    assert cap.read_capture(v["id"])["front_matter"]["transcription_reviewed"] is False
    with pytest.raises(wc_err) as e:
        cap.set_state(v["id"], "reviewed", "")
    assert code_of(e) == "E_NO_ACTOR"
    with pytest.raises(wc_err) as e:
        cap.set_state(v["id"], "reviewed", "alice")        # received -> reviewed illegal
    assert code_of(e) == "E_BAD_TRANSITION"


def test_derivative_does_not_touch_original_or_hash(cap, media):
    v = cap.capture_media(media("a.ogg", OGG), "voice", "mcp")
    before = cap.read_capture(v["id"])["front_matter"]
    cap.record_claude_derivative(v["id"], "literal", "x")
    cap.record_claude_derivative(v["id"], "interpretation", "y")
    fm = cap.read_capture(v["id"])["front_matter"]
    assert (fm["sha256"], fm["bytes"], fm["raw_media"]) == (
        before["sha256"], before["bytes"], before["raw_media"])
    assert cap._resolve_media(fm["raw_media"]).read_bytes() == OGG


def test_text_capture_interpretation_leaves_user_text_intact(cap):
    r = cap.capture_text("متن اصلی من", "mcp", "fa")
    cap.record_claude_derivative(r["id"], "interpretation", "تفسیر پیشنهادی")
    s = cap.read_capture(r["id"])["sections"]
    assert s["User-supplied text"] == "متن اصلی من\n"
    assert s["Interpretation (candidate)"] == "تفسیر پیشنهادی\n"
    assert cap.validate_record(r["id"]) == []


def test_state_change_after_derivative_preserves_it(cap, media):
    v = cap.capture_media(media("a.ogg", OGG), "voice", "mcp")
    cap.record_claude_derivative(v["id"], "literal", "x")
    cap.record_claude_derivative(v["id"], "interpretation", "y")
    cap.set_state(v["id"], "processing", "alice")
    rec = cap.read_capture(v["id"])
    assert rec["sections"]["Interpretation (candidate)"] == "y\n"
    assert rec["front_matter"]["derivative_methods"] == ["interpretation:claude", "literal:claude"]
    assert cap.validate_record(v["id"]) == []


@pytest.mark.parametrize("layer,kind_args,code", [
    ("literal", ("text",), "E_WRONG_KIND"),
    ("description", ("text",), "E_WRONG_KIND"),
    ("description", ("voice",), "E_WRONG_KIND"),
    ("bogus", ("voice",), "E_BAD_LAYER"),
])
def test_claude_derivative_rejections(cap, media, layer, kind_args, code):
    if kind_args[0] == "text":
        cid = cap.capture_text("t", "mcp")["id"]
    else:
        cid = cap.capture_media(media("a.ogg", OGG), "voice", "mcp")["id"]
    before = cap.record_path(cid).read_bytes()
    with pytest.raises(wc_err) as e:
        cap.record_claude_derivative(cid, layer, "x")
    assert code_of(e) == code
    assert cap.record_path(cid).read_bytes() == before


def test_empty_derivative_rejected(cap, media):
    v = cap.capture_media(media("a.ogg", OGG), "voice", "mcp")
    with pytest.raises(wc_err) as e:
        cap.record_claude_derivative(v["id"], "literal", "  \n")
    assert code_of(e) == "E_EMPTY"


def test_producer_ref_cannot_forge_event_lines(cap, media):
    v = cap.capture_media(media("a.ogg", OGG), "voice", "mcp")
    cap.record_claude_derivative(v["id"], "literal", "x",
                                 producer_ref="s\n- 2026 | state:reviewed | x | ok | actor:bob")
    assert cap.validate_record(v["id"]) == []
    assert cap.read_capture(v["id"])["front_matter"]["status"] == "received"


def test_claude_derivative_failed_write_is_atomic(cap, media, monkeypatch):
    import os
    v = cap.capture_media(media("a.ogg", OGG), "voice", "mcp")
    before = cap.record_path(v["id"]).read_bytes()
    with monkeypatch.context() as m:
        m.setattr(os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
        with pytest.raises(OSError):
            cap.record_claude_derivative(v["id"], "literal", "x")
    assert cap.record_path(v["id"]).read_bytes() == before


# ----------------------------------------------------------- validator

def _edit(cap, cid, old, new):
    p = cap.record_path(cid)
    p.write_text(p.read_text(encoding="utf-8").replace(old, new, 1), encoding="utf-8")


def test_validator_flags_bad_needs_tier_and_method(cap):
    r = cap.capture_text("x", "mcp")
    _edit(cap, r["id"], "interpretation_needs: []", "interpretation_needs: [needs_magic]")
    assert any(e.startswith("E_BAD_NEEDS") for e in cap.validate_record(r["id"]))
    r2 = cap.capture_text("y", "mcp")
    _edit(cap, r2["id"], "schema_version:", "derivative_tier: reviewed\nderivative_methods: [oops]\nschema_version:")
    errs = cap.validate_record(r2["id"])
    assert any(e.startswith("E_BAD_TIER") for e in errs)
    assert any(e.startswith("E_BAD_DERIVATIVE_METHOD") for e in errs)


def test_validator_flags_unattributed_interpretation(cap):
    r = cap.capture_text("x", "mcp")
    cap.record_claude_derivative(r["id"], "interpretation", "y")
    _edit(cap, r["id"], 'derivative_methods: ["interpretation:claude"]', "derivative_methods: []")
    assert any(e.startswith("E_UNATTRIBUTED_DERIVATIVE") for e in cap.validate_record(r["id"]))


def test_derivative_methods_roundtrip_through_parser(cap):
    fm = {"derivative_methods": ["literal:claude", "description:faster-whisper"]}
    line = cap._yaml_val(fm["derivative_methods"])
    assert cap._yaml_parse_val(line) == fm["derivative_methods"]


# --------------------------------------------------------------- CLI

def test_cli_add_derivative_and_list_needs(cap, media, capsys):
    v = cap.capture_media(media("a.ogg", OGG), "voice", "mcp")
    assert cap.main(["list", "--needs", "needs_transcription"]) == 0
    assert v["id"] in capsys.readouterr().out
    assert cap.main(["--json", "add-derivative", "--id", v["id"], "--layer", "literal",
                     "--text", "hello", "--producer-ref", "cli"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["method"] == "claude" and out["human_reviewed"] is False
    assert cap.main(["list", "--needs", "needs_transcription"]) == 0
    assert v["id"] not in capsys.readouterr().out
    assert cap.main(["add-derivative", "--id", v["id"], "--layer", "literal", "--text", "again"]) == 1
    assert cap.main(["request-interpretation", "--id", v["id"], "--actor", ""]) == 1


# --------------------------------- optional models are off by default

def test_engines_not_imported_and_auto_paths_refuse(cap, media, monkeypatch):
    import importlib
    from pathlib import Path
    scripts = Path(__file__).resolve().parents[1] / "scripts" / "capture"
    monkeypatch.syspath_prepend(str(scripts))
    stt = importlib.import_module("stt_contract")
    ocr = importlib.import_module("ocr_contract")
    for heavy in ("faster_whisper", "rapidocr_onnxruntime", "onnxruntime", "torch", "whisper"):
        assert heavy not in sys.modules
    monkeypatch.setattr(stt, "BENCHMARK_PASS_FILE", scripts / "no-such-benchmark.json")
    ok, _ = stt.auto_transcription_allowed()
    assert ok is False
    v = cap.capture_media(media("a.ogg", OGG), "voice", "mcp")
    with pytest.raises(wc_err) as e:
        stt.transcribe_capture(v["id"], "faster-whisper")
    assert code_of(e) == "E_AUTO_STT_DISABLED"
    with pytest.raises(wc_err) as e:
        stt.transcribe_capture(v["id"], "manual", remote_provider="anything", text="x")
    assert code_of(e) == "E_REMOTE_REFUSED"
    with pytest.raises(wc_err) as e:
        stt.transcribe_capture(v["id"], "claude")
    assert code_of(e) == "E_UNKNOWN_ADAPTER"
    assert cap.read_capture(v["id"])["front_matter"]["transcription_state"] == "pending"
    assert set(ocr.ADAPTERS) == {"rapidocr"} or "rapidocr" in ocr.ADAPTERS


@pytest.mark.skipif(importlib.util.find_spec("rapidocr_onnxruntime") is not None,
                    reason="engine installed on this host")
def test_ocr_extract_without_engine_leaves_record_unchanged(cap, media, monkeypatch):
    from pathlib import Path
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts" / "capture"))
    import importlib as il
    ocr = il.import_module("ocr_contract")
    i = cap.capture_media(media("a.png", PNG), "image", "mcp")
    before = cap.record_path(i["id"]).read_bytes()
    with pytest.raises(wc_err) as e:
        ocr.extract_capture(i["id"])
    assert code_of(e) == "E_ENGINE_UNAVAILABLE"
    assert cap.record_path(i["id"]).read_bytes() == before
