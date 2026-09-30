#!/usr/bin/env python3
"""Structured proposal intake for `brain_propose(kind, structured_fields, evidence_refs)`.

This is the ONLY function the remote MCP surface should call for proposals.
It has no review, accept, reject, edit, or promote capability (those live in
brain_review.py, which the MCP server must never import).

Flow:  validate (evidence_audit.validate_submission) -> reject junk BEFORE
touching the queue -> K9 sequence (lock, durable append, post-write
validation, pathspec commit) via git_safety.locked_write_commit.

Candidate tier by construction: authority_tier="candidate", status="new".
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import evidence_audit as ea  # noqa: E402
import git_safety as gs  # noqa: E402

QUEUE_REL = ea.QUEUE_REL


def _new_id(root: Path) -> str:
    existing = {r.get("id") for r in ea._load_proposals(root)}
    base = datetime.now().strftime("prop-%Y%m%d-%H%M%S-%f")[:-3]
    pid, n = base, 1
    while pid in existing:
        n += 1
        pid = f"{base}-{n}"
    return pid


def queue_errors(root: Path, proposal_id: str) -> list[str]:
    """Post-write validation: queue parses line-by-line and the new record
    round-trips through the shared validator."""
    q = root / QUEUE_REL
    errs: list[str] = []
    found = None
    for i, line in enumerate(q.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            errs.append(f"queue line {i} is not valid JSON")
            continue
        if rec.get("id") == proposal_id:
            found = rec
    if found is None:
        return errs + [f"proposal {proposal_id} missing from queue after write"]
    body = found.get("body") if isinstance(found.get("body"), dict) else {}
    fields = {k: v for k, v in body.items() if k != "evidence_refs"}
    chk = ea.validate_submission(root, found.get("kind"), fields, body.get("evidence_refs", []))
    return errs + chk.errors


def submit_proposal(root: Path, kind, structured_fields, evidence_refs,
                    commit: bool = True) -> dict:
    """Validate and enqueue one candidate proposal.

    Returns {"accepted": False, "errors": [...]} without touching disk when
    validation fails. On success the record is durably appended; `committed`
    reports whether the Git commit landed (data survives either way).
    """
    root = Path(root)
    chk = ea.validate_submission(root, kind, structured_fields, evidence_refs)
    if not chk.ok:
        return {"accepted": False, "errors": chk.errors,
                "note": "rejected before queueing; nothing was written"}
    pid = _new_id(root)
    record = {
        "id": pid,
        "received": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "kind": kind,
        "authority_tier": "candidate",
        "status": "new",
        "adjudication": None,
        "body": chk.body,
    }
    line = json.dumps(record, ensure_ascii=False)
    q = root / QUEUE_REL

    def write():
        gs.durable_append(q, line)
        return [QUEUE_REL.as_posix()]

    if not commit:
        with gs.BrainLock(root):
            paths = write()
        return {"accepted": True, "proposal_id": pid, "queue": QUEUE_REL.as_posix(),
                "committed": False, "commit": None, "uncommitted": paths,
                "note": "written without commit (commit=False)"}

    res = gs.locked_write_commit(
        root, write, f"brain: propose {kind} {pid}",
        validate=lambda: queue_errors(root, pid))
    return {"accepted": True, "proposal_id": pid, "queue": QUEUE_REL.as_posix(),
            "committed": res["committed"], "commit": res["commit"],
            "commit_error": res["error"], "uncommitted": res["uncommitted"],
            "note": ("Candidate-tier only. A human adjudicates via the local "
                     "review CLI; nothing enters canonical records automatically.")}


def open_proposals(root: Path) -> list[dict]:
    """Read-only: proposals still awaiting a human decision."""
    return [r for r in ea._load_proposals(Path(root))
            if r.get("status") in (None, "new", "audited")]
