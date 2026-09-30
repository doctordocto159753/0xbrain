"""Remote MCP transport + auth tests using the official MCP Python client.

A real uvicorn server runs on a loopback port; the client performs the full
OAuth 2.1 flow (discovery, DCR, PKCE, owner consent, token, refresh).
Skipped when the optional remote dependency set (requirements-remote.txt) is
not installed, so the base kit baseline is unaffected.
"""
from __future__ import annotations

import asyncio
import json
import socket
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("mcp")
pytest.importorskip("uvicorn")

import httpx2  # noqa: E402
import uvicorn  # noqa: E402
from mcp.client.auth import OAuthClientProvider  # noqa: E402
from mcp.client.session import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientInformationFull, OAuthClientMetadata, OAuthToken  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "capture"))

from remote_mcp.adapter import REMOTE_DENYLIST, ToolSpec, legacy_adapter, load_adapter  # noqa: E402
from remote_mcp.server import build_app  # noqa: E402
import wiki_mcp_server as wms  # noqa: E402

SECRET = "correct horse battery staple 42"
REDIRECT = "http://localhost:9/callback"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Running:
    def __init__(self, tools=None, state_dir=None, port=None):
        self.port = port or free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        app = build_app(public_url=self.url, owner_secret=SECRET, tools=tools,
                        state_dir=state_dir, allow_loopback_redirects=True)
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port,
                                                    log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                return self
            time.sleep(0.05)
        raise RuntimeError("server did not start")

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=10)


class MemStorage:
    def __init__(self):
        self.tokens = None
        self.client = None

    async def get_tokens(self):
        return self.tokens

    async def set_tokens(self, tokens: OAuthToken):
        self.tokens = tokens

    async def get_client_info(self):
        return self.client

    async def set_client_info(self, info: OAuthClientInformationFull):
        self.client = info


def make_auth(base: str, storage: MemStorage, secret: str = SECRET):
    box: dict = {}

    async def redirect(auth_url: str) -> None:
        async with httpx2.AsyncClient() as h:
            page = await h.get(auth_url, follow_redirects=True)  # /authorize -> owner login form
            assert page.status_code == 200 and "Owner secret" in page.text
            resp = await h.post(str(page.url), data={"secret": secret})
            box["status"] = resp.status_code
            if resp.status_code == 303:
                q = parse_qs(urlparse(resp.headers["location"]).query)
                box["code"], box["state"] = q["code"][0], q["state"][0]

    async def callback() -> AuthorizationCodeResult:
        if "code" not in box:
            raise RuntimeError(f"owner consent failed: {box.get('status')}")
        return AuthorizationCodeResult(code=box["code"], state=box["state"])

    meta = OAuthClientMetadata(client_name="test-client", redirect_uris=[REDIRECT],
                               grant_types=["authorization_code", "refresh_token"],
                               response_types=["code"], scope="brain")
    return OAuthClientProvider(base + "/mcp", meta, storage, redirect, callback), box


async def with_session(base, storage, fn, secret=SECRET):
    auth, box = make_auth(base, storage, secret)
    async with httpx2.AsyncClient(auth=auth, follow_redirects=True) as http:
        async with streamable_http_client(base + "/mcp", http_client=http) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                return await fn(session)


def run(coro):
    return asyncio.run(coro)


def text_of(result) -> dict:
    return json.loads(result.content[0].text)


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(wms, "ROOT", tmp_path)
    monkeypatch.setattr(wms, "PROPOSALS_DIR", tmp_path / "_proposals")
    monkeypatch.setattr(wms, "PROPOSALS_LOG", tmp_path / "_proposals" / "proposals.jsonl")
    cap = wms._cap
    monkeypatch.setattr(cap, "CAPTURES_ROOT", tmp_path / "01-inbox" / "captures")
    monkeypatch.setattr(cap, "ORPHAN_QUARANTINE", tmp_path / "01-inbox" / "captures" / ".quarantine")
    monkeypatch.setattr(cap, "REPO_ROOT", tmp_path)
    (tmp_path / "03-objects").mkdir()
    (tmp_path / "03-objects" / "a.md").write_text("alpha\nunique-needle-77\n", encoding="utf-8")
    return tmp_path


