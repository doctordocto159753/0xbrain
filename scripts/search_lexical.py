#!/usr/bin/env python3
"""Model-free search over the wiki: QMD lexical (BM25) plus exact fallback.

Contract (Agent C, frozen K3 search scope):

    search(query, mode="lexical"|"exact", scope="canonical"|"captures"|"all", n=10)

* No embedding, vector search, reranker or query-expansion model is ever run.
  Every qmd call goes through `_run_qmd`, which refuses forbidden commands and
  any `qmd query` that is not a typed all-`lex:` document with `--no-rerank`.
* `scope` is explicit. `canonical` never returns a capture; `captures` never
  returns a canonical record; `all` returns the two as separate groups and
  never merges them into one ranking.
* Ranking is navigation only. Scores are not returned as evidence.
* Persian/Arabic orthography is handled on the query side (QMD's tokenizer
  does not fold it) and, for the exact fallback, on both sides.

CLI: configure | refresh | status | search | exact | verify-model-free
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_REL = "00-system/configuration/qmd-collections.json"

AUTHORITY_NOTE = (
    "Retrieval rank organizes attention and is never evidence. Open the record "
    "and follow it to its source record and immutable original before asserting."
)
CAPTURE_NOTE = (
    "Captures are noncanonical intake material; never cite them as canonical evidence."
)

MODES = ("lexical", "exact")
SCOPES = ("canonical", "captures", "all")


class SearchError(Exception):
    """Bad input or an unusable QMD installation."""


class ModelFreeViolation(SearchError):
    """A qmd invocation that could load or download a model was attempted."""


# --------------------------------------------------------------------------
# configuration and qmd invocation
# --------------------------------------------------------------------------

def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / CONFIG_REL).read_text(encoding="utf-8"))


def qmd_home(root: Path = ROOT, cfg: dict | None = None) -> Path:
    override = os.environ.get("WIKI_QMD_HOME")
    if override:
        return Path(override)
    cfg = cfg or load_config(root)
    return root / cfg.get("index_home", "_search/qmd")


def qmd_env(root: Path = ROOT, cfg: dict | None = None) -> dict:
    """Environment that pins the QMD index and config inside the wiki's own
    git-ignored `_search/` directory, so a search never depends on (or
    pollutes) the operator's home directory."""
    home = qmd_home(root, cfg)
    env = dict(os.environ)
    env["XDG_CACHE_HOME"] = str(home / "cache")
    env["XDG_CONFIG_HOME"] = str(home / "config")
    env.setdefault("NO_COLOR", "1")
    return env


def _qmd_binary() -> str:
    binary = shutil.which("qmd")
    if not binary:
        raise SearchError("qmd not found on PATH (npm install -g @tobilu/qmd)")
    return binary


def assert_model_free(args: list[str], cfg: dict) -> None:
    rules = cfg["model_free"]
    if not args:
        raise ModelFreeViolation("empty qmd invocation")
    command = args[0]
    if command in rules["forbidden_qmd_commands"]:
        raise ModelFreeViolation(f"qmd {command!r} is forbidden in the default path")
    if command not in rules["allowed_qmd_commands"]:
        raise ModelFreeViolation(f"qmd {command!r} is not on the model-free allowlist")
    if command == "query":
        if "--no-rerank" not in args:
            raise ModelFreeViolation("qmd query requires --no-rerank")
        if len(args) < 2:
            raise ModelFreeViolation("qmd query needs a typed query document")
        lines = [ln for ln in args[1].split("\n") if ln.strip()]
        if not lines or not all(ln.startswith("lex:") for ln in lines):
            raise ModelFreeViolation(
                "qmd query must be a typed document with only 'lex:' lines "
                "(a bare query triggers a query-expansion model)")


def _run_qmd(args: list[str], root: Path, cfg: dict, timeout: int = 120) -> subprocess.CompletedProcess:
    assert_model_free(args, cfg)
    return subprocess.run(
        [_qmd_binary(), *args], cwd=root, env=qmd_env(root, cfg),
        text=True, capture_output=True, timeout=timeout, check=False,
    )


# --------------------------------------------------------------------------
# Persian / Arabic orthography
# --------------------------------------------------------------------------
# Verified against qmd 2.8.3 (see AGENT_C handoff): the FTS tokenizer does not
# fold ک/ك, ی/ي/ى, digit scripts, tatweel or harakat; ZWNJ splits tokens in the
# index but makes a QUERY match nothing; harakat inside indexed text split
# words apart, so a vocalised word cannot be found by its bare form.

