"""Characterization tests for the existing stdio MCP server (scripts/wiki_mcp_server.py).

They pin CURRENT behavior before any transport work (Agent A). They must not
be edited to "fix" behavior; a deliberate behavior change needs a new test.
All writes are redirected to tmp_path; nothing touches the repository.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "capture"))

import wiki_mcp_server as wms  # noqa: E402

EXPECTED_TOOLS = {
    "wiki_read", "wiki_search", "wiki_exact", "wiki_propose",
    "wiki_capture_text", "wiki_list_captures", "wiki_read_capture",
    "wiki_get_media", "wiki_search_captures", "wiki_propose_from_capture",
    "wiki_mark_capture_reviewed", "wiki_transcribe_capture",
}


def rpc(method, params=None, msg_id=1):
    msg = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        msg["params"] = params
    return wms.handle(msg)


def call(name, arguments):
    resp = rpc("tools/call", {"name": name, "arguments": arguments})
    assert resp["result"]["isError"] is False
    return json.loads(resp["result"]["content"][0]["text"])


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(wms, "ROOT", tmp_path)
    monkeypatch.setattr(wms, "PROPOSALS_DIR", tmp_path / "_proposals")
    monkeypatch.setattr(wms, "PROPOSALS_LOG", tmp_path / "_proposals" / "proposals.jsonl")
    cap = wms._cap
    monkeypatch.setattr(cap, "CAPTURES_ROOT", tmp_path / "01-inbox" / "captures")
    monkeypatch.setattr(cap, "ORPHAN_QUARANTINE", tmp_path / "01-inbox" / "captures" / ".quarantine")
    monkeypatch.setattr(cap, "REPO_ROOT", tmp_path)
    return tmp_path


def test_initialize_and_list_tools():
    init = rpc("initialize")
    assert init["result"]["protocolVersion"] == "2024-11-05"
    assert init["result"]["serverInfo"]["name"] == "wiki-governance"
    assert init["result"]["capabilities"] == {"tools": {}}
    listed = rpc("tools/list")["result"]["tools"]
    assert {t["name"] for t in listed} == EXPECTED_TOOLS
    for t in listed:
        assert t["inputSchema"]["type"] == "object"


def test_notifications_return_nothing():
    assert wms.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_exact_search_hit_and_miss(sandbox):
    obj = sandbox / "03-objects"
    obj.mkdir()
    (obj / "a.md").write_text("line one\nthe needle is here\n", encoding="utf-8")
    hit = call("wiki_exact", {"text": "needle"})
    assert [m["file"] for m in hit["matches"]] == ["03-objects/a.md"]
    assert hit["matches"][0]["line"] == 2
    assert call("wiki_exact", {"text": "absent-token"})["matches"] == []
    assert call("wiki_exact", {"text": "  "})["error"] == "empty text"


def test_lexical_search_via_stub_filters_captures(sandbox, monkeypatch):
    fixture = [
        {"file": "qmd://wiki/03-objects/a.md", "score": 0.9},
        {"file": "qmd://wiki/01-inbox/captures/2026/cap-x.md", "score": 0.8},
    ]
    monkeypatch.setattr(wms, "_qmd_query_lexical", lambda q, n: fixture)
    out = call("wiki_search", {"query": "anything", "n": 5})
    assert out["mode"] == "lexical-hybrid"
    assert [h["file"] for h in out["results"]] == ["qmd://wiki/03-objects/a.md"]
    assert "never" in out["authority_note"]


def test_search_reports_missing_qmd(sandbox, monkeypatch):
    monkeypatch.setattr(wms, "_qmd_query_lexical", lambda q, n: None)
    monkeypatch.setattr(wms.shutil, "which", lambda name: None)
    assert call("wiki_search", {"query": "x"}) == {"error": "qmd not found on PATH"}


def test_capture_text_roundtrip(sandbox):
    r = call("wiki_capture_text", {"text": "hello remote world", "channel": "mcp",
                                   "language_hint": "en"})
    assert r["ok"] is True and r["status"] == "received"
    assert (sandbox / "01-inbox" / "captures").exists()
    rec = call("wiki_read_capture", {"id": r["id"]})
    assert rec["id"] == r["id"]
    listed = call("wiki_list_captures", {})
    assert r["id"] in json.dumps(listed)
    found = call("wiki_search_captures", {"query": "remote world"})
    assert [h["id"] for h in found["hits"]] == [r["id"]]


def test_capture_text_rejects_bad_input(sandbox):
    assert call("wiki_capture_text", {"text": "   "})["code"] == "E_EMPTY"
    assert call("wiki_capture_text", {"text": "x", "channel": "nope"})["code"] == "E_BAD_CHANNEL"
    assert call("wiki_capture_text", {"text": "x", "language_hint": "zz"})["code"] == "E_BAD_LANG"


def test_propose_appends_candidate_record(sandbox):
    r = call("wiki_propose", {"kind": "object-note", "body": "note body"})
    assert r["accepted"] is True
    lines = (sandbox / "_proposals" / "proposals.jsonl").read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[-1])
    assert rec["authority_tier"] == "candidate" and rec["adjudication"] is None
    assert rec["id"] == r["proposal_id"]


def test_propose_rejects_bad_kind_and_empty_body(sandbox):
    assert "unsupported kind" in call("wiki_propose", {"kind": "x", "body": "b"})["error"]
    assert call("wiki_propose", {"kind": "object-note", "body": " "})["error"] == "empty proposal body"


def test_read_rejects_traversal_and_non_md(sandbox):
    assert "rejected" in call("wiki_read", {"path": "../etc/passwd"})["error"]
    assert "rejected" in call("wiki_read", {"path": "secret.txt"})["error"]
    (sandbox / "n.md").write_text("body", encoding="utf-8")
    assert call("wiki_read", {"path": "n.md"})["content"] == "body"


def test_error_paths():
    unk = rpc("tools/call", {"name": "nope", "arguments": {}})
    assert unk["error"]["code"] == -32602
    assert rpc("bogus/method")["error"]["code"] == -32601


def test_tool_exception_is_wrapped_not_raised(monkeypatch):
    def boom(_args):
        raise RuntimeError("kaboom")
    monkeypatch.setitem(wms.DISPATCH, "wiki_read", boom)
    resp = rpc("tools/call", {"name": "wiki_read", "arguments": {}})
    # Current behavior: isError stays False and the error is inside the text payload.
    assert resp["result"]["isError"] is False
    assert "RuntimeError: kaboom" in resp["result"]["content"][0]["text"]


def test_stdio_process_end_to_end():
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "wiki_mcp_server.py")],
        input="\n".join(json.dumps(m) for m in msgs) + "\n",
        text=True, capture_output=True, timeout=30,
    )
    assert proc.returncode == 0
    out = [json.loads(line) for line in proc.stdout.splitlines()]
    assert [o["id"] for o in out] == [1, 2]
    assert {t["name"] for t in out[1]["result"]["tools"]} == EXPECTED_TOOLS