# ------------------------------------------------------------- unauthorized
def test_unauthenticated_gets_nothing():
    with Running(tools=legacy_adapter()) as srv, httpx2.Client() as h:
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "x", "version": "0"}}}
        hdr = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        r = h.post(srv.url + "/mcp", json=init, headers=hdr)
        assert r.status_code == 401
        assert "resource_metadata" in r.headers["www-authenticate"]
        assert "tools" not in r.text
        r = h.post(srv.url + "/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                   headers={**hdr, "Authorization": "Bearer not-a-real-token"})
        assert r.status_code == 401
        assert h.get(srv.url + "/mcp", headers={"Accept": "text/event-stream"}).status_code == 401
        # only liveness is public; it leaks no tool or wiki state
        assert h.get(srv.url + "/healthz").json() == {"ok": True}


def test_discovery_documents_advertise_pkce_and_dcr():
    with Running(tools=[]) as srv, httpx2.Client() as h:
        prm = h.get(srv.url + "/.well-known/oauth-protected-resource/mcp").json()
        assert prm["resource"] == srv.url + "/mcp"
        assert prm["authorization_servers"][0].rstrip("/") == srv.url
        asm = h.get(srv.url + "/.well-known/oauth-authorization-server").json()
        assert "S256" in asm["code_challenge_methods_supported"]
        assert asm["registration_endpoint"].endswith("/register")
        assert asm["revocation_endpoint"].endswith("/revoke")


def test_dcr_rejects_foreign_redirect_uri():
    with Running(tools=[]) as srv, httpx2.Client() as h:
        r = h.post(srv.url + "/register", json={
            "client_name": "evil", "redirect_uris": ["https://evil.example/cb"],
            "grant_types": ["authorization_code"], "response_types": ["code"]})
        assert r.status_code == 400 and r.json()["error"] == "invalid_redirect_uri"
        ok = h.post(srv.url + "/register", json={
            "client_name": "claude", "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})
        assert ok.status_code == 201


def test_wrong_owner_secret_and_lockout():
    async def attempt(base, secret):
        auth, box = make_auth(base, MemStorage(), secret)
        async with httpx2.AsyncClient(auth=auth, follow_redirects=True) as http:
            with pytest.raises(BaseException):
                async with streamable_http_client(base + "/mcp", http_client=http) as (r, w):
                    async with ClientSession(r, w) as session:
                        await session.initialize()
        return box.get("status")

    with Running(tools=[]) as srv:
        statuses = [run(attempt(srv.url, "wrong-secret-value")) for _ in range(5)]
        assert statuses == [401] * 5          # wrong secret never yields a code
        assert run(attempt(srv.url, SECRET)) == 429   # window exhausted: even the right secret is refused


# ---------------------------------------------------------- authorized flow
def test_full_flow_lists_tools_without_review_tool(sandbox):
    with Running(tools=load_adapter()) as srv:
        result = run(with_session(srv.url, MemStorage(), lambda s: s.list_tools()))
    names = {t.name for t in result.tools}
    assert "wiki_exact" in names and "wiki_capture_text" in names
    assert not (names & REMOTE_DENYLIST)
    assert len(names) == 11


def test_business_logic_reused_through_remote(sandbox):
    async def scenario(s: ClientSession):
        exact = text_of(await s.call_tool("wiki_exact", {"text": "unique-needle-77"}))
        cap = text_of(await s.call_tool("wiki_capture_text", {"text": "from claude", "channel": "mcp"}))
        prop = text_of(await s.call_tool("wiki_propose", {"kind": "object-note", "body": "b"}))
        bad = text_of(await s.call_tool("wiki_capture_text", {"text": "  "}))
        unknown = await s.call_tool("does_not_exist", {})
        denied = await s.call_tool("wiki_mark_capture_reviewed", {"id": "x", "actor": "a"})
        return exact, cap, prop, bad, unknown, denied

    with Running(tools=load_adapter()) as srv:
        exact, cap, prop, bad, unknown, denied = run(with_session(srv.url, MemStorage(), scenario))
    assert [m["file"] for m in exact["matches"]] == ["03-objects/a.md"]
    assert cap["ok"] and (sandbox / "01-inbox" / "captures").exists()
    assert prop["accepted"] is True
    assert (sandbox / "_proposals" / "proposals.jsonl").read_text(encoding="utf-8").count("\n") == 1
    assert bad["code"] == "E_EMPTY"
    assert unknown.is_error and denied.is_error


def test_denylist_holds_for_any_adapter():
    rogue = [ToolSpec("wiki_mark_capture_reviewed", "x", {"type": "object"}, lambda a: "{}"),
             ToolSpec("ok_tool", "x", {"type": "object"}, lambda a: '{"ok": true}')]
    import types as _t
    mod = _t.ModuleType("rogue_adapter_mod")
    mod.make = lambda: rogue
    sys.modules["rogue_adapter_mod"] = mod
    assert [t.name for t in load_adapter("rogue_adapter_mod:make")] == ["ok_tool"]


def test_handler_exception_does_not_crash_server():
    def boom(a):
        raise RuntimeError("kaboom")
    tools = [ToolSpec("boom", "x", {"type": "object"}, boom),
             ToolSpec("fine", "x", {"type": "object"}, lambda a: '{"ok": true}')]

    async def scenario(s):
        first = text_of(await s.call_tool("boom", {}))
        second = text_of(await s.call_tool("fine", {}))
        return first, second

    with Running(tools=tools) as srv:
        first, second = run(with_session(srv.url, MemStorage(), scenario))
    assert "RuntimeError: kaboom" in first["error"] and second == {"ok": True}


def test_refresh_rotation_revocation_and_restart_persistence(tmp_path):
    port = free_port()
    storage = MemStorage()
    with Running(tools=[ToolSpec("t", "x", {"type": "object"}, lambda a: '{"ok": true}')],
                 state_dir=tmp_path, port=port) as srv:
        run(with_session(srv.url, storage, lambda s: s.list_tools()))
        first = storage.tokens
        assert first.refresh_token
        with httpx2.Client() as h:
            assert h.post(srv.url + "/mcp", headers={"Authorization": f"Bearer {first.access_token}"},
                          json={}).status_code != 401
            # rotate through the token endpoint
            r = h.post(srv.url + "/token", data={
                "grant_type": "refresh_token", "refresh_token": first.refresh_token,
                "client_id": storage.client.client_id, "client_secret": storage.client.client_secret})
            assert r.status_code == 200, r.text
            second = r.json()
            # old access token and old refresh token are dead after rotation
            assert h.post(srv.url + "/mcp", headers={"Authorization": f"Bearer {first.access_token}"},
                          json={}).status_code == 401
            again = h.post(srv.url + "/token", data={
                "grant_type": "refresh_token", "refresh_token": first.refresh_token,
                "client_id": storage.client.client_id, "client_secret": storage.client.client_secret})
            assert again.status_code == 400
    # restart with same state dir: refresh token still valid, no re-consent needed
    state_text = (tmp_path / "oauth_state.json").read_text(encoding="utf-8")
    assert second["refresh_token"] not in state_text and first.refresh_token not in state_text
    with Running(tools=[], state_dir=tmp_path, port=port) as srv2, httpx2.Client() as h:
        r = h.post(srv2.url + "/token", data={
            "grant_type": "refresh_token", "refresh_token": second["refresh_token"],
            "client_id": storage.client.client_id, "client_secret": storage.client.client_secret})
        assert r.status_code == 200, r.text
        rev = h.post(srv2.url + "/revoke", data={
            "token": r.json()["access_token"], "client_id": storage.client.client_id,
            "client_secret": storage.client.client_secret})
        assert rev.status_code == 200
        assert h.post(srv2.url + "/mcp", headers={"Authorization": f"Bearer {r.json()['access_token']}"},
                      json={}).status_code == 401
        dead = h.post(srv2.url + "/token", data={
            "grant_type": "refresh_token", "refresh_token": r.json()["refresh_token"],
            "client_id": storage.client.client_id, "client_secret": storage.client.client_secret})
        assert dead.status_code == 400


def test_owner_secret_must_be_strong():
    with pytest.raises(ValueError):
        build_app(public_url="http://127.0.0.1:1", owner_secret="short", tools=[])


def test_public_url_must_be_https_unless_loopback():
    with pytest.raises(ValueError):
        build_app(public_url="http://brain.example.com", owner_secret=SECRET, tools=[])
