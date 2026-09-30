#!/usr/bin/env python3
"""HUMAN-ONLY review and promotion CLI for candidate proposals.

    python scripts/brain_review.py list [--status new]
    python scripts/brain_review.py inspect PROP_ID
    python scripts/brain_review.py accept  PROP_ID --actor NAME [--note ..] [--yes]
    python scripts/brain_review.py reject  PROP_ID --actor NAME --note ..   [--yes]
    python scripts/brain_review.py defer   PROP_ID --actor NAME [--note ..] [--yes]
    python scripts/brain_review.py edit    PROP_ID --actor NAME --set field=value [--yes]
    python scripts/brain_review.py promote PROP_ID --actor NAME [--paths ...] [--yes]
    python scripts/brain_review.py status | flush

Boundary (K3/K9): the remote MCP surface has NO access to this module. It
must never be imported by wiki_mcp_server.py; tests enforce that the MCP tool
list contains no review verb and that the server does not import this file.
Defense in depth: every mutating command requires --actor and an explicit
interactive confirmation (type the proposal id) unless --yes is given, and
refuses to run when BRAIN_REMOTE_SESSION is set.

accept/reject/defer/edit only change the QUEUE (noncanonical, own commit).
`promote` is the only path that commits canonical content: proposal must be
accepted, full validation must pass, and canonical files are committed in a
commit of their own, apart from any noncanonical commit. Applying the change
to canonical files (authoring) is a human/`/wiki-write` step that happens
before `promote`; this tool never writes canonical text.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import evidence_audit as ea  # noqa: E402
import git_safety as gs  # noqa: E402

QUEUE_REL = ea.QUEUE_REL
DECISIONS = {"accept": "accepted", "reject": "rejected", "defer": "deferred"}
DECIDABLE_FROM = (None, "new", "audited", "deferred")
FULL_VALIDATION = (
    ["scripts/validate_repo.py", "--full"],
    ["scripts/validate_content_release.py"],
    ["scripts/check_against_baseline.py"],
)


class ReviewError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def assert_human_context() -> None:
    if os.environ.get("BRAIN_REMOTE_SESSION"):
        raise ReviewError("review/promotion is forbidden in a remote session")


def _load(root: Path) -> list[dict]:
    return ea._load_proposals(root)


def _dump(recs: list[dict]) -> str:
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs)


def _find(recs: list[dict], pid: str) -> dict:
    for r in recs:
        if r.get("id") == pid:
            return r
    raise ReviewError(f"no such proposal: {pid}")


def list_proposals(root: Path, status: str | None = None) -> list[dict]:
    out = []
    for r in _load(root):
        if status and r.get("status") != status:
            continue
        out.append({"id": r.get("id"), "kind": r.get("kind"),
                    "status": r.get("status"), "received": r.get("received")})
    return out


def inspect(root: Path, pid: str) -> dict:
    """Read-only: the record plus a FRESH evidence check on current disk state."""
    root = Path(root)
    rec = _find(_load(root), pid)
    body = rec.get("body") if isinstance(rec.get("body"), dict) else {}
    fields = {k: v for k, v in body.items() if k != "evidence_refs"}
    chk = ea.validate_submission(root, rec.get("kind"), fields, body.get("evidence_refs", []))
    return {"record": rec, "evidence_check": {"ok": chk.ok, "errors": chk.errors,
                                              "resolved_refs": chk.refs},
            "note": "Evidence check is deterministic form/existence checking, not a semantic verdict."}


def decide(root: Path, pid: str, decision: str, actor: str, note: str = "",
           edits: dict | None = None) -> dict:
    """Record a human decision (own noncanonical commit). Accept re-validates."""
    root = Path(root)
    assert_human_context()
    if decision not in DECISIONS:
        raise ReviewError(f"decision must be one of {sorted(DECISIONS)}")
    if not actor.strip():
        raise ReviewError("--actor is required")
    if decision == "reject" and not note.strip():
        raise ReviewError("reject requires a --note (reason)")
    state: dict = {}

    def write():
        recs = _load(root)
        rec = _find(recs, pid)
        if rec.get("status") not in DECIDABLE_FROM:
            raise ReviewError(f"{pid} is already {rec.get('status')!r}")
        if edits:
            body = dict(rec.get("body") or {})
            before = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
            refs = body.pop("evidence_refs", [])
            body.update(edits)
            chk = ea.validate_submission(root, rec["kind"], body, refs)
            if not chk.ok:
                raise ReviewError(f"edit rejected by validator: {chk.errors}")
            rec["body"] = chk.body
            rec.setdefault("edits", []).append(
                {"actor": actor, "at": _now(), "fields": sorted(edits),
                 "prior_body_sha256": before})
        if decision == "accept":
            chk = inspect(root, pid) if not edits else None
            if chk is not None and not chk["evidence_check"]["ok"]:
                raise ReviewError(f"cannot accept: {chk['evidence_check']['errors']}")
        rec["status"] = DECISIONS[decision]
        rec["adjudication"] = {"decision": DECISIONS[decision], "actor": actor,
                               "at": _now(), "note": note}
        gs.durable_replace(root / QUEUE_REL, _dump(recs))
        state["rec"] = rec
        return [QUEUE_REL.as_posix()]

    label = "edit+" + decision if edits else decision
    res = gs.locked_write_commit(root, write, f"brain-review: {label} {pid} by {actor}")
    return {"proposal_id": pid, "status": DECISIONS[decision],
            "committed": res["committed"], "commit": res["commit"],
            "commit_error": res["error"], "uncommitted": res["uncommitted"]}


def edit(root: Path, pid: str, actor: str, edits: dict, note: str = "") -> dict:
    """Edit fields of a still-open proposal, keeping it open (status unchanged)."""
    root = Path(root)
    assert_human_context()
    if not edits:
        raise ReviewError("nothing to edit")

    def write():
        recs = _load(root)
        rec = _find(recs, pid)
        if rec.get("status") not in DECIDABLE_FROM:
            raise ReviewError(f"{pid} is already {rec.get('status')!r}")
        body = dict(rec.get("body") or {})
        before = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        refs = body.pop("evidence_refs", [])
        body.update(edits)
        chk = ea.validate_submission(root, rec["kind"], body, refs)
        if not chk.ok:
            raise ReviewError(f"edit rejected by validator: {chk.errors}")
        rec["body"] = chk.body
        rec.setdefault("edits", []).append(
            {"actor": actor, "at": _now(), "fields": sorted(edits),
             "prior_body_sha256": before, "note": note})
        gs.durable_replace(root / QUEUE_REL, _dump(recs))
        return [QUEUE_REL.as_posix()]

    res = gs.locked_write_commit(root, write, f"brain-review: edit {pid} by {actor}")
    return {"proposal_id": pid, "committed": res["committed"], "commit": res["commit"],
            "commit_error": res["error"]}


# ---------------------------------------------------------------- promotion

def _run_validation(root: Path) -> list[str]:
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    failures = []
    for cmd in FULL_VALIDATION:
        p = subprocess.run([sys.executable, *cmd], cwd=root, text=True,
                           capture_output=True, encoding="utf-8", env=env)
        if p.returncode != 0:
            tail = "\n".join((p.stdout + p.stderr).strip().splitlines()[-6:])
            failures.append(f"{' '.join(cmd)} exit {p.returncode}: {tail}")
    return failures


def _dirty_paths(root: Path) -> list[str]:
    out = gs._git(root, "status", "--porcelain=v1", "-z", "-uall")
    paths, entries, i = [], out.stdout.split("\0"), 0
    while i < len(entries):
        e = entries[i]
        i += 1
        if len(e) < 4:
            continue
        code, path = e[:2], e[3:]
        if code[0] in "RC":
            i += 1
        paths.append(path)
    return sorted(paths)


def promote(root: Path, pid: str, actor: str, paths: list[str] | None = None,
            validate=None) -> dict:
    """Commit human-authored canonical changes for an ACCEPTED proposal.

    Sequence (all under the lock): proposal accepted -> select canonical
    dirty paths (never _originals/, never noncanonical) -> full validation ->
    canonical-only commit -> (separate commit) record promotion in the queue.
    Failure at any step leaves the working tree exactly as the human left it.
    """
    root = Path(root)
    assert_human_context()
    if not actor.strip():
        raise ReviewError("--actor is required")
    with gs.BrainLock(root):
        rec = _find(_load(root), pid)
        if rec.get("status") != "accepted":
            raise ReviewError(f"{pid} must be 'accepted' before promotion (is {rec.get('status')!r})")
        dirty = _dirty_paths(root)
        canon = [p for p in dirty if not gs.is_noncanonical(p)]
        if paths:
            wanted = {Path(p).as_posix() for p in paths}
            unknown = sorted(wanted - set(canon))
            if unknown:
                raise ReviewError(f"paths are not dirty canonical changes: {unknown}")
            canon = sorted(wanted)
        if not canon:
            raise ReviewError("no canonical changes in the working tree to promote")
        originals = [p for p in canon if p.startswith("_originals/")]
        if originals:
            raise ReviewError(f"_originals/ is immutable and is never promoted here: {originals}")
        failures = (validate or _run_validation)(root)
        if failures:
            return {"promoted": False, "stage": "validation", "failures": failures,
                    "note": "working tree untouched; fix and retry"}
        add = gs._git(root, "add", "--", *canon)
        if add.returncode != 0:
            return {"promoted": False, "stage": "git-add", "failures": [add.stderr.strip()]}
        msg = f"canonical: promote {pid} (human: {actor})"
        com = gs._git(root, "commit", "-m", msg, "--", *canon)
        if com.returncode != 0:
            gs._git(root, "reset", "-q", "--", *canon)
            return {"promoted": False, "stage": "commit",
                    "failures": [(com.stdout + com.stderr).strip()[-1500:]]}
        sha = gs._git(root, "rev-parse", "HEAD").stdout.strip()

        recs = _load(root)
        r2 = _find(recs, pid)
        r2.setdefault("adjudication", {})
        r2["adjudication"]["promoted_commit"] = sha
        r2["adjudication"]["promoted_by"] = actor
        r2["adjudication"]["promoted_at"] = _now()
        r2["adjudication"]["promoted_paths"] = canon
        gs.durable_replace(root / QUEUE_REL, _dump(recs))
        ok, qsha, detail = gs._commit_paths(root, [QUEUE_REL.as_posix()],
                                            f"brain-review: record promotion of {pid}")
        return {"promoted": True, "canonical_commit": sha, "paths": canon,
                "queue_record_committed": ok, "queue_commit": qsha,
                "queue_detail": detail}


# ---------------------------------------------------------------------- CLI

def _confirm(pid: str, action: str, assume_yes: bool) -> None:
    if assume_yes:
        return
    if not sys.stdin.isatty():
        raise ReviewError("non-interactive: pass --yes to confirm explicitly")
    ans = input(f"{action} {pid}? Type the proposal id to confirm: ").strip()
    if ans != pid:
        raise ReviewError("confirmation did not match; aborted")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("list"); s.add_argument("--status")
    s = sub.add_parser("inspect"); s.add_argument("id")
    for name in DECISIONS:
        s = sub.add_parser(name); s.add_argument("id")
        s.add_argument("--actor", required=True); s.add_argument("--note", default="")
        s.add_argument("--yes", action="store_true")
    s = sub.add_parser("edit"); s.add_argument("id")
    s.add_argument("--actor", required=True); s.add_argument("--note", default="")
    s.add_argument("--set", action="append", default=[], metavar="FIELD=VALUE")
    s.add_argument("--yes", action="store_true")
    s = sub.add_parser("promote"); s.add_argument("id")
    s.add_argument("--actor", required=True); s.add_argument("--paths", nargs="*")
    s.add_argument("--yes", action="store_true")
    sub.add_parser("status"); sub.add_parser("flush")
    args = ap.parse_args(argv)
    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parents[1]
    try:
        if args.cmd == "list":
            print(json.dumps(list_proposals(root, args.status), ensure_ascii=False, indent=1))
        elif args.cmd == "inspect":
            print(json.dumps(inspect(root, args.id), ensure_ascii=False, indent=1))
        elif args.cmd in DECISIONS:
            assert_human_context()
            _confirm(args.id, args.cmd, args.yes)
            print(json.dumps(decide(root, args.id, args.cmd, args.actor, args.note),
                             ensure_ascii=False, indent=1))
        elif args.cmd == "edit":
            assert_human_context()
            edits = {}
            for kv in args.set:
                if "=" not in kv:
                    raise ReviewError(f"--set expects FIELD=VALUE, got {kv!r}")
                k, v = kv.split("=", 1)
                edits[k] = v
            _confirm(args.id, "edit", args.yes)
            print(json.dumps(edit(root, args.id, args.actor, edits, args.note),
                             ensure_ascii=False, indent=1))
        elif args.cmd == "promote":
            assert_human_context()
            _confirm(args.id, "PROMOTE TO CANONICAL", args.yes)
            res = promote(root, args.id, args.actor, args.paths)
            print(json.dumps(res, ensure_ascii=False, indent=1))
            return 0 if res.get("promoted") else 1
        elif args.cmd == "status":
            st = gs.uncommitted_state(root)
            print(json.dumps(st, ensure_ascii=False, indent=1))
        elif args.cmd == "flush":
            assert_human_context()
            res = gs.flush_pending(root)
            print(json.dumps(res, ensure_ascii=False, indent=1))
            return 0 if res["committed"] else 1
    except (ReviewError, gs.GovernanceError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
