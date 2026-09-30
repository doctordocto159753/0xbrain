#!/usr/bin/env python3
"""Owner-side smoke test of a deployed 0xBrain Remote MCP endpoint.

Runs the real OAuth 2.1 flow (discovery, dynamic client registration, PKCE,
owner consent with BRAIN_OWNER_SECRET, token) with the official MCP client,
then checks the public contract:

  1. tools/list is exactly the seven brain_* tools
  2. brain_status answers
  3. brain_capture stores a unique marker (persisted + commit_state)
  4. brain_search (captures, lexical) and brain_read find it
  5. optional: brain_propose with --evidence REF --quote TEXT
  6. a path-style ref is rejected
  7. optional: --ingest-file FILE uploads FILE to /upload with the owner's
     bearer token and ingests it with brain_ingest_file (exact bytes)

The registered redirect URI is Claude's callback (allowlisted by default);
the smoke client never follows it: it reads the authorization code from the
consent redirect itself. Tokens are kept in --token-file (0600) so a second
run after a server restart proves refresh-token persistence without a new
consent (--reuse-only fails instead of prompting a new consent).

    python scripts/remote_smoke.py --url https://brain.example.com [--insecure]

Needs requirements-remote.txt (mcp). BRAIN_OWNER_SECRET is read from the
environment; it is never printed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx2
from mcp.client.auth import OAuthClientProvider
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import (AuthorizationCodeResult, OAuthClientInformationFull,
                             OAuthClientMetadata, OAuthToken)

EXPECTED = ["brain_search", "brain_read", "brain_capture", "brain_ingest_file",
            "brain_reconcile_context", "brain_propose", "brain_status"]
REDIRECT = "https://claude.ai/api/mcp/auth_callback"


class FileStorage:
    def __init__(self, path: Path):
        self.path = path
        self.data = json.loads(path.read_text()) if path.is_file() else {}

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data))
        self.path.chmod(0o600)

    async def get_tokens(self):
        t = self.data.get("tokens")
        return OAuthToken.model_validate(t) if t else None

    async def set_tokens(self, tokens):
        self.data["tokens"] = tokens.model_dump(mode="json")
        self._save()

    async def get_client_info(self):
        c = self.data.get("client")
        return OAuthClientInformationFull.model_validate(c) if c else None

    async def set_client_info(self, info):
        self.data["client"] = info.model_dump(mode="json")
        self._save()


def text(result) -> dict:
    return json.loads(result.content[0].text)


async def smoke(args) -> dict:
    report: dict = {"url": args.url, "consent_performed": False}
    box: dict = {}
    secret = os.environ.get("BRAIN_OWNER_SECRET", "")

    async def redirect(auth_url: str) -> None:
        if args.reuse_only:
            raise RuntimeError("stored tokens were not accepted and --reuse-only forbids a new consent")
        if not secret:
            raise RuntimeError("BRAIN_OWNER_SECRET is not set")
        async with httpx2.AsyncClient(verify=not args.insecure) as h:
            page = await h.get(auth_url, follow_redirects=True)
            if page.status_code != 200 or "Owner secret" not in page.text:
                raise RuntimeError(f"owner consent page not reached ({page.status_code})")
            resp = await h.post(str(page.url), data={"secret": secret})
            if resp.status_code != 303:
                raise RuntimeError(f"owner consent refused ({resp.status_code})")
            loc = resp.headers["location"]
            if not loc.startswith(REDIRECT):
                raise RuntimeError("consent redirected somewhere unexpected")
            q = parse_qs(urlparse(loc).query)
            box["code"], box["state"] = q["code"][0], q["state"][0]
            report["consent_performed"] = True

    async def callback() -> AuthorizationCodeResult:
        return AuthorizationCodeResult(code=box["code"], state=box["state"])

    meta = OAuthClientMetadata(client_name="0xbrain-remote-smoke", redirect_uris=[REDIRECT],
                               grant_types=["authorization_code", "refresh_token"],
                               response_types=["code"], scope="brain")
    storage = FileStorage(Path(args.token_file))
    auth = OAuthClientProvider(args.url.rstrip("/") + "/mcp", meta, storage, redirect, callback)
    marker = "smoke-" + secrets.token_hex(6)
    async with httpx2.AsyncClient(auth=auth, follow_redirects=True, verify=not args.insecure,
                                  timeout=120) as http:
        async with streamable_http_client(args.url.rstrip("/") + "/mcp", http_client=http) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                names = [t.name for t in (await s.list_tools()).tools]
                report["tools"] = names
                report["tools_exact"] = names == EXPECTED
                st = text(await s.call_tool("brain_status", {}))
                report["status"] = {k: st.get(k) for k in ("ok", "snapshot", "degraded",
                                                           "degraded_reasons")}
                report["status_uncommitted"] = st.get("uncommitted", {}).get("count")
                if args.read_ref:
                    rd = text(await s.call_tool("brain_read", {"ref": args.read_ref}))
                    report["read_ref"] = {"ok": rd.get("ok"), "tier": rd.get("tier"),
                                          "bytes": rd.get("bytes")}
                if args.status_only:
                    return report
                cap = text(await s.call_tool("brain_capture", {
                    "text": f"Remote smoke {marker}: verbatim owner text.", "language_hint": "en"}))
                report["capture"] = {k: cap.get(k) for k in ("ok", "ref", "persisted",
                                                             "commit_state", "commit", "sha256")}
                found = text(await s.call_tool("brain_search", {"query": marker,
                                                                "scope": "captures"}))
                refs = [h.get("ref") for h in found.get("groups", {}).get("captures", {}).get(
                    "results", [])]
                report["search_found_capture"] = cap.get("ref") in refs
                read = text(await s.call_tool("brain_read", {"ref": cap.get("ref")}))
                report["read_verbatim"] = marker in json.dumps(read.get("sections", {}))
                bad = await s.call_tool("brain_read", {"ref": "../_originals/x.md"})
                report["path_ref_rejected"] = bad.is_error and text(bad)["error"]["code"] == "E_BAD_REF"
                if args.ingest_file:
                    import hashlib
                    raw = Path(args.ingest_file).read_bytes()
                    tok = (await storage.get_tokens()).access_token
                    async with httpx2.AsyncClient(verify=not args.insecure, timeout=120) as h:
                        up = await h.post(args.url.rstrip("/") + "/upload",
                                          headers={"Authorization": f"Bearer {tok}"},
                                          files={"file": (Path(args.ingest_file).name, raw)})
                    staged = up.json()
                    ing = text(await s.call_tool("brain_ingest_file", {
                        "upload_ref": staged.get("upload_ref", ""),
                        "title": "remote smoke ingest"}))
                    report["ingest"] = {k: ing.get(k) for k in (
                        "ok", "duplicate", "source_ref", "derivative_ref", "sha256", "bytes",
                        "extraction", "commit_state", "error")}
                    report["ingest"]["local_sha256"] = hashlib.sha256(raw).hexdigest()
                    if args.ingest_phrase:
                        hits = text(await s.call_tool("brain_search", {
                            "query": args.ingest_phrase, "mode": "exact", "scope": "canonical"}))
                        refs = [h.get("ref") for h in
                                hits.get("groups", {}).get("canonical", {}).get("results", [])]
                        report["ingest"]["search_found_derivative"] = ing.get("derivative_ref") in refs
                    if ing.get("derivative_ref"):
                        rd = text(await s.call_tool("brain_read", {"ref": ing["derivative_ref"]}))
                        report["ingest"]["derivative_has_sha"] = ing.get("sha256", "") in rd.get(
                            "content", "")

                    report["ingest"]["upload_status"] = up.status_code
                if args.evidence:
                    prop = text(await s.call_tool("brain_propose", {
                        "kind": "object-note",
                        "structured_fields": {"object_id": args.evidence.split(":", 1)[1],
                                              "note": f"smoke note {marker}",
                                              "why": "remote smoke qualification"},
                        "evidence_refs": [{"ref": args.evidence, "quote": args.quote},
                                          {"ref": cap.get("ref")}]}))
                    report["proposal"] = {k: prop.get(k) for k in (
                        "ok", "ref", "authority_tier", "persisted", "commit_state", "error")}
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--url", required=True, help="public origin, e.g. https://brain.example.com")
    ap.add_argument("--token-file", default=str(Path.home() / ".0xbrain-smoke-tokens.json"))
    ap.add_argument("--insecure", action="store_true", help="accept a self-signed certificate (tls internal)")
    ap.add_argument("--reuse-only", action="store_true", help="fail rather than perform a new consent")
    ap.add_argument("--status-only", action="store_true")
    ap.add_argument("--evidence", help="rec:<id> to cite in a test proposal")
    ap.add_argument("--quote", help="verbatim quote from --evidence (>= 20 chars)")
    ap.add_argument("--ingest-file", help="upload and ingest this file exactly")
    ap.add_argument("--ingest-phrase", help="exact phrase expected in the ingested file's text")
    ap.add_argument("--read-ref", help="brain_read this ref (e.g. after a restart)")
    args = ap.parse_args(argv)
    try:
        report = asyncio.run(smoke(args))
    except BaseException as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 1
    ok = report.get("tools_exact") and report["status"].get("ok")
    if not args.status_only:
        ok = ok and report["capture"].get("persisted") and report["search_found_capture"] \
            and report["read_verbatim"] and report["path_ref_rejected"]
        if args.evidence:
            ok = ok and report["proposal"].get("ok")
        if args.ingest_file:
            ing = report["ingest"]
            ok = ok and ing.get("ok") and ing.get("sha256") == ing.get("local_sha256")
            if args.ingest_phrase:
                ok = ok and ing.get("search_found_derivative")
        if args.read_ref:
            ok = ok and report["read_ref"].get("ok")
    report["ok"] = bool(ok)
    print(json.dumps(report, indent=1, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
