"""Agent F: file-to-md conversion fixtures and interchange/public export qualification.

Fixtures are generated at test time (no binaries committed). Exports run against a
throwaway populated tree so the empty kit is never mutated. Tests that need an
optional converter dependency skip when it is absent.
"""
import hashlib
import json
import shutil
import socket
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TO_MD = REPO / "scripts/file-to-md/to_md.py"
PARA = "The quick brown fox jumps over the lazy dog near the riverbank today."


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def convert(src, out=None):
    out = out or src.with_suffix(".extracted.md")
    r = subprocess.run([sys.executable, str(TO_MD), str(src), "-o", str(out)],
                       capture_output=True, text=True)
    return r, out


# ---------------------------------------------------------------- builders

def mk_pdf(p, text=PARA):
    pymupdf = pytest.importorskip("pymupdf")
    d = pymupdf.open()
    pg = d.new_page()
    pg.insert_text((72, 72), text)
    d.save(str(p))


def mk_pdf_scanned(p):
    pymupdf = pytest.importorskip("pymupdf")
    d = pymupdf.open()
    d.new_page()
    d.save(str(p))


def mk_docx(p):
    docx = pytest.importorskip("docx")
    d = docx.Document()
    d.add_heading("Main Title", 1)
    d.add_paragraph(PARA)
    t = d.add_table(rows=2, cols=2)
    for i, v in enumerate(["a", "b", "c", "d"]):
        t.cell(i // 2, i % 2).text = v
    d.save(str(p))


def mk_pptx(p):
    pptx = pytest.importorskip("pptx")
    prs = pptx.Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[1])
    s.shapes.title.text = "Slide Title"
    s.placeholders[1].text = PARA
    prs.save(str(p))


def mk_xlsx(p):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    ws.append(["name", "value"])
    ws.append(["alpha", 1])
    wb.save(str(p))


def mk_html(p):
    pytest.importorskip("bs4")
    pytest.importorskip("lxml")
    p.write_text("<html><body><script>evil()</script><h1>Head</h1><p>" + PARA
                 + "</p></body></html>", encoding="utf-8")


def mk_epub(p):
    pytest.importorskip("pymupdf")
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container version="1.0" '
                   'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                   '<rootfile full-path="c.opf" media-type="application/oebps-package+xml"/>'
                   '</rootfiles></container>')
        z.writestr("c.opf",
                   '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
                   'version="2.0" unique-identifier="i"><metadata xmlns:dc='
                   '"http://purl.org/dc/elements/1.1/"><dc:title>T</dc:title>'
                   '<dc:identifier id="i">x</dc:identifier><dc:language>en</dc:language>'
                   '</metadata><manifest><item id="c" href="c.xhtml" '
                   'media-type="application/xhtml+xml"/></manifest>'
                   '<spine><itemref idref="c"/></spine></package>')
        z.writestr("c.xhtml",
                   '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>T</title></head>'
                   '<body><p>' + PARA * 3 + '</p></body></html>')


BUILDERS = {"pdf": mk_pdf, "docx": mk_docx, "pptx": mk_pptx, "xlsx": mk_xlsx,
            "html": mk_html, "epub": mk_epub}
EXPECT = {"pdf": "quick brown fox", "docx": "Main Title", "pptx": "Slide Title",
          "xlsx": "alpha", "html": "Head", "epub": "quick brown fox"}


@pytest.mark.parametrize("fmt", list(BUILDERS))
def test_convert_format(tmp_path, fmt, monkeypatch):
    src = tmp_path / ("sample." + fmt)
    BUILDERS[fmt](src)
    before = sha(src)
    r, out = convert(src)
    assert r.returncode == 0, r.stdout + r.stderr
    summary = json.loads(r.stdout)
    assert summary["ok"] and summary["sha256"] == before
    md = out.read_text(encoding="utf-8")
    assert EXPECT[fmt] in md
    assert "sha256: " + before in md.splitlines()[2]
    assert sha(src) == before  # original untouched
    if fmt == "html":
        assert "evil()" not in md
    # deterministic: second run byte-identical
    r2, out2 = convert(src, tmp_path / "second.md")
    assert out2.read_text(encoding="utf-8") == md


def test_scanned_pdf_flags_ocr_not_invented(tmp_path):
    src = tmp_path / "scan.pdf"
    mk_pdf_scanned(src)
    r, out = convert(src)
    assert r.returncode == 0
    assert json.loads(r.stdout)["needs_ocr"] is True
    md = out.read_text(encoding="utf-8")
    assert "NEEDS-OCR" in md and "sha256: " in md


def test_unsupported_and_legacy_refused(tmp_path):
    for name, code in (("x.doc", 2), ("x.bin", 2), ("missing.pdf", 1)):
        p = tmp_path / name
        if name != "missing.pdf":
            p.write_bytes(b"\x00")
        r, _ = convert(p)
        assert r.returncode == code


def test_converter_needs_no_network(tmp_path):
    """Run the converter in-process with sockets disabled."""
    src = tmp_path / "n.docx"
    mk_docx(src)
    code = (
        "import socket,sys,runpy\n"
        "def boom(*a,**k): raise RuntimeError('network used')\n"
        "socket.socket.connect=boom; socket.create_connection=boom\n"
        "socket.getaddrinfo=boom\n"
        "sys.argv=['to_md.py',%r,'-o',%r]\n"
        "runpy.run_path(%r,run_name='__main__')\n"
    ) % (str(src), str(tmp_path / "n.md"), str(TO_MD))
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


