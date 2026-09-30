"""Single-owner OAuth 2.1 authorization server for the remote MCP.

Built on the MCP Python SDK's auth scaffolding (metadata, DCR, /authorize,
/token with PKCE, /revoke, bearer middleware). This module supplies only the
provider protocol plus the one owner-consent step:

  authorize()  -> redirect to /owner/login?p=<pending-id>
  /owner/login -> owner types BRAIN_OWNER_SECRET; on success an authorization
                  code is minted and the browser returns to the client.

Properties: one owner, no accounts, no external IdP, no database. Secrets come
only from the environment. Access tokens live in memory (1 h). Registered
clients and refresh tokens (stored as SHA-256 hashes, 30 d, rotated) persist
in one JSON file under BRAIN_STATE_DIR so a restart does not force a
reconnect. Tokens are opaque 256-bit random strings.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import secrets
import tempfile
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

SCOPE = "brain"
CODE_TTL = 300
PENDING_TTL = 600
ACCESS_TTL = 3600
REFRESH_TTL = 30 * 24 * 3600
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW = 300
MIN_SECRET_LEN = 16
DEFAULT_REDIRECTS = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
)
LOGIN_PATH = "/owner/login"

_SEC_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
}


def _h(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _is_loopback(uri: str) -> bool:
    u = urlparse(uri)
    return u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1", "::1")


class OwnerAuthProvider:
    """Implements mcp.server.auth.provider.OAuthAuthorizationServerProvider."""

    def __init__(
        self,
        *,
        public_url: str,
        owner_secret: str,
        state_file: Path | None = None,
        allowed_redirects: tuple[str, ...] = DEFAULT_REDIRECTS,
        allow_loopback_redirects: bool = False,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if len(owner_secret) < MIN_SECRET_LEN:
            raise ValueError(f"BRAIN_OWNER_SECRET must be at least {MIN_SECRET_LEN} characters")
        self.public_url = public_url.rstrip("/")
        self._secret = owner_secret.encode("utf-8")
        self._state_file = state_file
        self._allowed = tuple(allowed_redirects)
        self._loopback = allow_loopback_redirects
        self._now = clock
        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._refresh: dict[str, dict] = {}  # sha256(token) -> record
        self._codes: dict[str, AuthorizationCode] = {}  # sha256(code) -> model
        self._access: dict[str, AccessToken] = {}  # sha256(token) -> model
        self._access_family: dict[str, str] = {}  # sha256(access) -> refresh hash
        self._pending: dict[str, tuple[OAuthClientInformationFull, AuthorizationParams, float]] = {}
        self._failures: list[float] = []
        self._load_state()

    # ---------------------------------------------------------------- state
    def _load_state(self) -> None:
        if not self._state_file or not self._state_file.is_file():
            return
        data = json.loads(self._state_file.read_text(encoding="utf-8"))
        for cid, raw in data.get("clients", {}).items():
            self._clients[cid] = OAuthClientInformationFull.model_validate(raw)
        self._refresh = dict(data.get("refresh", {}))

    def _save_state(self) -> None:
        if not self._state_file:
            return
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "clients": {cid: c.model_dump(mode="json") for cid, c in self._clients.items()},
            "refresh": self._refresh,
        }
        fd, tmp = tempfile.mkstemp(dir=str(self._state_file.parent), prefix=".tmp-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._state_file)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _redirect_allowed(self, uri: str) -> bool:
        return uri in self._allowed or (self._loopback and _is_loopback(uri))

    # -------------------------------------------------- provider: registration
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        for uri in client_info.redirect_uris or []:
            if not self._redirect_allowed(str(uri)):
                raise RegistrationError("invalid_redirect_uri", f"redirect_uri not allowed: {uri}")
        if not client_info.client_id:
            raise RegistrationError("invalid_client_metadata", "missing client_id")
        self._clients[client_info.client_id] = client_info
        self._save_state()

    # ------------------------------------------------------ provider: authorize
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if params.resource is not None and params.resource.rstrip("/") not in (
            self.public_url + "/mcp", self.public_url):
            raise AuthorizeError("invalid_target", "unknown resource")
        scopes = params.scopes or [SCOPE]
        if set(scopes) - {SCOPE}:
            raise AuthorizeError("invalid_scope", f"only scope '{SCOPE}' exists")
        now = self._now()
        self._pending = {k: v for k, v in self._pending.items() if v[2] > now}
        pid = secrets.token_urlsafe(32)
        self._pending[pid] = (client, params.model_copy(update={"scopes": scopes}), now + PENDING_TTL)
        return f"{self.public_url}{LOGIN_PATH}?p={pid}"

    # ------------------------------------------------- owner consent (HTTP form)
    def routes(self) -> list[Route]:
        return [Route(LOGIN_PATH, self._login, methods=["GET", "POST"])]

    def _page(self, body: str, status: int = 200) -> HTMLResponse:
        doc = (
            "<!doctype html><html lang=en><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>0xBrain owner sign-in</title>"
            "<style>body{font:16px system-ui;margin:2rem auto;max-width:28rem;padding:0 1rem}"
            "input{width:100%;padding:.6rem;margin:.5rem 0;box-sizing:border-box}"
            "button{padding:.6rem 1rem}</style>" + body
        )
        return HTMLResponse(doc, status_code=status, headers=_SEC_HEADERS)

    def _lockout(self) -> bool:
        cutoff = self._now() - LOGIN_WINDOW
        self._failures = [t for t in self._failures if t > cutoff]
        return len(self._failures) >= LOGIN_MAX_FAILURES

    async def _login(self, request: Request) -> Response:
        pid = request.query_params.get("p", "")
        entry = self._pending.get(pid)
        if entry is None or entry[2] <= self._now():
            self._pending.pop(pid, None)
            return self._page("<h1>Link expired</h1><p>Start the connection again from Claude.</p>", 400)
        client, params, _ = entry
        if request.method == "GET":
            return self._form(client, params, pid)
        if self._lockout():
            return self._page("<h1>Too many attempts</h1><p>Wait a few minutes.</p>", 429)
        form = await request.form()
        supplied = str(form.get("secret", "")).encode("utf-8")
        if not hmac.compare_digest(supplied, self._secret):
            self._failures.append(self._now())
            return self._form(client, params, pid, error="Wrong secret.", status=401)
        self._pending.pop(pid, None)
        code = secrets.token_urlsafe(32)
        self._codes[_h(code)] = AuthorizationCode(
            code=code,
            scopes=params.scopes or [SCOPE],
            expires_at=self._now() + CODE_TTL,
            client_id=client.client_id or "",
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource or self.public_url + "/mcp",
            subject="owner",
        )
        target = construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)
        return RedirectResponse(target, status_code=303, headers=_SEC_HEADERS)

    def _form(self, client, params, pid: str, error: str = "", status: int = 200) -> HTMLResponse:
        host = html.escape(urlparse(str(params.redirect_uri)).netloc)
        name = html.escape(client.client_name or "unnamed client")
        err = f"<p style='color:#b00'>{html.escape(error)}</p>" if error else ""
        return self._page(
            f"<h1>Authorize access to 0xBrain</h1>"
            f"<p>Client <b>{name}</b> (returns to <b>{host}</b>) asks for scope "
            f"<b>{html.escape(' '.join(params.scopes or []))}</b>.</p>{err}"
            f"<form method=post action='{LOGIN_PATH}?p={html.escape(pid)}'>"
            "<label>Owner secret<input type=password name=secret autocomplete=current-password autofocus></label>"
            "<button type=submit>Authorize</button></form>",
            status,
        )

    # ------------------------------------------------------- provider: tokens
    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self._codes.get(_h(authorization_code))
        if code is None or code.client_id != client.client_id or code.expires_at < self._now():
            return None
        return code

    def _issue(self, client_id: str, scopes: list[str], resource: str | None, subject: str | None,
               old_refresh_hash: str | None = None) -> OAuthToken:
        now = int(self._now())
        if old_refresh_hash:
            self._drop_family(old_refresh_hash)
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self._access[_h(access)] = AccessToken(
            token=access, client_id=client_id, scopes=scopes, expires_at=now + ACCESS_TTL,
            resource=resource, subject=subject)
        self._access_family[_h(access)] = _h(refresh)
        self._refresh[_h(refresh)] = {
            "client_id": client_id, "scopes": scopes, "expires_at": now + REFRESH_TTL,
            "resource": resource, "subject": subject}
        self._save_state()
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=ACCESS_TTL,
                          scope=" ".join(scopes), refresh_token=refresh)

    def _drop_family(self, refresh_hash: str) -> None:
        self._refresh.pop(refresh_hash, None)
        for ah in [a for a, r in self._access_family.items() if r == refresh_hash]:
            self._access_family.pop(ah, None)
            self._access.pop(ah, None)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if self._codes.pop(_h(authorization_code.code), None) is None:
            raise TokenError("invalid_grant", "authorization code already used")
        return self._issue(authorization_code.client_id, authorization_code.scopes,
                           authorization_code.resource, authorization_code.subject)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        rec = self._refresh.get(_h(refresh_token))
        if rec is None or rec["client_id"] != client.client_id or rec["expires_at"] < self._now():
            return None
        return RefreshToken(token=refresh_token, **rec)

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        granted = scopes or refresh_token.scopes
        if set(granted) - set(refresh_token.scopes):
            raise TokenError("invalid_scope", "cannot widen scope on refresh")
        return self._issue(refresh_token.client_id, granted, refresh_token.resource,
                           refresh_token.subject, old_refresh_hash=_h(refresh_token.token))

    async def load_access_token(self, token: str) -> AccessToken | None:
        rec = self._access.get(_h(token))
        if rec is None:
            return None
        if rec.expires_at is not None and rec.expires_at < self._now():
            self._access.pop(_h(token), None)
            return None
        return rec

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        th = _h(token.token)
        family = self._access_family.get(th) if isinstance(token, AccessToken) else th
        if family:
            self._drop_family(family)
            self._save_state()
