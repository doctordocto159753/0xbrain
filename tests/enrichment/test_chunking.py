"""chunk-1 (CONTRACT-E1 section 4): partition, thresholds, block boundaries,
sentence splitting, determinism and the golden boundaries of support.long_text().

Regenerate the golden file only after a reviewed algorithm change:
    python tests/enrichment/test_chunking.py --write-golden
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parents[1] / "scripts")]

from brain_enrichment import contract as C  # noqa: E402
from brain_enrichment.chunking import Chunker, block_starts, spans  # noqa: E402
from support import long_text, make_projection  # noqa: E402

GOLDEN = HERE / "fixtures" / "golden_chunks.json"
REF = "cap:cap-20260101-000000-aaaa"
POLICY = C.ChunkPolicy()
FA_SENT = "این یک جملهٔ آزمایشی دربارهٔ پروژهٔ نیل و برنامهٔ نوشتن است؟ "
EN_SENT = "The ferry to Kolan left every Thursday before dawn. "


def build(text: str, ref: str = REF, layer_id: str = "user-text"):
    proj = make_projection(ref, [(layer_id, ref, text, None)])
    return proj, Chunker().build(proj, POLICY)


def check_invariants(text: str, chunks) -> None:
    if not text:
        assert chunks == []
        return
    assert chunks[0].start_char == 0 and chunks[-1].end_char == len(text)
    for a, b in zip(chunks, chunks[1:]):
        assert a.end_char == b.start_char                      # no gap, no overlap
        assert not (text[b.start_char - 1] == "\r" and text[b.start_char] == "\n")   # CRLF kept whole
    for i, c in enumerate(chunks):
        assert c.text == text[c.start_char:c.end_char]
        assert 0 < c.chars <= C.HARD_MAX
        assert c.index == i
        assert c.chunk_ref == C.chunk_ref(c.chunk_digest) and C.CHUNK_REF_RE.match(c.chunk_ref)
        assert c.prev_chunk_ref == (chunks[i - 1].chunk_ref if i else None)
        assert c.next_chunk_ref == (chunks[i + 1].chunk_ref if i + 1 < len(chunks) else None)
    assert "".join(c.text for c in chunks) == text


# ------------------------------------------------------------ generators
def random_text(rng: random.Random) -> str:
    frags = [FA_SENT, EN_SENT, "جمله‌ای با نیم‌فاصله و ZWNJ‌ در میانه. ", "Third? Yes! Fourth… ok۔ ",
             "x" * rng.randint(1, 900), "کلمه" * rng.randint(1, 400), "سلام.دنیا", "3.5 و 4.25 ", "z" * rng.randint(4200, 9000)]
    seps = ["\n", "\n\n", "\r\n", "\r\n\r\n", " ", "\n\n\n", "\n   \n"]
    if rng.random() < 0.4:                      # no blank lines: giant blocks that must be split inside
        seps = [" ", "\n", "\r\n", "  "]
    heads = ["# عنوان اصلی\n", "## Page 3\n", "### بخش\n", "## Slide 2\n", "\\## escaped heading\n"]
    tables = ["| a | b |\n| --- | --- |\n| 1 | 2 |\n", "|x|y|\r\n|z|w|\r\n"]
    out = []
    if rng.random() < 0.2:
        out.append("---\nid: x\ntitle: y\n---\n\n")
    target = rng.choice([50, 3000, 4100, 6500, 9000, 14000, 30000])
    size = 0
    while size < target:
        r = rng.random()
        piece = rng.choice(heads) if r < 0.08 else rng.choice(tables) if r < 0.14 else rng.choice(frags)
        piece += rng.choice(seps) if rng.random() < 0.7 else ""
        out.append(piece)
        size += len(piece)
    return "".join(out)


# ---------------------------------------------------------------- tests
def test_empty_short_and_single_chunk_threshold():
    assert spans("") == []
    assert spans("abc") == [(0, 3)]
    assert spans("x" * 4000) == [(0, 4000)]
    assert spans("x" * 4001) == [(0, 4001)]            # one block <= HARD_MAX is its own chunk
    assert spans("x" * 6000) == [(0, 6000)]
    assert build("")[1] == []


@pytest.mark.parametrize("seed", range(120))
def test_random_texts_partition_exactly(seed):
    rng = random.Random(seed)
    text = random_text(rng)
    proj, chunks = build(text)
    check_invariants(text, chunks)
    again = Chunker().build(proj, POLICY)
    assert [(c.chunk_ref, c.start_char, c.end_char) for c in chunks] == \
        [(c.chunk_ref, c.start_char, c.end_char) for c in again]          # deterministic
    assert [c.text for c in chunks] == [c.text for c in again]


@pytest.mark.parametrize("seed", range(25))
def test_any_text_change_moves_every_chunk_ref(seed):
    rng = random.Random(1000 + seed)
    text = random_text(rng)
    _, before = build(text)
    pos = rng.randrange(len(text))
    changed = text[:pos] + ("Z" if text[pos] != "Z" else "Y") + text[pos + 1:]
    _, after = build(changed)
    assert {c.chunk_ref for c in before}.isdisjoint({c.chunk_ref for c in after})
    # same text under another source_ref also gets other refs (identity never merges)
    _, other = build(text, ref="cap:cap-20260101-000000-bbbb")
    assert {c.chunk_ref for c in before}.isdisjoint({c.chunk_ref for c in other})


def test_random_corpus_exercises_every_splitting_path():
    multi = hard = sentence = 0
    for seed in range(120):
        text = random_text(random.Random(seed))
        sp = spans(text)
        multi += len(sp) > 1
        for a, b in sp[:-1]:
            tail = text[a:b].rstrip()
            starts_block = b in block_starts(text)
            if not starts_block:
                sentence += tail[-1:] in ".!?؟۔…" or text[b - 1] == "\n"
                hard += tail[-1:] not in ".!?؟۔…" and text[b - 1] != "\n"
    assert multi > 60 and sentence > 8 and hard > 2, (multi, sentence, hard)


def test_blocks_start_at_headings_paragraphs_tables_markers_and_front_matter():
    text = ("---\nid: a\n---\n\n# Title\nline\n\nPara one\nstill para one\n\n| a | b |\n| - | - |\n| 1 | 2 |\n"
            "after table\n\n## Page 4\nbody\n## Slide 9\nslide\n\\## escaped not a heading\n")
    starts = block_starts(text)
    lines = {text[s:].split("\n", 1)[0] for s in starts}
    assert "# Title" in lines and "Para one" in lines and "| a | b |" in lines
    assert "## Page 4" in lines and "## Slide 9" in lines
    assert text[starts[1]:].startswith("\n# Title") or text[starts[1]:].startswith("\n")   # front matter end
    fm_end = text.index("---\n", 4) + 4
    assert fm_end in starts
    assert "still para one" not in lines and "| 1 | 2 |" not in lines      # not boundaries
    assert "\\## escaped not a heading" not in lines


def test_table_is_kept_whole_between_chunks():
    rows = "".join(f"| r{i} | {'v' * 60} |\n" for i in range(40))          # ~3000 chars table
    text = ("para. " * 400 + "\n\n") + rows + "\n" + ("tail text. " * 200)
    proj, chunks = build(text)
    check_invariants(text, chunks)
    t0, t1 = text.index("| r0 "), text.index("| r39 ")
    owner = {i for i, c in enumerate(chunks) if c.start_char <= t0 < c.end_char or c.start_char <= t1 < c.end_char}
    assert len(owner) == 1


def test_front_matter_end_is_a_block_boundary_in_long_records():
    fm = "---\nid: obj-x\ntitle: T\n---\n"
    body = "".join(f"Paragraph {i}. " + "word " * 120 + "\n\n" for i in range(12))
    text = fm + body
    assert len(fm) in block_starts(text)
    check_invariants(text, build(text)[1])


def test_greedy_soft_max_and_oversized_block_own_chunk():
    p = lambda n: ("a" * (n - 2)) + "\n\n"  # noqa: E731
    text = p(1500) + p(1500) + p(1500) + p(5000) + p(300)
    # [1500+1500] | [1500] | [5000 alone > SOFT_MAX] | tail 300 < MIN_TAIL merges back (5300 <= HARD_MAX)
    assert spans(text) == [(0, 3000), (3000, 4500), (4500, 9800)]
    no_merge = p(1500) + p(5000) + p(900)
    assert spans(no_merge) == [(0, 1500), (1500, 6500), (6500, 7400)]


def test_block_over_hard_max_splits_at_sentence_boundaries_incl_persian_punctuation():
    text = (FA_SENT * 400)                     # one paragraph, ~25k chars, boundaries '؟ '
    sp = spans(text)
    proj, chunks = build(text)
    check_invariants(text, chunks)
    assert all(c.chars <= C.SOFT_MAX for c in chunks)
    for c in chunks[:-1]:
        assert c.text.rstrip().endswith("؟")      # every cut is after a sentence end
    arabic_stop = ("جمله‌ی آزمایشی۔ " * 600)
    chunks = build(arabic_stop)[1]
    check_invariants(arabic_stop, chunks)
    assert all(c.text.rstrip().endswith("۔") for c in chunks[:-1])
    ellipsis = ("متن بدون نقطه پایان … " * 500)
    check_invariants(ellipsis, build(ellipsis)[1])
    assert all(c.text.rstrip().endswith("…") for c in build(ellipsis)[1][:-1])
    assert sp[0][0] == 0


def test_decimal_numbers_and_abbreviation_dots_are_not_sentence_boundaries():
    text = ("3.14159 " * 800)
    chunks = build(text)[1]
    check_invariants(text, chunks)
    assert all(c.chars <= C.SOFT_MAX for c in chunks)


def test_hard_cut_when_no_boundary_never_splits_crlf():
    text = "x" * 3999 + "\r\n" + "y" * 5000 + "\r\n" + "z" * 5000            # no boundary at all
    sp = spans(text)
    assert sp[0] == (0, 4001)                                                # shifted +1 to keep \r\n whole
    check_invariants(text, build(text)[1])
    crlf = ("لینک بدون نقطه\r\n" * 700)
    check_invariants(crlf, build(crlf)[1])


def test_crlf_lines_and_blank_runs_with_carriage_returns():
    para = "".join("یک خط فارسی با پایان.\r\n" * 20 + "\r\n" for _ in range(30))
    proj, chunks = build(para)
    check_invariants(para, chunks)
    assert len(chunks) > 1
    for c in chunks[1:]:
        assert para[c.start_char - 1] == "\n"       # cuts land on line starts here


def test_tail_merge_respects_min_tail_and_hard_max():
    body = ("w" * 1998 + "\n\n") * 2                       # exactly 4000 -> one chunk
    short_tail = body + "t" * 100
    assert spans(short_tail) == [(0, 4100)]                # 100-char tail merged into the 4000-char chunk
    # a short tail merges into its predecessor when the sum stays <= HARD_MAX
    base = ("p" * 3598 + "\n\n")                           # one 3600 block
    text = base + base + "q" * 300                         # 3600 | 3600 | 300 -> merge 3900
    assert spans(text) == [(0, 3600), (3600, 7500)]
    # ...and does not when it would exceed HARD_MAX
    big = "p" * 5998 + "\n\n"
    text2 = big + "q" * 300
    assert spans(text2) == [(0, 6000), (6000, 6300)]


def test_chunks_inherit_flags_and_links_never_cross_layers():
    a, b = "A" * 4500, "B" * 100
    proj = make_projection(REF, [("user-text", REF, a, None), ("transcript", REF, b, None)],
                           flags=("unreviewed_transcript",))
    chunks = Chunker().build(proj, POLICY)
    check_layers = [c.layer_id for c in chunks]
    assert check_layers == sorted(check_layers, key=["user-text", "transcript"].index)
    assert [c.index for c in chunks] == list(range(len(chunks)))
    assert all(c.quality_flags == ("unreviewed_transcript",) for c in chunks)
    first_t = next(c for c in chunks if c.layer_id == "transcript")
    assert first_t.prev_chunk_ref is None                      # links stay within a layer
    last_u = [c for c in chunks if c.layer_id == "user-text"][-1]
    assert last_u.next_chunk_ref is None


def test_heading_path_is_informational():
    chunks = build(long_text())[1]
    assert chunks[1].heading_path and all(isinstance(h, str) for h in chunks[1].heading_path)


# ---------------------------------------------------------------- golden
def golden_cases() -> dict[str, str]:
    crlf_doc = "".join(f"سطر {i} با نیم‌فاصله، ZWNJ‌ و متن انگلیسی sentence {i}.\r\n" + ("\r\n" if i % 9 == 8 else "")
                       for i in range(220))
    return {
        "long_text_default": long_text(),
        "long_text_60": long_text(60),
        "persian_single_paragraph": FA_SENT * 300,
        "crlf_document": crlf_doc,
        "hard_cut_crlf": "x" * 3999 + "\r\n" + "y" * 5000 + "\r\n" + "z" * 5000,
    }


def compute_golden() -> dict:
    out = {"chunker_version": C.CHUNKER_VERSION, "source_ref": REF,
           "policy": {"soft_max": C.SOFT_MAX, "hard_max": C.HARD_MAX, "min_tail": C.MIN_TAIL, "overlap": C.OVERLAP},
           "cases": {}}
    for name, text in golden_cases().items():
        proj, chunks = build(text)
        out["cases"][name] = {
            "text_sha256": C.sha256_text(text), "chars": len(text), "source_version": proj.source_version,
            "chunks": [{"index": c.index, "start": c.start_char, "end": c.end_char, "chars": c.chars,
                        "chunk_ref": c.chunk_ref, "head": c.text[:32], "tail": c.text[-32:]} for c in chunks]}
    return out


def test_golden_chunk_boundaries_are_pinned():
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert golden == compute_golden()


def test_golden_long_text_has_the_reviewed_shape():
    case = json.loads(GOLDEN.read_text(encoding="utf-8"))["cases"]["long_text_default"]
    assert [(c["start"], c["end"]) for c in case["chunks"]] == [(0, 3872), (3872, 7783)]
    text = long_text()
    assert text[3872:].startswith("پاراگراف 16: ")
    assert text[:3872].endswith("timeline.\n\n") and text[3872:].rstrip().endswith("| b | 2 |")


if __name__ == "__main__":
    if "--write-golden" in sys.argv:
        GOLDEN.write_text(json.dumps(compute_golden(), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print("wrote", GOLDEN)
