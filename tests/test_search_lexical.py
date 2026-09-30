"""Tests for the model-free search path (scripts/search_lexical.py).

Unit tests need no qmd. Integration tests build a clean temporary wiki from
tests/search_eval/corpus with its own QMD index and skip when qmd is absent.
The full evaluation (minutes) runs only with WIKI_RUN_SEARCH_EVAL=1.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import run_lexical_eval as ev  # noqa: E402
import search_lexical as sl  # noqa: E402

Z = "‌"
CFG = sl.load_config(ROOT)
needs_qmd = pytest.mark.skipif(shutil.which("qmd") is None, reason="qmd not installed")


@pytest.fixture()
def wiki(tmp_path: Path) -> Path:
    ev.materialise(tmp_path)
    return tmp_path


@pytest.fixture()
def indexed(wiki: Path) -> Path:
    sl.configure(wiki)
    return wiki


# ---- query shaping -------------------------------------------------------

def test_clean_query_handles_zwnj_harakat_tatweel():
    assert sl.clean_query(f"می{Z}خواهم") == "می خواهم"
    assert sl.clean_query("مُحَمَّد") == "محمد"
    assert sl.clean_query("کـتـاب") == "کتاب"


def test_variants_cover_letter_and_digit_scripts():
    v = sl.query_variants("ویرایش ۱۴۰۳")
    assert v[0] == "ویرایش ۱۴۰۳"  # caller's spelling leads
    assert "ويرايش 1403" in v and "ويرايش ١٤٠٣" in v and "ویرایش 1403" in v
    assert len(v) <= 6 and len(set(v)) == len(v)


def test_plain_english_query_is_one_line():
    assert sl.query_variants("provenance tracing") == ["provenance tracing"]


def test_identifier_tokens_become_phrases():
    assert sl.query_variants("annual-report-1402.pdf")[0] == '"annual report 1402 pdf"'
    assert sl.query_variants("-skip foo-bar.txt")[0] == '-skip "foo bar txt"'
    assert sl.query_variants('"a-b.c" x') == ['"a-b.c" x']  # quoted phrase untouched


def test_unbalanced_quote_is_dropped_not_sent():
    assert '"' not in sl.query_variants('say "hello')[0]


def test_ladder_orders_strict_before_relaxed():
    names = [n for n, _ in sl.ladder("Who is in charge of the Kavir Project?")]
    assert names[0] == "strict"
    assert names.index("content-terms") < names.index("any-term")
    assert [n for n, _ in sl.ladder("provenance")] == ["strict"]


def test_persian_stem_prefix_step():
    steps = dict(sl.ladder("کتابها"))
    assert "persian-stem-prefix" in steps and steps["persian-stem-prefix"][0] == "کتاب*"


# ---- model-free guard ----------------------------------------------------

@pytest.mark.parametrize("args", [
    ["embed"], ["embed", "-f"], ["vsearch", "x"], ["pull"], ["mcp"], ["bench", "f.json"],
    ["query", "a bare natural language question", "--no-rerank"],
    ["query", "lex: x"],                                # missing --no-rerank
    ["query", "lex: x\nvec: y", "--no-rerank"],         # vector line
    ["query", "lex: x\nhyde: y", "--no-rerank"],
    ["query", "expand: x", "--no-rerank"],
    [],
])
def test_guard_refuses_model_paths(args):
    with pytest.raises(sl.ModelFreeViolation):
        sl.assert_model_free(args, CFG)


@pytest.mark.parametrize("args", [
    ["search", "x", "--json"], ["update"], ["status"], ["collection", "list"],
    ["query", "lex: a\nlex: b", "--no-rerank", "--json"],
])
def test_guard_allows_model_free_paths(args):
    sl.assert_model_free(args, CFG)


def test_no_default_helper_runs_a_model_command():
    """No shell/PowerShell/Python helper may run embed/vsearch/pull by default.
    The only allowed mention is the opt-in -WithEmbeddings branch of the
    Windows refresh helper."""
    banned = re.compile(r"\bqmd\s+(embed|vsearch|pull)\b|\bqmd\s+query\s+[\"'$]")
    scripts = list((ROOT / "scripts").glob("*.sh")) + list((ROOT / "scripts").glob("*search*.ps1"))
    scripts.append(ROOT / "scripts" / "search_lexical.py")
    for path in scripts:
        text = path.read_text(encoding="utf-8")
        if path.name == "refresh-search.ps1":
            head, _, opt_in = text.partition("if ($WithEmbeddings)")
            assert not banned.search(head), path.name
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith(("#", '"""')):
                continue
            assert not banned.search(line), f"{path.name}:{lineno}: {line.strip()}"


# ---- config consistency --------------------------------------------------

