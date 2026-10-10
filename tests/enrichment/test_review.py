"""WP-D: human-only review (CONTRACT-E1 section 11) and the proof that no tool
and no CLI path can approve or promote. Uses the injected-fake store."""
from __future__ import annotations

import ast
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from brain_enrichment import cli
from brain_enrichment import contract as C
from brain_enrichment import review as RV
from brain_enrichment import surface as S
from brain_enrichment.surface import Services
from support import corpus_hashes, make_projection, naive_chunks
from test_surface import ON, RichStore, Rig, annotate, load_source

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
HUMAN = {"actor": "sina@test", "actor_name": "sina", "os_user": "test", "remote": False, "tty": True,
         "confirmation": "interactive", "via": "brain_enrichment.cli"}


class Tty(io.StringIO):
    def isatty(self):
        return True


class Pipe(io.StringIO):
    def isatty(self):
        return False


class Exploding:
    """A store that must never be touched."""

    def __getattr__(self, name):
        raise AssertionError(f"store.{name} was touched")


@pytest.fixture()
def annotated(enrichment_wiki):
    rig = Rig(enrichment_wiki)
    proj, chunks = load_source(rig.store, enrichment_wiki, "fa_short")
    annotate(rig.store, proj, chunks)
    return rig, proj, chunks


def decide(rig, proj, **over):
    kw = {"source_ref": proj.source_ref, "annotation_revision": 1, "decision": "confirmed",
          "note": "checked against the capture", "human_context": HUMAN, **over}
    return RV.record_decision(rig.store, rig.w.root, **kw)


def audit_lines(root):
    p = root / RV.AUDIT_REL
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.is_file() else []


def run_main(rig, argv, *, env=None, stdin=None, stdout=None, services=None):
    out, err = stdout or Pipe(), Pipe()
    rc = cli.main(["--root", str(rig.w.root), "--json", *argv],
                  services=services or Services(rig.w.root, ON, store=rig.store), env={} if env is None else env,
                  stdin=stdin or Pipe(), stdout=out, stderr=err)
    return rc, out.getvalue(), err.getvalue()


# -------------------------------------------------------------- the decision
def test_decision_is_recorded_and_the_receipt_says_nothing_was_promoted(annotated):
    rig, proj, _ = annotated
    before_view = rig.store.get(proj.source_ref)
    before = corpus_hashes(rig.w.root)
    out = decide(rig, proj)
    assert out["ok"] and out["recorded"] and out["decision"] == "confirmed" and out["annotation_revision"] == 1
    assert out["canonical_promotion"] is False and out["promoted"] is False
    assert "nothing was promoted" in out["message"] and "canonical_promotion=false" in out["message"]
    assert out["reviewed_by"] == "sina@test"
    assert rig.store.reviews[0]["note"] == "checked against the capture"
    view = rig.store.get(proj.source_ref)
    assert view.review == {"state": "confirmed", "revision": 1}
    for key in ("source_tier", "source_authority_level", "source_version", "layers", "source_kind"):
        assert view.projection[key] == before_view.projection[key]       # 'confirmed' never raises tier/authority
    assert view.annotation == before_view.annotation
    assert corpus_hashes(rig.w.root) == before
    line, = audit_lines(rig.w.root)
    assert line["outcome"] == "recorded" and line["actor"] == "sina@test" and line["decision"] == "confirmed"
    assert line["source_ref"] == proj.source_ref and line["annotation_revision"] == 1 and line["note"]
    assert (rig.w.root / "_enrichment").stat().st_mode & 0o777 == 0o700


def test_rejected_decision_and_audit_line_per_decision(annotated):
    rig, proj, _ = annotated
    decide(rig, proj, decision="rejected", note="summary is wrong")
    decide(rig, proj, decision="confirmed", note="actually fine")
    lines = audit_lines(rig.w.root)
    assert [x["decision"] for x in lines] == ["rejected", "confirmed"] and all(x["outcome"] == "recorded" for x in lines)
    assert rig.store.get(proj.source_ref).review["state"] == "confirmed"          # append-only: last decision wins


