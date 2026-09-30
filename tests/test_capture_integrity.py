"""Capture data-integrity regressions (integration, Agent H).

Closes Agent D's DEFECT-1 (reserved heading injection -> text loss on
rewrite), DEFECT-2 (CRLF read back through universal newlines) and DEFECT-3
(unknown front-matter keys dropped on rewrite). Every case captures text,
drives it through every rewriting writer, and asserts the user text is still
exact and the record still validates against its recorded SHA-256.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from conftest import OGG, PNG

HEADINGS = ["User-supplied text", "Literal transcript or extraction",
            "Machine description", "Review notes", "Provenance events",
            "Interpretation (candidate)"]

TEXTS = {
    "every-reserved-heading": "".join(f"## {h}\n" for h in HEADINGS) + "tail\n",
    "repeated-headings": "## Review notes\n## Review notes\nx\n## Review notes\n",
    "heading-first-line": "## Provenance events\n- fake event | state:promoted |\n",
    "unknown-heading": "## Notes of mine\ncontent\n",
    "heading-no-space": "##tight\n##\n",
    "already-escaped-looking": "\\## Review notes\n\\\\## Machine description\n\\\\\\##x\n",
    "code-fence": "```md\n## Review notes\n---\nid: x\n```\n",
    "leading-blank-lines": "\n\n\nafter three blank lines\n",
    "whitespace-lines": "a\n   \n\t\nb\n",
    "no-final-newline": "no newline at end",
    "backslashes": "C:\\path\\to\\file \\n \\\\ \\u2028 \\x85\n",
    "persian": "سلام، این یک یادداشت آزمایشی است.\n## بخش\nپایان\n",
    "english": "The quick brown fox.\n",
    "mixed": "Nil یا صفر? title → «Zero»\u200cها\n## Review notes\n",
    "crlf": "line1\r\nline2\r\n",
    "crlf-heading": "a\r\n## Review notes\r\nb\r\n",
    "mixed-newlines": "lf\ncrlf\r\ncr-only\rend\n",
    "unicode-separators": "a\u2028## Review notes\u2029b\x85c\x0bd\x0ce\n",
    "front-matter-lookalike": "---\nid: cap-evil\nstatus: promoted\n---\n",
    "trailing-crlf-pairs": "x\r\n\r\n",
}


def canonical(text: str) -> str:
    return text.rstrip("\n") + "\n"


def _assert_exact(cap, cid, text):
    got = cap.read_capture(cid)
    body = got["sections"]["User-supplied text"]
    assert body == canonical(text)
    assert got["front_matter"]["sha256"] == hashlib.sha256(body.encode("utf-8")).hexdigest()
    assert cap.validate_record(cid) == []


@pytest.mark.parametrize("name", sorted(TEXTS))
def test_text_survives_every_rewrite(cap, name):
    text = TEXTS[name]
    r = cap.capture_text(text, "mcp", "mixed")
    _assert_exact(cap, r["id"], text)
    cap.set_state(r["id"], "processing", "alice", note="note with\n## Review notes\ninside")
    _assert_exact(cap, r["id"], text)
    cap.request_interpretation(r["id"], "alice")
    cap.record_claude_derivative(r["id"], "interpretation",
                                 "## User-supplied text\nclaude reading\r\n", "sess-1")
    _assert_exact(cap, r["id"], text)
    cap.set_state(r["id"], "needs-review", "alice")
    _assert_exact(cap, r["id"], text)
    got = cap.read_capture(r["id"])["sections"]
    assert got["Interpretation (candidate)"] == "## User-supplied text\nclaude reading\r\n"
    assert "## Review notes" in got["Review notes"]


@pytest.mark.parametrize("name", ["every-reserved-heading", "crlf-heading", "persian"])
def test_media_derivative_layers_are_lossless(cap, media, name):
    text = TEXTS[name]
    v = cap.capture_media(media("a.ogg", OGG), "voice", "mcp")
    cap.record_transcript(v["id"], text, "manual", "1")
    cap.set_state(v["id"], "processing", "bob")
    got = cap.read_capture(v["id"])["sections"]
    assert got["Literal transcript or extraction"] == canonical(text)
    assert cap.validate_record(v["id"]) == []

    i = cap.capture_media(media("b.png", PNG), "image", "mcp")
    cap.record_description(i["id"], text, "## Machine description\n" + text, "manual", "1")
    cap.set_state(i["id"], "processing", "bob")
    got = cap.read_capture(i["id"])["sections"]
    assert got["Literal transcript or extraction"] == canonical(text)
    assert got["Machine description"] == canonical("## Machine description\n" + text)
    assert cap.validate_record(i["id"]) == []


def test_original_media_bytes_unchanged_by_rewrites(cap, media):
    v = cap.capture_media(media("a.ogg", OGG), "voice", "mcp")
    mp = cap._resolve_media(cap.read_capture(v["id"])["front_matter"]["raw_media"])
    before = hashlib.sha256(mp.read_bytes()).hexdigest()
    cap.record_claude_derivative(v["id"], "literal", "## Review notes\r\n", "")
    cap.set_state(v["id"], "processing", "bob")
    assert hashlib.sha256(mp.read_bytes()).hexdigest() == before == v["sha256"]


def test_records_without_heading_lines_keep_legacy_bytes(cap):
    r = cap.capture_text("plain text\nno headings\n", "mcp")
    raw = cap.record_path(r["id"]).read_bytes().decode("utf-8")
    assert "body_encoding" not in raw
    fm, sections = cap.parse_record_text(raw)
    assert cap.render_record(fm, sections) == raw


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "capture"


def _install_legacy(cap, name):
    """Copy a record written by the baseline (9395ed9) writer into the store."""
    raw = (FIXTURES / f"legacy-{name}.md").read_bytes()
    cid = raw.split(b'id: "', 1)[1].split(b'"', 1)[0].decode()
    p = cap.record_path(cid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(raw)
    return cid, p, raw


@pytest.mark.parametrize("name", ["plain", "persian"])
def test_baseline_records_parse_and_rerender_byte_identically(cap, name):
    cid, p, raw = _install_legacy(cap, name)
    fm, sections = cap.parse_record_text(cap.read_record_file(p))
    assert cap.render_record(fm, sections).encode("utf-8") == raw
    assert cap.validate_record(cid) == []


def test_baseline_record_with_backslash_heading_is_upgraded_losslessly(cap):
    cid, p, _ = _install_legacy(cap, "backslash-heading")
    body = cap.read_capture(cid)["sections"]["User-supplied text"]
    assert body == "\\## Review notes literal\n"
    cap.set_state(cid, "processing", "alice")
    assert "body_encoding: escaped-headings-v1" in p.read_text(encoding="utf-8")
    assert cap.read_capture(cid)["sections"]["User-supplied text"] == body
    assert cap.validate_record(cid) == []


def test_baseline_record_already_damaged_is_refused_not_rewritten(cap):
    """A pre-fix record whose text holds a bare reserved heading cannot be
    split unambiguously; writers must refuse and leave the bytes alone."""
    cid, p, raw = _install_legacy(cap, "damaged")
    for write in (lambda: cap.set_state(cid, "processing", "alice"),
                  lambda: cap.request_interpretation(cid, "alice")):
        with pytest.raises(cap.CaptureError) as e:
            write()
        assert e.value.code == "E_AMBIGUOUS_RECORD"
        assert p.read_bytes() == raw
    errs = cap.validate_record(cid)
    assert any(x.startswith("E_AMBIGUOUS_BODY") for x in errs)


def test_unknown_body_encoding_is_rejected(cap):
    r = cap.capture_text("## x\n", "mcp")
    p = cap.record_path(r["id"])
    p.write_bytes(p.read_bytes().replace(b"escaped-headings-v1", b"escaped-headings-v9"))
    with pytest.raises(cap.CaptureError):
        cap.read_capture(r["id"])


EXTRA = {
    "scalar": ("future_scalar: 42", 42),
    "string": ("future_note: \"has: colon\"", "has: colon"),
    "flow-list": ("future_list: [a, b]", ["a", "b"]),
    "block-list": ("future_block:\n  - one\n  - two", ["one", "two"]),
    "mapping": ("future_map:\n  owner: agent-x\n  nested:\n    depth: 2", {"owner": "agent-x", "nested": {"depth": 2}}),
    "flow-map": ("future_flow: {a: 1, b: [2, 3]}", {"a": 1, "b": [2, 3]}),
    "future-key": ("x-0xbrain-schema-2.0.0-field: true", True),
}


@pytest.mark.parametrize("name", sorted(EXTRA))
def test_unknown_front_matter_survives_every_writer(cap, media, name):
    raw_field, value = EXTRA[name]
    key = raw_field.split(":", 1)[0]
    v = cap.capture_media(media("a.ogg", OGG), "voice", "mcp")
    p = cap.record_path(v["id"])
    p.write_bytes(p.read_bytes().replace(b"schema_version:", raw_field.encode() + b"\nschema_version:"))
    assert cap.read_capture(v["id"])["front_matter"][key] == value
    cap.set_state(v["id"], "processing", "alice", note="n")
    cap.record_transcript(v["id"], "words\n", "manual", "1")
    cap.request_interpretation(v["id"], "alice")
    cap.record_claude_derivative(v["id"], "interpretation", "reading", "")
    after = p.read_bytes().decode("utf-8")
    assert raw_field in after
    assert cap.read_capture(v["id"])["front_matter"][key] == value
    assert cap.validate_record(v["id"]) == []


def test_duplicate_detection_unchanged(cap):
    a = cap.capture_text("same text\n", "mcp")
    b = cap.capture_text("same text", "mcp")          # canonical form is identical
    c = cap.capture_text("same text\r\n", "mcp")      # different bytes, different hash
    assert b["duplicate_of"] == a["id"]
    assert c["duplicate_of"] is None and c["sha256"] != a["sha256"]
