#!/usr/bin/env python3
"""Real Docker rehearsal of the R1 enrichment upgrade (see README.md next to this file).

pre-R1 deployment (base SHA) -> real inputs over OAuth/MCP -> preflight (read-only proof) ->
backup + scratch restore -> plan -> copy rehearsal -> live apply -> verify off -> features on ->
extension E2E over HTTP -> backup with a live store -> post-upgrade inputs -> rollback 1 -> 2 -> 3.
Every step is asserted; results go to <work>/out/summary.json. Throwaway environments only.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

CHECKS: list[dict] = []


def check(name: str, ok: bool, detail="") -> bool:
    CHECKS.append({"check": name, "result": "PASS" if ok else "FAIL", "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not ok else ""), flush=True)
    return ok


def sh(cmd, *, cwd=None, env=None, log=None, check_rc=True, inp=None):
    p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, input=inp)
    if log:
        Path(log).write_text(f"$ {' '.join(map(str, cmd))}\nrc={p.returncode}\n--- stdout\n{p.stdout}\n--- stderr\n{p.stderr}")
    if check_rc and p.returncode != 0:
        raise SystemExit(f"command failed rc={p.returncode}: {' '.join(map(str, cmd))[:300]}\n{p.stderr[-2000:]}")
    return p


def manifest(*roots) -> dict:
    """sha256 + mode + owner of EVERY file (tracked, untracked, ignored, .git) under roots."""
    out = {}
    for root in roots:
        files = {}
        for dp, _dn, fns in os.walk(root):
            for f in fns:
                p = Path(dp) / f
                rel = p.relative_to(root).as_posix()
                if p.is_symlink():
                    files[rel] = "link:" + os.readlink(p)
                    continue
                st = p.lstat()
                files[rel] = f"{hashlib.sha256(p.read_bytes()).hexdigest()}:{oct(st.st_mode & 0o7777)}:{st.st_uid}:{st.st_gid}"
        out[str(root)] = files
    return out


def changed(a: dict, b: dict, ignore=()) -> list[str]:
    out = []
    for root in set(a) | set(b):
        x, y = a.get(root, {}), b.get(root, {})
        out += [f"{Path(root).name}/{k}" for k in sorted(set(x) | set(y)) if x.get(k) != y.get(k) and not k.startswith(ignore)]
    return out


def h(v):
    return v.split(":")[0] if v else None


def inspect(*names) -> dict:
    out = {}
    for n in names:
        p = sh(["docker", "inspect", "-f", "{{.Id}} {{.State.StartedAt}} {{.Image}} {{json .HostConfig.PortBindings}}", n],
               check_rc=False)
        out[n] = p.stdout.strip()
    return out


def token_of(text: str) -> str | None:
    m = re.findall(r"CONFIRM-[A-Z0-9]+", text)
    return m[-1] if m else None


# ------------------------------------------------------------------ MCP client (official SDK, refresh only)
async def mcp_session(url, token_file, fn, *, smoke_dir):
    sys.path.insert(0, str(smoke_dir))
    import httpx2
    from mcp.client.auth import OAuthClientProvider
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared.auth import OAuthClientMetadata
    from remote_smoke import REDIRECT, FileStorage

    async def no(*_):
        raise RuntimeError("stored tokens were not accepted (this client never performs a new consent)")

    meta = OAuthClientMetadata(client_name="0xbrain-remote-smoke", redirect_uris=[REDIRECT],
                               grant_types=["authorization_code", "refresh_token"], response_types=["code"], scope="brain")
    auth = OAuthClientProvider(url + "/mcp", meta, FileStorage(Path(token_file)), no, no)
    async with httpx2.AsyncClient(auth=auth, follow_redirects=True, timeout=180) as http:
        async with streamable_http_client(url + "/mcp", http_client=http) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()

                async def call(name, args):
                    res = await s.call_tool(name, args)
                    return res.is_error, (json.loads(res.content[0].text) if res.content else {})

                async def tools():
                    return sorted(t.name for t in (await s.list_tools()).tools)
                return await fn(call, tools)


# ------------------------------------------------------------------ steps
class Rig:
    def __init__(self, a):
        self.a = a
        self.work = Path(a.work).resolve()
        self.wiki, self.state, self.out = self.work / "wiki", self.work / "state", self.work / "out"
        self.url = f"http://127.0.0.1:{a.port}"
        self.py = sys.executable
        self.release = Path(a.release_repo).resolve()
        self.tool = [self.py, str(self.release / "scripts/upgrade-enrichment.py")]
        self.backup_tool = [self.py, str(self.release / "scripts/enrichment-backup.py")]
        self.smoke = self.release / "scripts/remote_smoke.py"
        self.tokens = self.out / "tokens.json"
        self.n = 0

    def log(self, name):
        self.n += 1
        return self.out / f"{self.n:02d}-{name}.log"

    def dc(self, *args, **kw):
        return sh(["docker", "compose", "-p", self.a.project, "--project-directory", str(self.wiki), *args], cwd=self.wiki, **kw)

    def mcp(self, fn):
        return asyncio.run(mcp_session(self.url, self.tokens, fn, smoke_dir=self.release / "scripts"))

    def secret(self) -> str:
        return next(ln.split("=", 1)[1] for ln in (self.wiki / ".env").read_text().splitlines() if ln.startswith("BRAIN_OWNER_SECRET="))

    def wait_healthy(self, timeout=240):
        t0 = time.time()
        while time.time() - t0 < timeout:
            st = sh(["docker", "inspect", "-f", "{{.State.Health.Status}}", f"{self.a.project}-brain-1"], check_rc=False).stdout.strip()
            if st == "healthy":
                return True
            time.sleep(2)
        return False

    # -- 0. teardown
    def teardown(self):
        sh(["docker", "compose", "-p", self.a.project, "down", "--remove-orphans"], check_rc=False)
        sh(["docker", "rm", "-f", "music-other"], check_rc=False)

    # -- 1. pre-R1 deployment
    def setup(self):
        a = self.a
        if self.work.exists():
            shutil.rmtree(self.work)
        self.out.mkdir(parents=True)
        os.chmod(self.out, 0o700)
        sh(["id", "brainrh"], check_rc=False).returncode == 0 or sh(
            ["useradd", "--system", "--uid", str(a.uid), "--user-group", "--home-dir", str(self.state / "home"),
             "--shell", "/usr/sbin/nologin", "brainrh"])
        sh(["git", "clone", "-q", "--no-hardlinks", str(self.release), str(self.wiki)])
        sh(["git", "-C", str(self.wiki), "checkout", "-q", "-B", "main", a.base_sha])
        sh(["git", "-C", str(self.wiki), "remote", "set-url", "origin", "https://example.invalid/0xbrain.git"])
        for d in ("qmd", "auth", "home", "caddy/data", "caddy/config", "backups"):
            (self.state / d).mkdir(parents=True, exist_ok=True)
        env = {"BRAIN_DOMAIN": "rehearsal.invalid", "BRAIN_OWNER_EMAIL": "owner@rehearsal.invalid", "BRAIN_WIKI_NAME": "Rehearsal",
               "BRAIN_PREFIX": "rh", "BRAIN_DATA_DIR": str(self.wiki), "BRAIN_STATE_DIR": str(self.state), "BRAIN_UID": str(a.uid),
               "BRAIN_GID": str(a.uid), "BRAIN_HTTP_PORT": "80", "BRAIN_HTTPS_PORT": "443", "BRAIN_PUBLIC_URL": self.url,
               "BRAIN_TLS_DIRECTIVE": "", "BRAIN_PYTHON_BASE": a.python_base, "BRAIN_IMAGE": a.image,
               "BRAIN_OWNER_SECRET": secrets.token_hex(24)}
        if a.build_proxy:
            env["BRAIN_BUILD_NETWORK"] = "host"
        (self.wiki / ".env").write_text("".join(f"{k}={v}\n" for k, v in env.items()))
        (self.wiki / "compose.override.yaml").write_text(
            "# instance override: brain on loopback for a host proxy; bundled caddy unused\n"
            f"services:\n  brain:\n    ports: [\"127.0.0.1:{a.port}:8080\"]\n  caddy:\n    profiles: [\"disabled\"]\n")
        for p in (self.wiki, self.state / "qmd", self.state / "auth", self.state / "home", self.state / "backups"):
            sh(["chown", "-R", f"{a.uid}:{a.uid}", str(p)])        # rehearsal bootstrap only (install.sh equivalent)
        os.chmod(self.state / "auth", 0o700)
        os.chmod(self.wiki / ".env", 0o600)
        if sh(["docker", "image", "inspect", a.image], check_rc=False).returncode != 0:
            extra = ["--build-arg", f"HTTPS_PROXY={a.build_proxy}", "--build-arg", f"https_proxy={a.build_proxy}"] if a.build_proxy else []
            benv = dict(os.environ, **({"BRAIN_BUILD_CA": a.build_ca} if a.build_ca else {}))
            self.dc("build", *extra, "brain", env=benv, log=self.log("build"))

        def run(*cmd):
            return self.dc("run", "--rm", "--no-deps", "-T", "brain", *cmd)
        run("python", "scripts/instantiate.py", "--name", "Rehearsal", "--prefix", "rh")
        run("git", "config", "core.hooksPath", ".githooks")
        run("git", "add", "-A", "--", ".", ":!compose.override.yaml")
        run("git", "commit", "-q", "-m", "bootstrap: instantiate rh (Rehearsal)")
        run("bash", "scripts/configure-search.sh")
        run("bash", "scripts/refresh-search.sh")
        self.dc("up", "-d", "brain")
        check("base deployment healthy", self.wait_healthy())
        sh(["docker", "run", "-d", "--name", "music-other", "--label", "com.docker.compose.project=music",
            "-p", f"127.0.0.1:{a.port + 1}:8000", a.other_image, "python", "-m", "http.server", "8000"])

    # -- 2. real inputs before the upgrade
    def populate(self):
        (self.work / "inputs").mkdir(exist_ok=True)
        letter = self.work / "inputs/letter.txt"
        letter.write_text("Letter about the Kolan ferry schedule.\nنامهٔ کلان دربارهٔ برنامهٔ کشتی و پروژه نیل.\n")
        p = sh([self.py, str(self.smoke), "--url", self.url, "--token-file", str(self.tokens), "--ingest-file", str(letter),
                "--ingest-phrase", "Kolan ferry schedule"], env=dict(os.environ, BRAIN_OWNER_SECRET=self.secret()),
               log=self.log("smoke-base"), check_rc=False)
        rep = json.loads(p.stdout)
        check("base: OAuth consent, 7 tools, capture + ingest", rep.get("ok") is True and rep.get("tools_exact") is True)
        self.ingested, self.ingested_doc = rep["ingest"]["source_ref"], rep["ingest"]["derivative_ref"]

        async def fn(call, tools):
            refs = []
            for text, lang in (("فیت پلن: هر روز ۳۰ دقیقه پیاده‌روی و دو بار در هفته تمرین قدرتی انجام بدهم.", "fa"),
                               ("line one\r\nخط دوم با نیم‌فاصله\r\n\r\nparagraph after blank\r\n", "fa"),
                               ("Same words twice, two separate captures.", "en"),
                               ("Same words twice, two separate captures.", "en")):
                err, body = await call("brain_capture", {"text": text, "language_hint": lang})
                refs.append(body.get("ref") if not err and body.get("persisted") else None)
            return refs
        self.caps = self.mcp(fn)
        check("base: 4 more captures committed (Persian, CRLF, two identical texts)", all(self.caps) and len(set(self.caps)) == 4)
        # an uncommitted capture and an unregistered proposal line (a K9 failure leaves both behind)
        self.dc("run", "--rm", "--no-deps", "-T", "brain", "python", "-c",
                "import sys; sys.path.insert(0,'scripts/capture'); import wiki_capture as wc; "
                "wc.capture_text('یادداشت ثبت‌نشده دربارهٔ فیت پلن', 'filesystem', 'fa')")
        with open(self.wiki / "_proposals/proposals.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"id": "prop-dirty-1", "received": "2026-10-10T00:00:00Z", "kind": "note",
                                 "authority_tier": "candidate", "status": "new", "body": "unregistered"}) + "\n")

    # -- 3..6
    def upgrade(self):
        a, W = self.a, str(self.wiki)
        rel = ["--release-repo", str(self.release), "--target-sha", a.target_sha, "--base-sha", a.base_sha]
        m0, c0 = manifest(self.wiki, self.state), inspect(f"{a.project}-brain-1", "music-other")
        p = sh([*self.tool, "preflight", "--wiki", W, *rel, "--out", str(self.out / "preflight.json"),
                "--token-file", str(self.tokens)], log=self.log("preflight"), check_rc=False)
        check("preflight: no STOP", p.returncode == 0 and "STOP" not in p.stdout.split("\n")[0], p.stdout[:400])
        check("preflight: read-only (every file incl. .git/ignored/untracked, modes, owners)",
              not changed(m0, manifest(self.wiki, self.state)))
        check("preflight: containers not restarted", inspect(f"{a.project}-brain-1", "music-other") == c0)
        check("preflight: real tools/list = 7", "[PASS] tools" in p.stdout)

        p = sh([*self.tool, "backup", "--wiki", W, "--dest", str(self.work / "backups")], log=self.log("backup-dry"))
        p = sh([*self.tool, "backup", "--wiki", W, "--dest", str(self.work / "backups"), "--execute", "--confirm",
                token_of(p.stdout)], log=self.log("backup"), check_rc=False)
        check("backup: written, paused writer, verified by scratch restore",
              p.returncode == 0 and '"ok": true' in p.stdout and '"paused": true' in p.stdout, p.stdout[-300:])
        self.archive = sorted((self.work / "backups").glob("*.tar.gz"))[-1]
        self.restore_compare(self.archive, "pre-upgrade")

        m2 = manifest(self.wiki, self.state)
        (self.out / "m-pre-apply.json").write_text(json.dumps(m2[str(self.wiki)]))
        p = sh([*self.tool, "plan", "--wiki", W, *rel, "--out", str(self.out / "plan.json")], log=self.log("plan"), check_rc=False)
        check("plan: no STOP, image unchanged, features stay off", p.returncode == 0 and "unchanged" in p.stdout)
        self.apply_token = token_of(p.stdout)
        plan = json.loads((self.out / "plan.json").read_text())
        self.release_paths = {e["path"] for e in plan["paths"]}
        p = sh([*self.tool, "apply", "--wiki", W, "--plan", str(self.out / "plan.json"), "--copy-dir", str(self.work / "copy")],
               log=self.log("apply-copy"), check_rc=False)
        check("apply on a copy", p.returncode == 0 and "COPY-FIRST OK" in p.stdout)
        rb = [*self.tool, "rollback", "--wiki", W, "--level", "2", "--plan", str(self.out / "plan.json"),
              "--work-dir", str(self.work / "copy.work"), "--copy-dir", str(self.work / "copy")]
        p = sh(rb, log=self.log("rollback-copy-dry"))
        p = sh([*rb, "--execute", "--confirm", token_of(p.stdout)], log=self.log("rollback-copy"), check_rc=False)
        check("rollback level 2 on the copy", p.returncode == 0 and "ROLLBACK OK" in p.stdout)
        check("copy rehearsal left the live tree untouched", not changed(m2, manifest(self.wiki, self.state)))

        c1 = inspect(f"{a.project}-brain-1", "music-other")
        p = sh([*self.tool, "apply", "--wiki", W, "--plan", str(self.out / "plan.json"), "--work-dir", str(self.work / "work"),
                "--execute", "--confirm", self.apply_token], log=self.log("apply-live"), check_rc=False)
        c2 = inspect(f"{a.project}-brain-1", "music-other")
        check("apply live: healthy", p.returncode == 0 and "APPLY OK" in p.stdout, p.stdout[-300:])
        b1, b2 = c1[f"{a.project}-brain-1"].split(), c2[f"{a.project}-brain-1"].split()
        check("apply live: only brain recreated, same image and port binding",
              b1[0] != b2[0] and b1[2:] == b2[2:] and c1["music-other"] == c2["music-other"])
        m4 = manifest(self.wiki)
        moved = {k.split("/", 1)[1] for k in changed(m2, m4) if k.startswith("wiki/")}
        check("apply live: exactly the plan's paths changed", moved == self.release_paths, sorted(moved ^ self.release_paths)[:10])
        p = sh([self.py, str(self.smoke), "--url", self.url, "--token-file", str(self.tokens), "--reuse-only", "--status-only"],
               log=self.log("oauth-refresh-after-apply"), check_rc=False)
        check("OAuth refresh after apply, no new consent", '"consent_performed": false' in p.stdout and '"tools_exact": true' in p.stdout)
        p = sh([*self.tool, "verify", "--wiki", W, "--baseline", str(self.out / "preflight.json"), "--applied",
                str(self.work / "work/applied.json"), "--out", str(self.out / "verify-off.json"), "--token-file", str(self.tokens),
                "--expect-features", "off"], log=self.log("verify-off"), check_rc=False)
        check("verify off: rc 0, 7 tools, auth/config/sources/no-touch PASS",
              p.returncode == 0 and "[FAIL]" not in p.stdout and "tools/list = 7" in p.stdout)

    def restore_compare(self, archive, label, store_rows=None):
        tgt, st = self.work / f"restore-{label}", self.work / f"restore-{label}-state"
        base = [*self.backup_tool, "restore", "--archive", str(archive), "--target", str(tgt), "--state-target", str(st),
                "--live-wiki", str(self.wiki), "--preserve-owner"]
        p = sh(base, log=self.log(f"restore-{label}-dry"))
        p = sh([*base, "--execute", "--confirm", token_of(p.stdout)], log=self.log(f"restore-{label}"), check_rc=False)
        live = manifest(self.wiki)[str(self.wiki)]
        rest = manifest(tgt)[str(tgt)]
        skip = ("_search/", "_enrichment/")
        miss = [k for k in live if not k.startswith(skip) and h(live[k]) != h(rest.get(k))]
        check(f"restore {label}: every wiki file byte-identical (.git, dirty, .env, override)", p.returncode == 0 and not miss, miss[:5])
        auth = self.state / "auth/oauth_state.json"
        check(f"restore {label}: auth state identical",
              hashlib.sha256(auth.read_bytes()).digest() == hashlib.sha256((st / "auth/oauth_state.json").read_bytes()).digest())
        check(f"restore {label}: no raw SQLite WAL/SHM archived",
              not [k for k in rest if k.endswith(("-wal", "-shm"))])
        if store_rows is not None:
            con = sqlite3.connect(tgt / "_enrichment/store-v1.sqlite3")
            ok = con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            rows = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in store_rows}
            con.close()
            check(f"restore {label}: store snapshot integrity ok and row counts equal the live store",
                  ok and rows == store_rows, f"{rows} vs {store_rows}")

    def features_on(self):
        W = str(self.wiki)
        p = sh([*self.tool, "rollback", "--wiki", W, "--level", "1", "--plan", str(self.out / "plan.json"),
                "--work-dir", str(self.work / "work")], log=self.log("print-up-command"))
        up = next(ln.split("then:", 1)[1].split() for ln in p.stdout.splitlines() if "then:" in ln)
        with open(self.wiki / ".env", "a") as fh:
            fh.write("BRAIN_ENRICHMENT=on\nBRAIN_ENRICHMENT_WRITES=1\nBRAIN_ENRICHMENT_RETRIEVAL=1\n")
        other = inspect("music-other")
        sh(up, cwd=self.wiki, log=self.log("recreate-brain-on"))
        check("features on: brain healthy, witness untouched", self.wait_healthy() and inspect("music-other") == other)
        p = sh([*self.tool, "verify", "--wiki", W, "--baseline", str(self.out / "preflight.json"), "--applied",
                str(self.work / "work/applied.json"), "--out", str(self.out / "verify-on.json"), "--token-file", str(self.tokens),
                "--expect-features", "on"], log=self.log("verify-on"), check_rc=False)
        check("verify on: 11 tools, no FAIL", "tools/list = 11" in p.stdout and "[FAIL]" not in p.stdout)
        fit = self.caps[0]

        async def fn(call, tools):
            r = {"tools": await tools()}
            item, cur = None, None
            for _ in range(20):
                _, pg = await call("brain_enrichment_pending", {"limit": 5, **({"cursor": cur} if cur else {})})
                item = next((i for i in pg.get("items", []) if i["source_ref"] == fit), None)
                if item:
                    break
                cur = pg.get("next_cursor")
            ch = item["page"]["chunks"][0]
            sub = {"source_ref": fit, "source_version": item["source_version"],
                   "expected_annotation_revision": item["expected_annotation_revision"], "idempotency_key": "rehearsal-key-0001",
                   "annotation": {"summary": "برنامهٔ ورزشی", "tags": ["fitness", "تناسب‌اندام"],
                                  "entities": [{"name": "فیت پلن", "kind": "project",
                                                "mention": {"chunk_ref": ch["chunk_ref"], "text": "فیت پلن"}}]}}
            r["submit"] = await call("brain_enrichment_submit", sub)
            r["replay"] = await call("brain_enrichment_submit", sub)
            r["forged"] = await call("brain_enrichment_submit", {**sub, "idempotency_key": "rehearsal-key-0002",
                                     "expected_annotation_revision": 1, "annotation": {"links": [{"relation": "about",
                                     "target": {"source_ref": fit}, "evidence": [{"chunk_ref": ch["chunk_ref"], "quote": "never written here"}]}]}})
            r["actor"] = await call("brain_enrichment_submit", {**sub, "actor": "owner"})
            r["retrieve"] = await call("brain_retrieve", {"query": "فیت پلن"})
            r["tag"] = await call("brain_retrieve", {"query": "تناسب‌اندام"})
            r["derivative"] = await call("brain_retrieve", {"query": "Kolan ferry schedule"})
            r["chunk"] = await call("brain_artifact_read", {"source_ref": fit, "chunk_ref": ch["chunk_ref"]})
            r["base_read"] = await call("brain_read", {"ref": fit})
            r["chk_in_base"] = await call("brain_read", {"ref": ch["chunk_ref"]})
            r["ch"] = ch
            return r
        r = self.mcp(fn)
        (self.out / "e2e-on.json").write_text(json.dumps(r, ensure_ascii=False, indent=1))
        hits = lambda body: [h for g in body.get("groups", {}).values() for h in g["results"]]  # noqa: E731
        check("on: 11 tools over HTTP", len(r["tools"]) == 11)
        check("on: submit persisted, replay idempotent", r["submit"][1].get("persisted") is True
              and r["replay"][1].get("idempotent_replay") is True)
        check("on: forged quote and caller actor rejected, nothing persisted",
              r["forged"][1]["error"]["code"] == "E_VALIDATION" and r["forged"][1].get("persisted") is False
              and r["actor"][1]["error"]["code"] == "E_UNKNOWN_ARGUMENT")
        ret = r["retrieve"][1]
        check("on: 'فیت پلن' found without a ref, exact chunk text, no model call",
              ret.get("query_model_calls") == 0 and any(x["source_ref"] == fit and x["chunk_ref"] and "فیت پلن" in x["text"] for x in hits(ret)))
        check("on: tag-only query reaches the capture through candidate metadata",
              any(x["source_ref"] == fit and "metadata" in x["inclusion_reason"]["paths"] for x in hits(r["tag"][1])))
        dh = hits(r["derivative"][1])
        check("on: ingested file returns derivative text, tier derivative / level 5, one holding",
              any((x["source_ref"], x["text_ref"], x["tier"], x["authority_level"]) == (self.ingested, self.ingested_doc, "derivative", 5)
                  for x in dh) and self.ingested_doc not in [x["source_ref"] for x in dh])
        ch = r["ch"]
        check("on: one ref, same text through brain_artifact_read and brain_read",
              r["chunk"][1]["chunk"]["text"] == r["base_read"][1]["sections"]["User-supplied text"][ch["start_char"]:ch["end_char"]])
        check("on: chk: refused by the base brain_read", r["chk_in_base"][1]["error"]["code"] == "E_BAD_REF")
        # backup while the store is live (WAL mode) and the server runs
        con = sqlite3.connect(f"file:{self.wiki / '_enrichment/store-v1.sqlite3'}?mode=ro", uri=True)
        rows = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("sources", "chunks", "annotation_revisions")}
        con.close()
        p = sh([*self.tool, "backup", "--wiki", W, "--dest", str(self.work / "backups2")], log=self.log("backup2-dry"))
        p = sh([*self.tool, "backup", "--wiki", W, "--dest", str(self.work / "backups2"), "--execute", "--confirm", token_of(p.stdout)],
               log=self.log("backup2"), check_rc=False)
        check("backup with a live store: snapshot via the SQLite backup API",
              p.returncode == 0 and "store-v1.sqlite3" in p.stdout and '"ok": true' in p.stdout, p.stdout[-300:])
        self.restore_compare(sorted((self.work / "backups2").glob("*.tar.gz"))[-1], "with-store", rows)

    def post_inputs_and_rollback(self):
        a, W = self.a, str(self.wiki)
        memo = self.work / "inputs/memo.txt"
        memo.write_text("Post-upgrade memo: the Shiraz workshop moves to Thursday.\nیادداشت پس از ارتقا.\n")
        m_before = manifest(self.wiki)
        p = sh([self.py, str(self.smoke), "--url", self.url, "--token-file", str(self.tokens), "--reuse-only",
                "--ingest-file", str(memo), "--ingest-phrase", "Shiraz workshop"], log=self.log("post-upgrade-inputs"), check_rc=False)
        rep = json.loads(p.stdout)
        check("post-upgrade capture + ingest committed (refresh token, no consent)",
              rep["consent_performed"] is False and rep["capture"]["commit_state"] == "committed"
              and rep["ingest"]["commit_state"] == "committed")
        self.post_cap, self.post_doc = rep["capture"]["ref"], rep["ingest"]["derivative_ref"]
        m7 = manifest(self.wiki)
        markers = [k for k in changed(m_before, m7, (".git/", "_search/", "_enrichment/"))]
        check("post-upgrade ingest moved the registers and entry-page markers",
              any(k.endswith("CORPUS_STATE.json") for k in markers) and any(k.endswith("HOME.md") for k in markers), markers)
        rb = [*self.tool, "rollback", "--wiki", W, "--plan", str(self.out / "plan.json"), "--work-dir", str(self.work / "work")]
        other = inspect("music-other")
        p = sh([*rb, "--level", "1"], log=self.log("rollback1-dry"))
        p = sh([*rb, "--level", "1", "--execute", "--confirm", token_of(p.stdout)], log=self.log("rollback1"), check_rc=False)
        n = len(self.mcp(lambda call, tools: tools()))
        check("rollback 1: features off, 7 tools, only brain recreated", p.returncode == 0 and n == 7 and inspect("music-other") == other)
        p = sh([*rb, "--level", "2"], log=self.log("rollback2-dry"))
        p = sh([*rb, "--level", "2", "--execute", "--confirm", token_of(p.stdout)], log=self.log("rollback2"), check_rc=False)
        check("rollback 2: code restored, hashes verified, brain healthy", p.returncode == 0 and "ROLLBACK OK" in p.stdout
              and "state=healthy" in p.stdout)
        check("rollback 2: warns that the kept sidecar is no longer git-ignored", "no longer git-ignored" in p.stdout)
        m8 = manifest(self.wiki)
        m3 = json.loads((self.out / "m-pre-apply.json").read_text())
        back = all(h(m3.get(pth)) == h(m8[str(self.wiki)].get(pth)) for pth in self.release_paths)
        check("rollback 2: every release path back to its pre-upgrade bytes (new files removed)", back)
        kept = [k for k in changed(m7, m8, (".git/", "_search/")) if not k.split("/", 1)[1] in self.release_paths
                and not k.endswith("/.env")]
        check("rollback 2: post-upgrade inputs, registers and entry pages kept; _enrichment kept", not kept and
              (self.wiki / "_enrichment/store-v1.sqlite3").is_file(), kept[:5])

        async def fn(call, tools):
            return {"tools": await tools(), "read_cap": await call("brain_read", {"ref": self.post_cap}),
                    "read_doc": await call("brain_read", {"ref": self.post_doc}),
                    "search": await call("brain_search", {"query": "Shiraz workshop", "scope": "all"}),
                    "capture": await call("brain_capture", {"text": "after rollback: یادداشت", "language_hint": "fa"})}
        r = self.mcp(fn)
        check("after rollback: old code reads/searches post-upgrade inputs and commits a new capture",
              len(r["tools"]) == 7 and not r["read_cap"][0] and not r["read_doc"][0]
              and r["capture"][1].get("commit_state") == "committed")
        p = self.dc("run", "--rm", "--no-deps", "-T", "brain", "sh", "-c",
                    "python scripts/validate_repo.py --full && python scripts/validate_content_release.py "
                    "&& python scripts/check_against_baseline.py", check_rc=False, log=self.log("old-validators"))
        check("after rollback: old validators + baseline gate pass with _enrichment/ present", p.returncode == 0)
        p = sh([self.py, str(self.smoke), "--url", self.url, "--token-file", str(self.tokens), "--reuse-only", "--status-only"],
               log=self.log("oauth-after-rollback"), check_rc=False)
        check("after rollback: OAuth refresh without consent", '"consent_performed": false' in p.stdout)
        # level 3: explicit disaster restore beside the live tree
        base = [*self.tool, "rollback", "--wiki", W, "--level", "3", "--disaster-restore", "--archive", str(self.archive),
                "--restore-dir", str(self.work / "dr"), "--work-dir", str(self.work / "dr-work"), "--replay-inputs"]
        live = manifest(self.wiki)
        p = sh(base, log=self.log("rollback3-dry"))
        p = sh([*base, "--execute", "--confirm", token_of(p.stdout)], log=self.log("rollback3"), check_rc=False)
        salv = json.loads(next((self.work / "dr-work").glob("post-backup-inputs-*/SALVAGE.json")).read_text())
        check("rollback 3: salvage holds post-backup inputs and entry pages; store snapshotted",
              p.returncode == 0 and self.post_cap.split(":")[1] in " ".join(salv["paths"]) and "HOME.md" in salv["paths"]
              and salv["enrichment_snapshots"])
        check("rollback 3: the live wiki is not modified",
              not changed(live, manifest(self.wiki), ("_enrichment/",)))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--work", required=True)
    ap.add_argument("--release-repo", required=True)
    ap.add_argument("--target-sha", required=True)
    ap.add_argument("--base-sha", required=True)
    ap.add_argument("--project", default="rh0xbrain")
    ap.add_argument("--port", type=int, default=18180)
    ap.add_argument("--uid", type=int, default=1500)
    ap.add_argument("--image", default="0xbrain/brain:rh-base")
    ap.add_argument("--python-base", default="python:3.12-slim-bookworm")
    ap.add_argument("--other-image", default="python:3.13-slim-bookworm")
    ap.add_argument("--build-ca", default="")
    ap.add_argument("--build-proxy", default="")
    ap.add_argument("--teardown", action="store_true")
    a = ap.parse_args()
    r = Rig(a)
    r.teardown()
    if a.teardown:
        return 0
    r.setup()
    r.populate()
    r.upgrade()
    r.features_on()
    r.post_inputs_and_rollback()
    return finish(r)


def finish(r):
    (r.out / "summary.json").write_text(json.dumps({"checks": CHECKS, "fail": sum(c["result"] == "FAIL" for c in CHECKS)},
                                                   ensure_ascii=False, indent=1))
    print(f"{len(CHECKS)} checks, {sum(c['result'] == 'FAIL' for c in CHECKS)} FAIL -> {r.out / 'summary.json'}")
    return 1 if any(c["result"] == "FAIL" for c in CHECKS) else 0


if __name__ == "__main__":
    sys.exit(main())