# ---------------------------------------------------------------- exports

def write(p, text):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


@pytest.fixture
def wiki(tmp_path):
    pytest.importorskip("yaml")
    root = tmp_path / "wiki"
    (root / "scripts").mkdir(parents=True)
    for n in ("export_interchange.py", "export_public.py"):
        shutil.copy(REPO / "scripts" / n, root / "scripts" / n)
    orig = root / "_originals/doc.pdf"
    orig.parent.mkdir(parents=True)
    orig.write_bytes(b"%PDF-fake")
    digest = sha(orig)
    write(root / "02-sources/src-1.md",
          "---\nid: SRC-1\ntype: source-record\ntitle: Pub Source\nvisibility: public\n"
          "validation_status: verified\n---\n# Pub\n\nBody [[Secret Person]].\n")
    write(root / "03-objects/obj-1.md",
          "---\nid: OBJ-1\ntype: person\ntitle: Secret Person\nvisibility: private\n"
          "aliases: [SP]\ncurrent_claim_permission: candidate\n---\nPRIVATE-BODY-TEXT\n")
    write(root / "00-system/registers/MATERIALS_INDEX.jsonl", json.dumps({
        "id": "SRC-1", "filename": "doc.pdf", "original_path": "_originals/doc.pdf",
        "sha256": digest, "extracted_text_path": "02-sources/text/doc.extracted.md",
        "source_record_path": "02-sources/src-1.md", "format": "pdf",
        "page_count": 1, "word_count": 3, "extraction_quality": "clean",
        "preservation_status": "held"}) + "\n")
    return root, digest


def run(root, script, *args):
    return subprocess.run([sys.executable, str(root / "scripts" / script), *args],
                          capture_output=True, text=True, cwd=root)


def test_prov_skos_tei_exports(wiki):
    root, digest = wiki
    for args in (("prov",), ("skos",), ("tei", "--source-id", "SRC-1")):
        r = run(root, "export_interchange.py", *args)
        assert r.returncode == 0, r.stdout + r.stderr
    out = root / "_exports/interchange"
    prov = json.loads((out / "prov-graph.jsonld").read_text(encoding="utf-8"))
    nodes = {n["@id"]: n for n in prov["@graph"] if "mw:sha256" in n or "@id" in n}
    orig = next(n for n in prov["@graph"] if n["@id"] == "file:_originals/doc.pdf")
    assert orig["mw:sha256"] == digest
    assert any(n.get("prov:wasDerivedFrom") == {"@id": "file:_originals/doc.pdf"}
               for n in prov["@graph"])
    obj = nodes["mw:OBJ-1"]
    assert obj["mw:authorityTierNote"] == "candidate"  # tier travels intact
    skos = json.loads((out / "skos-concepts.jsonld").read_text(encoding="utf-8"))
    assert {c["skos:notation"] for c in skos["@graph"] if "skos:notation" in c} == {"SRC-1", "OBJ-1"}
    tei = (out / "SRC-1.tei.xml").read_text(encoding="utf-8")
    assert digest in tei
    import xml.dom.minidom
    xml.dom.minidom.parseString(tei)  # well-formed
    # deterministic
    first = (out / "prov-graph.jsonld").read_bytes()
    run(root, "export_interchange.py", "prov")
    assert (out / "prov-graph.jsonld").read_bytes() == first


def test_tei_unknown_id_fails(wiki):
    root, _ = wiki
    assert run(root, "export_interchange.py", "tei", "--source-id", "NOPE").returncode == 1


def test_rocrate_absent_and_valid(wiki):
    root, _ = wiki
    r = run(root, "export_interchange.py", "rocrate-check")
    assert r.returncode == 0 and "NO DESCRIPTOR" in r.stdout
    crate = {"@context": "https://w3id.org/ro/crate/1.1/context", "@graph": [
        {"@id": "ro-crate-metadata.json", "@type": "CreativeWork",
         "about": {"@id": "./"},
         "conformsTo": {"@id": "https://w3id.org/ro/crate/1.1"}},
        {"@id": "./", "@type": "Dataset",
         "hasPart": [{"@id": "02-sources/src-1.md"}]}]}
    write(root / "ro-crate-metadata.json", json.dumps(crate))
    r = run(root, "export_interchange.py", "rocrate-check")
    assert r.returncode == 0 and "VALID" in r.stdout, r.stdout
    crate["@graph"][1]["hasPart"].append({"@id": "nope.md"})
    write(root / "ro-crate-metadata.json", json.dumps(crate))
    r = run(root, "export_interchange.py", "rocrate-check")
    assert r.returncode == 1 and "nope.md" in r.stdout


def test_public_export_privacy_valve(wiki, tmp_path):
    root, _ = wiki
    out = tmp_path / "public"
    r = run(root, "export_public.py", "--out", str(out))
    assert r.returncode == 0, r.stdout + r.stderr
    pages = sorted(p.relative_to(out).as_posix() for p in out.rglob("*.html"))
    assert pages == ["02-sources/src-1.html"]
    blob = "".join(p.read_text(encoding="utf-8") for p in out.rglob("*.html"))
    assert "PRIVATE-BODY-TEXT" not in blob
    assert "_originals" not in blob and "doc.pdf" not in blob
    assert "SRC-1" in blob  # provenance footer keeps record id
    assert "verified" in blob  # authority fields as recorded
    manifest = json.loads((out / "EXPORT_MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["counts"]["not_public"] == 1
