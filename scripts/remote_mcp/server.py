"""Remote MCP server: streamable HTTP + OAuth 2.1 (single owner).

Run:  python -m scripts.remote_mcp.server      (or python scripts/remote_mcp/server.py)

Environment (secrets only from env, never from files in the repo):
  BRAIN_PUBLIC_URL              public https origin, e.g. https://brain.example.com   (required)
  BRAIN_OWNER_SECRET            owner passphrase, >= 16 chars                          (required)
  BRAIN_STATE_DIR               dir for client/refresh-token state (default: in-memory only)
  BRAIN_ALLOWED_REDIRECTS       comma list replacing the default Claude callbacks
  BRAIN_ALLOW_LOOPBACK_REDIRECTS=1   permit http://localhost redirects (dev/tests only)
  BRAIN_MCP_ADAPTER             'package.module:callable' tool surface (default: legacy wiki_* tools)
  BRAIN_HOST / BRAIN_PORT       bind address (default 127.0.0.1:8787); put TLS in front

The server exposes nothing to an unauthenticated caller except liveness
(/healthz), OAuth discovery/registration/authorize/token/revoke, and the owner
login form. /mcp answers 401 with a resource-metadata pointer.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import anyio
import mcp_types as types
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
    from remote_mcp.adapter import ToolSpec, load_adapter  # type: ignore  # noqa: E402
    from remote_mcp.owner_auth import DEFAULT_REDIRECTS, SCOPE, OwnerAuthProvider  # type: ignore  # noqa: E402
else:
    from .adapter import ToolSpec, load_adapter
    from .owner_auth import DEFAULT_REDIRECTS, SCOPE, OwnerAuthProvider

SERVER_NAME = "0xbrain"
SERVER_VERSION = "0.1.0"


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
    specs = {t.name: t for t in (tools if tools is not None else load_adapter())}

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
        try:
            text = await anyio.to_thread.run_sync(spec.handler, dict(params.arguments or {}))
        except Exception as exc:  # noqa: BLE001 - mirror stdio behavior: wrap, never crash
            text = f'{{"error": "{type(exc).__name__}: {exc}"}}'.replace("\n", " ")
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)])

    server = Server(SERVER_NAME, version=SERVER_VERSION, on_list_tools=list_tools, on_call_tool=call_tool)

    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    host = parsed.netloc
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[host, "127.0.0.1:*", "localhost:*"],
        allowed_origins=[f"{parsed.scheme}://{host}", "http://127.0.0.1:*", "http://localhost:*"],
    )
    return server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=False,
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


def app_from_env() -> Starlette:
    env = os.environ
    for key in ("BRAIN_PUBLIC_URL", "BRAIN_OWNER_SECRET"):
        if not env.get(key):
            raise SystemExit(f"missing required environment variable {key}")
    redirects = tuple(x.strip() for x in env.get("BRAIN_ALLOWED_REDIRECTS", "").split(",") if x.strip())
    state = env.get("BRAIN_STATE_DIR")
    return build_app(
        public_url=env["BRAIN_PUBLIC_URL"],
        owner_secret=env["BRAIN_OWNER_SECRET"],
        state_dir=Path(state) if state else None,
        allowed_redirects=redirects or DEFAULT_REDIRECTS,
        allow_loopback_redirects=env.get("BRAIN_ALLOW_LOOPBACK_REDIRECTS") == "1",
    )


def main() -> int:
    import uvicorn

    uvicorn.run(app_from_env(), host=os.environ.get("BRAIN_HOST", "127.0.0.1"),
                port=int(os.environ.get("BRAIN_PORT", "8787")), proxy_headers=True,
                forwarded_allow_ips=os.environ.get("BRAIN_FORWARDED_ALLOW_IPS", "127.0.0.1"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