_HARAKAT = {chr(c) for c in range(0x064B, 0x0660)} | {"ٰ"} | {chr(c) for c in range(0x06D6, 0x06EE)}
_TATWEEL = "ـ"
_JOINERS = {"\u200c", "\u200d", "\u200e", "\u200f"}

_LETTERS_FA = str.maketrans({"ك": "ک", "ي": "ی", "ى": "ی", "ې": "ی"})
_LETTERS_AR = str.maketrans({"ک": "ك", "ی": "ي", "ى": "ي"})
_DIGITS_FA = "۰۱۲۳۴۵۶۷۸۹"
_DIGITS_AR = "٠١٢٣٤٥٦٧٨٩"
_TO_EN = str.maketrans({**{a: str(i) for i, a in enumerate(_DIGITS_FA)},
                        **{a: str(i) for i, a in enumerate(_DIGITS_AR)}})
_TO_FA = str.maketrans({**{str(i): _DIGITS_FA[i] for i in range(10)},
                        **{a: _DIGITS_FA[i] for i, a in enumerate(_DIGITS_AR)}})
_TO_AR = str.maketrans({**{str(i): _DIGITS_AR[i] for i in range(10)},
                        **{a: _DIGITS_AR[i] for i, a in enumerate(_DIGITS_FA)}})


def clean_query(query: str) -> str:
    """Strip what QMD cannot match: joiners become spaces, harakat/tatweel
    are removed, presentation forms are folded (NFKC), whitespace collapsed."""
    text = unicodedata.normalize("NFKC", query)
    out = []
    for ch in text:
        if ch in _JOINERS:
            out.append(" ")
        elif ch in _HARAKAT or ch == _TATWEEL:
            continue
        elif ch in "\r\n\t":
            out.append(" ")
        else:
            out.append(ch)
    return re.sub(r" +", " ", "".join(out)).strip()


def _balance_quotes(text: str) -> str:
    return text.replace('"', "") if text.count('"') % 2 else text


_IDENT_JOINERS = re.compile(r"[-_./:]+")


def _shape_identifiers(text: str) -> str:
    """QMD's lexical parser returns nothing for a hyphenated token followed by
    an extension (`report-1402.pdf`), although the same words as a quoted
    phrase match. Rewrite identifier-like tokens (outside quotes) as phrases:
    `report-1402.pdf` -> `"report 1402 pdf"`. Negations (-x) and prefixes (x*)
    are left alone."""
    out = []
    for part in re.split(r'("[^"]*")', text):
        if part.startswith('"'):
            out.append(part)
            continue
        toks = []
        for tok in part.split(" "):
            if tok.startswith("-") or "*" in tok or not re.search(r"\w[-_./:]\w", tok):
                toks.append(tok)
                continue
            pieces = [x for x in _IDENT_JOINERS.split(tok) if x]
            toks.append('"' + " ".join(pieces) + '"' if len(pieces) > 1 else (pieces[0] if pieces else tok))
        out.append(" ".join(toks))
    return re.sub(r" +", " ", "".join(out)).strip()


def query_variants(query: str) -> list[str]:
    """Deterministic orthographic variants of one query (<= 6).

    Profile grid: letters {Persian, Arabic} x digits {Persian, Arabic, ASCII}.
    Variants that equal another are dropped, so a plain English query yields
    exactly one line. The lines are OR-ed by QMD (multi-line typed query)."""
    base = _shape_identifiers(_balance_quotes(clean_query(query)))
    if not base:
        return []
    fa_letters = base.translate(_LETTERS_FA)
    ar_letters = base.translate(_LETTERS_AR)
    seen: list[str] = []
    for letters in (fa_letters, ar_letters):
        for digits in (_TO_EN, _TO_FA, _TO_AR):
            candidate = letters.translate(digits)
            if candidate not in seen:
                seen.append(candidate)
    # the caller's own spelling always leads
    if base in seen:
        seen.remove(base)
    return [base, *seen]


