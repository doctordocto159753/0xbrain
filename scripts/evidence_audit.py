#!/usr/bin/env python3
"""Evidence audit for the candidate layer (Phase 3).

Reads _proposals/proposals.jsonl, validates every record against
00-system/policies/proposal_schema.json, and AUTO-REJECTS any proposal
whose required evidence is missing or unverifiable:
  - source_passage.quote must be >= 20 chars
  - source_passage.path must exist and be inside a canonical zone
  - intake-registration: original_path must exist; sha256 must be 64-hex
  - tier-change.to_tier must be an allowed tier

Writes ONLY an audit report to _audits/ (never mutates the queue, never
touches canonical zones - the human adjudicates from the report).
Retrieval/search scores are never evidence; a passage that cannot be
found verbatim in the cited file fails even if a score is high.

Importable API (single validator, no second implementation):
  validate_body(root, kind, struct)            -> list[str]   (used by audit())
  validate_submission(root, kind, fields, evidence_refs) -> SubmissionCheck
      pre-queue gate for brain_propose: kind + structured fields + evidence
      refs. audit() and validate_submission() share validate_body().

Usage: python scripts/evidence_audit.py [--root WIKI_ROOT] [--verbose]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path

SCHEMA_REL = Path("00-system/policies/proposal_schema.json")
QUEUE_REL = Path("_proposals/proposals.jsonl")
CANONICAL_ZONES = ("02-sources", "03-objects", "04-notes", "05-claims", "06-relations")
SHA256_RE = re.compile(r"\b[0-9a-f]{64}\b", re.IGNORECASE)


def _load_proposals(root: Path) -> list[dict]:
    p = root / QUEUE_REL
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                out.append({"id": None, "kind": None, "body": line,
                            "received": None, "status": "new",
                            "_malformed": True})
    return out


def _body_struct(body) -> dict | None:
    if isinstance(body, dict):
        return body
    if isinstance(body, str):
        s = body.strip()
        if s.startswith("{"):
            try:
                v = json.loads(s)
                return v if isinstance(v, dict) else None
            except json.JSONDecodeError:
                return None
    return None


def _extract_passage_from_prose(body: str) -> dict | None:
    """Free-prose fallback: find quote/path evidence markers in text."""
    pm = re.search(r'(?:source_passage|path)\s*[:=]\s*["\']?([^\n"\']+\.md)', body)
    qm = re.search(r'[""\"]([^""\"]{20,})[""\"]', body)
    if pm and qm:
        return {"path": pm.group(1).strip(), "quote": qm.group(1).strip()}
    return None


def _zone_path(root: Path, rel: str) -> Path | None:
    """Resolve a repo-relative path to a file INSIDE a canonical zone.

    Rejects traversal (`03-objects/../_originals/x.md`), absolute paths and
    anything that resolves outside the zone it names. Returns None if the
    path is not a safe canonical-zone path (existence is checked by callers).
    """
    norm = rel.replace("\\", "/").strip()
    if not norm or norm.startswith("/") or not norm.startswith(CANONICAL_ZONES):
        return None
    zone = norm.split("/", 1)[0]
    try:
        resolved = (root / norm).resolve()
        resolved.relative_to((root / zone).resolve())
    except (ValueError, OSError):
        return None
    return resolved


def _check_passage(root: Path, sp: dict) -> list[str]:
    errs: list[str] = []
    quote = str(sp.get("quote", ""))
    path = str(sp.get("path", "")).strip()
    if len(quote) < 20:
        errs.append(f"source_passage.quote too short ({len(quote)} chars, min 20)")
    if not path:
        errs.append("source_passage.path missing")
        return errs
    f = _zone_path(root, path)
    if f is None:
        errs.append(f"source_passage.path not canonical: {path}")
        return errs
    if not f.is_file():
        errs.append(f"source_passage.path does not exist: {path}")
    elif quote and quote not in f.read_text(encoding="utf-8", errors="ignore"):
        errs.append("source_passage.quote NOT found verbatim in cited file")
    return errs


def validate_body(root: Path, kind: str, struct: dict, schema: dict | None = None) -> list[str]:
    """Validate a proposal body against the schema entry for `kind`.

    The one implementation of the per-kind evidence rules. Returns a list of
    error strings (empty = passes). Called by audit() for queued records and
    by validate_submission() before a new record is appended.
    """
    if schema is None:
        schema = json.loads((root / SCHEMA_REL).read_text(encoding="utf-8-sig"))
    kinds = schema["kinds"]
    errs: list[str] = []
    if kind not in kinds:
        return [f"unknown kind: {kind!r}"]
    required = kinds[kind]["required"]
    missing = [f for f in required if f not in struct and f != "source_passage"]
    if missing:
        errs.append(f"missing body fields: {missing}")
    sp = struct.get("source_passage")
    if "source_passage" in required:
        if isinstance(sp, dict):
            errs += _check_passage(root, sp)
        elif isinstance(sp, str):
            errs += _check_passage(root, {"path": sp, "quote": ""})
        else:
            errs.append("source_passage missing (auto-reject per schema)")
    if kind == "intake-registration":
        raw = str(struct.get("original_path", ""))
        op = (root / raw).resolve() if raw else None
        inside = False
        if op is not None:
            try:
                op.relative_to(root.resolve())
                inside = True
            except ValueError:
                inside = False
        if op is None or not inside or not op.exists():
            errs.append(f"original_path does not exist: {struct.get('original_path')}")
        if not SHA256_RE.search(str(struct.get("sha256", ""))):
            errs.append("sha256 not 64-hex")
    if kind == "tier-change":
        allowed = kinds[kind].get("to_tier_enum", [])
        if struct.get("to_tier") not in allowed:
            errs.append(f"to_tier {struct.get('to_tier')!r} not in {allowed}")
    return errs


# ---------------------------------------------------------------------------
# Pre-queue gate (brain_propose)

MAX_FIELD_CHARS = 20_000
CAPTURE_ID_RE = re.compile(r"^cap-\d{8}-\d{6}-[0-9a-f]{4}$")
CAPTURES_REL = Path("01-inbox/captures")
_FM_ID_RE = re.compile(r"^id:\s*[\"']?([^\"'\r\n]+?)[\"']?\s*$", re.MULTILINE)


class SubmissionCheck:
    """Result of validate_submission(); `errors` empty means acceptable."""

    def __init__(self, errors: list[str], body: dict | None, refs: list[dict]):
        self.errors = errors
        self.body = body
        self.refs = refs

    @property
    def ok(self) -> bool:
        return not self.errors


def resolve_ref(root: Path, ref: str) -> dict | None:
    """Resolve an evidence ref to {ref, tier, path} or None.

    Accepted grammar (never an arbitrary path):
      - capture id  cap-YYYYMMDD-HHMMSS-xxxx   -> noncanonical capture record
      - record id   frontmatter `id:` of a record in a canonical zone
      - canonical zone path  (02-sources|03-objects|04-notes|05-claims|
        06-relations)/....md  (traversal-safe)
    """
    ref = str(ref).strip()
    if not ref or len(ref) > 300:
        return None
    if CAPTURE_ID_RE.match(ref):
        cdir = root / CAPTURES_REL
        if cdir.is_dir():
            for p in sorted(cdir.rglob(f"{ref}.md")):
                return {"ref": ref, "tier": "capture",
                        "path": p.relative_to(root).as_posix()}
        return None
    if "/" in ref or ref.endswith(".md"):
        p = _zone_path(root, ref)
        if p is not None and p.is_file() and p.suffix == ".md":
            return {"ref": ref, "tier": "canonical",
                    "path": p.relative_to(root.resolve()).as_posix()}
        return None
    for zone in CANONICAL_ZONES:
        zdir = root / zone
        if not zdir.is_dir():
            continue
        for md in sorted(zdir.rglob("*.md")):
            head = md.read_text(encoding="utf-8", errors="ignore")[:2000]
            if head.startswith("---"):
                m = _FM_ID_RE.search(head)
                if m and m.group(1).strip() == ref:
                    return {"ref": ref, "tier": "canonical",
                            "path": md.relative_to(root).as_posix()}
    return None


def validate_submission(root: Path, kind: str, structured_fields,
                        evidence_refs) -> SubmissionCheck:
    """Validate a NEW proposal before it may be appended to the queue.

    Checks kind, structured fields (types, no unknown keys, size) via
    validate_body(), and evidence refs (each must resolve; a required
    source_passage must be covered by an evidence ref). No model call, no
    write. Semantic quality is out of scope: this gates form and evidence
    existence only.
    """
    root = Path(root)
    errs: list[str] = []
    try:
        schema = json.loads((root / SCHEMA_REL).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        return SubmissionCheck([f"proposal schema unavailable: {exc}"], None, [])
    kinds = schema["kinds"]
    if not isinstance(kind, str) or kind not in kinds:
        return SubmissionCheck([f"unknown kind: {kind!r} (allowed: {sorted(kinds)})"],
                               None, [])
    if not isinstance(structured_fields, dict) or not structured_fields:
        return SubmissionCheck(["structured_fields must be a non-empty object"], None, [])
    required = kinds[kind]["required"]
    unknown = sorted(set(structured_fields) - set(required))
    if unknown:
        errs.append(f"unknown structured field(s) for {kind}: {unknown}")
    for name in required:
        v = structured_fields.get(name)
        if name == "source_passage":
            if not isinstance(v, dict):
                continue  # reported by validate_body
            for k in ("quote", "path"):
                if not isinstance(v.get(k), str) or not v.get(k).strip():
                    errs.append(f"source_passage.{k} must be a non-empty string")
        elif name in structured_fields and (not isinstance(v, str) or not v.strip()):
            errs.append(f"field {name!r} must be a non-empty string")
        elif isinstance(v, str) and len(v) > MAX_FIELD_CHARS:
            errs.append(f"field {name!r} exceeds {MAX_FIELD_CHARS} chars")
    if not errs:
        errs += validate_body(root, kind, dict(structured_fields), schema)

    refs: list[dict] = []
    if not isinstance(evidence_refs, list) or any(not isinstance(r, str) for r in evidence_refs):
        errs.append("evidence_refs must be a list of strings")
        evidence_refs = []
    needs_passage = "source_passage" in required
    if needs_passage and not evidence_refs:
        errs.append("evidence_refs must be non-empty for this kind")
    if len(evidence_refs) > 100:
        errs.append("evidence_refs exceeds 100 entries")
        evidence_refs = evidence_refs[:100]
    for r in evidence_refs:
        hit = resolve_ref(root, r)
        if hit is None:
            errs.append(f"evidence ref does not resolve: {str(r)[:120]!r}")
        else:
            refs.append(hit)
    sp = structured_fields.get("source_passage")
    if needs_passage and isinstance(sp, dict) and not errs:
        cited = _zone_path(root, str(sp.get("path", "")))
        cited_rel = cited.relative_to(root.resolve()).as_posix() if cited else None
        if cited_rel not in {x["path"] for x in refs if x["tier"] == "canonical"}:
            errs.append("source_passage.path is not covered by any canonical evidence ref")
    if errs:
        return SubmissionCheck(errs, None, refs)
    body = dict(structured_fields)
    body["evidence_refs"] = [x["ref"] for x in refs]
    return SubmissionCheck([], body, refs)


def audit(root: Path, verbose: bool = False) -> tuple[int, int, int]:
    schema = json.loads((root / SCHEMA_REL).read_text(encoding="utf-8-sig"))
    kinds = schema["kinds"]
    sp_rule = schema["source_passage"]
    proposals = _load_proposals(root)

    results: list[dict] = []
    accepted = rejected = skipped = 0
    for rec in proposals:
        pid = rec.get("id") or f"unparsed-line-{len(results)+1}"
        errs: list[str] = []
        if rec.get("_malformed"):
            errs.append("unparseable JSONL line")
        kind = rec.get("kind")
        if not rec.get("_malformed"):
            if not kind or kind not in kinds:
                errs.append(f"unknown kind: {kind!r}")
            if rec.get("authority_tier") != "candidate":
                errs.append(f"authority_tier must be 'candidate', got {rec.get('authority_tier')!r}")
            struct = _body_struct(rec.get("body")) or {}
            if not struct and isinstance(rec.get("body"), str):
                struct = _extract_passage_from_prose(rec.get("body", "")) or {}
            if kind in kinds:
                errs += validate_body(root, kind, struct, schema)
        verdict = "rejected-audit" if errs else "audited"
        if errs:
            rejected += 1
        elif rec.get("status") not in (None, "new"):
            skipped += 1
            verdict = f"skipped (status={rec.get('status')})"
        else:
            accepted += 1
        results.append({"id": pid, "kind": kind, "verdict": verdict,
                        "errors": errs})

    report = {
        "generated_by": "evidence_audit.py",
        "generated_at_epoch": int(time.time()),
        "schema_version": schema.get("version"),
        "totals": {"audited": accepted, "rejected-audit": rejected,
                   "skipped": skipped, "total": len(proposals)},
        "authority_note": ("Audit is deterministic tooling; its verdicts are "
                           "advisory to the human adjudicator. Proposals stay "
                           "inert in _proposals/proposals.jsonl."),
        "results": results,
    }
    out_dir = root / "_audits"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"evidence-audit-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"evidence audit: {len(proposals)} proposals -> "
          f"{accepted} pass / {rejected} auto-reject / {skipped} skipped")
    print(f"report: {out}")
    if verbose:
        for r in results:
            if r["errors"]:
                print(f"  REJECT {r['id']} ({r['kind']}): {r['errors']}")
    return 0 if not rejected else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=None)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parents[1]
    return audit(root, args.verbose)


if __name__ == "__main__":
    sys.exit(main())
