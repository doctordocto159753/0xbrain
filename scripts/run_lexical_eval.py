#!/usr/bin/env python3
"""Lexical evaluation of the model-free search path.

Materialises the synthetic corpus in tests/search_eval/corpus into a clean
temporary wiki root (a fresh QMD index, no model, no network needed), then
runs every case in tests/search_eval/queries.json against these strategies:

  raw-search       qmd search <query>                 (upstream baseline)
  raw-lex          qmd query "lex: <query>"           (what the MCP server did)
  variants         cleaned + orthographic variants, no relaxation
  ladder           variants + relaxation ladder       (search_lexical default)
  exact            normalisation-aware exact fallback
  ladder+exact     ladder, then exact when the ladder found nothing
  reformulated     ladder on the case's Claude-style rewrite (paraphrase only)

The corpus and the queries share an author: this measures the mechanism and
guards regressions; it does not estimate recall on a real corpus.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import search_lexical as sl  # noqa: E402

EVAL_DIR = ROOT / "tests" / "search_eval"
STRATEGIES = ["raw-search", "raw-lex", "variants", "ladder", "exact", "ladder+exact", "reformulated"]


def materialise(dest: Path) -> None:
    src = EVAL_DIR / "corpus"
    for f in src.rglob("*.md.fixture"):
        target = dest / f.relative_to(src).with_suffix("")  # strips .fixture
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(f, target)
    cfg_dir = dest / "00-system" / "configuration"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / sl.CONFIG_REL, dest / sl.CONFIG_REL)


def _flat_paths(result: dict) -> list[str]:
    paths: list[str] = []
    for gname in ("canonical", "captures"):
        g = result["groups"].get(gname)
        if g:
            paths.extend(r["path"] for r in g["results"])
    return paths


class Runner:
    def __init__(self, root: Path, top_k: int):
        self.root, self.top_k = root, top_k
        self.cfg = sl.load_config(root)
        self.by_name = {c["name"]: c for c in self.cfg["collections"]}
        self._cache: dict = {}

    def _collections(self, scope: str) -> list[dict]:
        groups = sl._group_names(scope, self.cfg)
        names: list[str] = []
        for g in groups:
            gscope = "canonical" if g == "canonical" else "captures"
            names.extend(self.cfg["scopes"][gscope]["collections"])
        return [self.by_name[n] for n in names]

    def _lex(self, coll: dict, lines: tuple[str, ...]) -> list[str]:
        key = ("lex", coll["name"], lines)
        if key not in self._cache:
            self._cache[key] = [h["path"] for h in
                                sl._qmd_lex(coll, list(lines), self.top_k, self.root, self.cfg)]
        return self._cache[key]

    def raw_search(self, query: str, scope: str) -> list[str]:
        paths: list[str] = []
        for coll in self._collections(scope):
            key = ("search", coll["name"], query)
            if key not in self._cache:
                p = sl._run_qmd(["search", query, "--json", "-n", str(self.top_k), "-c", coll["name"]],
                                self.root, self.cfg)
                hits = sl._parse_json(p.stdout) if p.returncode == 0 else []
                prefix = f"qmd://{coll['name']}/"
                self._cache[key] = [str(h.get("file", ""))[len(prefix):] for h in hits]
            paths.extend(self._cache[key])
        return paths

    def raw_lex(self, query: str, scope: str) -> list[str]:
        paths: list[str] = []
        for coll in self._collections(scope):
            try:
                paths.extend(self._lex(coll, (query,)))
            except sl.SearchError:
                pass  # e.g. unbalanced quote: the raw route simply fails
        return paths

    def variants(self, query: str, scope: str) -> list[str]:
        lines = tuple(sl.query_variants(query))
        paths: list[str] = []
        for coll in self._collections(scope):
            paths.extend(self._lex(coll, lines))
        return paths

    def ladder(self, query: str, scope: str) -> list[str]:
        key = ("ladder", query, scope)
        if key not in self._cache:
            self._cache[key] = _flat_paths(self._search(query, "lexical", scope))
        return self._cache[key]

    def exact(self, query: str, scope: str) -> list[str]:
        key = ("exact", query, scope)
        if key not in self._cache:
            self._cache[key] = _flat_paths(self._search(query, "exact", scope))
        return self._cache[key]

    def _search(self, query: str, mode: str, scope: str) -> dict:
        # route lexical tiers through the memoised _lex so strategies share qmd calls
        original = sl._qmd_lex
        runner = self

        def cached(collection, lines, n, root, cfg):
            key = ("full", collection["name"], tuple(lines), n)
            if key not in runner._cache:
                runner._cache[key] = original(collection, lines, n, root, cfg)
            return [dict(h) for h in runner._cache[key]]

        sl._qmd_lex = cached
        try:
            return sl.search(query, mode=mode, scope=scope, n=self.top_k, root=self.root)
        finally:
            sl._qmd_lex = original

    def strategy(self, name: str, case: dict) -> list[str] | None:
        q, scope = case["query"], case["scope"]
        if name == "raw-search":
            return self.raw_search(q, scope) if case["mode"] == "lexical" else None
        if name == "raw-lex":
            return self.raw_lex(q, scope) if case["mode"] == "lexical" else None
        if name == "variants":
            return self.variants(q, scope) if case["mode"] == "lexical" else None
        if name == "ladder":
            return self.ladder(q, scope) if case["mode"] == "lexical" else None
        if name == "exact":
            return self.exact(q, scope) if scope != "all" else None
        if name == "ladder+exact":
            if case["mode"] != "lexical":
                return None
            got = self.ladder(q, scope)
            return got if got else (self.exact(q, scope) if scope != "all" else got)
        if name == "reformulated":
            return self.ladder(case["reformulated"], scope) if case.get("reformulated") else None
        raise ValueError(name)


def judge(case: dict, paths: list[str], top_k: int) -> dict:
    expect = case["expect"]
    ranks = []
    for e in expect:
        ranks.append(paths.index(e) + 1 if e in paths else None)
    if not expect:
        ok = not paths
        rank = None
    elif case["scope"] == "all":
        ok = all(r is not None for r in ranks)
        rank = max((r for r in ranks if r), default=None) if ok else None
    else:
        found = [r for r in ranks if r]
        rank = min(found) if found else None
        ok = rank is not None
    leaks = [a for a in case.get("absent", []) if a in paths]
    return {"ok": ok, "rank": rank, "leaks": leaks, "returned": len(paths)}


def evaluate(root: Path, top_k: int = 10) -> dict:
    data = json.loads((EVAL_DIR / "queries.json").read_text(encoding="utf-8"))
    cases = data["cases"]
    runner = Runner(root, top_k)
    rows = []
    for case in cases:
        row = {"id": case["id"], "category": case["category"], "query": case["query"],
               "scope": case["scope"], "mode": case["mode"], "known_limit": case.get("known_limit"),
               "strategies": {}}
        for name in STRATEGIES:
            paths = runner.strategy(name, case)
            if paths is None:
                continue
            row["strategies"][name] = judge(case, paths, top_k)
        rows.append(row)
    return {"top_k": top_k, "rows": rows, "summary": summarise(rows, cases)}


def summarise(rows: list[dict], cases: list[dict]) -> dict:
    """Per strategy: hit@1, hit@5, MRR over cases with expectations; false
    positives over cases with no expectation; scope leaks anywhere."""
    expects = {c["id"]: c["expect"] for c in cases}
    out: dict = {}
    for name in STRATEGIES:
        per_cat: dict = defaultdict(lambda: {"n": 0, "hit1": 0, "hit5": 0, "rr": 0.0, "fp": 0, "neg": 0})
        leaks = 0
        for row in rows:
            res = row["strategies"].get(name)
            if res is None:
                continue
            leaks += len(res["leaks"])
            bucket = per_cat[row["category"]]
            if not expects[row["id"]]:
                bucket["neg"] += 1
                bucket["fp"] += 0 if res["ok"] else 1
                continue
            bucket["n"] += 1
            if res["ok"] and res["rank"]:
                bucket["hit1"] += res["rank"] == 1
                bucket["hit5"] += res["rank"] <= 5
                bucket["rr"] += 1.0 / res["rank"]
        tot = {"n": 0, "hit1": 0, "hit5": 0, "rr": 0.0, "fp": 0, "neg": 0}
        for b in per_cat.values():
            for k in tot:
                tot[k] += b[k]
        out[name] = {"leaks": leaks, "overall": _fmt(tot),
                     "by_category": {c: _fmt(b) for c, b in sorted(per_cat.items())}}
    return out


def _fmt(b: dict) -> dict:
    n = b["n"]
    return {"cases": n, "hit@1": b["hit1"], "hit@5": b["hit5"],
            "mrr": round(b["rr"] / n, 3) if n else None,
            "negatives": b["neg"], "false_positives": b["fp"]}


def markdown(report: dict) -> str:
    lines = ["# Lexical evaluation", "",
             f"top_k = {report['top_k']}. hit@k counts cases with at least one expected record in the first k results "
             "(scope=all: all expected records). Negatives pass only when nothing is returned.", ""]
    summ = report["summary"]
    lines += ["| strategy | cases | hit@1 | hit@5 | MRR | false pos. | scope leaks |", "|---|---|---|---|---|---|---|"]
    for name, s in summ.items():
        o = s["overall"]
        if not o["cases"] and not o["negatives"]:
            continue
        lines.append(f"| {name} | {o['cases']} | {o['hit@1']} | {o['hit@5']} | {o['mrr']} | "
                     f"{o['false_positives']}/{o['negatives']} | {s['leaks']} |")
    cats = sorted({c for s in summ.values() for c in s["by_category"]})
    lines += ["", "hit@5 / cases, by category:", "",
              "| category | " + " | ".join(n for n in summ if summ[n]["overall"]["cases"]) + " |",
              "|---|" + "---|" * sum(1 for n in summ if summ[n]["overall"]["cases"])]
    for cat in cats:
        cells = []
        for name, s in summ.items():
            if not s["overall"]["cases"]:
                continue
            b = s["by_category"].get(cat)
            cells.append(f"{b['hit@5']}/{b['cases']}" if b and b["cases"] else "-")
        lines.append(f"| {cat} | " + " | ".join(cells) + " |")
    lines += ["", "Cases that fail under `ladder+exact` (documented limits are marked):", ""]
    for row in report["rows"]:
        res = row["strategies"].get("ladder+exact") or row["strategies"].get("exact")
        if res and not res["ok"]:
            lines.append(f"- {row['id']} [{row['category']}] {row['query']!r}"
                         + (f" (known limit: {row['known_limit']})" if row["known_limit"] else " (UNEXPECTED)"))
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--json-out", type=Path)
    ap.add_argument("--markdown-out", type=Path)
    ap.add_argument("--keep", action="store_true", help="keep the temporary wiki root")
    args = ap.parse_args(argv)
    if not shutil.which("qmd"):
        print("ERROR: qmd not on PATH", file=sys.stderr)
        return 2
    tmp = Path(tempfile.mkdtemp(prefix="wiki-search-eval-"))
    try:
        materialise(tmp)
        sl.configure(tmp)
        report = evaluate(tmp, args.top_k)
        report["model_free_evidence"] = sl.verify_model_free(tmp)
    finally:
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)
    if args.json_out:
        args.json_out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    text = markdown(report)
    if args.markdown_out:
        args.markdown_out.write_text(text, encoding="utf-8")
    print(text)
    leaks = sum(s["leaks"] for n, s in report["summary"].items() if n in ("ladder", "exact", "ladder+exact"))
    return 1 if leaks or not report["model_free_evidence"]["model_free"] else 0


if __name__ == "__main__":
    sys.exit(main())