def test_revision_change_since_display_is_a_conflict_and_records_nothing(annotated):
    rig, proj, chunks = annotated
    annotate(rig.store, proj, chunks, key="key-00000002", expected=1, tags=("b",))     # revision 2 appears
    with pytest.raises(C.EnrichmentError) as e:
        decide(rig, proj, annotation_revision=1)
    assert e.value.code == "E_CONFLICT" and e.value.extra["current_revision"] == 2
    assert rig.store.reviews == []
    assert audit_lines(rig.w.root)[-1]["outcome"] == "E_CONFLICT"
    assert decide(rig, proj, annotation_revision=2)["annotation_revision"] == 2


def test_stale_unknown_and_unannotated_sources_cannot_be_reviewed(annotated):
    rig, proj, chunks = annotated
    other, _ = load_source(rig.store, rig.w, "dup_a")
    with pytest.raises(C.EnrichmentError) as e:
        decide(rig, other)                                                    # nothing to review
    assert e.value.code == "E_NOT_FOUND"
    with pytest.raises(C.EnrichmentError) as e:
        decide(rig, proj, source_ref="cap:cap-20990101-000000-abcd")
    assert e.value.code == "E_NOT_FOUND"
    with pytest.raises(C.EnrichmentError) as e:
        decide(rig, proj, source_version="0" * 64)
    assert e.value.code == "E_STALE_SOURCE"
    new = make_projection(proj.source_ref, [("user-text", proj.source_ref, rig.w.texts["fa_short"] + "edit\n", None)])
    rig.store.upsert_projection(new, naive_chunks(new))
    with pytest.raises(C.EnrichmentError) as e:
        decide(rig, proj)                                                     # annotation describes older text
    assert e.value.code == "E_STALE_SOURCE" and rig.store.reviews == []


@pytest.mark.parametrize("over,code", [
    ({"decision": "approved"}, "E_BAD_ARGUMENTS"), ({"decision": "promote"}, "E_BAD_ARGUMENTS"),
    ({"decision": "accepted"}, "E_BAD_ARGUMENTS"), ({"note": ""}, "E_BAD_ARGUMENTS"), ({"note": "   "}, "E_BAD_ARGUMENTS"),
    ({"note": "x" * 1001}, "E_BAD_ARGUMENTS"), ({"annotation_revision": 0}, "E_BAD_ARGUMENTS"),
    ({"annotation_revision": True}, "E_BAD_ARGUMENTS"), ({"annotation_revision": "1"}, "E_BAD_ARGUMENTS"),
    ({"source_ref": "01-inbox/captures/x.md"}, "E_BAD_REF"), ({"source_ref": "prop:abc"}, "E_BAD_REF"),
    ({"source_version": "short"}, "E_BAD_ARGUMENTS"),
])
def test_malformed_requests_are_refused_before_the_store_is_written(annotated, over, code):
    rig, proj, _ = annotated
    with pytest.raises(C.EnrichmentError) as e:
        decide(rig, proj, **over)
    assert e.value.code == code and rig.store.reviews == []


def test_typed_confirmation_must_match_and_a_mismatch_records_nothing(annotated):
    rig, proj, _ = annotated
    ref = f"{proj.source_ref}@1"
    with pytest.raises(C.EnrichmentError) as e:
        decide(rig, proj, confirm=lambda prompt: "yes")
    assert e.value.code == "E_REVIEW_FORBIDDEN" and rig.store.reviews == []
    assert audit_lines(rig.w.root)[-1]["outcome"] == "aborted"
    prompts = []
    out = decide(rig, proj, confirm=lambda p: prompts.append(p) or ref + "\n")
    assert out["recorded"] and ref in prompts[0]


def test_a_store_that_claims_promotion_is_an_internal_error(annotated):
    rig, proj, _ = annotated
    real = rig.store.record_review

    def lying(request, ctx):
        r = real(request, ctx)
        return C.ReviewReceipt(r.source_ref, r.annotation_revision, r.decision, r.reviewed_by, r.reviewed_at,
                               canonical_promotion=True)

    rig.store.record_review = lying
    with pytest.raises(C.EnrichmentError) as e:
        decide(rig, proj)
    assert e.value.code == "E_INTERNAL"


