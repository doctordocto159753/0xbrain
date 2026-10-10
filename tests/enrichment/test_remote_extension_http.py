"""AGENT-0: the four extension tools over the real HTTP/OAuth transport.
Claude's path (base brain_* tools) and the enrichment tools share refs and text."""
from __future__ import annotations

import pytest

pytest.importorskip("mcp")
pytest.importorskip("uvicorn")

from test_mcp_remote import MemStorage, Running, run, text_of, with_session  # noqa: E402

from brain_enrichment import tools_spec as T  # noqa: E402
from remote_mcp.adapter import SEMANTIC_TOOLS, semantic_adapter  # noqa: E402

ENV = {"BRAIN_ENRICHMENT": "on", "BRAIN_ENRICHMENT_WRITES": "1", "BRAIN_ENRICHMENT_RETRIEVAL": "1"}


def test_extension_tools_work_over_http_with_shared_refs(enrichment_wiki):
    w = enrichment_wiki
    ref = w.refs["fa_short"]
    quote = "پیاده‌روی"

    async def scenario(s):
        out = {"tools": [t.name for t in (await s.list_tools()).tools]}
        out["base_read"] = text_of(await s.call_tool("brain_read", {"ref": ref}))
        pend = text_of(await s.call_tool("brain_enrichment_pending", {"limit": 5}))
        out["pending"] = pend
        item = next(i for i in pend["items"] if i["source_ref"] == ref)
        chunk = item["page"]["chunks"][0]
        out["submit"] = text_of(await s.call_tool("brain_enrichment_submit", {
            "source_ref": ref, "source_version": item["source_version"],
            "expected_annotation_revision": item["expected_annotation_revision"],
            "idempotency_key": "http-test-0001",
            "annotation": {"summary": "برنامهٔ ورزشی", "tags": ["fitness"],
                           "chunk_annotations": [{"chunk_ref": chunk["chunk_ref"], "summary": "پیاده‌روی و تمرین"}],
                           "entities": [{"name": "فیت پلن", "kind": "project",
                                         "mention": {"chunk_ref": chunk["chunk_ref"], "text": "فیت پلن"}}]}}))
        out["replay"] = text_of(await s.call_tool("brain_enrichment_submit", {
            "source_ref": ref, "source_version": item["source_version"],
            "expected_annotation_revision": item["expected_annotation_revision"],
            "idempotency_key": "http-test-0001",
            "annotation": {"summary": "برنامهٔ ورزشی", "tags": ["fitness"],
                           "chunk_annotations": [{"chunk_ref": chunk["chunk_ref"], "summary": "پیاده‌روی و تمرین"}],
                           "entities": [{"name": "فیت پلن", "kind": "project",
                                         "mention": {"chunk_ref": chunk["chunk_ref"], "text": "فیت پلن"}}]}}))
        out["forged"] = text_of(await s.call_tool("brain_enrichment_submit", {
            "source_ref": ref, "source_version": item["source_version"],
            "expected_annotation_revision": 1, "idempotency_key": "http-test-0002",
            "annotation": {"links": [{"relation": "about", "target": {"entity": "x"},
                                      "evidence": [{"chunk_ref": chunk["chunk_ref"], "quote": "never written text"}]}]}}))
        out["art"] = text_of(await s.call_tool("brain_artifact_read", {"source_ref": ref}))
        out["chunk"] = text_of(await s.call_tool("brain_artifact_read", {"source_ref": ref, "chunk_ref": chunk["chunk_ref"]}))
        out["retrieve"] = text_of(await s.call_tool("brain_retrieve", {"query": "فیت پلن", "scope": "all"}))
        out["unknown"] = text_of(await s.call_tool("brain_retrieve", {"query": "x", "path": "/etc/passwd"}))
        out["old_prop_with_chk"] = text_of(await s.call_tool("brain_read", {"ref": chunk["chunk_ref"]}))
        return out

    with Running(tools=semantic_adapter(w.root, env=ENV)) as srv:
        o = run(with_session(srv.url, MemStorage(), scenario))
    assert tuple(o["tools"]) == SEMANTIC_TOOLS + T.EXTENSION_TOOL_NAMES
    assert o["submit"]["ok"] and o["submit"]["annotation_revision"] == 1 and not o["submit"]["idempotent_replay"]
    assert o["replay"]["idempotent_replay"] is True and o["replay"]["annotation_revision"] == 1
    assert o["forged"]["ok"] is False and o["forged"]["error"]["code"] == "E_VALIDATION" and o["forged"]["persisted"] is False
    assert o["art"]["coverage"]["annotated_chunks"] >= 1 and o["art"]["annotation"]["tags"] == ["fitness"]
    # one ref, same literal text through the base tool and the extension
    assert o["chunk"]["chunk"]["text"] in str(o["base_read"]["sections"])
    assert o["chunk"]["chunk"]["text"] == w.texts["fa_short"]
    assert o["retrieve"]["ok"] and o["retrieve"]["query_model_calls"] == 0
    hits = o["retrieve"]["groups"]["captures"]["results"]
    assert any(h["source_ref"] == ref for h in hits)
    assert o["unknown"]["error"]["code"] == "E_UNKNOWN_ARGUMENT"
    assert o["old_prop_with_chk"]["ok"] is False          # chk: is not a base-tool ref