def test_config_scopes_reference_declared_collections():
    names = {c["name"] for c in CFG["collections"]}
    for scope in ("canonical", "captures"):
        assert set(CFG["scopes"][scope]["collections"]) <= names
    assert CFG["scopes"]["all"]["groups"] == ["canonical", "captures"]
    canon = {p for n in CFG["scopes"]["canonical"]["collections"]
             for c in CFG["collections"] if c["name"] == n for p in c["zone_prefixes"]}
    assert not any(p.startswith("01-inbox/") for p in canon), "capture zone leaked into canonical scope"
    cap_zone = CFG["scopes"]["captures"]["exact_zones"]
    assert cap_zone == ["01-inbox/captures"]


def test_canonical_exact_zones_match_frozen_k3():
    assert CFG["scopes"]["canonical"]["exact_zones"] == [
        "02-sources", "03-objects", "04-notes", "05-claims", "06-relations"]


# ---- K3 argument validation ---------------------------------------------

@pytest.mark.parametrize("kwargs", [
    {"mode": "semantic"}, {"scope": "everything"}, {"scope": ""}, {"query": "  "},
])
def test_search_rejects_bad_arguments(kwargs):
    base = {"query": "x", "mode": "lexical", "scope": "canonical"}
    base.update(kwargs)
    with pytest.raises(sl.SearchError):
        sl.search(**base, root=ROOT)


# ---- exact fallback (no qmd needed) -------------------------------------

def _paths(res, group="canonical"):
    return [r["path"] for r in res["groups"][group]["results"]]


def test_exact_folds_harakat_arabic_letters_and_zwnj(wiki):
    assert "02-sources/text/DER-0003.md" in _paths(sl.search("محمد", "exact", "canonical", 5, wiki))
    hits = _paths(sl.search("كتاب", "exact", "canonical", 5, wiki))
    assert "02-sources/text/DER-0002.md" in hits and "02-sources/text/DER-0004.md" in hits


def test_exact_digit_scripts_fold(wiki):
    hits = _paths(sl.search("1402", "exact", "canonical", 5, wiki))
    assert {"02-sources/records/SRC-0002.md", "02-sources/text/DER-0005.md"} <= set(hits)


def test_exact_strict_mode_is_case_and_byte_sensitive(wiki):
    assert sl.exact_search("FERRY SCHEDULE", "canonical", 5, wiki)["returned"] == 1
    assert sl.exact_search("FERRY SCHEDULE", "canonical", 5, wiki, normalize=False)["returned"] == 0


def test_exact_reports_line_and_context_of_original_text(wiki):
    hit = sl.exact_search("محمد", "canonical", 5, wiki)["results"][0]
    body = (wiki / hit["path"]).read_text(encoding="utf-8").splitlines()
    assert "مُحَمَّد" in body[hit["line"] - 1]  # points at the vocalised original
    assert hit["tier"] == "derivative"


def test_exact_scope_isolation(wiki):
    can = _paths(sl.search("ferry schedule", "exact", "canonical", 10, wiki))
    cap = _paths(sl.search("ferry schedule", "exact", "captures", 10, wiki), "captures")
    assert can == ["02-sources/text/DER-0001.md"]
    assert cap == ["01-inbox/captures/CAP-0002.md"]


def test_exact_all_scope_returns_separate_groups(wiki):
    res = sl.search("ferry schedule", "exact", "all", 10, wiki)
    assert set(res["groups"]) == {"canonical", "captures"}
    assert "Captures are noncanonical" in res["groups"]["captures"]["authority_note"]
    assert not any(p.startswith("01-inbox/") for p in _paths(res))


def test_exact_returns_one_result_per_record_with_hit_count(wiki):
    res = sl.exact_search("ferry schedule", "captures", 10, wiki)
    assert [r["path"] for r in res["results"]] == ["01-inbox/captures/CAP-0002.md"]
    assert res["results"][0]["hit_count"] >= 2 and res["total_hits"] == res["results"][0]["hit_count"]


def test_exact_truncation_is_reported(wiki):
    res = sl.exact_search("ی", "canonical", 2, wiki)
    assert res["returned"] == 2 and res["truncated"] is True and res["matched_files"] > 2


def test_exact_missing_zones_are_fine(tmp_path):
    (tmp_path / "00-system/configuration").mkdir(parents=True)
    shutil.copyfile(ROOT / sl.CONFIG_REL, tmp_path / sl.CONFIG_REL)
    assert sl.exact_search("anything", "canonical", 5, tmp_path)["returned"] == 0


# ---- integration with qmd -----------------------------------------------

@needs_qmd
def test_configure_is_idempotent_and_model_free(indexed):
    sl.configure(indexed)  # second run must not fail or duplicate
    state = sl.verify_model_free(indexed)
    assert state["model_free"] and state["files"] == ["cache/qmd/index.sqlite", "config/qmd/index.yml"]