_STOP_EN = set("""a an the of is are was were be been what which who whom whose how why when where
do does did in on at to for and or not that this these those with by from about between into as it
its than then there their they he she we you i me my our your can could should would may might
tell me explain describe""".split())
_STOP_FA = set("""چیست چیه چه چی چگونه چطور چرا کجا کی کدام کدامند آیا است هست هستند بود بودند شد شدند
در از به با که را این آن آنها اینها و یا برای درباره بین میان تا هم نیز باید می ی
بگو بگویید توضیح بده توضیح""".split())
_FA_SUFFIXES = ("ترین", "های", "ها", "ات", "ان", "تر", "ین", "ی")


def content_tokens(query: str) -> list[str]:
    tokens = []
    for tok in clean_query(query).split(" "):
        tok = tok.strip('"“”«»،؛؟?!.,;:()[]{}')
        if len(tok) < 2:
            continue
        if tok.casefold() in _STOP_EN or tok in _STOP_FA:
            continue
        tokens.append(tok)
    return tokens


def _fa_stem(token: str) -> str:
    if not re.search(r"[؀-ۿ]", token):
        return token
    for suf in _FA_SUFFIXES:
        if token.endswith(suf) and len(token) - len(suf) >= 3:
            return token[: -len(suf)]
    return token


def _lex_document(variants: list[str]) -> str:
    return "\n".join(f"lex: {v}" for v in variants)


def ladder(query: str, max_or_terms: int = 8) -> list[tuple[str, list[str]]]:
    """Deterministic relaxation ladder: (strategy, variant lines) in order.
    Each step runs only if every earlier step returned nothing. This replaces
    server-side LLM query expansion; Claude may still reformulate and call
    again with a better query."""
    steps: list[tuple[str, list[str]]] = []
    steps.append(("strict", query_variants(query)))
    toks = content_tokens(query)
    if toks and " ".join(toks) != clean_query(query):
        steps.append(("content-terms", query_variants(" ".join(toks))))
    stems = [_fa_stem(t) for t in toks]
    if any(s != t for s, t in zip(stems, toks)):
        steps.append(("persian-stem-prefix", query_variants(" ".join(s + "*" if s != t else t
                                                                   for s, t in zip(stems, toks)))))
    if len(toks) > 1:
        lines: list[str] = []
        for t in toks[:max_or_terms]:
            for v in query_variants(t):
                if v not in lines:
                    lines.append(v)
        steps.append(("any-term", lines))
    out, seen = [], set()
    for name, lines in steps:
        key = tuple(lines)
        if lines and key not in seen:
            seen.add(key)
            out.append((name, lines))
    return out


# --------------------------------------------------------------------------
# lexical search through QMD
# --------------------------------------------------------------------------

def _parse_json(stdout: str) -> list[dict]:
    text = stdout.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("[")
        if start < 0:
            raise SearchError("qmd returned unparseable output")
        data = json.loads(text[start:])
    return data if isinstance(data, list) else []


_FM_ID = re.compile(r"^id:\s*(.+?)\s*$", re.M)


def _record_id(root: Path, rel: str) -> str | None:
    try:
        head = (root / rel).read_text(encoding="utf-8", errors="replace")[:2000]
    except OSError:
        return None
    if not head.startswith("---"):
        return None
    block = head.split("---", 2)[1] if head.count("---") >= 2 else head
    m = _FM_ID.search(block)
    return m.group(1).strip("'\"") if m else None


def _clean_snippet(snippet: str) -> str:
    lines = snippet.split("\n")
    if lines and lines[0].startswith("@@"):
        lines = lines[1:]
    return "\n".join(lines).strip()


def _qmd_lex(collection: dict, lines: list[str], n: int, root: Path, cfg: dict) -> list[dict]:
    proc = _run_qmd(["query", _lex_document(lines), "--no-rerank", "--json",
                     "-n", str(n), "-c", collection["name"]], root, cfg)
    if proc.returncode != 0:
        raise SearchError(f"qmd query failed for {collection['name']}: {proc.stderr.strip()[:300]}")
    prefix = f"qmd://{collection['name']}/"
    hits = []
    for rank, item in enumerate(_parse_json(proc.stdout), start=1):
        f = str(item.get("file", ""))
        rel = f[len(prefix):] if f.startswith(prefix) else f
        if not any(rel.startswith(z) for z in collection["zone_prefixes"]):
            continue  # belt and braces: a scope never leaks another zone
        hits.append({
            "rank": rank,
            "path": rel,
            "id": _record_id(root, rel),
            "title": item.get("title"),
            "tier": collection["tier"],
            "zone": rel.split("/", 1)[0] if "/" in rel else "",
            "collection": collection["name"],
            "snippet": _clean_snippet(str(item.get("snippet", ""))),
        })
    return hits


