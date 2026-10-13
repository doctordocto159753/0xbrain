"""AGENT-0: the extension is wired into the remote adapter and nothing else changes."""
from __future__ import annotations

import json

from brain_enrichment import tools_spec as T
from brain_surface import TOOL_NAMES
from remote_mcp import adapter as A


def names(tools):
    return tuple(t.name for t in tools)


def test_off_is_exactly_the_seven(brain_wiki, monkeypatch):
    for env in ({}, {"BRAIN_ENRICHMENT": "off"}, {"BRAIN_ENRICHMENT": "shadow", "BRAIN_ENRICHMENT_WRITES": "1"},
                {"BRAIN_ENRICHMENT": "garbage"}):
        assert names(A.semantic_adapter(brain_wiki, env=env)) == A.SEMANTIC_TOOLS == TOOL_NAMES


def test_on_is_seven_then_four_and_filtering_keeps_them(brain_wiki):
    tools = A.semantic_adapter(brain_wiki, env={"BRAIN_ENRICHMENT": "on"})
    assert names(tools) == A.SEMANTIC_TOOLS + T.EXTENSION_TOOL_NAMES
    assert names(A.filter_remote(tools)) == names(tools)
    for t in tools[7:]:
        assert t.input_schema["additionalProperties"] is False
    assert json.dumps([t.input_schema for t in tools[:7]], sort_keys=True) == \
        json.dumps([t["inputSchema"] for t in __import__("brain_surface").TOOLS], sort_keys=True)


def test_on_tools_work_through_the_adapter_and_unknown_args_are_rejected(enrichment_wiki):
    tools = {t.name: t for t in A.semantic_adapter(enrichment_wiki.root, env={"BRAIN_ENRICHMENT": "on"})}
    bad = tools["brain_retrieve"].handler({"query": "x", "path": "/etc/passwd"})
    assert bad["ok"] is False and bad["error"]["code"] == "E_UNKNOWN_ARGUMENT"
    assert tools["brain_enrichment_submit"].handler({"source_ref": "cap:cap-1"})["persisted"] is False
    st = tools["brain_enrichment_pending"].handler({"limit": 5})
    assert st["ok"] is True and st["items"], st
    assert all(i["source_kind"] in ("capture-text", "capture-media", "ingested-file") for i in st["items"])


def test_extension_import_failure_leaves_the_seven(brain_wiki, monkeypatch):
    import builtins
    real = builtins.__import__

    def boom(name, *a, **k):
        if name.startswith("brain_enrichment"):
            raise ImportError(name)
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", boom)
    assert names(A.semantic_adapter(brain_wiki, env={"BRAIN_ENRICHMENT": "on"})) == A.SEMANTIC_TOOLS
