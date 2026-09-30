"""Behavioral fixtures (docs/claude/fixtures/*.json).

Each fixture is a reference transcript: the tool calls a compliant Claude
makes, and its reply. The test (1) replays the calls against the real
surface + FakeBackend and checks the results, (2) checks forbidden tools are
absent, (3) lints the reply against the standing rules (no persistence claim
without a successful tool result; required/forbidden phrases).

This proves the tools support and enforce the behavior and that the
reference behavior is self-consistent. It does NOT prove a live model follows
the instructions; that needs the manual/live run in CONNECTOR_GUIDE.md.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from brain_surface import BrainSurface  # noqa: E402
from brain_surface.backend import FakeBackend  # noqa: E402

FIXDIR = ROOT / "docs" / "claude" / "fixtures"
FIXTURES = sorted(FIXDIR.glob("*.json"))
CLAIM_RE = re.compile(
    r"(?i)\b(saved|stored|captured|recorded|filed|queued)\b|ذخیره شد|ثبت شد")
NEG_RE = re.compile(r"(?i)nothing was saved|not (been )?(saved|stored)|unavailable")


def dig(obj, dotted):
    for part in dotted.split("."):
        if not isinstance(obj, dict) or part not in obj:
            return KeyError
        obj = obj[part]
    return obj


def check(result, expect):
    for path, want in expect.items():
        got = dig(result, path)
        assert got is not KeyError, f"{path} missing in {result}"
        if isinstance(want, str) and want.startswith(">="):
            assert got >= int(want[2:]), (path, got, want)
        else:
            assert got == want, (path, got, want)


def load(fx):
    b = FakeBackend()
    for r in fx.get("setup", {}).get("records", []):
        b.add_record(r["id"], r["title"], r["text"], zone=r.get("zone", "03-objects"),
                     level=r.get("level", 4), edges=r.get("edges", ()))
    for c in fx.get("setup", {}).get("captures", []):
        b.add_capture(c["id"], c["text"], media=c.get("media"), needs=c.get("needs"))
    b.unavailable |= set(fx.get("connector_unavailable", []))
    kw = {"page_limit": fx["page_limit"]} if "page_limit" in fx else {}
    return BrainSurface(b, **kw), b


def lint_honesty(reply, results, fx):
    """Persistence claims need a successful persisted write; cited refs must
    have appeared in a tool result (or fixture setup)."""
    wrote_ok = any(r.get("ok") and r.get("persisted") for r in results)
    if CLAIM_RE.search(reply) and not NEG_RE.search(reply):
        assert wrote_ok, "reply claims persistence without a successful write"
    blob = json.dumps(results) + json.dumps(fx.get("setup", {}))
    for ref in re.findall(r"\b(?:rec|cap|prop):[A-Za-z0-9_-]+", reply):
        assert ref in blob or ref.split(":", 1)[1] in blob, \
            f"reply cites {ref} not seen in results"


@pytest.mark.parametrize("path", FIXTURES, ids=[p.stem for p in FIXTURES])
def test_fixture(path):
    fx = json.loads(path.read_text(encoding="utf-8"))
    s, b = load(fx)
    used, results, cursor = [], [], None
    for step in fx["reference_calls"]:
        args = json.loads(json.dumps(step["args"]).replace("$cursor", cursor or ""))
        res = s.call(step["tool"], args)
        used.append(step["tool"])
        results.append(res)
        check(res, step.get("expect", {}))
        if step.get("save_cursor"):
            cursor = res["next_cursor"]
    assert not set(used) & set(fx["forbidden_tools"]), (used, fx["forbidden_tools"])

    reply = fx["reference_reply"]
    for pat in fx["reply_must_match"]:
        assert re.search(pat, reply), f"reply lacks {pat!r}"
    for pat in fx["reply_must_not_match"]:
        assert not re.search(pat, reply), f"reply contains forbidden {pat!r}"

    lint_honesty(reply, results, fx)


def test_nine_required_scenarios_present():
    ids = [p.stem for p in FIXTURES]
    assert len(ids) >= 9
    for key in ("retrieve", "capture-durable", "trivial", "conflict",
                "provisional", "evidence", "pagination", "unavailable", "media"):
        assert any(key in i for i in ids), key


def test_lint_rejects_violations():
    """Mutation check: the honesty lint must fail on real violations."""
    failed_write = [{"ok": False, "persisted": False}]
    with pytest.raises(AssertionError):
        lint_honesty("Saved it as cap:cap-1.", failed_write, {})
    with pytest.raises(AssertionError):
        lint_honesty("Stored.", [], {})
    with pytest.raises(AssertionError):  # invented ref
        lint_honesty("See rec:ghost.", [{"ok": True}], {})
    lint_honesty("Nothing was saved; the connector is unavailable.", failed_write, {})
    ok = [{"ok": True, "persisted": True, "ref": "cap:cap-1"}]
    lint_honesty("Captured as cap:cap-1.", ok, {})
