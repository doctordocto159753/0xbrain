"""WP-D end-to-end: the REAL stack (real store + real annotation / retrieval /
indexing services, wired by `surface.REAL_FACTORIES`) through the four tools,
the CLI and the base surface, over the seeded `enrichment_wiki`.

TEMPORARY SKIPS (and only these): each `importorskip` below skips this module
while the named work package has not been merged into the branch under test.
AGENT-0 runs the file unskipped after merging A/B/C; if a landed module uses
other factory names than `surface.REAL_FACTORIES`, fix that single table
(docs/enrichment/requests/d-wiring.md). Nothing else here is conditional.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("brain_enrichment.store", reason="WP-A (store/projection) not merged into this branch yet")
pytest.importorskip("brain_enrichment.source", reason="WP-A (SourceReader) not merged into this branch yet")
pytest.importorskip("brain_enrichment.annotation", reason="WP-B (AnnotationService) not merged into this branch yet")
pytest.importorskip("brain_enrichment.retrieval", reason="WP-C (RetrievalEngine) not merged into this branch yet")
pytest.importorskip("brain_enrichment.indexing", reason="WP-C (IndexBuilder) not merged into this branch yet")

from brain_enrichment import cli  # noqa: E402
from brain_enrichment import contract as C  # noqa: E402
from brain_enrichment.config import EnrichmentConfig  # noqa: E402
from brain_enrichment.surface import build_extension, tool_specs  # noqa: E402
from brain_surface import TOOL_NAMES, BrainSurface  # noqa: E402
from brain_surface.backend import WikiBackend  # noqa: E402
from support import corpus_hashes  # noqa: E402

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
CFG = EnrichmentConfig(mode="on", writes=True, retrieval=True, batch_size=5)
ENV_DROP = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "BRAIN_AGENT_SESSION", "BRAIN_REMOTE_SESSION")


class Pipe(io.StringIO):
    def isatty(self):
        return False


@pytest.fixture()
def stack(enrichment_wiki):
    w = enrichment_wiki
    ext = build_extension(w.root, CFG, legacy_backend=WikiBackend(w.root))
    return w, ext, BrainSurface(WikiBackend(w.root))


def find_pending(ext, source_ref, tries=40):
    """Walk brain_enrichment_pending pages (each call also advances discovery) until the source shows up."""
    cursor = None
    for _ in range(tries):
        out = ext.call("brain_enrichment_pending", {"limit": 5, **({"cursor": cursor} if cursor else {})})
        assert out["ok"], out
        for item in out["items"]:
            if item["source_ref"] == source_ref:
                return item, out
        cursor = out["next_cursor"]
    pytest.fail(f"{source_ref} never appeared in brain_enrichment_pending")


def annotation_for(item):
    chunk = item["page"]["chunks"][0]
    return {"summary": "برنامهٔ ورزشی روزانه", "tags": ["fitness"], "topics": ["ورزش"],
            "entities": [{"name": "فیت پلن", "kind": "project",
                          "mention": {"chunk_ref": chunk["chunk_ref"], "text": "فیت پلن"}}],
            "links": [{"relation": "about", "target": {"entity": "فیت پلن"},
                       "evidence": [{"chunk_ref": chunk["chunk_ref"], "quote": chunk["text"][:20]}]}],
            "chunk_annotations": [{"chunk_ref": chunk["chunk_ref"], "summary": "plan"}],
            "producer_reported": {"harness": "pytest"}}


def submit(ext, item, annotation, key="e2e-key-0001", **over):
    return ext.call("brain_enrichment_submit", {
        "source_ref": item["source_ref"], "source_version": item["source_version"],
        "expected_annotation_revision": item["expected_annotation_revision"], "idempotency_key": key,
        "annotation": annotation, **over})


def scan_all(w, rounds=6):
    for _ in range(rounds):
        out = Pipe()
        rc = cli.main(["--root", str(w.root), "--json", "scan"], env={"BRAIN_ENRICHMENT": "on"},
                      stdin=Pipe(), stdout=out, stderr=Pipe())
        if rc == 0:
            return json.loads(out.getvalue())
    pytest.fail("scan never completed a pass")


def build_index(ext):
    idx = ext.s.index(create=True)
    for _ in range(20):
        if idx.refresh(C.INDEX_REFRESH_BATCH).artifacts_pending == 0:
            return idx
    pytest.fail("index never caught up")


def test_pending_submit_read_roundtrip_one_ref_two_paths_and_zero_source_mutation(stack):
    w, ext, base = stack
    before = corpus_hashes(w.root)
    ref = w.refs["fa_short"]
    item, page = find_pending(ext, ref)
    assert page["discovery"]["inventory_generation"] >= 1 and item["current_annotation_revision"] == 0
    chunk = item["page"]["chunks"][0]
    assert chunk["text"] == w.texts["fa_short"][chunk["start_char"]:chunk["end_char"]]
    ok = submit(ext, item, annotation_for(item))
    assert ok["ok"] and ok["persisted"] and ok["annotation_revision"] == 1 and ok["processing_state"] == "annotated"
    replay = submit(ext, item, annotation_for(item))
    assert replay["ok"] and replay["idempotent_replay"] is True and replay["annotation_revision"] == 1
    art = ext.call("brain_artifact_read", {"source_ref": ref})
    assert art["ok"] and art["artifact"]["current_annotation_revision"] == 1 and art["annotation"]
    assert art["artifact"]["source_version"] == item["source_version"] and art["review"]["state"] == "unreviewed"
    got = ext.call("brain_artifact_read", {"source_ref": ref, "chunk_ref": chunk["chunk_ref"]})
    # one ref, the same text and version through the extension path and the base path
    read = base.call("brain_read", {"ref": ref})
    assert got["chunk"]["text"] == chunk["text"] == read["sections"]["User-supplied text"][chunk["start_char"]:chunk["end_char"]]
    assert got["source"]["source_version"] == item["source_version"]
    assert got["layer"]["text_ref"] == ref and got["layer"]["text_sha256"] == C.sha256_text(read["sections"]["User-supplied text"])
    held = w.refs["held_ok"]
    h_item, _ = find_pending(ext, held)
    doc = base.call("brain_read", {"ref": w.refs["held_ok_text"]})
    h_chunk = ext.call("brain_artifact_read", {"source_ref": held, "chunk_ref": h_item["page"]["chunks"][0]["chunk_ref"]})
    assert h_chunk["chunk"]["text"] == doc["content"][h_chunk["chunk"]["start_char"]:h_chunk["chunk"]["end_char"]]
    assert h_chunk["source"]["tier"] == "derivative" and h_chunk["source"]["authority_level"] == 5
    for blob in (ok, replay, art, got, h_chunk):
        assert str(w.root) not in json.dumps(blob, ensure_ascii=False)
    assert corpus_hashes(w.root) == before


def test_forged_evidence_is_rejected_and_a_changed_source_goes_stale(stack):
    w, ext, base = stack
    ref = w.refs["fa_short"]
    item, _ = find_pending(ext, ref)
    forged = annotation_for(item)
    forged["links"][0]["evidence"][0]["quote"] = "this sentence was never written"
    out = submit(ext, item, forged)
    assert out["ok"] is False and out["error"]["code"] == "E_VALIDATION" and out["persisted"] is False
    assert ext.call("brain_artifact_read", {"source_ref": ref})["artifact"]["current_annotation_revision"] == 0
    no_server_fields = ext.call("brain_enrichment_submit", {**{k: item[k] for k in ("source_ref", "source_version")},
                                                           "expected_annotation_revision": 0, "idempotency_key": "e2e-key-0002",
                                                           "annotation": {"tags": ["a"], "authority_level": 1}})
    assert no_server_fields["ok"] is False and no_server_fields["persisted"] is False
    old_chunk = item["page"]["chunks"][0]["chunk_ref"]
    path = w.root / "01-inbox" / "captures"
    rec = next(p for p in path.rglob("*.md") if ref[4:] in p.name)
    text = rec.read_bytes().decode("utf-8")
    rec.write_bytes(text.replace("پیاده‌روی", "دویدن", 1).encode("utf-8"))      # the source changes on disk
    stale = ext.call("brain_artifact_read", {"source_ref": ref, "chunk_ref": old_chunk})
    assert stale["ok"] is False and stale["error"]["code"] == "E_STALE_SOURCE"
    late = submit(ext, item, annotation_for(item), key="e2e-key-0003")
    assert late["ok"] is False and late["error"]["code"] == "E_STALE_SOURCE" and late["persisted"] is False
    fresh, _ = find_pending(ext, ref)
    assert fresh["source_version"] != item["source_version"]
    assert ext.call("brain_artifact_read", {"source_ref": ref, "chunk_ref": fresh["page"]["chunks"][0]["chunk_ref"]})["ok"]


def test_retrieve_uses_the_index_then_falls_back_explicitly_when_it_disappears(stack):
    w, ext, base = stack
    ref = w.refs["fa_short"]
    item, _ = find_pending(ext, ref)
    assert submit(ext, item, annotation_for(item))["ok"]
    scan_all(w)                                                   # project every source (canonical records too)
    build_index(ext)
    out = ext.call("brain_retrieve", {"query": "فیت پلن", "scope": "captures"})
    assert out["ok"] and out["path"] == "enrichment" and out["query_model_calls"] == 0
    hits = out["groups"]["captures"]["results"]
    mine = next(h for h in hits if h["source_ref"] == ref)
    assert mine["hit_level"] == "chunk" and mine["chunk_ref"] and "فیت پلن" in mine["text"]
    assert mine["tier"] == "capture" and mine["authority_level"] == 7 and mine["inclusion_reason"]["paths"]
    assert out["index"]["state"] in ("ok", "lagging") and out["authority_note"]
    canon = ext.call("brain_retrieve", {"query": "Project Nil", "scope": "canonical"})
    assert canon["ok"] and "rec:obj-nil" in [h["source_ref"] for h in canon["groups"]["canonical"]["results"]]
    assert str(w.root) not in json.dumps(out, ensure_ascii=False)
    import shutil
    shutil.rmtree(w.root / "_search" / "enrichment")
    ext2 = build_extension(w.root, CFG, legacy_backend=WikiBackend(w.root))   # a fresh process view: index gone
    fb = ext2.call("brain_retrieve", {"query": "فیت پلن", "scope": "captures"})
    assert fb["ok"] and fb["path"] in ("enrichment", "fallback-lexical", "fallback-exact")
    if fb["path"] != "enrichment":
        assert fb["index"]["state"] in ("missing", "unavailable") and fb["notes"]
    off = build_extension(w.root, EnrichmentConfig(mode="on", retrieval=False), legacy_backend=WikiBackend(w.root))
    fb = off.call("brain_retrieve", {"query": "فیت پلن", "scope": "captures"})
    assert fb["path"].startswith("fallback-") and "BRAIN_ENRICHMENT_RETRIEVAL=0" in fb["notes"][0]


def cli_env(**extra):
    env = {k: v for k, v in os.environ.items() if k not in ENV_DROP}
    env.update({"PYTHONPATH": str(SCRIPTS), "PYTHONIOENCODING": "utf-8", "BRAIN_ENRICHMENT": "on"}, **extra)
    return env


def test_cli_on_the_real_store_status_verify_export_and_human_review(stack):
    w, ext, base = stack
    before = corpus_hashes(w.root)
    ref = w.refs["fa_short"]
    assert scan_all(w)["complete"] is True
    item, _ = find_pending(ext, ref)
    assert submit(ext, item, annotation_for(item))["ok"]
    build_index(ext)

    def go(*argv):
        return subprocess.run([sys.executable, "-m", "brain_enrichment.cli", "--root", str(w.root), *argv],
                              capture_output=True, text=True, encoding="utf-8", env=cli_env(), stdin=subprocess.DEVNULL)

    st = json.loads(go("--json", "status").stdout)
    e = st["enrichment"]
    assert e["store"]["state"] == "ok" and e["store"]["schema_version"] == C.STORE_SCHEMA_VERSION
    assert e["processing_state"]["annotated"] >= 1 and e["sidecar_bytes"] > 0 and e["index"]["state"] != "missing"
    assert st["legacy_capture_workflow"]["total"] == 8 and "NOT the enrichment backlog" in st["legacy_capture_workflow"]["note"]
    ver = go("--json", "verify")
    assert ver.returncode == 0 and json.loads(ver.stdout)["ok"], ver.stdout
    exp = go("export")
    lines = [json.loads(x) for x in exp.stdout.splitlines()]
    assert exp.returncode == 0 and lines[0]["type"] == "header" and any(x["type"] == "artifact" for x in lines)
    ins = json.loads(go("--json", "inspect", ref).stdout)
    assert ins["ok"] and ins["artifact"]["current_annotation_revision"] == 1
    # human review: refused remotely, recorded locally, never promotes
    refused = subprocess.run([sys.executable, "-m", "brain_enrichment.cli", "--root", str(w.root), "--json", "review", ref,
                              "--revision", "1", "--decision", "confirmed", "--note", "n", "--actor", "Sina", "--yes"],
                             capture_output=True, text=True, encoding="utf-8", env=cli_env(BRAIN_REMOTE_SESSION="1"),
                             stdin=subprocess.DEVNULL)
    assert refused.returncode == 3 and json.loads(refused.stdout)["error"]["code"] == "E_REVIEW_FORBIDDEN"
    assert ext.call("brain_artifact_read", {"source_ref": ref})["review"]["state"] == "unreviewed"
    out = Pipe()
    rc = cli.main(["--root", str(w.root), "--json", "review", ref, "--revision", "1", "--decision", "confirmed",
                   "--note", "checked against the capture", "--actor", "Sina", "--yes"],
                  env={}, stdin=Pipe(), stdout=out, stderr=Pipe())
    body = json.loads(out.getvalue())
    assert rc == 0 and body["recorded"] and body["canonical_promotion"] is False and body["promoted"] is False
    again = ext.call("brain_artifact_read", {"source_ref": ref})
    assert again["review"] == {"state": "confirmed", "revision": 1}
    assert again["artifact"]["source_tier"] == "capture" and again["artifact"]["source_authority_level"] == 7
    stale_review = Pipe()
    rc = cli.main(["--root", str(w.root), "--json", "review", ref, "--revision", "9", "--decision", "confirmed",
                   "--note", "n", "--actor", "Sina", "--yes"], env={}, stdin=Pipe(), stdout=stale_review, stderr=Pipe())
    assert rc == 4 and json.loads(stale_review.getvalue())["error"]["code"] == "E_CONFLICT"
    assert (w.root / "_enrichment" / "review-audit.jsonl").read_text(encoding="utf-8").count("\n") == 2
    assert corpus_hashes(w.root) == before


def test_tool_list_with_real_services_is_seven_plus_four_and_off_is_seven(stack):
    w, ext, base = stack
    names = [t["name"] for t in base.tools()] + [n for n, *_ in tool_specs(ext)]
    assert len(names) == 11 and tuple(names[:7]) == tuple(TOOL_NAMES)
    assert not any(v in n for n in names for v in ("review", "approve", "promote"))
    assert build_extension(w.root, EnrichmentConfig(mode="shadow")) is None
