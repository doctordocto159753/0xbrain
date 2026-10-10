"""CONTRACT-E1 conformance (AGENT-0): hashes are re-derived here from the
spec text with plain hashlib, so the golden vectors cross-check the code and
a second implementation can check itself against them."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import jsonschema
import pytest

from brain_enrichment import contract as C
from brain_enrichment import tools_spec as T
from brain_enrichment.config import EnrichmentConfig
from support import FakeProjectionStore, corpus_hashes, make_projection, naive_chunks

FIX = Path(__file__).resolve().parent / "fixtures"


def test_golden_vectors_match_independent_derivation():
    g = json.loads((FIX / "golden_vectors.json").read_text(encoding="utf-8"))
    a = g["artifact_id"]
    assert C.artifact_id(a["instance"], a["source_ref"]) == a["expected"] == \
        hashlib.sha256(f'{a["instance"]}\0{a["source_ref"]}'.encode()).hexdigest()
    s = g["source_version"]
    payload = json.dumps({"v": 1, "source_ref": s["source_ref"], "layers": s["layers"]},
                         sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert C.source_version(s["source_ref"], s["layers"]) == s["expected"] == \
        hashlib.sha256(payload.encode("utf-8")).hexdigest()
    k = g["chunk"]
    dg = hashlib.sha256(json.dumps([k["source_ref"], k["source_version"], k["layer_id"], k["start"], k["end"],
                                    "chunk-1"], separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    assert C.chunk_digest(k["source_ref"], k["source_version"], k["layer_id"], k["start"], k["end"]) == dg == k["digest"]
    assert C.chunk_ref(dg) == k["chunk_ref"] and C.CHUNK_REF_RE.fullmatch(k["chunk_ref"])
    for text, expected in g["normal_form"]:
        assert C.normal_form(text) == expected


def test_version_changes_with_text_and_not_with_projection_version():
    a = make_projection("cap:cap-20260101-000000-aaaa", [("user-text", "cap:cap-20260101-000000-aaaa", "one\n", None)])
    b = make_projection("cap:cap-20260101-000000-aaaa", [("user-text", "cap:cap-20260101-000000-aaaa", "two\n", None)])
    assert a.source_version != b.source_version
    assert a.artifact_id == b.artifact_id            # identity stays, version moves
    assert naive_chunks(a)[0].chunk_ref != naive_chunks(b)[0].chunk_ref


def test_identical_text_two_sources_have_distinct_identity():
    t = "same words\n"
    x = make_projection("cap:cap-1", [("user-text", "cap:cap-1", t, None)])
    y = make_projection("cap:cap-2", [("user-text", "cap:cap-2", t, None)])
    assert x.artifact_id != y.artifact_id and x.source_version != y.source_version


def test_request_digest_ignores_only_the_idempotency_key():
    r = {"source_ref": "cap:cap-1", "idempotency_key": "k-0000001", "annotation": {"tags": ["a"]}}
    assert C.request_digest(r) == C.request_digest({**r, "idempotency_key": "other-key-1"})
    assert C.request_digest(r) != C.request_digest({**r, "annotation": {"tags": ["b"]}})


def test_cursor_roundtrip_and_rejection():
    cur = C.encode_cursor("retrieve", g=3, o=20)
    assert C.decode_cursor(cur, "retrieve") == {"k": "retrieve", "g": 3, "o": 20}
    for bad in ("", "!!!", cur, "a" * 3000, None):
        with pytest.raises(C.EnrichmentError) as e:
            C.decode_cursor(bad, "pending")
        assert e.value.code == "E_BAD_CURSOR"


def test_budget_requires_token_estimate_and_char_ceiling():
    assert C.fits_budget("hello world", 500)
    assert not C.fits_budget("x" * 2001, 500)                 # 4 chars/token ceiling
    assert C.estimate_tokens("سلام" * 100) > C.estimate_tokens("abcd" * 100)   # Persian costs more


def test_error_codes_are_closed():
    with pytest.raises(ValueError):
        C.EnrichmentError("E_MADE_UP", "x")
    assert C.EnrichmentError("E_CONFLICT", "m", current_revision=2).to_public()["error"]["current_revision"] == 2


def test_public_projection_leaks_no_text_or_path():
    p = make_projection("cap:cap-9", [("user-text", "cap:cap-9", "secret body\n", None)])
    blob = json.dumps(p.to_public(), ensure_ascii=False)
    assert "secret body" not in blob and "origin_path" not in blob
    jsonschema.validate(p.to_public(), json.loads((T._SCHEMAS / "envelope.schema.json").read_text()))


def test_extension_tool_schemas_are_strict_and_self_contained():
    assert T.EXTENSION_TOOL_NAMES == ("brain_enrichment_pending", "brain_enrichment_submit",
                                      "brain_artifact_read", "brain_retrieve")
    from brain_surface import contract as BC
    assert not set(T.EXTENSION_TOOL_NAMES) & set(BC.TOOL_NAMES)
    for t in T.EXTENSION_TOOLS:
        jsonschema.Draft202012Validator.check_schema(t["inputSchema"])
        assert t["inputSchema"]["additionalProperties"] is False
        assert "$ref" not in json.dumps(t["inputSchema"])
        for forbidden in ("path", "actor", "authority_level", "approve", "promote"):
            assert forbidden not in t["inputSchema"]["properties"]


def test_annotation_schema_accepts_example_and_rejects_server_fields():
    schema = json.loads((T._SCHEMAS / "annotation.schema.json").read_text())
    ex = json.loads((FIX / "example_annotation.json").read_text(encoding="utf-8"))
    jsonschema.validate(ex, schema)
    for bad in ({}, {**ex, "authority_level": 1}, {**ex, "actor": "owner"}, {**ex, "path": "x"},
                {**ex, "tags": ["t"] * 13}, {**ex, "message_role": "verdict"}):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(bad, schema)


def test_config_defaults_off_and_invalid_values_fail_safe():
    assert EnrichmentConfig.from_env({}).mode == "off"
    assert not EnrichmentConfig.from_env({}).extension_enabled
    c = EnrichmentConfig.from_env({"BRAIN_ENRICHMENT": "maybe", "BRAIN_ENRICHMENT_BATCH_SIZE": "x"})
    assert c.mode == "off" and c.batch_size == 1 and len(c.warnings) == 2
    assert not EnrichmentConfig.from_env({"BRAIN_ENRICHMENT": "shadow", "BRAIN_ENRICHMENT_WRITES": "1"}).writes_enabled
    assert EnrichmentConfig.from_env({"BRAIN_ENRICHMENT": "on", "BRAIN_ENRICHMENT_WRITES": "1"}).writes_enabled


def test_fake_store_cas_idempotency_and_stale_source():
    st = FakeProjectionStore()
    p = make_projection("cap:cap-1", [("user-text", "cap:cap-1", "hello world text\n", None)])
    st.upsert_projection(p, naive_chunks(p))
    req = {"source_ref": "cap:cap-1", "source_version": p.source_version, "expected_annotation_revision": 0,
           "idempotency_key": "key-00000001", "annotation": {"tags": ["a"]}}
    r1 = st.append_annotation(req)
    assert r1.annotation_revision == 1 and not r1.idempotent_replay
    assert st.append_annotation(req).idempotent_replay
    with pytest.raises(C.EnrichmentError) as e:
        st.append_annotation({**req, "annotation": {"tags": ["b"]}})
    assert e.value.code == "E_IDEMPOTENCY_CONFLICT"
    with pytest.raises(C.EnrichmentError) as e:
        st.append_annotation({**req, "idempotency_key": "key-00000002"})
    assert e.value.code == "E_CONFLICT"
    with pytest.raises(C.EnrichmentError) as e:
        st.append_annotation({**req, "idempotency_key": "key-00000003", "expected_annotation_revision": 1,
                              "source_version": "0" * 64})
    assert e.value.code == "E_STALE_SOURCE"
    with pytest.raises(C.EnrichmentError) as e:
        st.record_review({"source_ref": "cap:cap-1", "annotation_revision": 1, "decision": "confirmed"},
                         {"actor": "x", "remote": True})
    assert e.value.code == "E_REVIEW_FORBIDDEN"


def test_seeded_wiki_covers_the_contract_cases(enrichment_wiki):
    w = enrichment_wiki
    assert C.instance_id(w.root) == "zb-instance"
    for name in ("fa_short", "crlf", "escaped_headings", "long", "dup_a", "dup_b", "voice_plain",
                 "voice_transcribed", "held_ok", "held_nodoc", "held_ocr", "canonical", "proposal"):
        assert w.refs[name]
    assert w.refs["dup_a"] != w.refs["dup_b"]
    assert "\r\n" in w.texts["crlf"] and len(w.texts["long"]) > C.HARD_MAX
    before = corpus_hashes(w.root)
    assert before and corpus_hashes(w.root) == before