def test_store_refusal_is_audited_and_propagated(annotated):
    rig, proj, _ = annotated

    def refuse(request, ctx):
        raise C.EnrichmentError("E_REVIEW_FORBIDDEN", "store says no")

    rig.store.record_review = refuse
    with pytest.raises(C.EnrichmentError) as e:
        decide(rig, proj)
    assert e.value.code == "E_REVIEW_FORBIDDEN" and audit_lines(rig.w.root)[-1]["outcome"] == "E_REVIEW_FORBIDDEN"


# ------------------------------------------------------------ human context
def test_remote_session_refuses_everywhere_before_touching_anything(enrichment_wiki, monkeypatch):
    root = enrichment_wiki.root
    with pytest.raises(C.EnrichmentError) as e:
        RV.resolve_human_context("sina", env={"BRAIN_REMOTE_SESSION": "1"}, stdin=Tty(), stdout=Tty(), yes=True)
    assert e.value.code == "E_REVIEW_FORBIDDEN"
    monkeypatch.setenv("BRAIN_REMOTE_SESSION", "1")
    with pytest.raises(C.EnrichmentError) as e:                                # even with a perfect context object
        RV.record_decision(Exploding(), root, source_ref="cap:cap-20260101-000000-aaaa", annotation_revision=1,
                           decision="confirmed", note="n", human_context=HUMAN)
    assert e.value.code == "E_REVIEW_FORBIDDEN"
    monkeypatch.delenv("BRAIN_REMOTE_SESSION")
    out, err = Pipe(), Pipe()
    rc = cli.main(["--root", str(root), "--json", "review", "cap:cap-20260101-000000-aaaa", "--revision", "1",
                   "--decision", "confirmed", "--note", "n", "--actor", "sina", "--yes"],
                  services=Services(root, ON, store=Exploding()), env={"BRAIN_REMOTE_SESSION": "1"},
                  stdin=Tty(), stdout=out, stderr=err)
    body = json.loads(out.getvalue())
    assert rc == 3 and body["error"]["code"] == "E_REVIEW_FORBIDDEN" and body["ok"] is False
    assert not (root / "_enrichment").exists()


@pytest.mark.parametrize("actor", ["", "   ", None, "claude", "Hermes", "mcp", "ChatGPT", "assistant", "bot",
                                   "claude@laptop", "sina\nclaude", "sina\x00", "a" * 81, "system", 5, ["sina"]])
def test_empty_forged_or_machine_actors_are_refused(actor):
    with pytest.raises(C.EnrichmentError) as e:
        RV.resolve_human_context(actor, env={}, stdin=Tty(), stdout=Tty(), user="sina")
    assert e.value.code == "E_REVIEW_FORBIDDEN"


def test_forged_human_context_objects_are_refused(annotated):
    rig, proj, _ = annotated
    for ctx in (None, {}, {"actor": ""}, {"actor": "   "}, {"actor": "sina", "remote": True},
                {"remote": False}):
        with pytest.raises(C.EnrichmentError) as e:
            decide(rig, proj, human_context=ctx)
        assert e.value.code == "E_REVIEW_FORBIDDEN", ctx
    assert rig.store.reviews == []


def test_terminal_policy():
    ctx = RV.resolve_human_context("Sina", env={}, stdin=Tty(), stdout=Tty(), user="sina")
    assert ctx["actor"] == "Sina@sina" and ctx["tty"] is True and ctx["remote"] is False and ctx["confirmation"] == "interactive"
    ctx = RV.resolve_human_context("سینا", env={}, stdin=Tty(), stdout=Tty(), user="sina")
    assert ctx["actor"] == "سینا@sina"                                        # Persian names are fine
    with pytest.raises(C.EnrichmentError) as e:                               # no tty, no --yes
        RV.resolve_human_context("sina", env={}, stdin=Pipe(), stdout=Pipe(), user="sina")
    assert e.value.code == "E_REVIEW_FORBIDDEN" and "terminal" in e.value.message
    ctx = RV.resolve_human_context("sina", env={}, stdin=Pipe(), stdout=Pipe(), yes=True, user="sina")
    assert ctx["tty"] is False and ctx["confirmation"] == "flag"
    for marker in RV.AGENT_ENV_MARKERS:                                       # agent shell: --yes is not enough
        with pytest.raises(C.EnrichmentError) as e:
            RV.resolve_human_context("sina", env={marker: "1"}, stdin=Pipe(), stdout=Pipe(), yes=True, user="sina")
        assert e.value.code == "E_REVIEW_FORBIDDEN"
    with pytest.raises(C.EnrichmentError):
        RV.resolve_human_context("sina", env={}, stdin=Tty(), stdout=Tty(), user="")


