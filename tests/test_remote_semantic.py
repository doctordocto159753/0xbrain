"""Integrated release gate over real HTTP (Agent H).

The official MCP Python client talks to the real uvicorn server with the
production (semantic, fail-closed) surface over a real throwaway git wiki:
OAuth 2.1 (DCR, PKCE S256, owner consent), the exact six brain_* tools,
the capture -> search -> read -> reconcile -> propose flow, K9 commits, and
the security regressions the release requires.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import stat
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import pytest

pytest.importorskip("mcp")
pytest.importorskip("uvicorn")

import httpx2  # noqa: E402

from conftest import commit_all, record  # noqa: E402
from test_mcp_remote import (REDIRECT, SECRET, MemStorage, Running, run,  # noqa: E402
                             text_of, with_session)

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import brain_review as br  # noqa: E402
from remote_mcp.adapter import SEMANTIC_TOOLS, load_adapter, semantic_adapter  # noqa: E402
from remote_mcp import server as srv_mod  # noqa: E402

QUOTE = "Project Nil is the working title of the second novel."
FORBIDDEN = {"wiki_read", "wiki_get_media", "wiki_mark_capture_reviewed", "wiki_propose",
             "get", "multi_get", "query", "vsearch", "qmd_get", "qmd_multi_get",
             "brain_review", "brain_accept", "brain_promote", "wiki_write"}


@pytest.fixture()
def wiki(brain_wiki):
    record(brain_wiki, "03-objects/nil.md", "obj-nil", "Project Nil", QUOTE)
    commit_all(brain_wiki, "corpus")
    return brain_wiki


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, text=True, capture_output=True,
                          check=True).stdout


# ------------------------------------------------------------ surface on the wire

def test_default_adapter_is_exactly_the_six_semantic_tools():
    assert tuple(t.name for t in load_adapter(env={})) == SEMANTIC_TOOLS


def test_remote_tool_list_is_exact_and_flow_works_end_to_end(wiki):
    marker = "marker-" + secrets.token_hex(4)

    async def scenario(s):
        out = {"tools": [t.name for t in (await s.list_tools()).tools]}
        out["status0"] = text_of(await s.call_tool("brain_status", {}))
        cap = await s.call_tool("brain_capture", {"text": f"Decision {marker}: keep Nil.",
                                                  "language_hint": "en"})
        out["capture"] = text_of(cap)
        out["capture_is_error"] = cap.is_error
        out["search"] = text_of(await s.call_tool(
            "brain_search", {"query": marker, "scope": "captures"}))
        out["exact"] = text_of(await s.call_tool(
            "brain_search", {"query": marker, "mode": "exact", "scope": "all"}))
        out["read"] = text_of(await s.call_tool("brain_read", {"ref": out["capture"]["ref"]}))
        out["reconcile"] = text_of(await s.call_tool(
            "brain_reconcile_context", {"seed_ref": "rec:obj-nil", "depth": "deep"}))
        out["propose"] = text_of(await s.call_tool("brain_propose", {
            "kind": "object-note",
            "structured_fields": {"object_id": "obj-nil", "note": f"see {marker}",
                                  "why": "owner decision captured"},
            "evidence_refs": [{"ref": "rec:obj-nil", "quote": QUOTE},
                              {"ref": out["capture"]["ref"]}]}))
        forged = await s.call_tool("brain_propose", {
            "kind": "object-note",
            "structured_fields": {"object_id": "obj-nil", "note": "x", "why": "y"},
            "evidence_refs": [{"ref": "rec:obj-nil", "quote": "this sentence was never written"}]})
        out["forged"], out["forged_is_error"] = text_of(forged), forged.is_error
        path = await s.call_tool("brain_read", {"ref": "03-objects/nil.md"})
        out["path"], out["path_is_error"] = text_of(path), path.is_error
        out["denied"] = {n: (await s.call_tool(n, {})).is_error for n in sorted(FORBIDDEN)}
        out["status1"] = text_of(await s.call_tool("brain_status", {}))
        return out

    with Running(tools=semantic_adapter(wiki)) as srv:
        o = run(with_session(srv.url, MemStorage(), scenario))
    assert tuple(o["tools"]) == SEMANTIC_TOOLS
    assert not set(o["tools"]) & FORBIDDEN
    assert o["status0"]["ok"] and o["status0"]["uncommitted"]["clean"]
    c = o["capture"]
    assert c["ok"] and c["persisted"] and c["commit_state"] == "committed" and not o["capture_is_error"]
    cap_rel = next((wiki / "01-inbox/captures").rglob(c["ref"][4:] + ".md")).relative_to(wiki).as_posix()
    assert c["commit"] == git(wiki, "log", "-1", "--format=%H", "--", cap_rel).strip()
    assert git(wiki, "show", "--name-only", "--format=", c["commit"]).split() == [cap_rel]
    assert [h["ref"] for h in o["search"]["groups"]["captures"]["results"]] == [c["ref"]]
    assert o["exact"]["groups"]["canonical"]["count"] == 0
    assert marker in o["read"]["sections"]["User-supplied text"]
    assert o["reconcile"]["ok"] and o["reconcile"]["verdict"] is None
    p = o["propose"]
    assert p["ok"] and p["authority_tier"] == "candidate" and p["commit_state"] == "committed"
    assert o["forged_is_error"] and o["forged"]["persisted"] is False
    assert o["path_is_error"] and o["path"]["error"]["code"] == "E_BAD_REF"
    assert all(o["denied"].values())
    assert o["status1"]["proposals"]["pending"] == 1 and o["status1"]["counts"]["captures"] == 1
    # the candidate never touched a canonical zone
    assert git(wiki, "log", "--format=%s", "-3").split("\n")[:2] == [
        f"brain: propose object-note {p['ref'][5:]}", "brain: capture (mcp)"]
    assert git(wiki, "status", "--porcelain").strip() == ""


# ------------------------------------------------------------ unauthenticated

def test_unauthenticated_list_and_call_rejected_on_every_route(wiki):
    body_list = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    body_call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "brain_status", "arguments": {}}}
    hdr = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    with Running(tools=semantic_adapter(wiki)) as srv, httpx2.Client() as h:
        for path in ("/mcp", "/mcp/"):
            for body in (body_list, body_call):
                r = h.post(srv.url + path, json=body, headers=hdr, follow_redirects=True)
                assert r.status_code == 401, (path, r.status_code)
                assert "brain_" not in r.text
        for path in ("/", "/tools", "/sse", "/messages", "/mcp/sse"):
            assert h.post(srv.url + path, json=body_call, headers=hdr).status_code in (401, 404, 405)
        hz = h.get(srv.url + "/healthz")
        assert hz.json() == {"ok": True} and len(hz.content) < 20


# ------------------------------------------------------------ OAuth hardening

def _pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _register(h, url):
    r = h.post(url + "/register", json={
        "client_name": "manual", "redirect_uris": [REDIRECT],
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        "token_endpoint_auth_method": "client_secret_post", "scope": "brain"})
    assert r.status_code == 201, r.text
    return r.json()


def _authorize(h, url, client, challenge, **over):
    q = {"response_type": "code", "client_id": client["client_id"], "redirect_uri": REDIRECT,
         "code_challenge": challenge, "code_challenge_method": "S256", "state": "st",
         "resource": url + "/mcp", "scope": "brain", **over}
    return h.get(url + "/authorize?" + urlencode({k: v for k, v in q.items() if v is not None}))


def _code(h, url, client, challenge):
    r = _authorize(h, url, client, challenge)
    assert r.status_code in (302, 307), r.text
    login = r.headers["location"]
    r = h.post(login, data={"secret": SECRET})
    assert r.status_code == 303
    return parse_qs(urlparse(r.headers["location"]).query)["code"][0]


def _token(h, url, client, code, verifier):
    return h.post(url + "/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
        "client_id": client["client_id"], "client_secret": client["client_secret"],
        "code_verifier": verifier, "resource": url + "/mcp"})


def test_pkce_s256_required_code_single_use_and_no_open_redirect():
    with Running(tools=[]) as srv, httpx2.Client() as h:
        client = _register(h, srv.url)
        verifier, challenge = _pkce()
        # no challenge / plain method: refused, never reaches the owner form
        for over in ({"code_challenge": None}, {"code_challenge_method": "plain"}):
            r = _authorize(h, srv.url, client, challenge, **over)
            assert "/owner/login" not in r.headers.get("location", "")
        # unregistered redirect: error page, no redirect anywhere
        r = _authorize(h, srv.url, client, challenge, redirect_uri="https://evil.example/cb")
        assert r.status_code == 400 and "location" not in r.headers
        code = _code(h, srv.url, client, challenge)
        assert _token(h, srv.url, client, code, "wrong-" + verifier).status_code == 400
        code = _code(h, srv.url, client, challenge)
        ok = _token(h, srv.url, client, code, verifier)
        assert ok.status_code == 200, ok.text
        assert _token(h, srv.url, client, code, verifier).status_code == 400     # one-time


def test_token_resource_and_scope_binding_and_revocation():
    with Running(tools=[]) as srv, httpx2.Client() as h:
        prov = srv.app.state.auth_provider
        hdr = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        init = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}

        def plant(resource, scopes):
            tok = secrets.token_urlsafe(32)
            prov._access[hashlib.sha256(tok.encode()).hexdigest()] = {
                "client_id": "c", "scopes": scopes, "expires_at": None, "resource": resource,
                "subject": None}
            return tok

        good = plant(srv.url + "/mcp", ["brain"])
        other = plant("https://other.example/mcp", ["brain"])
        noscope = plant(srv.url + "/mcp", [])
        post = lambda t: h.post(srv.url + "/mcp", json=init,
                                headers={**hdr, "Authorization": f"Bearer {t}"}).status_code
        assert post(good) == 200
        assert post(other) == 401
        assert post(noscope) == 403
        client = _register(h, srv.url)
        verifier, challenge = _pkce()
        tok = _token(h, srv.url, client, _code(h, srv.url, client, challenge), verifier).json()
        assert post(tok["access_token"]) == 200
        h.post(srv.url + "/revoke", data={"token": tok["refresh_token"],
                                          "client_id": client["client_id"],
                                          "client_secret": client["client_secret"]})
        assert post(tok["access_token"]) == 401          # family revoked


def test_auth_state_file_is_private_and_holds_no_bearer_material(tmp_path, capfd):
    state = tmp_path / "auth"
    with Running(tools=[], state_dir=state) as srv, httpx2.Client() as h:
        client = _register(h, srv.url)
        verifier, challenge = _pkce()
        tok = _token(h, srv.url, client, _code(h, srv.url, client, challenge), verifier).json()
    f = state / "oauth_state.json"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    text = f.read_text()
    assert tok["access_token"] not in text and tok["refresh_token"] not in text
    assert SECRET not in text
    out, err = capfd.readouterr()
    assert SECRET not in out + err


def test_registration_and_pending_consent_are_bounded():
    with Running(tools=[]) as srv, httpx2.Client() as h:
        prov = srv.app.state.auth_provider
        for _ in range(40):
            _register(h, srv.url)
        assert len(prov._clients) <= 32
        client = _register(h, srv.url)
        _, challenge = _pkce()
        for _ in range(80):
            _authorize(h, srv.url, client, challenge)
        assert len(prov._pending) <= 64


# ------------------------------------------------------------ startup fail-closed

def test_startup_configuration_fails_closed(monkeypatch, tmp_path):
    base = {"BRAIN_PUBLIC_URL": "https://brain.example.com", "BRAIN_OWNER_SECRET": SECRET}
    for k in ("BRAIN_STATE_DIR", "BRAIN_EPHEMERAL_AUTH", "BRAIN_ALLOWED_REDIRECTS",
              "BRAIN_MCP_ADAPTER", "BRAIN_UNSAFE_REMOTE_LEGACY",
              "BRAIN_REMOTE_SESSION", "BRAIN_UPLOAD_DIR"):
        monkeypatch.setenv(k, "")          # recorded, so teardown restores the original state
        monkeypatch.delenv(k)
    for k, v in base.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(SystemExit):                          # no persistent auth state
        srv_mod.app_from_env()
    monkeypatch.setenv("BRAIN_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("BRAIN_ALLOWED_REDIRECTS", "https://*.claude.ai/cb")
    with pytest.raises(SystemExit):                          # wildcard redirect
        srv_mod.app_from_env()
    monkeypatch.delenv("BRAIN_ALLOWED_REDIRECTS")
    monkeypatch.setenv("BRAIN_MCP_ADAPTER", "legacy")
    with pytest.raises(ValueError):                          # legacy without unsafe opt-in
        srv_mod.app_from_env()
    monkeypatch.delenv("BRAIN_MCP_ADAPTER")
    app = srv_mod.app_from_env()
    assert os.environ["BRAIN_REMOTE_SESSION"] == "1"
    names = [getattr(r, "path", None) for r in app.routes]
    assert names.count("/mcp") == 1


def test_unprotected_mcp_route_refuses_to_start():
    from starlette.applications import Starlette
    from starlette.routing import Route
    bare = Starlette(routes=[Route("/mcp", lambda r: None)])
    with pytest.raises(RuntimeError):
        srv_mod.assert_fail_closed(bare)


def test_remote_session_cannot_run_human_review(wiki, monkeypatch):
    s = semantic_adapter(wiki)
    prop = next(t for t in s if t.name == "brain_propose").handler({"kind": "object-note",
                         "structured_fields": {"object_id": "obj-nil", "note": "n", "why": "w"},
                         "evidence_refs": [{"ref": "rec:obj-nil", "quote": QUOTE}]})
    assert prop["ok"]
    monkeypatch.setenv("BRAIN_REMOTE_SESSION", "1")
    pid = prop["ref"][5:]
    for fn in (lambda: br.decide(wiki, pid, "accept", "owner"),
               lambda: br.promote(wiki, pid, "owner")):
        with pytest.raises(br.ReviewError):
            fn()
    assert json.loads((wiki / "_proposals/proposals.jsonl").read_text().splitlines()[-1])[
        "status"] == "new"
    src = "".join(p.read_text() for p in (REPO / "scripts/remote_mcp").glob("*.py")) + \
        "".join(p.read_text() for p in (REPO / "scripts/brain_surface").glob("*.py"))
    import re
    assert not re.search(r"^\s*(import brain_review|from brain_review)", src, re.M)
    assert "shell=True" not in src


# ------------------------------------------------------------ brain_ingest_file over HTTP

def test_remote_surface_is_exactly_seven_tools():
    assert SEMANTIC_TOOLS == ("brain_search", "brain_read", "brain_capture", "brain_ingest_file",
                              "brain_reconcile_context", "brain_propose", "brain_status")


def test_upload_requires_owner_auth_and_ingest_works_end_to_end(wiki, tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_UPLOAD_DIR", str(tmp_path / "staging"))
    monkeypatch.setenv("BRAIN_UPLOAD_MAX_BYTES", "4096")
    doc = "Minutes of the Nil meeting.\r\nمتن فارسی\r\n".encode()
    storage = MemStorage()
    with Running(tools=semantic_adapter(wiki)) as srv:
        # a session gives us an owner bearer token (same OAuth boundary as /mcp)
        run(with_session(srv.url, storage, lambda s: s.list_tools()))
        token = storage.tokens.access_token
        with httpx2.Client() as h:
            files = {"file": ("minutes.txt", doc, "text/plain")}
            page = h.get(srv.url + "/upload")
            assert page.status_code == 200 and "upl-" not in page.text
            assert h.post(srv.url + "/upload", files=files).status_code == 401      # no auth
            assert h.post(srv.url + "/upload", files=files,
                          data={"secret": "wrong-secret-value!"}).status_code == 401
            assert h.post(srv.url + "/upload", files=files,
                          headers={"Authorization": "Bearer forged"}).status_code == 401
            big = {"file": ("big.txt", b"x" * 100_000, "text/plain")}
            assert h.post(srv.url + "/upload", files=big,
                          headers={"Authorization": f"Bearer {token}"}).status_code == 413
            by_secret = h.post(srv.url + "/upload", files=files, data={"secret": SECRET})
            assert by_secret.status_code == 200 and "ingest upload_ref=upl-" in by_secret.text
            r = h.post(srv.url + "/upload", files={"file": ("minutes.txt", doc + b"v2", "text/plain")},
                       headers={"Authorization": f"Bearer {token}"})
            assert r.status_code == 200, r.text
            staged = r.json()
        assert staged["sha256"] == __import__("hashlib").sha256(doc + b"v2").hexdigest()

        async def scenario(s):
            names = [t.name for t in (await s.list_tools()).tools]
            res = await s.call_tool("brain_ingest_file", {"upload_ref": staged["upload_ref"],
                                                          "title": "Nil meeting minutes"})
            again = await s.call_tool("brain_ingest_file", {"upload_ref": staged["upload_ref"]})
            return names, text_of(res), res.is_error, text_of(again)

        names, out, is_err, again = run(with_session(srv.url, storage, scenario))
    assert names == list(SEMANTIC_TOOLS) and len(names) == 7
    assert not is_err and out["ok"] and out["commit_state"] == "committed", json.dumps(out)
    assert out["extraction"] == "complete" and out["source_ref"].startswith("rec:")
    orig = next((wiki / "_originals/remote-mcp").iterdir())
    assert orig.read_bytes() == doc + b"v2"
    assert again["error"]["code"] == "E_UPLOAD_NOT_FOUND"                     # single use


def test_unauthenticated_ingest_tool_call_rejected(wiki):
    hdr = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "brain_ingest_file", "arguments": {"upload_ref": "upl-" + "a" * 43}}}
    with Running(tools=semantic_adapter(wiki)) as srv, httpx2.Client() as h:
        assert h.post(srv.url + "/mcp", json=body, headers=hdr).status_code == 401
