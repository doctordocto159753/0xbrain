"""brain_ingest_file: exact-original held intake (archive, failure, security).

Runs against a real throwaway git wiki through the real surface/backend.
Files reach the server only as owner-staged uploads (upload_ref); see
scripts/brain_surface/uploads.py for why no other channel exists.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import commit_all
from test_io_conversion_exports import PARA, BUILDERS, EXPECT, mk_pdf, mk_pdf_scanned

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from brain_surface import BrainSurface  # noqa: E402
from brain_surface import ingest as ING  # noqa: E402
from brain_surface import refs as R  # noqa: E402
from brain_surface import uploads as UP  # noqa: E402
from brain_surface.backend import WikiBackend  # noqa: E402


@pytest.fixture()
def env(brain_wiki, tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_UPLOAD_DIR", str(tmp_path / "staging"))
    return brain_wiki, BrainSurface(WikiBackend(brain_wiki))


def git(root, *a):
    return subprocess.run(["git", *a], cwd=root, text=True, capture_output=True,
                          check=True).stdout


def stage(data: bytes, name: str) -> str:
    return UP.stage(io.BytesIO(data), name)["upload_ref"]


def build(tmp_path, fmt) -> bytes:
    p = tmp_path / f"sample.{fmt}"
    if fmt == "pdf":      # enough text that to_md does not classify it as needs-OCR
        mk_pdf(p, "\n".join([PARA] * 4))
    else:
        BUILDERS[fmt](p)
    return p.read_bytes()


def ingest(s, ref, **kw):
    return s.call("brain_ingest_file", {"upload_ref": ref, **kw})


def held(root) -> dict:
    return json.loads((root / ING.CORPUS_STATE).read_text())


# ---------------------------------------------------------------- archive

@pytest.mark.parametrize("fmt", ["pdf", "docx", "pptx", "xlsx", "html", "epub"])
def test_binary_formats_preserved_exactly_with_record_and_derivative(env, tmp_path, fmt):
    root, s = env
    data = build(tmp_path, fmt)
    sha = hashlib.sha256(data).hexdigest()
    out = ingest(s, stage(data, f"report.{fmt}"), title=f"Report ({fmt})")
    assert out["ok"] and out["duplicate"] is False, out
    assert out["sha256"] == sha and out["bytes"] == len(data)
    assert out["extraction"] == "complete" and out["commit_state"] == "committed"
    assert out["validation"] == "pass" and out["persisted"] is True
    rid = out["source_ref"][4:]
    assert rid.endswith(sha[:12])
    rec = (root / ING.RECORDS_DIR / f"{rid}.md").read_text(encoding="utf-8")
    orig_rel = next(l.split(": ", 1)[1] for l in rec.splitlines() if l.startswith("original_path:"))
    der_rel = next(l.split(": ", 1)[1] for l in rec.splitlines()
                   if l.startswith("extracted_text_path:"))
    assert orig_rel.startswith("_originals/remote-mcp/") and orig_rel.endswith(f".{fmt}")
    assert (root / orig_rel).read_bytes() == data                       # exact bytes
    assert not os.access(root / orig_rel, os.W_OK) or os.geteuid() == 0
    assert stat.S_IMODE((root / orig_rel).stat().st_mode) == 0o444
    for field in (f"sha256: {sha}", f"bytes: {len(data)}", "holdings_tier: pending-registration",
                  "status: pending-registration", "ingestion_channel: remote-mcp",
                  'filename: "report.' + fmt + '"', "type: source-record"):
        assert field in rec, field
    der = (root / der_rel).read_text(encoding="utf-8")
    assert f"sha256: {sha}" in der and EXPECT[fmt] in der             # F provenance header
    assert out["derivative_ref"] == R.public_ref_for_path(root, der_rel)
    st = held(root)
    assert st["held_artifact_count"] == 1 and st["holdings_by_tier"]["pending-registration"] == 1
    assert st["source_material_count"] == 0                            # not registered
    for page in ING.ENTRY_PAGES:
        assert "Artifacts held: 1" in (root / page).read_text(encoding="utf-8")
    changed = set(git(root, "show", "--name-only", "--format=", "HEAD").split())
    assert changed == {orig_rel, der_rel, f"{ING.RECORDS_DIR}/{rid}.md", ING.CORPUS_STATE,
                       *ING.ENTRY_PAGES}
    assert git(root, "status", "--porcelain").strip() == ""
    assert subprocess.run([sys.executable, "scripts/validate_repo.py", "--full"], cwd=root,
                          capture_output=True).returncode == 0
    got = s.call("brain_read", {"ref": out["source_ref"]})
    assert got["ok"] and sha in got["content"]


@pytest.mark.parametrize("name,data", [("notes.txt", "plain\r\nline two\n".encode()),
                                       ("notes.md", "# Title\n\nمتن فارسی **bold**\n".encode())])
def test_text_formats_and_search_find_derivative(env, name, data):
    root, s = env
    out = ingest(s, stage(data, name))
    assert out["ok"] and out["extraction"] == "complete"
    orig = next((root / "_originals/remote-mcp").iterdir())
    assert orig.read_bytes() == data                                   # CRLF kept in original
    word = "فارسی" if name.endswith(".md") else "plain"
    hits = s.call("brain_search", {"query": word, "mode": "exact", "scope": "canonical"})
    refs = [h["ref"] for h in hits["groups"]["canonical"]["results"]]
    assert out["derivative_ref"] in refs and out["source_ref"] in refs or out["derivative_ref"] in refs


def test_duplicate_bytes_do_not_create_a_second_original(env, tmp_path):
    root, s = env
    data = build(tmp_path, "pdf")
    first = ingest(s, stage(data, "a.pdf"))
    head = git(root, "rev-parse", "HEAD")
    again = ingest(s, stage(data, "renamed-copy.pdf"))
    assert again["ok"] and again["duplicate"] is True
    assert again["source_ref"] == first["source_ref"] and again["commit_state"] == "not_needed"
    assert len(list((root / "_originals").rglob("*.pdf"))) == 1
    assert git(root, "rev-parse", "HEAD") == head and held(root)["held_artifact_count"] == 1
    assert UP.pending() == 0                                           # staged copy consumed


def test_same_filename_different_bytes_are_two_artifacts(env):
    root, s = env
    a = ingest(s, stage(b"version one\n", "same.txt"))
    b = ingest(s, stage(b"version two\n", "same.txt"))
    assert a["source_ref"] != b["source_ref"] and not b["duplicate"]
    files = sorted(p.read_bytes() for p in (root / "_originals/remote-mcp").iterdir())
    assert files == [b"version one\n", b"version two\n"]
    assert held(root)["held_artifact_count"] == 2


def test_unicode_persian_filename(env):
    root, s = env
    out = ingest(s, stage("سلام\n".encode(), "یادداشت ‌جلسه ۱۴۰۳.txt"))
    assert out["ok"] and out["filename"] == "یادداشت ‌جلسه ۱۴۰۳.txt"
    orig = next((root / "_originals/remote-mcp").iterdir())
    assert "یادداشت" in orig.name and "/" not in orig.name and orig.suffix == ".txt"


# ---------------------------------------------------------------- failure

@pytest.mark.parametrize("name,data,code", [
    ("tool.exe", b"MZ\x90\x00", "E_UNSUPPORTED_TYPE"),
    ("script.sh", b"#!/bin/sh\nrm -rf /\n", "E_UNSUPPORTED_TYPE"),
    ("archive.zip", b"PK\x03\x04", "E_UNSUPPORTED_TYPE"),
    ("noext", b"hello", "E_UNSUPPORTED_TYPE"),
    ("fake.pdf", b"MZ not a pdf at all", "E_TYPE_MISMATCH"),
    ("fake.docx", b"%PDF-1.4 pretending", "E_TYPE_MISMATCH"),
    ("binary.txt", b"abc\x00def", "E_TYPE_MISMATCH"),
    ("latin1.txt", "caf\xe9".encode("latin-1"), "E_TYPE_MISMATCH"),
])
def test_rejected_types_store_nothing(env, name, data, code):
    root, s = env
    head = git(root, "rev-parse", "HEAD")
    out = ingest(s, stage(data, name))
    assert out["ok"] is False and out["persisted"] is False and out["error"]["code"] == code
    assert not (root / "_originals").exists() or not any((root / "_originals").rglob("*.*"))
    assert git(root, "rev-parse", "HEAD") == head


def test_docx_container_without_word_part_rejected(env, tmp_path):
    import zipfile
    p = tmp_path / "x.docx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("hello.txt", "not a document")
    out = ingest(env[1], stage(p.read_bytes(), "x.docx"))
    assert out["error"]["code"] == "E_TYPE_MISMATCH"


def test_zip_bomb_rejected_before_extraction(env, tmp_path, monkeypatch):
    import zipfile
    p = tmp_path / "bomb.docx"
    with zipfile.ZipFile(p, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", "")
        z.writestr("word/padding.bin", b"\0" * (5 * 1024 * 1024))
    out = ingest(env[1], stage(p.read_bytes(), "bomb.docx"))
    assert out["error"]["code"] == "E_ARCHIVE_ABUSE"


def test_oversized_upload_refused(env, monkeypatch):
    monkeypatch.setenv("BRAIN_UPLOAD_MAX_BYTES", "1000")
    with pytest.raises(UP.UploadError) as e:
        stage(b"x" * 1001, "big.txt")
    assert e.value.code == "E_TOO_LARGE"
    assert not any(Path(os.environ["BRAIN_UPLOAD_DIR"]).iterdir())


@pytest.mark.parametrize("args", [{}, {"upload_ref": ""}, {"upload_ref": 5},
                                  {"upload_ref": "upl-x", "title": 7},
                                  {"upload_ref": "upl-x", "path": "/etc/passwd"},
                                  {"upload_ref": "upl-x", "url": "https://example.com/a.pdf"},
                                  {"upload_ref": "upl-x", "content_base64": "AAAA"}])
def test_malformed_arguments(env, args):
    out = env[1].call("brain_ingest_file", args)
    assert out["ok"] is False and out["persisted"] is False


def test_scanned_pdf_preserved_and_marked_needs_ocr(env, tmp_path):
    root, s = env
    p = tmp_path / "scan.pdf"
    mk_pdf_scanned(p)
    out = ingest(s, stage(p.read_bytes(), "scan.pdf"))
    assert out["ok"] and out["extraction"] == "needs_ocr" and out["derivative_ref"] is None
    assert out["commit_state"] == "committed"
    rec = next((root / ING.RECORDS_DIR).glob("*.md")).read_text()
    assert "extraction_status: needs_ocr" in rec and "extracted_text_path: \n" in rec


def test_extraction_failure_still_preserves_original(env, tmp_path):
    root, s = env
    bad = b"%PDF-1.4\n" + b"garbage that no parser can read" * 10
    out = ingest(s, stage(bad, "broken.pdf"))
    assert out["ok"] and out["extraction"] == "preserved_only" and out["derivative_ref"] is None
    assert next((root / "_originals/remote-mcp").iterdir()).read_bytes() == bad
    assert out["commit_state"] == "committed"


def test_commit_failure_preserves_ingest_and_status_reports_it(env, tmp_path):
    root, s = env
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "pre-commit").write_text("#!/bin/sh\necho blocked >&2\nexit 1\n")
    (hooks / "pre-commit").chmod(0o755)
    git(root, "config", "core.hooksPath", str(hooks))
    out = ingest(s, stage(b"must survive\n", "keep.txt"))
    assert out["ok"] and out["persisted"] and out["commit_state"] == "persisted_uncommitted"
    assert "blocked" in out["commit_error"]
    assert next((root / "_originals/remote-mcp").iterdir()).read_bytes() == b"must survive\n"
    st = s.call("brain_status", {})
    assert st["ingest"]["uncommitted"] and st["degraded"]
    assert "uncommitted ingested material" in st["degraded_reasons"]
    git(root, "config", "core.hooksPath", "/dev/null")
    import brain_review as br
    res = br.flush_ingest(root)
    assert res["committed"], res
    assert s.call("brain_status", {})["ingest"]["uncommitted"] == []


# ---------------------------------------------------------------- security

def test_upload_ref_cannot_be_guessed_reused_or_used_after_expiry(env, monkeypatch):
    root, s = env
    for bogus in ("upl-" + "A" * 43, "../../etc/passwd", "upl-" + "A" * 42 + "/",
                  "upl-" + "a" * 43 + "\n"):
        out = ingest(s, bogus)
        assert out["ok"] is False and out["persisted"] is False
    ref = stage(b"once\n", "once.txt")
    assert ingest(s, ref)["ok"]
    assert ingest(s, ref)["error"]["code"] == "E_UPLOAD_NOT_FOUND"     # single use
    monkeypatch.setenv("BRAIN_UPLOAD_TTL", "1")
    ref2 = stage(b"late\n", "late.txt")
    time.sleep(1.2)
    assert ingest(s, ref2)["error"]["code"] == "E_UPLOAD_NOT_FOUND"    # expired and deleted
    assert UP.pending() == 0


def test_bidi_override_cannot_spoof_the_extension(env):
    out = ingest(env[1], stage(b"MZ", "invoice\u202etxt.exe"))
    assert out["error"]["code"] == "E_UNSUPPORTED_TYPE"


def test_staging_is_private_and_names_are_not_refs(env):
    ref = stage(b"secret doc\n", "../../../etc/passwd")
    d = Path(os.environ["BRAIN_UPLOAD_DIR"])
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    for p in d.iterdir():
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
        assert ref not in p.name and ref[4:] not in p.name
    info = UP.claim(ref)[1]
    assert info["filename"] == "passwd"                                  # basename only


def test_existing_original_is_never_overwritten(env):
    root, s = env
    data = b"original bytes\n"
    sha = hashlib.sha256(data).hexdigest()
    rid = f"mw-src-{sha[:12]}"
    target = root / ING.ORIGINALS_DIR / f"{rid}--x.txt"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"something else that is already held")
    commit_all(root)
    out = ingest(s, stage(data, "x.txt"))
    assert out["error"]["code"] == "E_EXISTS" and out["persisted"] is False
    assert target.read_bytes() == b"something else that is already held"


def test_symlinked_originals_zone_is_refused(env, tmp_path):
    root, s = env
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "_originals").mkdir(exist_ok=True)
    os.symlink(outside, root / ING.ORIGINALS_DIR)
    out = ingest(s, stage(b"escape?\n", "e.txt"))
    assert out["ok"] is False and out["error"]["code"] == "E_UNSAFE_PATH"
    assert not any(outside.iterdir())


def test_no_server_path_or_url_mode_exists():
    from brain_surface import TOOLS
    spec = next(t for t in TOOLS if t["name"] == "brain_ingest_file")["inputSchema"]
    assert set(spec["properties"]) == {"upload_ref", "title", "description"}
    assert spec["additionalProperties"] is False
    src = (ROOT / "scripts/brain_surface/ingest.py").read_text() + \
        (ROOT / "scripts/brain_surface/uploads.py").read_text() + \
        (ROOT / "scripts/remote_mcp/upload.py").read_text()
    for forbidden in ("urlopen", "requests.get", "httpx", "file://"):
        assert forbidden not in src


def test_ingest_path_policy():
    ok = ["_originals/remote-mcp/x.pdf", "02-sources/records/a.md", "02-sources/text/a.md",
          "00-system/registers/CORPUS_STATE.json", "HOME.md"]
    bad = ["03-objects/x.md", "_originals/../HOME.md", "/etc/passwd", "scripts/x.py",
           "00-system/registers/MATERIALS_INDEX.jsonl", "_proposals/proposals.jsonl"]
    assert all(ING.is_ingest_path(p) for p in ok)
    assert not any(ING.is_ingest_path(p) for p in bad)


def test_flush_ingest_never_commits_a_modified_original(env):
    root, s = env
    out = ingest(s, stage(b"held\n", "h.txt"))
    orig = next((root / "_originals/remote-mcp").iterdir())
    orig.chmod(0o644)
    orig.write_bytes(b"tampered\n")
    import brain_review as br
    res = br.flush_ingest(root)
    assert res["committed"] is False and res["stage"] == "refused-canonical"
    assert git(root, "show", f"HEAD:{orig.relative_to(root).as_posix()}") == "held\n"


def test_new_validator_error_blocks_commit_but_preexisting_does_not(env, tmp_path):
    root, _ = env
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import pathlib, sys\n"
        "print('ERROR: pre-existing problem'); \n"
        "held = list(pathlib.Path('_originals').rglob('*.txt'))\n"
        "if held: print('ERROR: introduced by ingest')\n"
        "sys.exit(1)\n")
    ref = stage(b"gate me\n", "g.txt")
    r = ING.ingest(root, ref, validate_cmds=[[str(probe)]])
    k9 = r["k9"]
    assert k9["committed"] is False and k9["stage"] == "validation"
    assert "introduced by ingest" in k9["error"] and "pre-existing" not in k9["error"]
    assert next((root / "_originals/remote-mcp").iterdir()).read_bytes() == b"gate me\n"