# ------------------------------------------------------------------- the CLI
def test_cli_review_end_to_end_json_and_text(annotated):
    rig, proj, _ = annotated
    argv = ["review", proj.source_ref, "--revision", "1", "--decision", "confirmed", "--note", "looks right",
            "--actor", "Sina", "--yes"]
    rc, out, err = run_main(rig, argv)
    body = json.loads(out)
    assert rc == 0 and body["ok"] and body["recorded"] and body["canonical_promotion"] is False and body["promoted"] is False
    assert body["reviewed_by"].startswith("Sina@") and err == ""
    # text mode, interactive typed confirmation
    stdin, stdout = Tty(f"{proj.source_ref}@1\n"), Tty()
    rc = cli.main(["--root", str(rig.w.root), "review", proj.source_ref, "--revision", "1", "--decision", "rejected",
                   "--note", "wrong tag", "--actor", "Sina"], services=Services(rig.w.root, ON, store=rig.store),
                  env={}, stdin=stdin, stdout=stdout, stderr=Pipe())
    text = stdout.getvalue()
    assert rc == 0 and "REVIEW RECORDED" in text and "canonical_promotion=false promoted=false" in text
    assert [r["decision"] for r in rig.store.reviews] == ["confirmed", "rejected"]
    assert [x["outcome"] for x in audit_lines(rig.w.root)] == ["recorded", "recorded"]


def test_cli_review_failures_have_distinct_exit_codes(annotated):
    rig, proj, chunks = annotated
    base = ["review", proj.source_ref, "--decision", "confirmed", "--note", "n", "--actor", "Sina", "--yes"]
    annotate(rig.store, proj, chunks, key="key-00000002", expected=1, tags=("b",))
    rc, out, _ = run_main(rig, base + ["--revision", "1"])
    body = json.loads(out)
    assert rc == 4 and body["error"]["code"] == "E_CONFLICT" and body["error"]["current_revision"] == 2
    rc, out, _ = run_main(rig, base + ["--revision", "2", "--actor", "claude"])
    assert rc == 3 and json.loads(out)["error"]["code"] == "E_REVIEW_FORBIDDEN"
    rc, out, _ = run_main(rig, ["review", "cap:cap-20990101-000000-abcd", "--revision", "1", "--decision", "confirmed",
                                "--note", "n", "--actor", "Sina", "--yes"])
    assert rc == 5 and json.loads(out)["error"]["code"] == "E_NOT_FOUND"
    rc, out, _ = run_main(rig, base + ["--revision", "2"], stdin=Pipe(), env={"CLAUDECODE": "1"})
    assert rc == 3 and "agent harness" in json.loads(out)["error"]["message"]
    rc, out, _ = run_main(rig, base + ["--revision", "2"])
    assert rc == 0 and json.loads(out)["annotation_revision"] == 2


def test_cli_review_requires_every_flag_and_only_two_decisions(annotated):
    rig, proj, _ = annotated
    for argv in (["review", proj.source_ref, "--revision", "1", "--note", "n", "--actor", "s"],
                 ["review", proj.source_ref, "--revision", "1", "--decision", "approve", "--note", "n", "--actor", "s"],
                 ["review", proj.source_ref, "--revision", "1", "--decision", "promote", "--note", "n", "--actor", "s"],
                 ["review", proj.source_ref, "--decision", "confirmed", "--note", "n", "--actor", "s"],
                 ["review", proj.source_ref, "--revision", "1", "--decision", "confirmed", "--actor", "s"],
                 ["review", proj.source_ref, "--revision", "1", "--decision", "confirmed", "--note", "n"]):
        with pytest.raises(SystemExit) as e:
            cli.main(["--root", str(rig.w.root), *argv], services=Services(rig.w.root, ON, store=rig.store),
                     env={}, stdin=Tty(), stdout=Pipe(), stderr=Pipe())
        assert e.value.code == 2, argv
    assert rig.store.reviews == []


