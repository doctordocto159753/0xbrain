"""Behavioral fixtures (docs/claude/fixtures/*.json).

Each fixture is a reference transcript: the tool calls a compliant Claude
makes, and its reply. The test (1) replays the calls against the real
surface over the real integrated backend (a throwaway git wiki seeded from
the fixture's setup) and checks the results, (2) checks forbidden tools are
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
from brain_surface.backend import BackendUnavailable, WikiBackend  # noqa: E402
from conftest import PNG, commit_all, record  # noqa: E402

FIXDIR = ROOT / "docs" / "claude" / "fixtures"
FIXTURES = sorted(FIXDIR.glob("*.json"))
CLAIM_RE = re.compile(
    r"(?i)\b(saved|stored|captured|recorded|filed|queued)\b|ذخیره شد|ثبت شد")
NEG_RE = re.compile(r"(?i)nothing was saved|not (been )?(saved|stored)|unavailable")
ZONE_DIR = {"03-objects": "03-objects", "04-notes": "04-notes", "05-claims": "05-claims",
            "06-relations": "06-relations", "02-sources": "02-sources/records"}


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
        elif want == "!null":
            assert got is not None, (path, got)
        else:
            assert got == want, (path, got, want)


class Outage:
    """Wraps the real backend; listed operations fail as a dead dependency would."""

    def __init__(self, inner, down):
        self.inner, self.down = inner, set(down)

    def __getattr__(self, name):
        if name in self.down:
            def fail(*a, **kw):
                raise BackendUnavailable(f"{name} unavailable")
            return fail
        return getattr(self.inner, name)


def load(fx, root, tmp_path):
    """Materialise the fixture setup as real records/captures in a real wiki.
    Returns the surface and the fake->real id map for setup captures."""
    import wiki_capture as wc
    for r in fx.get("setup", {}).get("records", []):
        zone = ZONE_DIR[r.get("zone", "03-objects")]
        record(root, f"{zone}/{r['id']}.md", r["id"], r["title"], r["text"],
               links=r.get("edges", ()))
    ids = {}
    for c in fx.get("setup", {}).get("captures", []):
        if c.get("media"):
            src = tmp_path / "photo.png"
            src.write_bytes(PNG)
            made = wc.capture_media(str(src), c["media"], "mcp")
        else:
            made = wc.capture_text(c["text"], "mcp")
        ids[c["id"]] = made["id"]
    commit_all(root)
    backend = WikiBackend(root, page_size=fx.get("page_limit"))
    if fx.get("connector_unavailable"):
        backend = Outage(backend, fx["connector_unavailable"])
    return BrainSurface(backend), ids


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
def test_fixture(path, brain_wiki, tmp_path):
    fx = json.loads(path.read_text(encoding="utf-8"))
    s, ids = load(fx, brain_wiki, tmp_path)
    used, results, cursor = [], [], None
    fake = {"cap": 0, "prop": 0}
    for step in fx["reference_calls"]:
        raw = json.dumps(step["args"], ensure_ascii=False).replace("$cursor", cursor or "")
        for fid, real in ids.items():
            raw = raw.replace(fid, real)
        res = s.call(step["tool"], json.loads(raw))
        used.append(step["tool"])
        results.append(res)
        check(res, step.get("expect", {}))
        if step.get("save_cursor"):
            cursor = res["next_cursor"]
        ref = res.get("ref") if step["tool"] in ("brain_capture", "brain_propose") else None
        if ref:   # the reference reply names the n-th new object by a placeholder id
            kind = ref.split(":", 1)[0]
            fake[kind] += 1
            ids[f"{kind}-fake-{fake[kind]:04d}"] = ref.split(":", 1)[1]
    assert not set(used) & set(fx["forbidden_tools"]), (used, fx["forbidden_tools"])

    reply = fx["reference_reply"]
    for fid, real in ids.items():
        reply = reply.replace(fid, real)
    for pat in fx["reply_must_match"]:
        for fid, real in ids.items():
            pat = pat.replace(fid, real)
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
