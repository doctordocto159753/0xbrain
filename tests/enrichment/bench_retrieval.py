"""Recall@10 / MRR benchmark for WP-C on fixtures/retrieval_queries.json.

    python tests/enrichment/bench_retrieval.py            # table
    python tests/enrichment/bench_retrieval.py --json     # machine-readable
    python tests/enrichment/bench_retrieval.py --misses   # list per-query misses

Systems
  legacy-exact   scripts/search_lexical.exact_search (normalisation-aware
                 substring over the whole record; the legacy fallback)
  r1-direct      R1 retrieval with NO candidate annotations (index over text only)
  r1             R1 retrieval with the fixture's hand-written annotations

Ranking for the metrics: scope "all" keeps two groups; for the metric the two
group lists are interleaved round-robin (canonical rank 1, captures rank 1,
canonical rank 2, ...) and cut at 10. The corpus, the queries and the
annotations share an author: this measures the mechanism, not recall on a
real corpus.
"""
from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for p in (ROOT / "scripts", ROOT / "scripts" / "capture", HERE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import search_lexical as sl  # noqa: E402
from brain_enrichment.config import EnrichmentConfig  # noqa: E402
from brain_enrichment.indexing import IndexBuilder  # noqa: E402
from brain_enrichment.retrieval import RetrievalEngine  # noqa: E402
from retrieval_support import Corpus, DerivedStore, cap_ref  # noqa: E402

FIXTURE = HERE / "fixtures" / "retrieval_queries.json"
K = 10


def load() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def interleave(groups: list[list[str]]) -> list[str]:
    out, i = [], 0
    while any(i < len(g) for g in groups):
        for g in groups:
            if i < len(g) and g[i] not in out:
                out.append(g[i])
        i += 1
    return out


# ------------------------------------------------------------------ systems
class LegacyExact:
    name = "legacy-exact"

    def __init__(self, data: dict, tmp: Path):
        root = tmp / "legacy-wiki"
        (root / "00-system/configuration").mkdir(parents=True)
        shutil.copyfile(ROOT / sl.CONFIG_REL, root / sl.CONFIG_REL)
        for d in data["documents"]:
            rel = {"capture": "01-inbox/captures", "canonical": "03-objects", "ingested": "02-sources/text"}[d["kind"]]
            (root / rel).mkdir(parents=True, exist_ok=True)
            (root / rel / f"{d['label']}.md").write_text(d["text"], encoding="utf-8", newline="")
        self.root = root
        self.cfg = sl.load_config(root)

    def run(self, query: str) -> list[str]:
        lists = []
        for scope in ("canonical", "captures"):
            res = sl.exact_search(query, scope, K, self.root, self.cfg)
            lists.append([Path(r["path"]).stem for r in res["results"]])
        return interleave(lists)[:K]


class R1:
    def __init__(self, data: dict, tmp: Path, *, annotations: bool, name: str):
        self.name = name
        self.corpus = Corpus(DerivedStore())
        self.label_of: dict[str, str] = {}
        for i, d in enumerate(data["documents"], 1):
            if d["kind"] == "capture":
                proj = self.corpus.capture(d["text"], cap_ref(i))
            elif d["kind"] == "canonical":
                proj = self.corpus.canonical(d["id"], d["text"])
            else:
                proj = self.corpus.ingested(d["id"], d["text"])
            self.label_of[proj.source_ref] = d["label"]
            if annotations and d.get("annotation") and d["kind"] != "canonical":
                self.corpus.annotate(proj.source_ref, d["annotation"])
        self.index = IndexBuilder(self.corpus.store, tmp / f"{name}.sqlite3")
        self.index.rebuild_full()
        self.engine = RetrievalEngine(self.corpus.store, self.index,
                                      config=EnrichmentConfig(mode="on", retrieval=True))

    def run(self, query: str) -> list[str]:
        out = self.engine.retrieve({"query": query, "limit": K})
        assert out["path"] == "enrichment" and out["query_model_calls"] == 0
        lists = [[self.label_of[h["source_ref"]] for h in out["groups"][g]["results"]]
                 for g in ("canonical", "captures")]
        return interleave(lists)[:K]


# ------------------------------------------------------------------ metrics
def evaluate(system, queries: list[dict]) -> dict:
    per, rec, rr, lat = [], [], [], []
    false_pos = 0
    for q in queries:
        t0 = time.perf_counter()
        got = system.run(q["query"])
        lat.append(time.perf_counter() - t0)
        exp = set(q["expect"])
        if not exp:
            false_pos += len(got)
            per.append({"id": q["id"], "query": q["query"], "got": got, "expect": [], "recall": None, "rr": None})
            continue
        found = [g for g in got if g in exp]
        recall = len(set(found)) / len(exp)
        first = next((i for i, g in enumerate(got, 1) if g in exp), None)
        rec.append(recall)
        rr.append(1.0 / first if first else 0.0)
        per.append({"id": q["id"], "query": q["query"], "got": got, "expect": sorted(exp),
                    "recall": recall, "rr": rr[-1]})
    lat.sort()
    return {"system": system.name, "queries": len(rec), f"recall@{K}": round(statistics.mean(rec), 4),
            "mrr": round(statistics.mean(rr), 4), "negative_false_positives": false_pos,
            "p95_ms": round(lat[max(0, int(len(lat) * 0.95) - 1)] * 1000, 2), "per_query": per}


def run_benchmark() -> dict:
    data = load()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        systems = [LegacyExact(data, tmp), R1(data, tmp, annotations=False, name="r1-direct"),
                   R1(data, tmp, annotations=True, name="r1")]
        return {"documents": len(data["documents"]), "queries_total": len(data["queries"]),
                "results": [evaluate(s, data["queries"]) for s in systems]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--misses", action="store_true")
    args = ap.parse_args(argv)
    out = run_benchmark()
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    print(f"documents={out['documents']} queries={out['queries_total']} (negative queries excluded from the means)")
    print(f"{'system':<14}{'Recall@10':>10}{'MRR':>8}{'neg FP':>8}{'p95 ms':>9}")
    for r in out["results"]:
        print(f"{r['system']:<14}{r['recall@10']:>10.3f}{r['mrr']:>8.3f}{r['negative_false_positives']:>8}{r['p95_ms']:>9.2f}")
    if args.misses:
        for r in out["results"]:
            print(f"\n[{r['system']}] queries with recall < 1:")
            for p in r["per_query"]:
                if p["recall"] is not None and p["recall"] < 1:
                    print(f"  {p['id']} {p['query']!r}: expect {p['expect']} got {p['got'][:5]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