def _lexical_group(collections: list[dict], query: str, n: int, root: Path, cfg: dict) -> dict:
    """Run the relaxation ladder over ALL tiers of a group. A step is accepted
    as soon as any tier matches, so a strict match in a lower tier is never
    outranked by a relaxed match in a higher tier."""
    attempts = []
    for name, lines in ladder(query):
        per_tier = [(c, _qmd_lex(c, lines, n + 1, root, cfg)) for c in collections]
        total = sum(len(h) for _, h in per_tier)
        attempts.append({"strategy": name, "lines": len(lines), "hits": total})
        if total:
            tiers = []
            for coll, hits in per_tier:
                more = len(hits) > n
                hits = hits[:n]
                for i, h in enumerate(hits, start=1):
                    h["rank"] = i
                tiers.append({"tier": coll["tier"], "collection": coll["name"], "strategy": name,
                              "returned": len(hits), "more_available": more, "results": hits})
            return {"strategy": name, "attempts": attempts, "tiers": tiers}
    return {"strategy": None, "attempts": attempts,
            "tiers": [{"tier": c["tier"], "collection": c["name"], "strategy": None,
                       "returned": 0, "more_available": False, "results": []} for c in collections]}


# --------------------------------------------------------------------------
# exact fallback (normalisation-aware, deterministic, no index needed)
# --------------------------------------------------------------------------

def _norm_char_map(text: str) -> tuple[str, list[int]]:
    """Normalise for matching and keep an index map back to `text`.
    Folds: NFKC, casefold, ک/ك ی/ي/ى ة->ه, digit scripts -> ASCII, drops
    harakat/tatweel/joiners, collapses whitespace."""
    out: list[str] = []
    idx: list[int] = []
    prev_space = True
    for i, ch in enumerate(text):
        for c in unicodedata.normalize("NFKC", ch):
            if c in _JOINERS or c in _HARAKAT or c == _TATWEEL:
                continue
            if c.isspace():
                if not prev_space:
                    out.append(" ")
                    idx.append(i)
                    prev_space = True
                continue
            c = c.translate(_LETTERS_FA).translate(_TO_EN)
            if c == "ة":
                c = "ه"
            for cc in c.casefold():
                out.append(cc)
                idx.append(i)
            prev_space = False
    return "".join(out), idx


def _iter_zone_files(root: Path, zones: list[str]) -> list[Path]:
    files: list[Path] = []
    for zone in zones:
        d = root / zone
        if d.is_dir():
            files.extend(p for p in d.rglob("*.md") if p.is_file())
    return sorted(files, key=lambda p: p.relative_to(root).as_posix())


def exact_search(text: str, scope: str = "canonical", n: int = 10,
                 root: Path = ROOT, cfg: dict | None = None,
                 max_hits_per_file: int = 3, normalize: bool = True) -> dict:
    """Deterministic substring search over the scope's zones, one result per
    record (`hit_count`, `lines`). Not model-backed, not index-backed: always current. Normalisation folds orthographic
    variants; normalize=False gives a byte-exact, case-sensitive match."""
    cfg = cfg or load_config(root)
    if scope not in cfg["scopes"] or scope == "all":
        raise SearchError(f"exact_search scope must be canonical or captures, got {scope!r}")
    needle_raw = text.strip()
    if not needle_raw:
        raise SearchError("empty text")
    zones = cfg["scopes"][scope]["exact_zones"]
    needle = _norm_char_map(needle_raw)[0] if normalize else needle_raw
    results: list[dict] = []
    matched_files = 0
    total_hits = 0
    for path in _iter_zone_files(root, zones):
        try:
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        hay, idx = _norm_char_map(body) if normalize else (body, list(range(len(body))))
        positions, start = [], 0
        while True:
            pos = hay.find(needle, start)
            if pos < 0:
                break
            positions.append(pos)
            start = pos + max(1, len(needle))
        if not positions:
            continue
        matched_files += 1
        total_hits += len(positions)
        rel = path.relative_to(root).as_posix()
        lines = []
        for pos in positions:
            line = body.count("\n", 0, idx[pos]) + 1
            if line not in lines:
                lines.append(line)
        first = idx[positions[0]]
        results.append({
            "path": rel, "id": _record_id(root, rel), "line": lines[0],
            "lines": lines[:max_hits_per_file], "hit_count": len(positions),
            "tier": _tier_for(rel, cfg), "zone": rel.split("/", 1)[0],
            "context": body[max(0, first - 80): first + len(needle_raw) + 120].replace("\n", " "),
        })
    limited = results[:n]
    return {"matched_files": matched_files, "total_hits": total_hits,
            "returned": len(limited), "truncated": len(results) > n,
            "normalized": normalize, "results": limited}


