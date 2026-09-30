"""Owner-authenticated file upload that feeds brain_ingest_file.

    GET  /upload   minimal HTML form (owner secret + one file; no JavaScript)
    POST /upload   multipart/form-data, field `file`; authenticated by EITHER
                   a bearer token valid for this server's /mcp resource with
                   scope `brain`, OR the owner secret in form field `secret`
                   (same lockout window as the OAuth consent page)

Returns an `upload_ref` (single use, short-lived; see
scripts/brain_surface/uploads.py) that the owner gives to Claude:
"ingest upload_ref=upl-...". The bytes stored are exactly the bytes sent.
No URL fetching, no server-path selection, no public listing.
"""
from __future__ import annotations

import html
import sys
from pathlib import Path

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

_SCRIPTS = str(Path(__file__).resolve().parents[1])
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from brain_surface import uploads  # noqa: E402

UPLOAD_PATH = "/upload"
MULTIPART_OVERHEAD = 64 * 1024
_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
                               "form-action 'self'; frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
}


def _page(body: str, status: int = 200) -> HTMLResponse:
    doc = ("<!doctype html><html lang=en><meta charset=utf-8>"
           "<meta name=viewport content='width=device-width,initial-scale=1'>"
           "<title>0xBrain upload</title>"
           "<style>body{font:16px system-ui;margin:2rem auto;max-width:34rem;padding:0 1rem}"
           "input{width:100%;padding:.6rem;margin:.5rem 0;box-sizing:border-box}"
           "code{word-break:break-all;background:#eee;padding:.2rem}button{padding:.6rem 1rem}"
           "</style>" + body)
    return HTMLResponse(doc, status_code=status, headers=_HEADERS)


def _form(error: str = "", status: int = 200) -> HTMLResponse:
    err = f"<p style='color:#b00'>{html.escape(error)}</p>" if error else ""
    limit = uploads.max_bytes() // (1024 * 1024)
    return _page(
        "<h1>Upload a document for exact ingestion</h1>"
        "<p>The file is stored privately for a short time. Give the returned "
        "<code>upload_ref</code> to Claude and ask it to ingest the file; the archive "
        f"keeps the exact bytes. Types: pdf, docx, pptx, xlsx, html, epub, txt, md; "
        f"at most {limit} MiB.</p>{err}"
        f"<form method=post action='{UPLOAD_PATH}' enctype='multipart/form-data'>"
        "<label>File<input type=file name=file required></label>"
        "<label>Owner secret<input type=password name=secret "
        "autocomplete=current-password></label>"
        "<button type=submit>Upload</button></form>", status)


def routes(provider, public_url: str) -> list[Route]:
    resource = public_url.rstrip("/") + "/mcp"

    async def bearer_ok(request: Request) -> bool | None:
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return None
        tok = await provider.load_access_token(auth[7:].strip())
        return bool(tok and "brain" in (tok.scopes or [])
                    and str(tok.resource or "").rstrip("/") == resource)

    def reply(request: Request, payload: dict, status: int, as_json: bool) -> Response:
        if as_json:
            return JSONResponse(payload, status_code=status, headers=_HEADERS)
        if status != 200:
            return _form(payload.get("error", {}).get("message", "upload failed"), status)
        return _page(
            "<h1>Uploaded</h1><p>Tell Claude:</p>"
            f"<p><code>ingest upload_ref={html.escape(payload['upload_ref'])}</code></p>"
            f"<p>{html.escape(payload['filename'])}: {payload['bytes']} bytes, SHA-256 "
            f"<code>{payload['sha256']}</code>. The reference works once and expires in "
            f"{payload['expires_in'] // 60} minutes.</p>")

    async def upload(request: Request) -> Response:
        if request.method == "GET":
            return _form()
        bearer = await bearer_ok(request)
        as_json = bearer is not None or "application/json" in request.headers.get("accept", "")
        if bearer is False:
            return reply(request, {"ok": False, "error": {"code": "E_UNAUTHORIZED",
                                                          "message": "invalid token"}}, 401, True)
        if uploads.upload_dir() is None:
            return reply(request, {"ok": False, "error": {
                "code": "E_UNAVAILABLE", "message": "upload staging not configured"}}, 503, as_json)
        try:
            length = int(request.headers.get("content-length", ""))
        except ValueError:
            return reply(request, {"ok": False, "error": {
                "code": "E_LENGTH_REQUIRED", "message": "Content-Length required"}}, 411, as_json)
        if length > uploads.max_bytes() + MULTIPART_OVERHEAD:
            return reply(request, {"ok": False, "error": {
                "code": "E_TOO_LARGE", "message": f"file exceeds {uploads.max_bytes()} bytes"}},
                413, as_json)
        if "multipart/form-data" not in request.headers.get("content-type", ""):
            return reply(request, {"ok": False, "error": {
                "code": "E_BAD_REQUEST", "message": "send multipart/form-data with field 'file'"}},
                400, as_json)
        form = await request.form(max_files=1, max_fields=4)
        try:
            if not bearer:
                verdict = provider.check_owner_secret(str(form.get("secret", "")))
                if verdict != "ok":
                    code, status = (("E_LOCKED", 429) if verdict == "locked"
                                    else ("E_UNAUTHORIZED", 401))
                    return reply(request, {"ok": False, "error": {
                        "code": code, "message": "too many attempts" if status == 429
                        else "wrong owner secret"}}, status, as_json)
            f = form.get("file")
            if f is None or not hasattr(f, "read") or not getattr(f, "filename", None):
                return reply(request, {"ok": False, "error": {
                    "code": "E_BAD_REQUEST", "message": "missing file"}}, 400, as_json)
            try:
                staged = uploads.stage(f.file, f.filename, f.content_type)
            except uploads.UploadError as exc:
                status = {"E_TOO_LARGE": 413, "E_TOO_MANY": 429}.get(exc.code, 400)
                return reply(request, {"ok": False, "error": {"code": exc.code,
                                                              "message": exc.message}},
                             status, as_json)
            return reply(request, {"ok": True, **staged}, 200, as_json)
        finally:
            await form.close()

    return [Route(UPLOAD_PATH, upload, methods=["GET", "POST"])]
