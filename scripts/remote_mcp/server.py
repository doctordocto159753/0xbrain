"""Remote MCP server: streamable HTTP + OAuth 2.1 (single owner).

Run:  python -m scripts.remote_mcp.server      (or python scripts/remote_mcp/server.py)

Environment (secrets only from env, never from files in the repo):
  BRAIN_PUBLIC_URL              public https origin, e.g. https://brain.example.com   (required)
  BRAIN_OWNER_SECRET            owner passphrase, >= 16 chars                          (required)
  BRAIN_STATE_DIR               persistent dir for OAuth client/refresh-token state   (required
                                unless BRAIN_EPHEMERAL_AUTH=1, which keeps it in memory: tests)
  BRAIN_ALLOWED_REDIRECTS       comma list replacing the default Claude callbacks (exact URIs)
  BRAIN_ALLOW_LOOPBACK_REDIRECTS=1   permit http://localhost redirects (dev/tests only)
  BRAIN_MCP_ADAPTER             'semantic' (default: the six brain_* tools) or 'legacy'
                                (development only; also needs BRAIN_UNSAFE_REMOTE_LEGACY=1)
  BRAIN_HOST / BRAIN_PORT       bind address (default 127.0.0.1:8787); put TLS in front
  BRAIN_FORWARDED_ALLOW_IPS     proxy addresses trusted for X-Forwarded-* (default 127.0.0.1)

The process always runs with BRAIN_REMOTE_SESSION=1, so the human review CLI
(brain_review.py) refuses to act if it is ever reached from this process.

The server exposes nothing to an unauthenticated caller except liveness
(/healthz), OAuth discovery/registration/authorize/token/revoke, and the owner
login form. /mcp answers 401 with a resource-metadata pointer.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import anyio
import mcp_types as types
from mcp.server.auth.middleware.bearer_auth import RequireAuthMiddleware
from mcp.server.auth.provider import ProviderTokenVerifier
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.lowlevel import Server
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

_HERE = Path(__file__).resolve().parent
if __package__ in (None, ""):  # executed as a script
    sys.path.insert(0, str(_HERE.parent))
    from remote_mcp.adapter import ToolSpec, filter_remote, load_adapter  # type: ignore  # noqa: E402
    from remote_mcp.owner_auth import DEFAULT_REDIRECTS, SCOPE, OwnerAuthProvider  # type: ignore  # noqa: E402
else:
    from .adapter import ToolSpec, filter_remote, load_adapter
    from .owner_auth import DEFAULT_REDIRECTS, SCOPE, OwnerAuthProvider

SERVER_NAME = "0xbrain"
SERVER_VERSION = "1.0.0-rc1"


def build_app(
    *,
    public_url: str,
    owner_secret: str,
    tools: list[ToolSpec] | None = None,
    state_dir: Path | None = None,
    allowed_redirects: tuple[str, ...] = DEFAULT_REDIRECTS,
    allow_loopback_redirects: bool = False,
) -> Starlette:
    public_url = public_url.rstrip("/")
    parsed = urlparse(public_url)
    if parsed.scheme != "https" and parsed.hostname not in ("localhost", "127.0.0.1"):
        raise ValueError("BRAIN_PUBLIC_URL must be https (loopback allowed for tests)")
    specs = {t.name: t for t in filter_remote(tools if tools is not None else load_adapter())}

    provider = OwnerAuthProvider(
        public_url=public_url,
        owner_secret=owner_secret,
        state_file=(state_dir / "oauth_state.json") if state_dir else None,
        allowed_redirects=allowed_redirects,
        allow_loopback_redirects=allow_loopback_redirects,
    )

    async def list_tools(ctx, params):
        return types.ListToolsResult(tools=[
            types.Tool(name=s.name, description=s.description, input_schema=s.input_schema)
            for s in specs.values()])

    async def call_tool(ctx, params: types.CallToolRequestParams):
        spec = specs.get(params.name)
        if spec is None:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"unknown tool: {params.name}")],
                is_error=True)
        is_error = False
        try:
            out = await anyio.to_thread.run_sync(spec.handler, dict(params.arguments or {}))
            if isinstance(out, dict):
                is_error = out.get("ok") is False
                out = json.dumps(out, ensure_ascii=False)
            text = out
        except Exception as exc:  # noqa: BLE001 - wrap, never crash, never leak a traceback
            text = json.dumps({"ok": False, "error": {"code": "E_INTERNAL",
                                                      "message": type(exc).__name__}})
            is_error = True
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)],
                                    is_error=is_error)

    server = Server(SERVER_NAME, version=SERVER_VERSION, on_list_tools=list_tools, on_call_tool=call_tool)

    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    host = parsed.netloc
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[host, "127.0.0.1:*", "localhost:*"],
        allowed_origins=[f"{parsed.scheme}://{host}", "http://127.0.0.1:*", "http://localhost:*"],
    )
    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        # Stateless: no server-side MCP session to lose on restart; every
        # request is independently authenticated. No tool needs server push.
        stateless_http=True,
        transport_security=security,
        auth=AuthSettings(
            issuer_url=public_url,
            resource_server_url=public_url + "/mcp",
            validate_token_resource=True,
            required_scopes=[SCOPE],
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]),
            revocation_options=RevocationOptions(enabled=True),
        ),
        # Without an explicit verifier the low-level app mounts /mcp UNAUTHENTICATED (fail-open).
        token_verifier=ProviderTokenVerifier(provider),
        auth_server_provider=provider,
        custom_starlette_routes=[Route("/healthz", healthz), *provider.routes()],
    )
    assert_fail_closed(app)
    app.state.auth_provider = provider      # in-process introspection for tests only
    return app


def assert_fail_closed(app: Starlette) -> None:
    """Refuse to serve if /mcp is reachable without the bearer middleware or
    if any other route could reach the MCP transport."""
    mcp_routes = [r for r in app.routes if getattr(r, "path", None) == "/mcp"]
    if len(mcp_routes) != 1 or not isinstance(mcp_routes[0].endpoint, RequireAuthMiddleware):
        raise RuntimeError("refusing to start: /mcp is not protected by the bearer middleware")
    for r in app.routes:
        if not isinstance(r, Route):
            raise RuntimeError(f"refusing to start: unexpected mount {r!r}")


def app_from_env() -> Starlette:
    env = os.environ
    for key in ("BRAIN_PUBLIC_URL", "BRAIN_OWNER_SECRET"):
        if not env.get(key):
            raise SystemExit(f"missing required environment variable {key}")
    redirects = tuple(x.strip() for x in env.get("BRAIN_ALLOWED_REDIRECTS", "").split(",") if x.strip())
    for uri in redirects:
        if "*" in uri or not uri.startswith("https://"):
            raise SystemExit(f"BRAIN_ALLOWED_REDIRECTS entries must be exact https URIs: {uri!r}")
    state = env.get("BRAIN_STATE_DIR")
    if not state and env.get("BRAIN_EPHEMERAL_AUTH") != "1":
        raise SystemExit("missing BRAIN_STATE_DIR (persistent OAuth state); set "
                         "BRAIN_EPHEMERAL_AUTH=1 only for throwaway test servers")
    os.environ["BRAIN_REMOTE_SESSION"] = "1"
    return build_app(
        public_url=env["BRAIN_PUBLIC_URL"],
        owner_secret=env["BRAIN_OWNER_SECRET"],
        state_dir=Path(state) if state else None,
        allowed_redirects=redirects or DEFAULT_REDIRECTS,
        allow_loopback_redirects=env.get("BRAIN_ALLOW_LOOPBACK_REDIRECTS") == "1",
    )


def main() -> int:
    import uvicorn

    os.environ["BRAIN_REMOTE_SESSION"] = "1"
    uvicorn.run(app_from_env(), host=os.environ.get("BRAIN_HOST", "127.0.0.1"),
                port=int(os.environ.get("BRAIN_PORT", "8787")), proxy_headers=True,
                forwarded_allow_ips=os.environ.get("BRAIN_FORWARDED_ALLOW_IPS", "127.0.0.1"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