# ----------------------------------------------- no approve / promote anywhere
def _modules():
    for name in ("surface.py", "review.py", "cli.py"):
        path = SCRIPTS / "brain_enrichment" / name
        yield name, ast.parse(path.read_text(encoding="utf-8"))


def test_no_module_imports_or_calls_the_canonical_promotion_path():
    for name, tree in _modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not any("brain_review" in a.name for a in node.names), name
            if isinstance(node, ast.ImportFrom):
                assert "brain_review" not in (node.module or ""), name
                assert not any("brain_review" in a.name for a in node.names), name
            if isinstance(node, ast.Call):
                fn = node.func
                called = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                assert called not in ("promote", "decide", "accept", "approve"), (name, called)


def test_cli_has_no_approve_or_promote_command_and_the_extension_has_no_review_tool():
    parser = cli.build_parser()
    choices = next(a for a in parser._actions if a.dest == "cmd").choices
    assert set(choices) == {"inspect", "scan", "index", "pending", "status", "verify", "review", "export"}
    review = choices["review"]
    decision = next(a for a in review._actions if a.dest == "decision")
    assert tuple(decision.choices) == ("confirmed", "rejected")
    for forbidden in ("approve", "promote", "accept", "reject", "adjudicate"):
        assert forbidden not in choices
    for t in S.T.EXTENSION_TOOLS:
        assert "review" not in t["name"] and "promote" not in t["name"]
    rig_services = Services(Path("."), ON, store=RichStore(), annotation=object(), retrieval=object())
    ext = S.ExtensionSurface(Path("."), ON, rig_services)
    for name in ("brain_review", "brain_enrichment_review", "brain_promote", "brain_review_promote"):
        out = ext.call(name, {"source_ref": "cap:cap-1", "decision": "confirmed"})
        assert out["error"]["code"] == "E_UNKNOWN_ARGUMENT"


def test_review_cannot_be_reached_through_submit_or_artifact_arguments(annotated):
    rig, proj, _ = annotated
    for tool, args in (("brain_enrichment_submit", {"decision": "confirmed", "source_ref": proj.source_ref}),
                       ("brain_artifact_read", {"source_ref": proj.source_ref, "review": "confirmed"}),
                       ("brain_enrichment_pending", {"actor": "owner"}),
                       ("brain_retrieve", {"query": "q", "promote": True})):
        assert rig.ext.call(tool, args)["error"]["code"] == "E_UNKNOWN_ARGUMENT"
    # a submit can never smuggle review state or authority into an annotation
    out = rig.ext.call("brain_enrichment_submit", {
        "source_ref": proj.source_ref, "source_version": proj.source_version, "expected_annotation_revision": 1,
        "idempotency_key": "key-00000009", "annotation": {"tags": ["x"], "review_state": "confirmed"}})
    assert rig.store.get(proj.source_ref).review["state"] == "unreviewed"
    assert out["ok"] is True or out["error"]["code"] in ("E_VALIDATION",)    # the real validator rejects unknown keys


def test_audit_directory_must_stay_inside_the_wiki(annotated, tmp_path):
    rig, proj, _ = annotated
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (rig.w.root / "_enrichment").symlink_to(outside, target_is_directory=True)
    with pytest.raises(C.EnrichmentError) as e:
        decide(rig, proj)
    assert e.value.code == "E_UNAVAILABLE" and list(outside.iterdir()) == [] and rig.store.reviews == []


def test_review_cli_as_a_subprocess_refuses_in_a_remote_session(enrichment_wiki):
    env = {**os.environ, "BRAIN_REMOTE_SESSION": "1", "PYTHONPATH": str(SCRIPTS)}
    p = subprocess.run([sys.executable, "-m", "brain_enrichment.cli", "--root", str(enrichment_wiki.root), "--json",
                        "review", "cap:cap-20260101-000000-aaaa", "--revision", "1", "--decision", "confirmed",
                        "--note", "n", "--actor", "sina", "--yes"],
                       capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL)
    body = json.loads(p.stdout)
    assert p.returncode == 3 and body["error"]["code"] == "E_REVIEW_FORBIDDEN"
    assert str(enrichment_wiki.root) not in p.stdout + p.stderr and not (enrichment_wiki.root / "_enrichment").exists()