def _tier_for(rel: str, cfg: dict) -> str:
    for coll in cfg["collections"]:
        if any(rel.startswith(z) for z in coll["zone_prefixes"]):
            return coll["tier"]
    return "unknown"


# --------------------------------------------------------------------------
# public entry point (K3 search scope behaviour)
# --------------------------------------------------------------------------

def _group_names(scope: str, cfg: dict) -> list[str]:
    return cfg["scopes"]["all"]["groups"] if scope == "all" else [cfg["scopes"][scope]["group"]]


def search(query: str, mode: str = "lexical", scope: str = "canonical", n: int = 10,
           root: Path = ROOT) -> dict:
    if mode not in MODES:
        raise SearchError(f"mode must be one of {MODES}, got {mode!r}")
    if scope not in SCOPES:
        raise SearchError(f"scope must be one of {SCOPES}, got {scope!r}")
    if not isinstance(query, str) or not query.strip():
        raise SearchError("empty query")
    n = max(1, min(int(n or 10), 100))
    cfg = load_config(root)
    by_name = {c["name"]: c for c in cfg["collections"]}
    groups: dict[str, dict] = {}
    for group in _group_names(scope, cfg):
        gscope = "canonical" if group == "canonical" else "captures"
        note = CAPTURE_NOTE if group == "captures" else AUTHORITY_NOTE
        if mode == "exact":
            ex = exact_search(query, gscope, n, root, cfg)
            groups[group] = {"authority_note": note, "returned": ex["returned"],
                             "truncated": ex["truncated"], "matched_files": ex["matched_files"],
                             "results": ex["results"]}
            continue
        found = _lexical_group([by_name[name] for name in cfg["scopes"][gscope]["collections"]],
                               query, n, root, cfg)
        flat = [r for t in found["tiers"] for r in t["results"]]
        groups[group] = {"authority_note": note, "returned": len(flat),
                         "strategy": found["strategy"], "attempts": found["attempts"],
                         "tiers": [{k: v for k, v in t.items() if k != "results"} for t in found["tiers"]],
                         "results": flat}
    return {"query": query, "mode": mode, "scope": scope, "model_free": True,
            "groups": groups, "authority_note": AUTHORITY_NOTE}


# --------------------------------------------------------------------------
# index lifecycle: configure / refresh / status / freshness
# --------------------------------------------------------------------------

def _registered(root: Path, cfg: dict) -> set[str]:
    proc = _run_qmd(["collection", "list"], root, cfg)
    if proc.returncode != 0:
        raise SearchError(f"qmd collection list failed: {proc.stderr.strip()[:300]}")
    return set(re.findall(r"^(\S+) \(qmd://", proc.stdout, re.M))


def configure(root: Path = ROOT) -> dict:
    """Idempotently (re)register the managed collections and index them. The
    index is a disposable, rebuildable derivative under `_search/`."""
    cfg = load_config(root)
    qmd_home(root, cfg).mkdir(parents=True, exist_ok=True)
    existing = _registered(root, cfg)
    managed = {c["name"] for c in cfg["collections"]}
    log = []
    for name in sorted(existing & managed):
        p = _run_qmd(["collection", "remove", name], root, cfg)
        if p.returncode != 0:
            raise SearchError(f"failed to remove {name}: {p.stderr.strip()[:300]}")
        log.append(f"removed {name}")
    for c in cfg["collections"]:
        p = _run_qmd(["collection", "add", str(root), "--name", c["name"], "--mask", c["mask"]], root, cfg)
        if p.returncode != 0 or "created successfully" not in p.stdout:
            raise SearchError(f"failed to add {c['name']}: {(p.stdout + p.stderr).strip()[:300]}")
        p = _run_qmd(["context", "add", f"qmd://{c['name']}", c["context"]], root, cfg)
        if p.returncode != 0:
            raise SearchError(f"failed to add context for {c['name']}: {p.stderr.strip()[:300]}")
        verb = "include" if c.get("include_by_default") else "exclude"
        _run_qmd(["collection", verb, c["name"]], root, cfg)
        log.append(f"added {c['name']}")
    refresh(root)
    return {"configured": sorted(managed), "log": log}


