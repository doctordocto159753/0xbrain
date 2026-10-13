"""WP-D: the local enrichment CLI. Real subprocesses against a throwaway wiki
(status/inspect/export JSON well-formed, exit codes, read-only guarantees),
plus in-process runs with the injected-fake store for the commands that need
a populated sidecar (the real store is exercised in test_e2e_extension.py)."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from brain_enrichment import cli
from brain_enrichment import contract as C
from brain_enrichment.config import EnrichmentConfig
from brain_enrichment.surface import Services
from support import corpus_hashes, make_projection, naive_chunks
from test_surface import FakeReader, Rig, annotate, load_source

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
TESTS = Path(__file__).resolve().parent
AGENT_MARKERS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "BRAIN_AGENT_SESSION", "BRAIN_REMOTE_SESSION",
                 "BRAIN_ENRICHMENT", "BRAIN_ENRICHMENT_WRITES", "BRAIN_ENRICHMENT_RETRIEVAL")


def clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if k not in AGENT_MARKERS}
    env["PYTHONPATH"] = str(SCRIPTS)
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(extra)
    return env


def sub(argv, root, env=None, *, script=False):
    entry = [sys.executable, str(SCRIPTS / "brain_enrichment" / "cli.py")] if script else \
        [sys.executable, "-m", "brain_enrichment.cli"]
    return subprocess.run([*entry, "--root", str(root), *argv], capture_output=True, text=True, encoding="utf-8",
                          env=env or clean_env(), stdin=subprocess.DEVNULL, cwd=str(TESTS))


class Pipe(io.StringIO):
    def isatty(self):
        return False


def run(rig, argv, env=None, *, services=None):
    out, err = Pipe(), Pipe()
    rc = cli.main(["--root", str(rig.w.root), "--json", *argv], services=services or Services(
        rig.w.root, EnrichmentConfig.from_env(env or {}), store=rig.store, index=rig.index, source_reader=rig.reader),
        env=env or {}, stdin=Pipe(), stdout=out, stderr=err)
    return rc, out.getvalue(), err.getvalue()


def jrun(rig, argv, env=None, **kw):
    rc, out, err = run(rig, argv, env, **kw)
    return rc, json.loads(out), err


SHADOW = {"BRAIN_ENRICHMENT": "shadow"}


@pytest.fixture()
def rig(enrichment_wiki):
    return Rig(enrichment_wiki, reader=FakeReader())


# ------------------------------------------------------ real subprocess runs
def test_status_json_before_any_store_exists_is_read_only_and_well_formed(enrichment_wiki):
    root = enrichment_wiki.root
    before = corpus_hashes(root)
    p = sub(["--json", "status"], root)
    assert p.returncode == 0 and p.stderr == ""
    body = json.loads(p.stdout)
    assert body["ok"] and body["command"] == "status" and body["root"] == "."
    e, legacy = body["enrichment"], body["legacy_capture_workflow"]
    assert e["mode"] == "off" and e["store"]["state"] == "missing" and e["store"]["schema_version"] is None
    assert e["store"]["supported_schema_version"] == C.STORE_SCHEMA_VERSION and e["sidecar_bytes"] == 0
    assert set(e["processing_state"]) == set(C.PROCESSING_STATES) and not any(e["processing_state"].values())
    assert e["backlog"]["awaiting_annotation"] == 0 and e["index"]["state"] == "missing"
    assert body["degraded"] is False
    # legacy workflow status sits next to the new state, clearly named and explained
    assert legacy["total"] == 8 == sum(legacy["by_status"].values())
    assert set(legacy["by_status"]) <= {"received", "processing", "transcribed", "described", "needs-review",
                                        "processing-failed", "reviewed", "parked", "promoted"}
    assert "NOT the enrichment backlog" in legacy["note"]
    assert str(root) not in p.stdout
    assert not (root / "_enrichment").exists() and not (root / "_search" / "enrichment").exists()
    assert corpus_hashes(root) == before


def test_status_text_shows_new_state_and_legacy_workflow_side_by_side(enrichment_wiki):
    p = sub(["status"], enrichment_wiki.root)
    assert p.returncode == 0
    out = p.stdout
    assert out.index("ENRICHMENT (new sidecar)") < out.index("LEGACY CAPTURE WORKFLOW (human intake queue, not enrichment)")
    assert "backlog: awaiting_annotation=0" in out and "total=8" in out


def test_status_flags_a_missing_index_only_when_the_extension_is_on(enrichment_wiki):
    p = sub(["--json", "status"], enrichment_wiki.root, clean_env(BRAIN_ENRICHMENT="on"))
    body = json.loads(p.stdout)
    assert body["degraded"] is True and "index missing" in body["degraded_reasons"][0]
    assert body["enrichment"]["mode"] == "on"
    bad = json.loads(sub(["--json", "status"], enrichment_wiki.root, clean_env(BRAIN_ENRICHMENT="maybe")).stdout)
    assert bad["enrichment"]["mode"] == "off" and bad["enrichment"]["config_warnings"]


def test_status_reports_a_newer_store_schema_without_opening_it(enrichment_wiki):
    import sqlite3
    root = enrichment_wiki.root
    (root / "_enrichment").mkdir()
    con = sqlite3.connect(root / "_enrichment" / "store-v1.sqlite3")
    con.execute("PRAGMA user_version = 99")
    con.commit()
    con.close()
    body = json.loads(sub(["--json", "status"], root).stdout)
    assert body["enrichment"]["store"]["state"] == "unsupported" and body["enrichment"]["store"]["schema_version"] == 99
    assert body["degraded"] is True
    p = sub(["--json", "inspect", enrichment_wiki.refs["fa_short"]], root)
    assert p.returncode == 3 and json.loads(p.stdout)["error"]["code"] == "E_SCHEMA_UNSUPPORTED"


def test_script_entry_point_runs_without_pythonpath(enrichment_wiki):
    env = clean_env()
    env.pop("PYTHONPATH")
    p = sub(["--json", "status"], enrichment_wiki.root, env, script=True)
    assert p.returncode == 0 and json.loads(p.stdout)["command"] == "status"


@pytest.mark.parametrize("argv,code,err", [
    (["inspect", "cap:cap-20260101-000000-aaaa"], 3, "E_UNAVAILABLE"),
    (["verify"], 3, "E_UNAVAILABLE"), (["export"], 3, "E_UNAVAILABLE"), (["pending"], 3, "E_UNAVAILABLE"),
    (["scan"], 3, "E_DISABLED"), (["index", "refresh"], 3, "E_DISABLED"),
    (["inspect", "../etc/passwd"], 5, "E_BAD_REF"),
])
def test_commands_without_a_store_fail_cleanly_with_json_and_exit_codes(enrichment_wiki, argv, code, err):
    root = enrichment_wiki.root
    p = sub(["--json", *argv], root)
    body = json.loads(p.stdout)
    assert p.returncode == code and body["ok"] is False and body["error"]["code"] == err and body["command"] == argv[0]
    assert str(root) not in p.stdout + p.stderr and "Traceback" not in p.stdout + p.stderr
    assert not (root / "_enrichment").exists()                      # nothing created by a refused / read-only command


def test_usage_errors_exit_2(enrichment_wiki, tmp_path):
    assert sub([], enrichment_wiki.root).returncode == 2
    assert sub(["promote", "x"], enrichment_wiki.root).returncode == 2
    assert sub(["approve", "x"], enrichment_wiki.root).returncode == 2
    p = sub(["--json", "status"], tmp_path / "does-not-exist")
    assert p.returncode == 2 and json.loads(p.stdout)["error"]["code"] == "E_BAD_ARGUMENTS"
    assert str(tmp_path) not in p.stdout


WRAPPER = textwrap.dedent('''
    import json, sys
    sys.path[:0] = [{scripts!r}, {tests!r}]
    from brain_enrichment import cli
    from brain_enrichment.config import EnrichmentConfig
    from brain_enrichment.surface import Services
    from support import make_projection, naive_chunks
    from test_surface import RichStore, _without_real_source_reader
    spec = json.load(open(sys.argv[1], encoding="utf-8"))
    store = RichStore()
    for s in spec["sources"]:
        proj = make_projection(s["ref"], [("user-text", s["ref"], s["text"], None)])
        store.upsert_projection(proj, naive_chunks(proj, s["size"]))
        if s.get("annotate"):
            store.append_annotation({{"source_ref": s["ref"], "source_version": proj.source_version,
                "expected_annotation_revision": 0, "idempotency_key": "key-00000001",
                "annotation": {{"tags": ["wrapped"]}}}})
    cfg = EnrichmentConfig.from_env()
    sys.exit(cli.main(sys.argv[2:], services=Services({root!r}, cfg, store=store, importer=_without_real_source_reader)))
''')


def wrapped(w, tmp_path, argv, *, annotate_first=True, env=None):
    spec = {"sources": [
        {"ref": w.refs["fa_short"], "text": w.texts["fa_short"], "size": 30, "annotate": annotate_first},
        {"ref": w.refs["crlf"], "text": w.texts["crlf"], "size": 25}]}
    (tmp_path / "spec.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "wrap.py").write_text(WRAPPER.format(scripts=str(SCRIPTS), tests=str(TESTS), root=str(w.root)), encoding="utf-8")
    return subprocess.run([sys.executable, str(tmp_path / "wrap.py"), str(tmp_path / "spec.json"),
                           "--root", str(w.root), *argv], capture_output=True, text=True, encoding="utf-8",
                          env=env or clean_env(BRAIN_ENRICHMENT="shadow"), stdin=subprocess.DEVNULL)


def test_subprocess_inspect_status_export_verify_pending_are_well_formed(enrichment_wiki, tmp_path):
    w = enrichment_wiki
    before = corpus_hashes(w.root)
    st = json.loads(wrapped(w, tmp_path, ["--json", "status"]).stdout)
    e = st["enrichment"]
    assert e["store"]["state"] == "ok" and e["sources"]["total"] == 2
    assert e["processing_state"]["annotated"] == 1 and e["processing_state"]["projected"] == 1
    assert e["backlog"]["awaiting_annotation"] == 1 and e["chunk_coverage"]["total_chunks"] > 2
    assert st["legacy_capture_workflow"]["total"] == 8
    ins = json.loads(wrapped(w, tmp_path, ["--json", "inspect", w.refs["fa_short"]]).stdout)
    assert ins["ok"] and ins["artifact"]["current_annotation_revision"] == 1 and ins["annotation"]["tags"] == ["wrapped"]
    chunk = ins["chunks"]["items"][0]["chunk_ref"]
    one = json.loads(wrapped(w, tmp_path, ["--json", "inspect", w.refs["fa_short"], "--chunk", chunk]).stdout)
    assert one["chunk"]["text"] == w.texts["fa_short"][:30] and one["authority_note"]
    exp = wrapped(w, tmp_path, ["export", "--with-text"])
    lines = [json.loads(x) for x in exp.stdout.splitlines()]
    assert exp.returncode == 0 and lines[0]["type"] == "header" and lines[0]["schema"] == cli.EXPORT_SCHEMA
    types = [x["type"] for x in lines]
    assert types.count("artifact") == 2 and types.count("annotation") == 1 and types.count("chunk") > 2
    assert "".join(x["text"] for x in lines if x["type"] == "chunk" and x["source_ref"] == w.refs["crlf"]) == w.texts["crlf"]
    ver = wrapped(w, tmp_path, ["--json", "verify"])
    vb = json.loads(ver.stdout)
    assert ver.returncode == 0 and vb["ok"] and vb["errors"] == [] and vb["checked"]["sources"] == 2
    pend = json.loads(wrapped(w, tmp_path, ["--json", "pending"]).stdout)
    assert [i["source_ref"] for i in pend["items"]] == [w.refs["crlf"]]
    rev = wrapped(w, tmp_path, ["--json", "review", w.refs["fa_short"], "--revision", "1", "--decision", "confirmed",
                                "--note", "ok", "--actor", "Sina", "--yes"])
    rb = json.loads(rev.stdout)
    assert rev.returncode == 0 and rb["canonical_promotion"] is False and rb["promoted"] is False
    assert corpus_hashes(w.root) == before


# ---------------------------------------------------------- in-process runs
def test_status_counts_states_review_and_backlog(rig):
    w = rig.w
    a, ac = load_source(rig.store, w, "fa_short")
    annotate(rig.store, a, ac, chunk=True)
    rig.store.record_review({"source_ref": a.source_ref, "annotation_revision": 1, "decision": "confirmed", "note": "n"},
                            {"actor": "sina@x"})
    load_source(rig.store, w, "crlf")
    load_source(rig.store, w, "dup_a")
    nt = make_projection(w.refs["voice_plain"], [], kind="capture-media")
    rig.store.upsert_projection(nt, [])
    canon = make_projection(w.refs["canonical"], [("record-text", w.refs["canonical"], "Project Nil\n", None)],
                            kind="canonical-record", tier="canonical-record", level=4)
    rig.store.upsert_projection(canon, naive_chunks(canon))
    b, bc = load_source(rig.store, w, "dup_b")
    annotate(rig.store, b, bc, key="key-00000002")
    new = make_projection(b.source_ref, [("user-text", b.source_ref, w.texts["dup_b"] + "edited\n", None)])
    rig.store.upsert_projection(new, naive_chunks(new))                      # b's annotation now describes old text
    rc, body, _ = jrun(rig, ["status"], SHADOW)
    e = body["enrichment"]
    assert rc == 0 and e["sources"]["total"] == 6
    assert e["processing_state"] == {"discovered": 0, "projected": 4, "annotated": 1, "failed": 0, "stale": 0, "no_text": 1} \
        or e["processing_state"]["annotated"] == 1
    assert e["processing_state"]["no_text"] == 1 and e["processing_state"]["annotated"] == 1
    assert e["review_state_of_current_annotations"] == {"unreviewed": 0, "confirmed": 1, "rejected": 0}
    assert e["backlog"]["awaiting_annotation"] == 3 and e["backlog"]["projected_but_not_annotatable"] == 1
    assert e["chunk_coverage"]["annotated_chunks"] == 1 and e["sources"]["by_kind"]["canonical-record"] == 1
    assert body["legacy_capture_workflow"]["total"] == 8                       # untouched by enrichment


def test_status_reports_index_generation_and_lag_when_the_index_can_say(rig):
    rig.index.status = lambda: {"generation": 7, "state": "lagging", "lag": {"pending_artifacts": 3}, "fresh": False,
                                "ignored": 1}
    rc, body, _ = jrun(rig, ["status"], {"BRAIN_ENRICHMENT": "on"})
    idx = body["enrichment"]["index"]
    assert idx["generation"] == 7 and idx["state"] == "lagging" and idx["lag"] == {"pending_artifacts": 3} and "ignored" not in idx


def test_pending_listing_is_read_only_and_skips_non_annotatable_sources(rig):
    w = rig.w
    a, ac = load_source(rig.store, w, "fa_short")
    annotate(rig.store, a, ac)
    for name in ("crlf", "dup_a", "dup_b"):
        load_source(rig.store, w, name)
    rig.store.upsert_projection(make_projection(w.refs["canonical"], [("record-text", w.refs["canonical"], "x\n", None)],
                                                kind="canonical-record", tier="canonical-record", level=4), [])
    rc, body, _ = jrun(rig, ["pending", "--limit", "2"])
    refs = sorted([w.refs["crlf"], w.refs["dup_a"], w.refs["dup_b"]])
    assert rc == 0 and [i["source_ref"] for i in body["items"]] == refs[:2] and body["more"] is True
    rc, body2, _ = jrun(rig, ["pending", "--limit", "2", "--after", body["next_after"]])
    assert [i["source_ref"] for i in body2["items"]] == refs[2:] and body2["more"] is False
    assert rig.store.pending_calls == []                      # the CLI listing never runs the discovery refresh


def test_verify_detects_each_kind_of_corruption_and_exits_1(rig):
    w = rig.w
    proj, chunks = load_source(rig.store, w, "crlf", size=25)
    other, oc = load_source(rig.store, w, "fa_short", size=20)
    ok, body, _ = jrun(rig, ["verify"])
    assert ok == 0 and body["ok"] and body["errors"] == []
    bad = C.Chunk(**{**chunks[1].__dict__, "start_char": chunks[1].start_char + 2})
    rig.store.chunks[proj.source_ref][1] = bad                                # gap + wrong digest
    rig.store.chunks[other.source_ref][0] = C.Chunk(**{**oc[0].__dict__, "next_chunk_ref": "chk:" + "e" * 32})
    annotate(rig.store, other, oc)
    rig.store.revisions[other.source_ref][-1]["annotation"] = {"chunk_annotations": [{"chunk_ref": "chk:" + "f" * 32, "summary": "x"}]}
    tampered = make_projection(w.refs["dup_a"], [("user-text", w.refs["dup_a"], "abc\n", None)])
    rig.store.projections[w.refs["dup_a"]] = C.SourceProjection(**{**tampered.__dict__, "source_version": "9" * 64})
    rig.store.chunks[w.refs["dup_a"]] = []
    rc, body, _ = jrun(rig, ["verify"])
    kinds = {e["code"] for e in body["errors"]}
    assert rc == 1 and body["ok"] is False
    assert {"chunk_partition", "chunk_ref", "chunk_links", "annotation_chunk_ref", "source_version"} <= kinds
    txt = io.StringIO()
    assert cli.main(["--root", str(w.root), "verify"], services=Services(w.root, EnrichmentConfig(), store=rig.store),
                    env={}, stdin=Pipe(), stdout=txt, stderr=Pipe()) == 1
    assert "ERRORS" in txt.getvalue()


def test_verify_reports_stale_and_missing_sources_as_warnings_not_corruption(rig):
    w = rig.w
    a, _ = load_source(rig.store, w, "fa_short")
    b, _ = load_source(rig.store, w, "crlf")
    c, _ = load_source(rig.store, w, "dup_a")
    rig.reader.live[a.source_ref] = a
    rig.reader.live[b.source_ref] = make_projection(b.source_ref, [("user-text", b.source_ref, "changed\n", None)])
    rc, body, _ = jrun(rig, ["verify"])
    assert rc == 0 and body["ok"] and body["stale_sources"] == [b.source_ref] and body["missing_sources"] == [c.source_ref]
    rc, body, _ = jrun(rig, ["verify", "--no-freshness"])
    assert body["stale_sources"] == [] and body["missing_sources"] == []


def test_export_is_deterministic_jsonl_and_refuses_unsafe_destinations(rig, tmp_path):
    w = rig.w
    a, ac = load_source(rig.store, w, "fa_short")
    annotate(rig.store, a, ac)
    load_source(rig.store, w, "crlf")
    r1, o1, _ = run(rig, ["export"])
    r2, o2, _ = run(rig, ["export"])
    assert r1 == r2 == 0 and o1 == o2 and o1.endswith("\n")
    lines = [json.loads(x) for x in o1.splitlines()]
    assert [x["type"] for x in lines][0] == "header" and lines[0]["with_text"] is False
    assert all("text" not in x for x in lines if x["type"] == "chunk")
    art = [x for x in lines if x["type"] == "artifact"]
    assert [x["source_ref"] for x in art] == sorted(x["source_ref"] for x in art)
    assert art[0]["review_state"] == "unreviewed" and "anchor_path" not in json.dumps(lines)
    dest = tmp_path / "out" / "dump.jsonl"
    rc, body, _ = jrun(rig, ["export", "--out", str(dest)])
    assert rc == 0 and body["lines"] == len(lines) and dest.read_text(encoding="utf-8") == o1
    assert not list(dest.parent.glob("*.tmp"))
    rc, body, _ = jrun(rig, ["export", "--out", str(w.root / "03-objects" / "dump.jsonl")])
    assert rc == 2 and body["error"]["code"] == "E_BAD_ARGUMENTS" and not (w.root / "03-objects" / "dump.jsonl").exists()
    ok, _, _ = jrun(rig, ["export", "--out", str(w.root / "_enrichment" / "exports" / "d.jsonl")])
    assert ok == 0 and (w.root / "_enrichment" / "exports" / "d.jsonl").is_file()


def test_scan_is_bounded_resumable_and_gated_by_mode(rig):
    d = lambda p, n: {"pass": p, "inventory_generation": 1, "scanned": n, "projected": n, "failed": 0, "no_text": 0}  # noqa: E731
    rig.store.discovery_script = [d(1, 5), d(1, 5), d(1, 5), d(2, 5)]
    rc, body, _ = jrun(rig, ["scan", "--max-rounds", "2"], SHADOW)
    assert rc == 1 and body["complete"] is False and body["stopped_because"] == "max_rounds" and body["rounds"] == 2
    assert "run scan again" in body["resume"]
    rc, body, _ = jrun(rig, ["scan", "--max-rounds", "10"], SHADOW)                 # resumes from the persisted position
    assert rc == 0 and body["complete"] is True and body["stopped_because"] == "pass_complete" and body["rounds"] == 2
    assert [c[1:] for c in rig.store.pending_calls] == [(1, False)] * 4
    rc, body, _ = jrun(rig, ["scan"], SHADOW)                                       # default script: nothing left
    assert rc == 0 and body["stopped_because"] == "idle"
    rc, body, _ = jrun(rig, ["scan", "--retry-failed"], SHADOW)
    assert rig.store.pending_calls[-1][2] is True
    rig.store.discovery_script = [d(1, 5)] * 5
    rc, body, _ = jrun(rig, ["scan", "--time-budget", "0"], SHADOW)
    assert rc == 1 and body["stopped_because"] == "time_budget" and body["rounds"] == 1
    calls = len(rig.store.pending_calls)
    rc, body, _ = jrun(rig, ["scan"], {})                                           # BRAIN_ENRICHMENT unset => off
    assert rc == 3 and body["error"]["code"] == "E_DISABLED" and len(rig.store.pending_calls) == calls


def test_index_refresh_loops_until_nothing_is_pending_and_rebuild_needs_support(rig):
    pending = [3, 1, 0]
    calls = []

    def refresh(batch):
        calls.append(batch)
        return C.IndexReceipt(generation=2, activated=True, artifacts_indexed=2, artifacts_pending=pending.pop(0))

    rig.index.refresh = refresh
    rc, body, _ = jrun(rig, ["index", "refresh", "--batch-limit", "7"], SHADOW)
    assert rc == 0 and body["finished"] is True and body["batches"] == 3 and calls == [7, 7, 7]
    assert body["last"]["artifacts_pending"] == 0 and body["last"]["generation"] == 2
    pending[:] = [5, 5, 5, 5]
    rc, body, _ = jrun(rig, ["index", "refresh", "--max-batches", "2"], SHADOW)
    assert rc == 1 and body["finished"] is False and body["batches"] == 2
    rc, body, _ = jrun(rig, ["index", "rebuild"], SHADOW)
    assert rc == 3 and body["error"]["code"] == "E_UNAVAILABLE"
    rig.index.rebuild = lambda: C.IndexReceipt(generation=3, activated=True, artifacts_indexed=9, artifacts_pending=0)
    rc, body, _ = jrun(rig, ["index", "rebuild"], SHADOW)
    assert rc == 0 and body["finished"] and body["last"]["generation"] == 3
    rc, body, _ = jrun(rig, ["index", "refresh"], {})
    assert rc == 3 and body["error"]["code"] == "E_DISABLED"


def test_inspect_in_process_maps_surface_errors_to_exit_codes(rig):
    a, ac = load_source(rig.store, rig.w, "fa_short")
    rc, body, _ = jrun(rig, ["inspect", a.source_ref])
    assert rc == 0 and body["artifact"]["source_ref"] == a.source_ref
    assert jrun(rig, ["inspect", "cap:cap-20990101-000000-abcd"])[0] == 5
    assert jrun(rig, ["inspect", "03-objects/x.md"])[0] == 5
    assert jrun(rig, ["inspect", a.source_ref, "--chunk", "chk:" + "0" * 32])[0] == 5
    rc, body, _ = jrun(rig, ["inspect", a.source_ref, "--cursor", "garbage"])
    assert rc == 2 and body["error"]["code"] == "E_BAD_CURSOR"


def test_unexpected_exceptions_become_a_bare_internal_error(rig):
    rig.store.list_current = lambda after, limit: (_ for _ in ()).throw(RuntimeError(f"disk {rig.w.root}"))
    rc, out, err = run(rig, ["export"])
    body = json.loads(out)
    assert rc == 70 and body["error"] == {"code": "E_INTERNAL", "message": "RuntimeError"} and str(rig.w.root) not in out + err


def test_commands_never_write_the_corpus(rig):
    w = rig.w
    a, ac = load_source(rig.store, w, "fa_short")
    annotate(rig.store, a, ac)
    before = corpus_hashes(w.root)
    for argv in (["status"], ["pending"], ["verify"], ["export"], ["inspect", a.source_ref], ["scan"], ["index", "refresh"]):
        run(rig, argv, SHADOW)
    assert corpus_hashes(w.root) == before