@needs_qmd
def test_lexical_scopes_never_leak(indexed):
    can = sl.search("ferry schedule", "lexical", "canonical", 10, indexed)
    cap = sl.search("ferry schedule", "lexical", "captures", 10, indexed)
    both = sl.search("ferry schedule", "lexical", "all", 10, indexed)
    assert _paths(can) == ["02-sources/text/DER-0001.md"]
    assert _paths(cap, "captures") == ["01-inbox/captures/CAP-0002.md"]
    assert set(both["groups"]) == {"canonical", "captures"}
    assert _paths(both) == _paths(can) and _paths(both, "captures") == _paths(cap, "captures")
    assert all(r["tier"] == "capture" for r in both["groups"]["captures"]["results"])


@needs_qmd
def test_results_carry_tier_zone_id_and_no_score(indexed):
    res = sl.search("collective memory", "lexical", "canonical", 5, indexed)
    top = res["groups"]["canonical"]["results"][0]
    assert top["path"] == "03-objects/OBJ-0001.md" and top["id"] == "OBJ-0001"
    assert top["tier"] == "canonical-record" and top["zone"] == "03-objects"
    assert "score" not in top and "authority_note" in res["groups"]["canonical"]


@needs_qmd
def test_persian_orthography_variants_reach_arabic_letter_text(indexed):
    # doc DER-0002 is written with Arabic yeh/kaf; the query uses Persian letters
    assert "02-sources/text/DER-0002.md" in _paths(sl.search("ویرایش نسخه", "lexical", "canonical", 5, indexed))


@needs_qmd
def test_zwnj_query_matches_zwnj_document(indexed):
    assert "02-sources/text/DER-0004.md" in _paths(sl.search(f"می{Z}خواهم", "lexical", "canonical", 5, indexed))


@needs_qmd
def test_filename_identifier_query_matches(indexed):
    res = sl.search("annual-report-1402.pdf", "lexical", "canonical", 5, indexed)
    assert _paths(res)[0] == "02-sources/records/SRC-0002.md"


@needs_qmd
def test_ladder_labels_relaxed_strategy(indexed):
    res = sl.search("Who is in charge of the Kavir Project?", "lexical", "canonical", 5, indexed)
    g = res["groups"]["canonical"]
    assert g["strategy"] in {"content-terms", "any-term"} and g["attempts"][0]["strategy"] == "strict"
    assert g["attempts"][0]["hits"] == 0


@needs_qmd
def test_strict_match_in_lower_tier_beats_relaxed_match_in_higher_tier(indexed):
    # 'ويرايش نسخه' strictly matches only a derivative; canonical notes share the
    # word 'نسخه' but the ladder must stop at the strict step for the whole scope.
    res = sl.search("ويرايش نسخه", "lexical", "canonical", 5, indexed)
    g = res["groups"]["canonical"]
    assert g["strategy"] == "strict"
    assert _paths(res)[0] == "02-sources/text/DER-0002.md"


@needs_qmd
def test_no_result_is_a_clean_empty_group(indexed):
    res = sl.search("zzzxyz qqqwww", "lexical", "all", 5, indexed)
    assert res["groups"]["canonical"]["returned"] == 0 and res["groups"]["captures"]["returned"] == 0


@needs_qmd
def test_freshness_detects_new_and_changed_files(indexed):
    assert sl.index_freshness(indexed)["stale"] is False
    new = indexed / "04-notes" / "NOTE-9999.md"
    new.write_text("---\nid: NOTE-9999\ntype: note\ntitle: late\n---\nlate note\n", encoding="utf-8")
    assert sl.index_freshness(indexed)["stale"] is True
    sl.refresh(indexed)
    assert sl.index_freshness(indexed)["stale"] is False
    assert "04-notes/NOTE-9999.md" in _paths(sl.search("late note", "lexical", "canonical", 5, indexed))


@needs_qmd
def test_empty_kit_configures_and_searches(tmp_path):
    (tmp_path / "00-system/configuration").mkdir(parents=True)
    shutil.copyfile(ROOT / sl.CONFIG_REL, tmp_path / sl.CONFIG_REL)
    sl.configure(tmp_path)
    res = sl.search("anything", "lexical", "all", 5, tmp_path)
    assert res["groups"]["canonical"]["returned"] == 0
    assert sl.index_freshness(tmp_path)["stale"] is False


@needs_qmd
@pytest.mark.skipif(os.environ.get("WIKI_RUN_SEARCH_EVAL") != "1", reason="set WIKI_RUN_SEARCH_EVAL=1 (about 4 minutes)")
def test_full_lexical_evaluation_meets_floor(tmp_path):
    ev.materialise(tmp_path)
    sl.configure(tmp_path)
    report = ev.evaluate(tmp_path, 10)
    s = report["summary"]
    assert all(s[n]["leaks"] == 0 for n in ("ladder", "exact", "ladder+exact"))
    o = s["ladder+exact"]["overall"]
    assert o["hit@5"] >= 64 and o["false_positives"] == 0
    assert s["ladder"]["overall"]["hit@5"] > s["raw-lex"]["overall"]["hit@5"]