def refresh(root: Path = ROOT) -> dict:
    """Re-index changed files. `qmd update` only; never `qmd embed`."""
    cfg = load_config(root)
    p = _run_qmd(["update"], root, cfg, timeout=600)
    if p.returncode != 0:
        raise SearchError(f"qmd update failed: {p.stderr.strip()[:300]}")
    return {"updated": True, "embeddings": "never generated by design"}


def _count_masked_files(root: Path, cfg: dict) -> dict[str, int]:
    counts = {}
    for c in cfg["collections"]:
        n = 0
        for prefix in c["zone_prefixes"]:
            d = root / prefix
            if d.is_dir():
                n += sum(1 for p in d.rglob("*.md") if p.is_file())
        counts[c["name"]] = n
    return counts


def index_freshness(root: Path = ROOT) -> dict:
    """Cheap staleness check for a status tool: file count per collection on
    disk vs indexed, and newest source mtime vs index mtime."""
    cfg = load_config(root)
    disk = _count_masked_files(root, cfg)
    db = qmd_home(root, cfg) / "cache" / "qmd" / "index.sqlite"
    indexed: dict[str, int] = {}
    if db.exists():
        p = _run_qmd(["status"], root, cfg)
        for name, files in re.findall(r"^\s{2}(\S+) \(qmd://\S+\)\n(?:.*\n)*?\s+Files:\s+(\d+)", p.stdout, re.M):
            indexed[name] = int(files)
    newest = 0.0
    for c in cfg["collections"]:
        for prefix in c["zone_prefixes"]:
            d = root / prefix
            if d.is_dir():
                for f in d.rglob("*.md"):
                    newest = max(newest, f.stat().st_mtime)
    index_mtime = db.stat().st_mtime if db.exists() else 0.0
    stale = (not db.exists()) or disk != {k: indexed.get(k, 0) for k in disk} or newest > index_mtime
    return {"index_present": db.exists(), "disk_files": disk, "indexed_files": indexed,
            "stale": bool(stale)}


def verify_model_free(root: Path = ROOT) -> dict:
    """Evidence helper: list any model artefact under the QMD home."""
    cfg = load_config(root)
    home = qmd_home(root, cfg)
    suspects = []
    if home.exists():
        for p in home.rglob("*"):
            if p.is_file() and (p.suffix.lower() in {".gguf", ".bin", ".safetensors", ".onnx"}
                                or "models" in p.relative_to(home).parts):
                suspects.append(p.relative_to(home).as_posix())
    files = sorted(p.relative_to(home).as_posix() for p in home.rglob("*") if p.is_file()) if home.exists() else []
    return {"qmd_home": str(home), "files": files, "model_artifacts": suspects,
            "model_free": not suspects}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", type=Path, default=ROOT)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("configure")
    sub.add_parser("refresh")
    sub.add_parser("status")
    sub.add_parser("verify-model-free")
    for name in ("search", "exact"):
        sp = sub.add_parser(name)
        sp.add_argument("query")
        sp.add_argument("--scope", choices=SCOPES if name == "search" else ("canonical", "captures"),
                        default="canonical")
        sp.add_argument("-n", type=int, default=10)
        if name == "search":
            sp.add_argument("--mode", choices=MODES, default="lexical")
    args = ap.parse_args(argv)
    root = args.root.resolve()
    try:
        if args.cmd == "configure":
            out = configure(root)
        elif args.cmd == "refresh":
            out = refresh(root)
        elif args.cmd == "status":
            out = index_freshness(root)
        elif args.cmd == "verify-model-free":
            out = verify_model_free(root)
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0 if out["model_free"] else 1
        elif args.cmd == "search":
            out = search(args.query, args.mode, args.scope, args.n, root)
        else:
            out = search(args.query, "exact", args.scope, args.n, root)
    except SearchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
