"""WP-E: upgrade / backup / rollback tooling (scripts/upgrade-enrichment.py, scripts/enrichment-backup.py).

Everything runs against throwaway copies: a real git wiki (`enrichment_wiki`), a real separate
"release" clone with a target commit, a fake state dir, and a FAKE docker (command runner double).
The fake docker is a test double: it proves the commands this tooling GENERATES and the logic
around them, NOT a deployment. The one real-Docker test is opt-in (BRAIN_E2E_DOCKER=1) and is a
clearly named SKIP when no daemon is reachable.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
for _p in (SCRIPTS, SCRIPTS / "capture"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import wiki_capture as wc  # noqa: E402


def _load(name: str, fname: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / fname)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


EB = _load("enrichment_backup", "enrichment-backup.py")
UP = _load("upgrade_enrichment", "upgrade-enrichment.py")

OWNER_SECRET = "owner-secret-PLANTED-0123456789abcdef"
TOKEN_SECRET = "tok-PLANTED-access-9876543210zyxwvu"
ENV_SECRET2 = "api-key-PLANTED-in-compose-config-77"
STDERR_SECRET = "stderr-PLANTED-leak-abcdef123456"
SENTINEL_AUTH = "AUTH-STATE-SENTINEL-do-not-print"
ALL_PLANTED = (OWNER_SECRET, TOKEN_SECRET, ENV_SECRET2, STDERR_SECRET, SENTINEL_AUTH)
PUBLIC_URL = "https://brain.example.test"


# ================================================================== fakes
def tree_hashes(*roots: Path, mtime: bool = True) -> dict[str, str]:
    """sha256 (+ mtime_ns) of every file under each root (including .git): the read-only witness."""
    out = {}
    for r in roots:
        for p in sorted(Path(r).rglob("*")):
            if p.is_file() and not p.is_symlink():
                st = p.stat()
                out[f"{r}:{p.relative_to(r).as_posix()}"] = hashlib.sha256(p.read_bytes()).hexdigest() + (f":{st.st_mtime_ns}" if mtime else "")
    return out


@dataclass
class FC:                                           # fake container
    id: str
    name: str
    image_id: str
    image_tag: str
    project: str
    service: str
    working_dir: str = ""
    config_files: str = ""
    env_file: str = ""
    user: str = ""
    env: list = field(default_factory=list)
    mounts: list = field(default_factory=list)
    ports: dict = field(default_factory=dict)
    state: str = "running"
    health: str | None = "healthy"
    started: str = "2026-10-01T00:00:00Z"


class FakeDocker:
    def __init__(self):
        self.containers: list[FC] = []
        self.images: dict[str, str] = {}
        self.calls: list[list[str]] = []
        self.ups: list[list[str]] = []
        self.builds: list[tuple] = []
        self.pauses: list[str] = []
        self.unexpected: list[list[str]] = []
        self.counter = 0
        self.available = True
        self.compose_cfg: dict = {}
        self.stderr_leak = ""
        self.fail_up = None          # stderr text -> `up` fails with it
        self.port_on_up = False      # simulate a release that (wrongly) publishes a port

    def find(self, prefix):
        return [c for c in self.containers if c.id.startswith(prefix)]

    def inspect(self, c: FC) -> dict:
        labels = {"com.docker.compose.project": c.project, "com.docker.compose.service": c.service,
                  "com.docker.compose.project.working_dir": c.working_dir,
                  "com.docker.compose.project.config_files": c.config_files,
                  "com.docker.compose.project.environment_file": c.env_file}
        return {"Id": c.id, "Name": "/" + c.name, "Image": c.image_id,
                "Config": {"Image": c.image_tag, "User": c.user, "Env": c.env, "Labels": labels},
                "State": {"Status": c.state, "Health": {"Status": c.health} if c.health else None, "StartedAt": c.started},
                "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}},
                "Mounts": [{"Source": s, "Destination": d, "RW": True} for s, d in c.mounts],
                "NetworkSettings": {"Ports": c.ports}}

    def handle(self, argv, env):
        R = EB.RunResult
        self.calls.append(list(argv))
        if argv[0] == "ss":
            return R(0, b"LISTEN 0 4096 0.0.0.0:443 0.0.0.0:*\nLISTEN 0 128 127.0.0.1:22 0.0.0.0:*\n")
        sub = argv[1]
        if not self.available and sub != "version":
            return R(1, b"", b"Cannot connect to the Docker daemon")
        if sub == "info":
            return R(0, b"27.0.1\n")
        if sub == "ps":
            lab = [a for a in argv if a.startswith("label=")]
            cs = self.containers
            if lab:
                svc = lab[0].split("=", 2)[2]
                cs = [c for c in cs if c.service == svc]
            return R(0, ("\n".join(c.id[:12] for c in cs) + "\n").encode())
        if sub == "inspect":
            out = [self.inspect(c) for a in argv[2:] for c in self.find(a)]
            return R(0 if out else 1, json.dumps(out).encode(), (b"" if out else b"No such object"))
        if sub == "image" and argv[2] == "inspect":
            ref = argv[3]
            known = ref in self.images or any(ref == v for v in self.images.values())
            return R(0, json.dumps([{"Id": self.images.get(ref, ref), "RepoDigests": ["0xbrain/brain@sha256:" + "d" * 64],
                                     "RepoTags": [ref]}]).encode()) if known else R(1, b"[]", b"No such image")
        if sub == "pause" or sub == "unpause":
            self.pauses.append(f"{sub}:{argv[2][:12]}")
            return R(0, b"")
        if sub == "exec":
            return R(0, b"")
        if sub == "compose":
            i, tail = 2, None
            while i < len(argv):
                if argv[i] in ("-f", "-p", "--project-name", "--project-directory", "--env-file"):
                    i += 2
                else:
                    tail = argv[i:]
                    break
            csub = tail[0]
            if csub == "config":
                return R(0, json.dumps(self.compose_cfg).encode(), self.stderr_leak.encode())
            if csub == "ps":
                brain = [c for c in self.containers if c.service == tail[-1]]
                return R(0, ("\n".join(c.id[:12] for c in brain) + "\n").encode())
            if csub == "build":
                self.builds.append((list(argv), dict(env or {})))
                tag = (env or {}).get("BRAIN_IMAGE")
                if tag:
                    self.images[tag] = "sha256:" + hashlib.sha256(tag.encode()).hexdigest()
                return R(0, b"built\n")
            if csub == "up":
                self.ups.append(list(argv))
                if self.fail_up:
                    return R(1, b"", self.fail_up.encode())
                brain = [c for c in self.containers if c.service == tail[-1]][0]
                self.counter += 1
                brain.id = hashlib.sha256(f"new{self.counter}".encode()).hexdigest()
                brain.started = f"2026-10-10T00:00:0{self.counter}Z"
                tag = next((l.split("=", 1)[1] for l in open(argv[argv.index("--env-file") + 1]).read().splitlines()
                            if l.startswith("BRAIN_IMAGE=")), None) if "--env-file" in argv else None
                if tag and tag in self.images:
                    brain.image_tag, brain.image_id = tag, self.images[tag]
                if self.port_on_up:
                    brain.ports = {"8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "18080"}]}
                return R(0, b"")
        self.unexpected.append(list(argv))
        return R(1, b"", b"unexpected command in fake docker")


class FakeRunner(EB.Runner):
    """Guard is the real guard; git/python run for real; docker/ss are the fake."""

    def __init__(self, fd: FakeDocker, **kw):
        super().__init__(**kw)
        self.fd = fd

    def _exec(self, argv, cwd, env, timeout):
        if os.path.basename(argv[0]) in ("docker", "ss"):
            return self.fd.handle(argv, env)
        return super()._exec(argv, cwd, env, timeout)


class FakeHttp:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.tools = list(UP.BASELINE_TOOLS)
        self.health_ok = True
        self.tokens_seen: list[str] = []

    def __call__(self, method, url, headers, body, timeout, insecure=False):
        self.calls.append((method, url))
        if url.endswith("/healthz"):
            return (200, {}, b'{"ok":true}') if self.health_ok else (502, {}, b"bad gateway")
        if url.endswith("/mcp"):
            self.tokens_seen.append(headers.get("Authorization", ""))
            req = json.loads(body)
            m, rid = req.get("method"), req.get("id")
            if m == "initialize":
                res = {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "fake", "version": "0"}}
            elif m == "tools/list":
                res = {"tools": [{"name": n, "description": "d", "inputSchema": {"type": "object", "properties": {"x": {"type": "string"}}}}
                                 for n in self.tools]}
            elif m == "tools/call":
                res = {"content": [{"type": "text", "text": json.dumps({"ok": True, "ref": req["params"]["arguments"].get("ref")})}]}
            else:
                return 202, {}, b""
            return 200, {"content-type": "text/event-stream"}, f"event: message\ndata: {json.dumps({'jsonrpc': '2.0', 'id': rid, 'result': res})}\n\n".encode()
        return 404, {}, b""


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def git(cwd, *args, check=True):
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=check).stdout.strip()


def commit_files(repo: Path, files: dict, msg: str) -> str:
    for rel, content in files.items():
        p = repo / rel
        if content is None:
            p.unlink()
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
    git(repo, "add", "-A", "-f")
    git(repo, "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


def make_store(wiki: Path, rows: int = 5):
    """A WAL database whose rows live only in the -wal file (checkpoint disabled, reader kept open)."""
    d = wiki / "_enrichment"
    d.mkdir(mode=0o700, exist_ok=True)
    con = sqlite3.connect(d / "store-v1.sqlite3", isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA wal_autocheckpoint=0")
    con.execute("PRAGMA user_version=1")
    con.execute("CREATE TABLE annotation_revisions(id INTEGER PRIMARY KEY, source_ref TEXT, body TEXT)")
    con.execute("BEGIN")
    for i in range(rows):
        con.execute("INSERT INTO annotation_revisions(source_ref, body) VALUES (?,?)", (f"cap:t{i}", "x" * 50))
    con.execute("COMMIT")
    return con


@dataclass
class Rig:
    root: Path
    wiki: Path
    proj: Path
    state: Path
    release: Path
    base: str
    target: str
    docker: FakeDocker
    runner: FakeRunner
    http: FakeHttp
    clock: Clock
    out: io.StringIO
    err: io.StringIO
    deps: object
    env_path: Path
    seeded: object

    def run(self, *args, **kw):
        self.out.seek(0)
        self.out.truncate()
        self.err.seek(0)
        self.err.truncate()
        rc = UP.main([str(a) for a in args], self.deps)
        return rc, self.out.getvalue(), self.err.getvalue()

    def brain(self) -> FC:
        return next(c for c in self.docker.containers if c.service == "brain")

    def up_cmds(self):
        return [c for c in self.runner.log if c[:2] == ["docker", "compose"] and "up" in c]

    def token(self, text: str) -> str:
        return re.search(r"CONFIRM-[0-9A-F]{16}", text).group(0)

    def plan(self, **kw):
        out = self.root / "plan.json"
        args = ["plan", "--wiki", self.wiki, "--release-repo", self.release, "--target-sha", kw.pop("target", self.target),
                "--base-sha", kw.pop("base", self.base), "--out", out]
        for k, v in kw.items():
            args += [f"--{k.replace('_', '-')}", v]
        rc, o, e = self.run(*args)
        return rc, o, e, out

    def live_apply(self, plan_path: Path, work: Path):
        plan = json.loads(plan_path.read_text())
        tok = UP.plan_token(plan, self.wiki)               # the same token the plan summary prints
        return self.run("apply", "--wiki", self.wiki, "--plan", plan_path, "--work-dir", work, "--execute", "--confirm", tok)


RELEASE_ADDS = {
    "scripts/brain_enrichment/store.py": "# WP-A store\n",
    "scripts/brain_enrichment/retrieval.py": "# WP-C retrieval\n",
    "harness/hermes/SKILL.md": "# hermes skill (disabled)\n",
    "harness/hermes/job.json": '{"enabled": false}\n',
    "docs/enrichment/store-schema.md": "# store schema\n",
    "tests/enrichment/test_store.py": "def test_x():\n    assert True\n",
    "scripts/enrichment-backup.py": "# release copy\n",
}


@pytest.fixture()
def rig(enrichment_wiki, tmp_path, monkeypatch):
    seeded = enrichment_wiki
    wiki = seeded.root
    root = tmp_path / "rigroot"
    root.mkdir()
    # --- the deployed wiki as of the base release
    env_ex = (wiki / ".env.example").read_text()
    assert "# BRAIN_ENRICHMENT_MAX_OUTPUT_BYTES=65536" in env_ex
    # The seeded wiki is a copy of THIS repo, which already contains the R1 code. The
    # deployed base predates it: drop everything R1 adds beyond the frozen contract files.
    keep = {"__init__.py", "contract.py", "config.py", "tools_spec.py"}
    gone = {}
    for sub in ("scripts/brain_enrichment", "harness", "docs/enrichment", "tests/enrichment"):
        d = wiki / sub
        for f in (sorted(d.rglob("*")) if d.is_dir() else []):
            rel = f.relative_to(wiki).as_posix()
            if f.is_file() and not (sub == "scripts/brain_enrichment" and (f.name in keep or "schemas/" in rel
                                                                          or "__pycache__" in rel)):
                gone[rel] = None
    gone.update({"scripts/enrichment-backup.py": None, "scripts/upgrade-enrichment.py": None})
    gone = {k: None for k in gone if (wiki / k).is_file()}
    commit_files(wiki, {**gone, ".env.example": env_ex.replace("# BRAIN_ENRICHMENT_MAX_OUTPUT_BYTES=65536\n", "")},
                 "older deployed state")
    for sub in ("scripts/brain_enrichment", "harness", "docs/enrichment", "tests/enrichment"):
        d = wiki / sub                       # git does not track empty dirs; the base had none either
        for dd in sorted((x for x in d.rglob("*") if x.is_dir()), reverse=True) if d.is_dir() else []:
            if not any(dd.iterdir()):
                dd.rmdir()
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
    for sub in ("harness", "docs", "tests"):
        if (wiki / sub).is_dir() and not any((wiki / sub).iterdir()):
            (wiki / sub).rmdir()
    base = git(wiki, "rev-parse", "HEAD")
    release = root / "release"
    subprocess.run(["git", "clone", "-q", str(wiki), str(release)], check=True, capture_output=True)
    git(release, "config", "user.email", "r@example.invalid")
    git(release, "config", "user.name", "release")
    claude = (release / "CLAUDE.md").read_text()
    anchor = "- Keep the main session bounded.\n"
    assert anchor in claude
    changes = dict(RELEASE_ADDS)
    changes["CLAUDE.md"] = claude.replace(anchor, anchor + "- Enrichment is a derived sidecar (docs/enrichment/CONTRACT-E1.md).\n")
    changes[".env.example"] = env_ex   # re-adds the commented MAX_OUTPUT_BYTES key (whitelisted, default off)
    changes[".gitignore"] = (release / ".gitignore").read_text() + "_enrichment-tmp/\n"
    changes["scripts/brain_enrichment/config.py"] = (release / "scripts/brain_enrichment/config.py").read_text() + "# r1 release note\n"
    changes["scripts/remote_mcp/adapter.py"] = (release / "scripts/remote_mcp/adapter.py").read_text() + "\n# r1 wiring hunk\n"
    target = commit_files(release, changes, "R1 release")
    # --- instance divergence AFTER the base: the owner's own edit of an instance-marker line
    c = (wiki / "CLAUDE.md").read_text()
    inst = c.replace("- Registered source artifacts: 0 (empty kit", "- Registered source artifacts: 7 (instance")
    assert inst != c
    commit_files(wiki, {"CLAUDE.md": inst}, "instance: artifact count")
    # --- deployment skeleton: project dir (compose files + env), state dir, fake docker
    proj = root / "opt" / "0xbrain"
    proj.mkdir(parents=True)
    shutil.copy(ROOT / "compose.yaml", proj / "compose.yaml")
    (proj / "compose.override.yaml").write_text("services:\n  brain:\n    restart: always\n")
    state = root / "state"
    (state / "auth").mkdir(parents=True, mode=0o700)
    (state / "auth/oauth_state.json").write_text(json.dumps({"clients": {}, "marker": SENTINEL_AUTH}))
    (state / "auth/oauth_state.json").chmod(0o600)
    for d in ("caddy/data", "caddy/config", "qmd", "home", "backups"):
        (state / d).mkdir(parents=True)
    (state / "caddy/data/cert.pem").write_text("tls-state\n")
    env_path = proj / ".env"
    env_path.write_text(
        f"BRAIN_DATA_DIR={wiki}\nBRAIN_STATE_DIR={state}\nBRAIN_UID={os.getuid()}\nBRAIN_GID={os.getgid()}\n"
        f"BRAIN_PUBLIC_URL={PUBLIC_URL}\nBRAIN_OWNER_SECRET={OWNER_SECRET}\nBRAIN_DOMAIN=brain.example.test\n"
        "BRAIN_ALLOWED_REDIRECTS=https://claude.ai/api/mcp/auth_callback\nBRAIN_IMAGE=0xbrain/brain:local\n")
    env_path.chmod(0o600)
    fd = FakeDocker()
    files = f"{proj}/compose.yaml,{proj}/compose.override.yaml"
    fd.images["0xbrain/brain:local"] = "sha256:" + "a" * 64
    fd.containers = [
        FC("1" * 64, "0xbrain-brain-1", "sha256:" + "a" * 64, "0xbrain/brain:local", "0xbrain", "brain", str(proj), files, str(env_path),
           f"{os.getuid()}:{os.getgid()}", [f"BRAIN_OWNER_SECRET={OWNER_SECRET}", "BRAIN_PORT=8080", f"OTHER_API_KEY={ENV_SECRET2}"],
           [(str(wiki), "/wiki"), (str(state / "qmd"), "/state/qmd"), (str(state / "auth"), "/state/auth")], {"8080/tcp": None}),
        FC("2" * 64, "edge-nginx-1", "sha256:" + "b" * 64, "nginx:1.27", "edge", "nginx", "/opt/edge", "/opt/edge/compose.yaml", "", "",
           [], [], {"443/tcp": [{"HostIp": "0.0.0.0", "HostPort": "443"}], "80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "80"}]}),
        FC("3" * 64, "0xbrain-worker-1", "sha256:" + "c" * 64, "busybox:1", "0xbrain", "worker", str(proj), files, str(env_path), "", [], [], {}),
    ]
    fd.compose_cfg = {"name": "0xbrain", "services": {
        "brain": {"image": "0xbrain/brain:local", "environment": {"BRAIN_OWNER_SECRET": OWNER_SECRET, "TOKENISH_API_KEY": ENV_SECRET2},
                  "volumes": [{"type": "bind", "source": str(wiki), "target": "/wiki"}], "ports": []},
        "worker": {"image": "busybox:1", "ports": []}}}
    fd.stderr_leak = f"warning: variable BRAIN_OWNER_SECRET={STDERR_SECRET} is set"
    runner = FakeRunner(fd)
    http, clock = FakeHttp(), Clock()
    out, err = io.StringIO(), io.StringIO()
    deps = UP.Deps(runner=runner, http=http, clock=clock, sleep=clock.sleep, out=out, err=err,
                   sysinfo=lambda paths: {"ram_available_mb": 4096, "disk_free_mb": {str(p): 50_000 for p in paths}},
                   geteuid=lambda: 12345)
    return Rig(root, wiki, proj, state, release, base, target, fd, runner, http, clock, out, err, deps, env_path, seeded)


# ================================================================== helpers for assertions
def leaked(text: str) -> list[str]:
    return [s for s in ALL_PLANTED if s in text]


def code_paths(applied: dict) -> set[str]:
    return {e["path"] for e in applied["entries"]}


# ================================================================== guard / policy (pure)
FORBIDDEN_CMDS = [
    ["git", "reset", "--hard"], ["git", "-C", "/x", "reset", "--hard", "HEAD~1"], ["git", "clean", "-fdx"], ["git", "stash"],
    ["git", "-C", "/x", "stash", "push", "-u"], ["git", "checkout", "--", "."], ["git", "revert", "a..b"], ["git", "rebase", "main"],
    ["git", "merge", "origin/main"], ["git", "push", "--force"], ["git", "merge-file", "a", "b", "c"], ["git", "diff", "--output=/tmp/x", "a", "b"],
    ["git", "-c", "core.hooksPath=/tmp/h", "status"],
    ["docker", "compose", "down"], ["docker", "compose", "-f", "c.yaml", "down", "-v"], ["docker", "compose", "up", "-d"],
    ["docker", "compose", "up", "-d", "--no-deps"], ["docker", "compose", "up", "-d", "--no-deps", "brain", "caddy"],
    ["docker", "compose", "up", "-d", "--no-deps", "--remove-orphans", "brain"], ["docker", "compose", "up", "--no-deps", "brain"],
    ["docker", "compose", "up", "-d", "brain"], ["docker", "compose", "restart"], ["docker", "compose", "stop", "brain"],
    ["docker", "compose", "rm", "-f"], ["docker", "compose", "build"], ["docker", "compose", "run", "--rm", "brain", "sh"],
    ["docker", "rm", "-f", "x"], ["docker", "stop", "x"], ["docker", "system", "prune", "-af"], ["docker", "volume", "rm", "x"],
    ["docker", "network", "create", "n"], ["docker", "run", "-p", "9000:8080", "img"], ["docker", "exec", "x", "sh"],
    ["chown", "-R", "1000:1000", "/data"], ["chown", "-hR", "1000", "/data"], ["chmod", "-R", "777", "/data"],
    ["rm", "-rf", "/x"], ["bash", "scripts/restore-backup.sh", "a.tgz", "--force"], ["sh", "-c", "docker compose down"],
    ["/opt/0xbrain/wiki/scripts/upgrade.sh"], ["scripts/create-backup.sh"], ["tar", "-xzf", "a"], ["rsync", "-a", "a", "b"],
    [sys.executable, "-c", "print(1)"], [sys.executable, "scripts/validate_repo.py", "--fix"],
]

ALLOWED_CMDS = [
    ["git", "-c", "safe.directory=*", "-C", "/x", "status", "--porcelain=v1", "-z"], ["git", "rev-parse", "HEAD"],
    ["git", "merge-file", "-p", "a", "b", "c"], ["git", "cat-file", "blob", "abc"], ["git", "diff", "--raw", "-z", "a", "b"],
    ["docker", "info"], ["docker", "ps", "-a", "--format", "{{.ID}}"], ["docker", "inspect", "x"], ["docker", "image", "inspect", "x"],
    ["docker", "pause", "abc"], ["docker", "unpause", "abc"],
    ["docker", "exec", "abc", "python", "/usr/local/lib/brain/healthcheck.py"],
    ["docker", "compose", "-f", "a.yaml", "-f", "b.yaml", "--env-file", ".env", "up", "-d", "--no-deps", "--force-recreate", "brain"],
    ["docker", "compose", "up", "-d", "--no-deps", "brain"], ["docker", "compose", "build", "brain"],
    ["docker", "compose", "config", "--format", "json"], ["docker", "compose", "ps", "-q", "brain"], ["ss", "-H", "-ltn"],
    [sys.executable, "scripts/validate_repo.py", "--full"], [sys.executable, "scripts/validate_content_release.py"],
]


@pytest.mark.parametrize("argv", FORBIDDEN_CMDS, ids=lambda a: " ".join(a)[:60])
def test_guard_refuses_forbidden_commands(argv):
    with pytest.raises(EB.UnsafeCommand):
        EB.guard_command(argv)


@pytest.mark.parametrize("argv", ALLOWED_CMDS, ids=lambda a: " ".join(a)[:60])
def test_guard_allows_only_the_known_forms(argv):
    EB.guard_command(argv)


def test_every_runner_checks_the_guard_even_a_fake():
    r = FakeRunner(FakeDocker())
    with pytest.raises(EB.UnsafeCommand):
        r.run(["docker", "compose", "down"])
    assert r.log == []


def test_compose_up_builder_is_single_service_no_deps():
    ctx = UP.ComposeCtx(project="p", project_dir="/p", files=["/p/compose.yaml", "/p/compose.override.yaml"], env_file="/p/.env", service="brain")
    up = ctx.up()
    assert up[-5:] == ["up", "-d", "--no-deps", "--force-recreate", "brain"]
    assert up.index("/p/compose.yaml") < up.index("/p/compose.override.yaml")
    for bad in ("down", "-R", "--publish", "--remove-orphans", "caddy", "--scale", "--detach-keys"):
        assert bad not in up, bad
    assert up.count("brain") == 1 and up[up.index("up") + 1:] == ["-d", "--no-deps", "--force-recreate", "brain"]
    EB.guard_command(up)
    assert ctx.build()[-2:] == ["build", "brain"] and ctx.ps_q()[-3:] == ["ps", "-q", "brain"]
    EB.guard_command(ctx.build())


def test_classify_path_policy():
    c = UP.classify_path
    assert c("scripts/brain_enrichment/store.py")[0] == "code"
    assert c("harness/hermes/job.json")[0] == "code"
    assert c("docs/enrichment/upgrade.md")[0] == "code"
    assert c("tests/enrichment/test_upgrade.py")[0] == "code"
    assert c("scripts/upgrade-enrichment.py")[0] == "code" and c("scripts/enrichment-backup.py")[0] == "code"
    assert c("CLAUDE.md")[0] == "marker" and c("SYSTEM_DESIGN.md")[0] == "marker"
    assert c(".gitignore")[0] == "shared" and c(".env.example")[0] == "shared"
    for p in ("_originals/x.pdf", "01-inbox/captures/c.md", "02-sources/text/a.md", "00-system/registers/CORPUS_STATE.json",
              "_enrichment/store-v1.sqlite3", "_search/x", ".git/config", "_proposals/proposals.jsonl", ".env", ".env.production"):
        assert c(p)[0] == "forbidden", p
    for p in ("compose.yaml", "Dockerfile", "deploy/entrypoint.sh", "install.sh", "scripts/upgrade.sh", "requirements.txt",
              "scripts/brain_enrichment/__pycache__/a.pyc", "../etc/passwd", "/abs"):
        assert c(p)[0] in ("outside", "forbidden"), p


def test_redactor_patterns_and_registered_values():
    r = EB.Redactor([OWNER_SECRET])
    s = f"x {OWNER_SECRET} Bearer abcdef123456789 BRAIN_OWNER_SECRET=hunter2hunter2 https://u:pw@host/x api_key: abc123 sk-abcdefghijklmnopqrstuv"
    t = r.text(s)
    for bad in (OWNER_SECRET, "abcdef123456789", "hunter2hunter2", "u:pw@", "sk-abcdefghijklmnopqrstuv"):
        assert bad not in t, (bad, t)
    assert r.obj({"a": [OWNER_SECRET]})["a"][0] == "[REDACTED]"


def test_confirm_token_binds_to_the_plan():
    t1, t2 = EB.confirm_token("apply", "d1"), EB.confirm_token("apply", "d2")
    assert t1 != t2 and EB.confirm_token("rollback-2", "d1") != t1
    assert EB.require_confirmation("apply", "d1", False, None) is False
    with pytest.raises(EB.ToolError):
        EB.require_confirmation("apply", "d1", True, None)
    with pytest.raises(EB.ToolError):
        EB.require_confirmation("apply", "d1", True, t2)
    with pytest.raises(EB.ToolError):
        EB.require_confirmation("apply", "d1", False, t1)
    assert EB.require_confirmation("apply", "d1", True, t1) is True


# ================================================================== preflight
def preflight(rig, *extra, out=None):
    out = out or rig.root / "preflight.json"
    return (*rig.run("preflight", "--wiki", rig.wiki, "--out", out, *extra), out)


def test_preflight_is_read_only(rig):
    store = make_store(rig.wiki)
    (rig.wiki / "_search").mkdir(exist_ok=True)
    (rig.wiki / "_search/idx.txt").write_text("rebuildable")
    dirty = wc.capture_text("uncommitted dirty capture before preflight", "mcp", "en")
    before = tree_hashes(rig.wiki, rig.proj, rig.state)
    names_before = {k.split(":", 1)[0] + ":" + k.split(":", 1)[1] for k in before}
    rc, o, e, rep_path = preflight(rig, "--target-sha", rig.target, "--base-sha", rig.base, "--release-repo", rig.release)
    after = tree_hashes(rig.wiki, rig.proj, rig.state)
    store.close()
    assert before == after, "preflight changed a file (content or mtime) under the wiki, project dir or state dir"
    assert set(names_before) == set(after)
    assert rc == 0, (o, e)
    assert (rep_path.stat().st_mode & 0o777) == 0o600
    rep = json.loads(rep_path.read_text())
    assert rep["status"] in ("PASS", "WARN") and rep["schema"] == "enrichment-preflight-1"
    # docker side: only read-only verbs were issued
    assert not rig.docker.ups and not rig.docker.pauses and not rig.docker.builds
    assert rig.docker.unexpected == []
    assert dirty["path"] in rep["git"]["dirty_names_noncanonical"] or any(dirty["path"].endswith(p) for p in rep["git"]["dirty_names_noncanonical"])


def test_preflight_discovers_everything_without_hardcoding(rig):
    rc, o, e, p = preflight(rig)
    rep = json.loads(p.read_text())
    assert rep["paths"]["data"] == str(rig.wiki) and rep["paths"]["state"] == str(rig.state)
    assert rep["paths"]["enrichment_store"].endswith("_enrichment/store-v1.sqlite3") and "/_search" not in rep["paths"]["enrichment_store"]
    comp = rep["compose"]
    assert comp["source"] == "container-labels" and comp["project"] == "0xbrain" and comp["service"] == "brain"
    assert [f["path"] for f in comp["files"]] == [str(rig.proj / "compose.yaml"), str(rig.proj / "compose.override.yaml")]
    assert comp["env_file"] == str(rig.env_path) and comp["project_dir"] == str(rig.proj)
    assert comp["override_after_base"] is True
    assert comp["files"][1]["looks_like_override"] and "brain" in comp["files"][1]["services_defined"]
    cont = rep["container"]
    assert cont["image_id"] == "sha256:" + "a" * 64 and cont["image_tag"] == "0xbrain/brain:local"
    assert cont["repo_digests"] and cont["user"] == f"{os.getuid()}:{os.getgid()}"
    assert rep["identity"]["env"] == f"{os.getuid()}:{os.getgid()}" and "container_user" in rep["identity"]
    assert rep["config"]["callbacks"]["count"] == 1 and len(rep["config"]["callbacks"]["sha256"]) == 64
    assert "BRAIN_OWNER_SECRET" in rep["config"]["secret_keys_present"] and "BRAIN_OWNER_SECRET" not in rep["config"]["values"]
    assert rep["endpoint"]["public_url"] == PUBLIC_URL
    assert rep["auth_state"]["exists"] and rep["auth_state"]["mode"] == "0o600" and len(rep["auth_state"]["sha256"]) == 64
    assert rep["resources"]["ram_available_mb"] == 4096
    # no-touch baseline: everything except brain, with port mappings
    names = {x["name"]: x for x in rep["others"]}
    assert set(names) == {"edge-nginx-1", "0xbrain-worker-1"}
    assert any("443->443/tcp" in port for port in names["edge-nginx-1"]["ports"])
    assert rep["listening"] and any(x.endswith(":443") for x in rep["listening"])
    # corpus manifest hashes immutable/capture/source/derivative files
    files = rep["corpus"]["files"]
    assert any(k.startswith("_originals/") for k in files) and any(k.startswith("01-inbox/captures/") for k in files)
    assert any(k.startswith("02-sources/text/") for k in files) and "_proposals/proposals.jsonl" in files
    assert files[".githooks/known-baseline-errors.txt"]["sha256"] == hashlib.sha256((rig.wiki / ".githooks/known-baseline-errors.txt").read_bytes()).hexdigest()
    assert rep["proposals"]["lines"] >= 1
    assert set(rep["validators"]) >= {"validate_repo", "validate_content_release"}
    assert rep["tools"] == {"status": "SKIP"}
    assert any(i["level"] == "SKIP" and i["area"] == "tools" for i in rep["findings"])
    # names and hashes only: no capture/source text and no auth-state content ever reaches the report or the screen
    from support import FA_SHORT, DUP_TEXT
    blob = p.read_text() + o
    assert FA_SHORT not in blob and DUP_TEXT not in blob and SENTINEL_AUTH not in blob


def test_preflight_detects_compose_override_ordering(rig):
    b = rig.brain()
    b.config_files = f"{rig.proj}/compose.override.yaml,{rig.proj}/compose.yaml"
    rc, o, e, p = preflight(rig)
    rep = json.loads(p.read_text())
    assert rep["compose"]["files"][0]["looks_like_override"]
    assert any(i["area"] == "compose" and "BEFORE the base" in i["msg"] for i in rep["findings"])
    # operator flags that disagree with the running labels are flagged, not trusted silently
    b.config_files = f"{rig.proj}/compose.yaml,{rig.proj}/compose.override.yaml"
    rc, o, e, p = preflight(rig, "--compose-file", rig.proj / "compose.yaml")
    rep = json.loads(p.read_text())
    assert any("differs from the running container" in i["msg"] for i in rep["findings"])
    assert "differs from labels" in rep["compose"]["source"]


def test_preflight_ambiguous_or_mismatched_deployment_stops(rig):
    twin = FC("4" * 64, "other-brain-1", "sha256:" + "e" * 64, "x", "other", "brain", str(rig.proj), "", "", "", [], [(str(rig.wiki), "/wiki")], {})
    rig.docker.containers.append(twin)
    rc, o, e, p = preflight(rig)
    assert rc == 3 and "ambiguous" in o
    assert json.loads(p.read_text())["status"] == "STOP"
    rc, o, e, p = preflight(rig, "--project", "0xbrain")
    assert rc == 0, o
    rig.docker.containers.remove(twin)
    # naming the brain container while pointing --wiki at a different checkout must STOP (mount mismatch)
    other = rig.root / "otherwiki"
    subprocess.run(["git", "clone", "-q", str(rig.wiki), str(other)], check=True, capture_output=True)
    rc, o, e = rig.run("preflight", "--wiki", other, "--container", "1" * 12, "--out", rig.root / "x.json")
    assert rc == 3 and "mount is not the --wiki tree" in o, o
    # a directory that is not a repository is a usage error and nothing is written
    empty = rig.root / "empty"
    empty.mkdir()
    assert rig.run("preflight", "--wiki", empty, "--out", rig.root / "y.json")[0] == 2
    assert not (rig.root / "y.json").exists()


def test_preflight_stops_on_out_of_allowlist_diff(rig):
    bad = commit_files(rig.release, {"compose.yaml": "services: {}\n", "_originals/forged.pdf": "x", "Dockerfile": "FROM scratch\n",
                                     "scripts/brain_enrichment/ok.py": "x = 1\n"}, "sneaky")
    rc, o, e, p = preflight(rig, "--target-sha", bad, "--base-sha", rig.base, "--release-repo", rig.release)
    assert rc == 3
    rep = json.loads(p.read_text())
    reasons = " ".join(i["msg"] for i in rep["findings"] if i["level"] == "STOP")
    for path in ("compose.yaml", "_originals/forged.pdf", "Dockerfile"):
        assert path in reasons
    assert "scripts/brain_enrichment/ok.py" not in reasons
    assert "STOP" in o


def test_preflight_stops_on_floating_target_and_missing_commit(rig):
    for ref in ("main", "HEAD", "origin/main", rig.target[:12], "0" * 40):
        rc, o, e, p = preflight(rig, "--target-sha", ref, "--release-repo", rig.release)
        assert rc == 3 and ("40-hex" in o or "not a commit" in o), (ref, o)


def test_preflight_writes_report_only_outside_the_wiki(rig):
    rc, o, e = rig.run("preflight", "--wiki", rig.wiki, "--out", rig.wiki / "report.json")
    assert rc == 2 and not (rig.wiki / "report.json").exists()


def test_preflight_redacts_planted_secrets_everywhere(rig, caplog, capsys):
    tokfile = rig.root / "token.txt"
    tokfile.write_text(json.dumps({"access_token": TOKEN_SECRET, "refresh_token": "r-" + TOKEN_SECRET}))
    tokfile.chmod(0o600)
    caplog.set_level(logging.DEBUG)
    lg = [logging.getLogger("upgrade_enrichment"), logging.getLogger("enrichment_backup")]
    old_levels = [l.level for l in lg]
    for l in lg:
        l.setLevel(logging.DEBUG)
    try:
        rc, o, e, p = preflight(rig, "--token-file", tokfile)
        log_is_live = UP.log.isEnabledFor(logging.DEBUG)
        UP.log.debug("deliberate leak attempt %s and Bearer %s", OWNER_SECRET, TOKEN_SECRET)    # must be scrubbed by the filter
    finally:
        for l, lv in zip(lg, old_levels):
            l.setLevel(lv)
    assert log_is_live and any("run: " in r.getMessage() for r in caplog.records), "the debug log must actually be exercised"
    report = p.read_text()
    logs = "\n".join(r.getMessage() for r in caplog.records)
    cap = capsys.readouterr()
    cmdlog = json.dumps(rig.runner.log)
    for name, blob in (("stdout", o), ("stderr", e), ("report", report), ("logs", logs), ("captured stdout", cap.out + cap.err), ("runner log", cmdlog)):
        assert leaked(blob) == [], f"{name} leaks {leaked(blob)}"
    assert "deliberate leak attempt [REDACTED]" in logs
    # the secrets really were seen by the tool (so the test is not vacuous)
    assert rig.deps.redactor.count >= 3, "the tool never learned the secrets it was supposed to hide"
    assert any(OWNER_SECRET in c.env[0] for c in rig.docker.containers[:1])
    assert rig.http.tokens_seen and all(TOKEN_SECRET in t for t in rig.http.tokens_seen)
    assert "Bearer" in rig.http.tokens_seen[0]
    rep = json.loads(report)
    assert rep["tools"]["baseline_equal"] is True and rep["tools"]["names"] == sorted(UP.BASELINE_TOOLS)


def test_preflight_tool_list_non_baseline_is_flagged(rig):
    rig.http.tools = list(UP.BASELINE_TOOLS) + list(UP.ENRICHMENT_TOOLS)
    tokfile = rig.root / "t.txt"
    tokfile.write_text(TOKEN_SECRET)
    rc, o, e, p = preflight(rig, "--token-file", tokfile)
    rep = json.loads(p.read_text())
    assert rep["tools"]["baseline_equal"] is False
    assert any(i["area"] == "tools" and i["level"] == "WARN" for i in rep["findings"])


def test_preflight_docker_unavailable_is_unverified_not_fabricated(rig):
    rig.docker.available = False
    rc, o, e, p = preflight(rig)
    rep = json.loads(p.read_text())
    assert rep["docker"]["available"] is False and "container" not in rep and rep["others"] == []
    assert any("UNVERIFIED" in i["msg"] for i in rep["findings"])
    assert rep["compose"]["source"].startswith("default file names") or rep["compose"]["source"] == "COMPOSE_FILE in .env"


# ================================================================== plan
def test_plan_pins_sha_and_rejects_floating_refs(rig):
    for ref in ("main", "HEAD", "origin/main", "v1.0", rig.target[:7], rig.target.upper(), "0" * 40):
        rc, o, e, out = rig.plan(target=ref)
        assert rc == 3 and not out.exists(), ref
        assert "floating" in o or "not a commit" in o
    rc, o, e, out = rig.plan()
    assert rc == 0, (o, e)
    plan = json.loads(out.read_text())
    assert plan["target_sha"] == rig.target and PIN_RE_OK(plan["target_sha"]) and plan["base_release_sha"] == rig.base
    assert plan["target_pinned"] is True and plan["release_id"] == "r1-" + rig.target[:12]
    assert (out.stat().st_mode & 0o777) == 0o600


def PIN_RE_OK(s):
    return re.fullmatch(r"[0-9a-f]{40}", s) is not None


def test_plan_manifest_has_per_path_hashes_for_the_allowlist_only(rig):
    rc, o, e, out = rig.plan()
    plan = json.loads(out.read_text())
    ents = {x["path"]: x for x in plan["paths"]}
    assert set(ents) == set(RELEASE_ADDS) | {"CLAUDE.md", ".env.example", ".gitignore", "scripts/brain_enrichment/config.py", "scripts/remote_mcp/adapter.py"}
    for p, x in ents.items():
        assert UP.classify_path(p)[0] in ("code", "shared", "marker")
        live = rig.wiki / p
        assert x["before_sha256"] == (hashlib.sha256(live.read_bytes()).hexdigest() if live.is_file() else None)
        blob = subprocess.run(["git", "-C", str(rig.release), "cat-file", "blob", x["target_oid"]], capture_output=True, check=True).stdout
        if x["method"] in ("add", "copy-target"):
            assert x["after_sha256"] == hashlib.sha256(blob).hexdigest()
    assert ents["scripts/brain_enrichment/store.py"]["action"] == "add" and ents["scripts/brain_enrichment/store.py"]["before_sha256"] is None
    assert ents["scripts/brain_enrichment/config.py"]["action"] == "modify"
    assert plan["image"]["rebuild_required"] is False and plan["image"]["new_tag"] is None
    assert "BRAIN_ENRICHMENT_MAX_OUTPUT_BYTES" in plan["config_delta"]["new_env_keys"]
    assert plan["config_delta"]["env_changes_required_in_live_env"] == []
    assert plan["features"]["after_apply"] == {"BRAIN_ENRICHMENT": "off", "BRAIN_ENRICHMENT_WRITES": "0", "BRAIN_ENRICHMENT_RETRIEVAL": "0"}
    assert "_originals" in plan["forbidden_zones"] and "01-inbox" in plan["forbidden_zones"]
    assert not any(x["path"].split("/")[0] in plan["forbidden_zones"] for x in plan["paths"])
    assert plan["compose"]["files"] == [str(rig.proj / "compose.yaml"), str(rig.proj / "compose.override.yaml")]


def test_plan_merges_instance_marker_files_never_copies_kit_text(rig):
    rc, o, e, out = rig.plan()
    plan = json.loads(out.read_text())
    ent = next(x for x in plan["paths"] if x["path"] == "CLAUDE.md")
    assert ent["class"] == "marker" and ent["method"] == "merge3" and ent["instance_lines_preserved"] is True
    release_text = subprocess.run(["git", "-C", str(rig.release), "show", f"{rig.target}:CLAUDE.md"], capture_output=True, text=True).stdout
    assert ent["after_sha256"] != hashlib.sha256(release_text.encode()).hexdigest(), "marker file would be overwritten with the empty-kit text"
    # materialise it through apply on a copy: instance line kept AND release hunk present
    rc, o, e = rig.run("apply", "--wiki", rig.wiki, "--plan", out, "--copy-dir", rig.root / "copy")
    assert rc == 0, (o, e)
    merged = (rig.root / "copy/CLAUDE.md").read_text()
    assert "Registered source artifacts: 7 (instance" in merged and "Registered source artifacts: 0 (empty kit" not in merged
    assert "Enrichment is a derived sidecar" in merged


def test_plan_conflicting_instance_edit_stops_instead_of_overwriting(rig):
    c = (rig.wiki / "CLAUDE.md").read_text()
    commit_files(rig.wiki, {"CLAUDE.md": c.replace("- Keep the main session bounded.", "- Keep the main session bounded. (owner note)")}, "owner edits the same line")
    rc, o, e, out = rig.plan()
    assert rc == 3 and not out.exists()
    assert "CLAUDE.md" in o and ("conflict" in o.lower() or "refused" in o.lower())
    assert "(owner note)" in (rig.wiki / "CLAUDE.md").read_text()


def test_plan_stops_on_out_of_allowlist_and_bad_config_delta(rig):
    cases = {
        "compose": {"compose.yaml": "x: 1\n"},
        "originals": {"_originals/x.pdf": "forged"},
        "deploy": {"deploy/entrypoint.sh": "#!/bin/sh\n"},
        "runtime": {"_enrichment/store-v1.sqlite3": "x"},
        "requirements": {"requirements.txt": "newdep==1\n"},
    }
    for name, files in cases.items():
        sha = commit_files(rig.release, files, name)
        rc, o, e, out = rig.plan(target=sha)
        assert rc == 3, (name, o)
        assert next(iter(files)) in o
    env_ex = (rig.release / ".env.example").read_text()
    on = commit_files(rig.release, {".env.example": env_ex + "BRAIN_ENRICHMENT=on\n"}, "default on")
    assert on and rig.plan(target=on)[0] == 3
    newkey = commit_files(rig.release, {".env.example": env_ex + "# OPENAI_API_KEY=\n"}, "new key")
    rc, o, e, out = rig.plan(target=newkey)
    assert rc == 3 and "OPENAI_API_KEY" in o and "whitelist" in o


def test_plan_stops_when_live_env_already_enables_enrichment(rig):
    rig.env_path.write_text(rig.env_path.read_text() + "BRAIN_ENRICHMENT=on\n")
    rc, o, e, out = rig.plan()
    assert rc == 3 and "already sets BRAIN_ENRICHMENT" in o


def test_plan_bounds_shared_file_hunks(rig):
    big = commit_files(rig.release, {".gitignore": (rig.release / ".gitignore").read_text() + "".join(f"x{i}\n" for i in range(500))}, "big hunk")
    rc, o, e, out = rig.plan(target=big)
    assert rc == 3 and "not a bounded shared-file hunk" in o


def test_plan_image_tag_rules(rig):
    for tag in ("0xbrain/brain:latest", "0xbrain/brain:local", "bad tag", "0xbrain/brain"):
        assert rig.plan(new_image_tag=tag)[0] == 3, tag
    rig.docker.images["0xbrain/brain:r1-existing"] = "sha256:" + "f" * 64
    rc, o, e, _ = rig.plan(new_image_tag="0xbrain/brain:r1-existing")
    assert rc == 3 and "never overwrite" in o
    rc, o, e, out = rig.plan(new_image_tag="0xbrain/brain:r1-new")
    assert rc == 0
    img = json.loads(out.read_text())["image"]
    assert img["new_tag"] == "0xbrain/brain:r1-new" and img["current_tag"] == "0xbrain/brain:local"
    assert img["build_command"][-2:] == ["build", "brain"] and img["build_env"] == {"BRAIN_IMAGE": "0xbrain/brain:r1-new"}


def test_plan_shared_hunks_file_cannot_widen_into_forbidden_zones(rig):
    sh = rig.root / "shared.json"
    sh.write_text(json.dumps({"allow": ["_originals/a.md"]}))
    rc, o, e, out = rig.plan(shared_hunks=sh)
    assert rc == 3 and "forbidden" in o


# ================================================================== backup
def backup_args(rig, dest, *extra):
    return ["backup", "--wiki", rig.wiki, "--dest", dest, *extra]


def test_backup_dry_run_writes_nothing_and_needs_matching_token(rig):
    dest = rig.root / "bk"
    before = tree_hashes(rig.wiki, rig.state)
    rc, o, e = rig.run(*backup_args(rig, dest))
    assert rc == 0 and "DRY RUN" in o and not dest.exists()
    tok = rig.token(o)
    assert rig.docker.pauses == []
    for bad in (["--execute"], ["--execute", "--confirm", "CONFIRM-0000000000000000"], ["--confirm", tok]):
        rc, o, e = rig.run(*backup_args(rig, dest, *bad))
        assert rc == 2 and not dest.exists(), bad
    assert tree_hashes(rig.wiki, rig.state) == before


def run_backup(rig, dest, *extra):
    rc, o, e = rig.run(*backup_args(rig, dest, *extra))
    assert rc == 0, (o, e)
    rc, o, e = rig.run(*backup_args(rig, dest, *extra, "--execute", "--confirm", rig.token(o)))
    return rc, o, e


def test_backup_contents_include_dirty_writes_store_snapshot_auth_and_exclude_search(rig):
    con = make_store(rig.wiki, rows=7)
    (rig.wiki / "_search").mkdir(exist_ok=True)
    (rig.wiki / "_search/idx.sqlite").write_text("rebuildable index")
    dirty_cap = wc.capture_text("dirty uncommitted capture that must be in the backup", "mcp", "en")
    with open(rig.wiki / "_proposals/proposals.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"id": "prop-dirty", "kind": "note", "status": "new", "body": "uncommitted proposal"}) + "\n")
    outside = rig.root / "proxy" / "nginx.conf"
    outside.parent.mkdir()
    outside.write_text("server { listen 443; }\n")
    dest = rig.root / "bk"
    rc, o, e = run_backup(rig, dest, "--extra-path", outside.parent)
    con.close()
    assert "BACKUP OK" in o
    arch = next(dest.glob("0xbrain-enrichment-backup-*.tar.gz"))
    assert (arch.stat().st_mode & 0o777) == 0o600
    assert (Path(str(arch) + ".sha256").stat().st_mode & 0o777) == 0o600
    assert not [p for p in dest.iterdir() if p.name.startswith(".tmp")], "scratch dir left behind"
    man = EB.read_manifest(arch)
    names = set(man["files"])
    # included
    assert "repo/.git/HEAD" in names and "repo/.env.example" in names
    assert f"repo/{dirty_cap['path']}" in names, "dirty (uncommitted) capture missing"
    assert "repo/_proposals/proposals.jsonl" in names and any(n.startswith("repo/_originals/") for n in names)
    assert "repo/_enrichment/store-v1.sqlite3" in names, "_enrichment must be included"
    assert "state/auth/oauth_state.json" in names and any(n.startswith("state/caddy/data/") for n in names)
    assert "compose/1-compose.override.yaml" in names and "compose/0-compose.yaml" in names, "compose files outside DATA are archived separately"
    assert any(n.startswith("extra/0/") and n.endswith("nginx.conf") for n in names)
    # excluded
    assert not any(n == "repo/_search" or n.startswith("repo/_search/") for n in names), "_search must be excluded"
    assert not any(n.endswith(("-wal", "-shm")) for n in names)
    # recorded facts
    assert man["image"]["id"] == "sha256:" + "a" * 64 and man["image"]["tag"] == "0xbrain/brain:local"
    assert man["compose"]["files"] == [str(rig.proj / "compose.yaml"), str(rig.proj / "compose.override.yaml")]
    assert man["git"]["head"] == git(rig.wiki, "rev-parse", "HEAD")
    assert any(p.endswith(dirty_cap["path"].split("/")[-1]) for p in man["git"]["dirty_paths"])
    assert "scripts/remote_mcp/server.py" in man["code_manifest"] and not any(k.startswith(("_originals", "_proposals", ".git/")) for k in man["code_manifest"])
    assert man["writer_pause"]["mode"] == "docker-pause" and man["writer_pause"]["paused"] is True
    # writer was paused before and resumed after
    assert rig.docker.pauses == ["pause:" + "1" * 12, "unpause:" + "1" * 12]
    # restore to scratch: identical hashes (against the live tree too), snapshot opens and is healthy
    res = EB.verify_archive(arch, against=rig.wiki)
    assert res["ok"], res["problems"]
    scratch = rig.root / "scratch"
    EB.extract_archive(arch, scratch)
    snap = scratch / "repo/_enrichment/store-v1.sqlite3"
    c = sqlite3.connect(snap)
    assert c.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 1
    assert c.execute("SELECT count(*) FROM annotation_revisions").fetchone()[0] == 7
    c.close()
    assert (scratch / "repo" / dirty_cap["path"]).read_bytes() == (rig.wiki / dirty_cap["path"]).read_bytes()
    for rel, rec in man["files"].items():
        if rec["kind"] == "file" and rel.startswith("repo/"):
            src = rig.wiki / rel[5:]
            if src.is_file():
                assert hashlib.sha256((scratch / rel).read_bytes()).hexdigest() == hashlib.sha256(src.read_bytes()).hexdigest(), rel
    assert leaked(o + json.dumps(man)) == []


def test_plain_copy_of_a_wal_database_is_not_a_backup_but_the_snapshot_is(rig, tmp_path):
    con = make_store(rig.wiki, rows=9)
    src = rig.wiki / "_enrichment/store-v1.sqlite3"
    assert Path(str(src) + "-wal").exists() and Path(str(src) + "-wal").stat().st_size > 0
    naive = tmp_path / "naive.sqlite3"
    shutil.copy(src, naive)
    try:
        n = sqlite3.connect(naive).execute("SELECT count(*) FROM annotation_revisions").fetchone()[0]
    except sqlite3.DatabaseError:
        n = 0
    snap = tmp_path / "snap.sqlite3"
    info = EB.snapshot_sqlite(src, snap)
    assert info["integrity"] == "ok" and info["user_version"] == 1 and info["journal_mode"] == "delete"
    assert sqlite3.connect(snap).execute("SELECT count(*) FROM annotation_revisions").fetchone()[0] == 9
    assert n < 9, "the naive copy unexpectedly contained the WAL rows; the test would prove nothing"
    assert not Path(str(snap) + "-wal").exists()
    con.close()


def test_backup_resumes_writer_even_when_the_snapshot_fails(rig, monkeypatch):
    make_store(rig.wiki)
    monkeypatch.setattr(EB, "snapshot_sqlite", lambda *a, **k: (_ for _ in ()).throw(EB.ToolError("boom")))
    dest = rig.root / "bk"
    rc, o, e = rig.run(*backup_args(rig, dest))
    rc, o, e = rig.run(*backup_args(rig, dest, "--execute", "--confirm", rig.token(o)))
    assert rc == 1 and "boom" in e
    assert rig.docker.pauses == ["pause:" + "1" * 12, "unpause:" + "1" * 12]
    assert not list(dest.glob("*.tar.gz")) and not [p for p in dest.iterdir() if p.name.startswith(".tmp")]


def test_backup_refuses_destination_inside_the_wiki(rig):
    rc, o, e = rig.run(*backup_args(rig, rig.wiki / "backups"))
    assert rc == 2 and "outside the wiki" in e


def test_backup_verify_detects_tampering_and_sidecar_mismatch(rig, tmp_path):
    import tarfile
    dest = rig.root / "bk"
    run_backup(rig, dest)
    arch = next(dest.glob("*.tar.gz"))
    assert EB.main(["verify", "--archive", str(arch)], out=io.StringIO()) == 0
    # same-size content change of one member, manifest untouched -> hash mismatch
    bad = tmp_path / "bad.tar.gz"
    with tarfile.open(arch) as src, tarfile.open(bad, "w:gz") as dst:
        for m in src:
            data = src.extractfile(m).read() if m.isreg() else None
            if m.name == "repo/CLAUDE.md":
                data = bytes(b ^ 1 if i == 10 else b for i, b in enumerate(data))
            dst.addfile(m, io.BytesIO(data) if data is not None else None)
    res = EB.verify_archive(bad)
    assert res["ok"] is False and any("hash mismatch repo/CLAUDE.md" in p for p in res["problems"])
    assert EB.main(["verify", "--archive", str(bad)], out=io.StringIO()) == EB.EXIT_UNVERIFIED
    # sidecar checksum disagrees
    shutil.copy(arch, tmp_path / "copy.tar.gz")
    (tmp_path / "copy.tar.gz.sha256").write_text("0" * 64 + "  copy.tar.gz\n")
    assert any("sha256 sidecar" in p for p in EB.verify_archive(tmp_path / "copy.tar.gz")["problems"])
    # a member that tries to escape the extraction directory is refused
    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(evil, "w:gz") as t:
        ti = tarfile.TarInfo("../escape.txt")
        ti.size = 1
        t.addfile(ti, io.BytesIO(b"x"))
    with pytest.raises(EB.ToolError):
        EB.extract_archive(evil, tmp_path / "out")
    assert not (tmp_path / "escape.txt").exists()


def test_backup_restore_only_into_new_dirs_and_never_force(rig, tmp_path):
    dest = rig.root / "bk"
    run_backup(rig, dest)
    arch = next(dest.glob("*.tar.gz"))
    new = tmp_path / "restored"
    out = io.StringIO()
    rc = EB.main(["restore", "--archive", str(arch), "--target", str(new), "--live-wiki", str(rig.wiki)], out=out)
    assert rc == 0 and "DRY RUN" in out.getvalue() and not new.exists()
    tok = re.search(r"CONFIRM-[0-9A-F]{16}", out.getvalue()).group(0)
    assert EB.main(["restore", "--archive", str(arch), "--target", str(new), "--live-wiki", str(rig.wiki), "--execute", "--confirm", tok], out=io.StringIO()) == 0
    assert (new / ".git/HEAD").exists() and (new / "CLAUDE.md").read_bytes() == (rig.wiki / "CLAUDE.md").read_bytes()
    # existing non-empty target, live wiki and nested live paths are refused
    for target in (new, rig.wiki, rig.wiki / "sub"):
        d2 = hashlib.sha256(json.dumps({"archive": EB.sha256_file(arch), "target": str(Path(target).resolve()), "state": None}, sort_keys=True,
                                      separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        rc = EB.main(["restore", "--archive", str(arch), "--target", str(target), "--live-wiki", str(rig.wiki), "--execute",
                      "--confirm", EB.confirm_token("restore", d2)], out=io.StringIO())
        assert rc == EB.EXIT_STOP, target
    with pytest.raises(SystemExit):
        EB.build_parser().parse_args(["restore", "--archive", "a", "--target", "t", "--force"])


def test_backup_pause_hook_runs_before_and_after_and_is_resumed(rig, tmp_path):
    logf = tmp_path / "hooks.log"
    for name in ("pause", "resume"):
        h = tmp_path / f"{name}-writer"
        h.write_text(f"#!/bin/sh\necho {name} >> {logf}\n")
        h.chmod(0o755)
    dest = rig.root / "bk"
    extra = ["--writer-pause", "hook", "--pause-hook", str(tmp_path / "pause-writer"), "--resume-hook", str(tmp_path / "resume-writer")]
    rc, o, e = run_backup(rig, dest, *extra)
    assert rc == 0, (o, e)
    assert logf.read_text().split() == ["pause", "resume"]
    assert rig.docker.pauses == [], "the hook replaces the docker pause"
    assert EB.read_manifest(next(dest.glob("*.tar.gz")))["writer_pause"]["mode"] == "operator-hook"
    # a relative or non-executable hook is refused
    rc, o, e = rig.run(*backup_args(rig, rig.root / "bk2", "--writer-pause", "hook", "--pause-hook", "pause.sh", "--resume-hook", "resume.sh"))
    assert rc == 2


def test_backup_without_a_container_says_the_writer_is_not_paused(rig):
    rig.docker.available = False
    dest = rig.root / "bk"
    rc, o, e = run_backup(rig, dest, "--writer-pause", "none")
    assert rc == 0 and rig.docker.pauses == []
    man = EB.read_manifest(next(dest.glob("*.tar.gz")))
    assert man["writer_pause"] == {"mode": "none", "paused": False}
    assert man["image"]["id"] is None, "UNVERIFIED facts must be recorded as absent, not invented"


def test_backup_aborts_on_a_file_that_changes_while_it_is_archived(rig, monkeypatch):
    target = rig.wiki / "HOME.md"
    real_read = EB._HashReader.read
    state = {"hit": False}

    def racing_read(self, n=-1):
        if not state["hit"] and getattr(self.fh, "name", "") == str(target):
            state["hit"] = True
            with open(target, "ab") as fh:
                fh.write(b"\nwriter appended while we read\n")
        return real_read(self, n)

    monkeypatch.setattr(EB._HashReader, "read", racing_read)
    dest = rig.root / "bk"
    rc, o, e = rig.run(*backup_args(rig, dest))
    rc, o, e = rig.run(*backup_args(rig, dest, "--execute", "--confirm", rig.token(o)))
    assert state["hit"] and rc == 1 and "changed while it was archived" in e
    assert not list(dest.glob("*.tar.gz")) and rig.docker.pauses[-1].startswith("unpause")


def test_snapshot_store_cli_is_dry_run_then_token_gated(rig, tmp_path):
    con = make_store(rig.wiki, rows=3)
    out = tmp_path / "snap" / "store.sqlite3"
    buf = io.StringIO()
    assert EB.main(["snapshot-store", "--wiki", str(rig.wiki), "--out", str(out)], out=buf) == 0 and not out.exists()
    tok = re.search(r"CONFIRM-[0-9A-F]{16}", buf.getvalue()).group(0)
    assert EB.main(["snapshot-store", "--wiki", str(rig.wiki), "--out", str(out), "--execute"], out=io.StringIO()) == 2
    assert EB.main(["snapshot-store", "--wiki", str(rig.wiki), "--out", str(rig.wiki / "inside.sqlite3"), "--execute", "--confirm", tok],
                   out=io.StringIO()) == 2                      # token is bound to the exact --out
    assert EB.main(["snapshot-store", "--wiki", str(rig.wiki), "--out", str(out), "--execute", "--confirm", tok], out=io.StringIO()) == 0
    con.close()
    assert sqlite3.connect(out).execute("SELECT count(*) FROM annotation_revisions").fetchone()[0] == 3
    assert (out.stat().st_mode & 0o777) == 0o600


# ================================================================== apply
def test_apply_is_copy_first_by_default_and_never_touches_live(rig):
    rc, o, e, plan_path = rig.plan()
    assert rc == 0
    before = tree_hashes(rig.wiki, rig.state, rig.proj)
    copy = rig.root / "copy"
    rc, o, e = rig.run("apply", "--wiki", rig.wiki, "--plan", plan_path, "--copy-dir", copy)
    assert rc == 0 and "COPY-FIRST OK" in o and "live tree untouched" in o
    assert tree_hashes(rig.wiki, rig.state, rig.proj) == before
    plan = json.loads(plan_path.read_text())
    for x in plan["paths"]:
        if x["action"] in ("add", "modify"):
            assert hashlib.sha256((copy / x["path"]).read_bytes()).hexdigest() == x["after_sha256"], x["path"]
    assert rig.docker.ups == [] and not rig.up_cmds()
    # without --copy-dir or without a token, the live tree is never written
    assert rig.run("apply", "--wiki", rig.wiki, "--plan", plan_path)[0] == 2
    assert rig.run("apply", "--wiki", rig.wiki, "--plan", plan_path, "--execute", "--work-dir", rig.root / "w")[0] == 2
    assert rig.run("apply", "--wiki", rig.wiki, "--plan", plan_path, "--execute", "--confirm", "CONFIRM-DEADBEEFDEADBEEF", "--work-dir", rig.root / "w")[0] == 2
    assert tree_hashes(rig.wiki, rig.state, rig.proj) == before


def test_apply_rejects_a_modified_plan_and_a_different_wiki(rig):
    rc, o, e, plan_path = rig.plan()
    p = json.loads(plan_path.read_text())
    p["paths"][0]["after_sha256"] = "0" * 64
    plan_path.write_text(json.dumps(p))
    assert rig.run("apply", "--wiki", rig.wiki, "--plan", plan_path, "--copy-dir", rig.root / "c")[0] == 2


def test_apply_live_writes_allowlist_hashes_and_restarts_only_brain(rig):
    rc, o, e, plan_path = rig.plan()
    plan = json.loads(plan_path.read_text())
    work = rig.root / "work"
    others_before = [(c.name, c.id, c.started) for c in rig.docker.containers if c.service != "brain"]
    env_before = rig.env_path.read_bytes()
    auth_before = hashlib.sha256((rig.state / "auth/oauth_state.json").read_bytes()).hexdigest()
    outside_before = {k: v for k, v in UP.corpus_manifest(rig.wiki)["files"].items()}
    rc, o, e = rig.live_apply(plan_path, work)
    assert rc == 0 and "APPLY OK" in o, (o, e)
    applied = json.loads((work / "applied.json").read_text())
    assert (work / "applied.json").stat().st_mode & 0o777 == 0o600
    for x in plan["paths"]:
        if x["action"] in ("add", "modify"):
            assert hashlib.sha256((rig.wiki / x["path"]).read_bytes()).hexdigest() == x["after_sha256"]
    assert code_paths(applied) <= {x["path"] for x in plan["paths"]}
    # exactly one compose up, brain only, discovered project/files/env in order
    ups = rig.up_cmds()
    assert len(ups) == 1
    up = ups[0]
    assert up[-5:] == ["up", "-d", "--no-deps", "--force-recreate", "brain"]
    assert up[:2] == ["docker", "compose"] and up[up.index("--project-name") + 1] == "0xbrain"
    fi = [up[i + 1] for i, a in enumerate(up) if a == "-f"]
    assert fi == [str(rig.proj / "compose.yaml"), str(rig.proj / "compose.override.yaml")]
    assert up[up.index("--env-file") + 1] == str(rig.env_path)
    # nothing else moved
    assert [(c.name, c.id, c.started) for c in rig.docker.containers if c.service != "brain"] == others_before
    assert rig.brain().id != "1" * 64                               # brain was recreated
    assert rig.env_path.read_bytes() == env_before                  # features stay off: no env edit
    assert hashlib.sha256((rig.state / "auth/oauth_state.json").read_bytes()).hexdigest() == auth_before
    assert UP.corpus_manifest(rig.wiki)["files"] == outside_before   # corpus bytes untouched
    assert not (rig.wiki / "_enrichment").exists()
    assert rig.docker.unexpected == []
    # root-created dirs were recorded
    assert "docs/enrichment" in applied["created_dirs"] and "harness/hermes" in applied["created_dirs"]
    assert (work / "preimage/scripts/brain_enrichment/config.py").is_file()


def test_apply_refuses_when_a_planned_file_changed_since_the_plan(rig):
    rc, o, e, plan_path = rig.plan()
    cfg = rig.wiki / "scripts/brain_enrichment/config.py"
    cfg.write_text(cfg.read_text() + "# local hotfix\n")
    before = tree_hashes(rig.wiki)
    rc, o, e = rig.live_apply(plan_path, rig.root / "work")
    assert rc == 3 and "changed since the plan" in e
    assert tree_hashes(rig.wiki) == before and not rig.docker.ups


def test_apply_is_all_or_nothing_when_a_write_fails_midway(rig, monkeypatch):
    rc, o, e, plan_path = rig.plan()
    before = tree_hashes(rig.wiki, mtime=False)
    dirs_before = sorted(p.relative_to(rig.wiki).as_posix() for p in rig.wiki.rglob("*") if p.is_dir() and ".git" not in p.parts)
    real, n = UP.atomic_write, {"n": 0}

    def flaky(deps, path, data, mode, owner):
        n["n"] += 1
        if n["n"] == 4:
            raise OSError("disk full (simulated)")
        return real(deps, path, data, mode, owner)

    monkeypatch.setattr(UP, "atomic_write", flaky)
    with pytest.raises(OSError):
        rig.live_apply(plan_path, rig.root / "work")
    monkeypatch.setattr(UP, "atomic_write", real)
    assert tree_hashes(rig.wiki, mtime=False) == before, "a failed apply must leave every file's content exactly as it was"
    assert sorted(p.relative_to(rig.wiki).as_posix() for p in rig.wiki.rglob("*") if p.is_dir() and ".git" not in p.parts) == dirs_before, "created dirs must be removed"
    assert not rig.docker.ups


def test_apply_refuses_when_compose_discovery_drifted(rig):
    rc, o, e, plan_path = rig.plan()
    rig.brain().config_files = str(rig.proj / "compose.yaml")        # an operator changed the running stack
    before = tree_hashes(rig.wiki)
    rc, o, e = rig.live_apply(plan_path, rig.root / "work")
    assert rc == 3 and "drifted" in e and tree_hashes(rig.wiki) == before


def test_apply_with_new_image_tag_builds_a_new_tag_and_keeps_the_old(rig):
    rc, o, e, plan_path = rig.plan(new_image_tag="0xbrain/brain:r1-test")
    assert rc == 0
    rc, o, e = rig.live_apply(plan_path, rig.root / "work")
    assert rc == 0, (o, e)
    assert len(rig.docker.builds) == 1 and rig.docker.builds[0][1] == {"BRAIN_IMAGE": "0xbrain/brain:r1-test"}
    assert rig.docker.images["0xbrain/brain:local"] == "sha256:" + "a" * 64          # old tag never overwritten
    env = EB.parse_env_file(rig.env_path)
    assert env["BRAIN_IMAGE"] == "0xbrain/brain:r1-test" and env["BRAIN_OWNER_SECRET"] == OWNER_SECRET
    assert (rig.env_path.stat().st_mode & 0o777) == 0o600
    assert rig.brain().image_tag == "0xbrain/brain:r1-test"
    applied = json.loads((rig.root / "work/applied.json").read_text())
    assert applied["env_edits"] == [{"key": "BRAIN_IMAGE", "old": "0xbrain/brain:local", "new": "0xbrain/brain:r1-test"}]


def test_apply_work_dir_must_be_outside_wiki_and_single_use(rig):
    rc, o, e, plan_path = rig.plan()
    rc, o, e = rig.run("apply", "--wiki", rig.wiki, "--plan", plan_path, "--copy-dir", rig.root / "c")
    tok = rig.token(o)
    assert rig.run("apply", "--wiki", rig.wiki, "--plan", plan_path, "--work-dir", rig.wiki / "w", "--execute", "--confirm", tok)[0] == 2
    work = rig.root / "work"
    assert rig.run("apply", "--wiki", rig.wiki, "--plan", plan_path, "--work-dir", work, "--execute", "--confirm", tok)[0] == 0
    assert rig.run("apply", "--wiki", rig.wiki, "--plan", plan_path, "--work-dir", work, "--execute", "--confirm", tok)[0] == 3


def test_stderr_secrets_in_failing_docker_commands_are_redacted_on_screen(rig):
    rc, o, e, plan_path = rig.plan()
    rig.docker.fail_up = f"error: BRAIN_OWNER_SECRET={STDERR_SECRET} Bearer {TOKEN_SECRET} refused"
    rc, o, e = rig.live_apply(plan_path, rig.root / "work")
    assert rc == 1 and "failed (rc=1)" in e
    assert leaked(o + e) == [] and "[REDACTED]" in e
    assert "rollback --level 2" in o                                   # the way back is printed
    assert (rig.wiki / "scripts/brain_enrichment/store.py").exists()  # code was written; rollback will remove it


def test_apply_flags_a_newly_published_port_as_failure(rig):
    rc, o, e, plan_path = rig.plan()
    rig.docker.port_on_up = True
    rc, o, e = rig.live_apply(plan_path, rig.root / "work")
    assert rc == 1 and "NEW PORT EXPOSURE" in o


def test_apply_and_rollback_refuse_without_a_reachable_docker(rig):
    rc, o, e, plan_path = rig.plan()
    rig.docker.available = False
    before = tree_hashes(rig.wiki)
    rc, o, e = rig.live_apply(plan_path, rig.root / "work")
    assert rc == 3 and "docker is not reachable" in e and tree_hashes(rig.wiki) == before


def test_preflight_reports_release_ancestry(rig):
    rc, o, e, p = preflight(rig, "--target-sha", rig.target, "--base-sha", rig.base, "--release-repo", rig.release)
    anc = json.loads(p.read_text())["release"]["ancestry"]
    assert anc == {"base_is_ancestor_of_target": True, "head_contains_base": True, "head_contains_target": None}
    other = commit_files(rig.release, {"scripts/brain_enrichment/x.py": "x=1\n"}, "unrelated tip")
    git(rig.release, "checkout", "-q", "--detach", rig.base)
    side = commit_files(rig.release, {"scripts/brain_enrichment/y.py": "y=1\n"}, "sibling")
    rc, o, e, p = preflight(rig, "--target-sha", side, "--base-sha", other, "--release-repo", rig.release)
    assert rc == 3 and "not an ancestor" in o


def test_copy_first_rejects_dirty_or_nested_copy_dirs(rig):
    rc, o, e, plan_path = rig.plan()
    full = rig.root / "full"
    full.mkdir()
    (full / "x").write_text("x")
    assert rig.run("apply", "--wiki", rig.wiki, "--plan", plan_path, "--copy-dir", full)[0] == 2
    assert rig.run("apply", "--wiki", rig.wiki, "--plan", plan_path, "--copy-dir", rig.wiki / "copy")[0] == 2
    assert not (rig.wiki / "copy").exists()


def test_rollback_rehearsal_on_a_copy_runs_no_docker_and_never_touches_live(rig):
    rc, o, e, plan_path = rig.plan()
    copy = rig.root / "copy"
    assert rig.run("apply", "--wiki", rig.wiki, "--plan", plan_path, "--copy-dir", copy)[0] == 0
    live_before = tree_hashes(rig.wiki, rig.state, rig.proj)
    n = len(rig.runner.log)
    work = rig.root / "copy.work"
    rc, o, e = rig.run("rollback", "--wiki", rig.wiki, "--level", 2, "--plan", plan_path, "--work-dir", work, "--copy-dir", copy)
    assert rc == 0 and "DRY RUN" in o
    rc, o, e = rig.run("rollback", "--wiki", rig.wiki, "--level", 2, "--plan", plan_path, "--work-dir", work, "--copy-dir", copy,
                       "--execute", "--confirm", rig.token(o))
    assert rc == 0 and "ROLLBACK OK" in o, (o, e)
    plan = json.loads(plan_path.read_text())
    for x in plan["paths"]:
        if x["before_sha256"]:
            assert hashlib.sha256((copy / x["path"]).read_bytes()).hexdigest() == x["before_sha256"], x["path"]
        else:
            assert not (copy / x["path"]).exists()
    assert tree_hashes(rig.wiki, rig.state, rig.proj) == live_before
    assert not [c for c in rig.runner.log[n:] if c[:2] == ["docker", "compose"] and "up" in c], "a rehearsal must not restart anything"
    assert not rig.docker.ups


def test_ownership_is_set_per_written_path_never_recursively(rig, monkeypatch):
    calls = []
    monkeypatch.setattr(os, "chown", lambda path, uid, gid: calls.append(str(path)))
    rig.deps.geteuid = lambda: 0                                      # exercise the root-only branch
    rc, o, e, plan_path = rig.plan()
    plan = json.loads(plan_path.read_text())
    rc, o, e = rig.live_apply(plan_path, rig.root / "work")
    assert rc == 0, (o, e)
    assert calls, "running as root must set per-path ownership"
    released = {x["path"] for x in plan["paths"]}
    created_dirs = set(json.loads((rig.root / "work/applied.json").read_text())["created_dirs"])
    for c in calls:
        rel = Path(c).relative_to(rig.wiki).as_posix() if str(c).startswith(str(rig.wiki)) else Path(c).relative_to(rig.proj).as_posix()
        parent = str(Path(rel).parent)
        assert rel in created_dirs or parent in {str(Path(p).parent) for p in released} | created_dirs, f"chown outside the release paths: {rel}"
    assert str(rig.wiki) not in calls and not any(c in (str(rig.state), str(rig.state / "auth")) for c in calls)
    assert len(calls) <= 2 * len(released) + len(created_dirs) + 2      # linear in the release, not in the corpus size


def test_cli_entry_points_run_as_real_processes():
    for script in ("upgrade-enrichment.py", "enrichment-backup.py"):
        assert os.access(SCRIPTS / script, os.X_OK), f"{script} must be executable"
        r = subprocess.run([sys.executable, str(SCRIPTS / script), "--help"], capture_output=True, text=True)
        assert r.returncode == 0 and "usage" in r.stdout.lower()
    for sub in ("preflight", "plan", "backup", "apply", "verify", "rollback"):
        r = subprocess.run([sys.executable, str(SCRIPTS / "upgrade-enrichment.py"), sub, "--help"], capture_output=True, text=True)
        assert r.returncode == 0 and "--wiki" in r.stdout, sub
    r = subprocess.run([sys.executable, str(SCRIPTS / "upgrade-enrichment.py"), "apply", "--wiki", "/nonexistent", "--plan", "/nonexistent"],
                       capture_output=True, text=True)
    assert r.returncode == 2 and "not a git checkout" in r.stderr


# ================================================================== verify
def baseline(rig):
    rc, o, e, p = preflight(rig, "--target-sha", rig.target, "--base-sha", rig.base, "--release-repo", rig.release)
    assert rc == 0, o
    return p


def verify(rig, base_path, *extra):
    out = rig.root / "verify.json"
    rc, o, e = rig.run("verify", "--wiki", rig.wiki, "--baseline", base_path, "--out", out, *extra)
    return rc, o, e, json.loads(out.read_text())


def level(rep, area):
    return [i["level"] for i in rep["findings"] if i["area"] == area]


def test_verify_all_gates_pass_after_a_clean_apply(rig):
    base = baseline(rig)
    rc, o, e, p = rig.plan()
    assert rig.live_apply(p, rig.root / "work")[0] == 0
    tokfile = rig.root / "t.txt"
    tokfile.write_text(TOKEN_SECRET)
    rig.http.calls.clear()
    # new legit writes after the baseline: a capture and an appended proposal
    new_cap = wc.capture_text("a brand new capture after the upgrade", "mcp", "en")
    with open(rig.wiki / "_proposals/proposals.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"id": "prop-after", "kind": "note", "status": "new", "body": "x"}) + "\n")
    rc, o, e, rep = verify(rig, base, "--token-file", tokfile, "--applied", rig.root / "work/applied.json", "--read-ref", "rec:obj-nil")
    assert rc == 0, o
    for area in ("container", "health-internal", "health-public", "tools", "auth", "config", "features", "code", "sources", "no-touch", "old-ref-read"):
        assert level(rep, area) == ["PASS"], (area, [i for i in rep["findings"] if i["area"] == area])
    assert level(rep, "capture-probe") == ["SKIP"] and level(rep, "fixture-ingest") == ["SKIP"]
    assert rep["sources"]["added"] >= 1 and rep["sources"]["appended"] == 1
    # health went to the DISCOVERED public URL, not a caddy/BRAIN_DOMAIN helper
    assert ("GET", PUBLIC_URL + "/healthz") in rig.http.calls and ("POST", PUBLIC_URL + "/mcp") in rig.http.calls
    assert not any("example.test:443" in u for _, u in rig.http.calls)
    assert leaked(o + json.dumps(rep)) == []
    assert new_cap["path"]


def test_verify_tool_list_must_equal_the_baseline_seven_when_off(rig):
    base = baseline(rig)
    tokfile = rig.root / "t.txt"
    tokfile.write_text(TOKEN_SECRET)
    rig.http.tools = list(UP.BASELINE_TOOLS) + ["brain_retrieve"]
    rc, o, e, rep = verify(rig, base, "--token-file", tokfile)
    assert rc == 1 and level(rep, "tools") == ["STOP"] and "brain_retrieve" in o
    rig.http.tools = list(UP.BASELINE_TOOLS) + list(UP.ENRICHMENT_TOOLS)
    rc, o, e, rep = verify(rig, base, "--token-file", tokfile, "--expect-features", "on")
    assert level(rep, "tools") == ["PASS"]
    rig.http.tools = list(UP.BASELINE_TOOLS)[:-1]
    rc, o, e, rep = verify(rig, base, "--token-file", tokfile)
    assert level(rep, "tools") == ["STOP"]


def test_verify_without_credentials_reports_skip_not_pass(rig):
    base = baseline(rig)
    rc, o, e, rep = verify(rig, base)
    assert level(rep, "tools") == ["SKIP"] and level(rep, "old-ref-read") == ["SKIP"]
    assert rc == 0 and "SKIP" in o


def test_verify_fails_on_source_mutation_or_removal_but_allows_new_writes(rig):
    base = baseline(rig)
    orig = next(p for p in sorted(rig.wiki.glob("_originals/**/*")) if p.is_file())
    orig.write_bytes(orig.read_bytes() + b"tamper")
    rc, o, e, rep = verify(rig, base)
    assert rc == 1 and level(rep, "sources") == ["STOP"] and rep["sources_names"]["originals_changed"]
    orig_text = rep["sources_names"]["originals_changed"][0]
    assert orig_text.startswith("_originals/")
    base2 = baseline(rig)
    victim = next(p for p in sorted((rig.wiki / "01-inbox/captures").rglob("*.md")) if p.is_file())
    victim.unlink()
    rc, o, e, rep = verify(rig, base2)
    assert rc == 1 and rep["sources_names"]["removed"]


def test_verify_modified_nonoriginal_is_a_warning_for_review(rig):
    base = baseline(rig)
    cap = next(p for p in sorted((rig.wiki / "01-inbox/captures").rglob("*.md")) if p.is_file())
    cap.write_text(cap.read_text() + "\nextra\n")
    rc, o, e, rep = verify(rig, base)
    assert level(rep, "sources") == ["WARN"] and rc == 0


def test_verify_no_touch_detects_changes_to_other_services_and_ports(rig):
    base = baseline(rig)
    rig.docker.containers[1].started = "2026-10-10T09:09:09Z"        # nginx restarted
    rc, o, e, rep = verify(rig, base)
    assert rc == 1 and level(rep, "no-touch") == ["STOP"]
    rig.docker.containers[1].started = "2026-10-01T00:00:00Z"
    rig.docker.containers[1].ports = {"443/tcp": [{"HostIp": "0.0.0.0", "HostPort": "443"}], "9999/tcp": [{"HostIp": "0.0.0.0", "HostPort": "9999"}]}
    rc, o, e, rep = verify(rig, base)
    assert level(rep, "no-touch")[0] == "STOP"


def test_verify_auth_state_unchanged_pass_changed_warn_missing_fail(rig):
    base = baseline(rig)
    rc, o, e, rep = verify(rig, base)
    assert level(rep, "auth") == ["PASS"]
    af = rig.state / "auth/oauth_state.json"
    af.write_text(af.read_text() + " ")
    rc, o, e, rep = verify(rig, base)
    assert level(rep, "auth") == ["WARN"] and rc == 0
    af.unlink()
    rc, o, e, rep = verify(rig, base)
    assert level(rep, "auth") == ["STOP"] and rc == 1


def test_verify_unhealthy_public_endpoint_fails_and_config_drift_fails(rig):
    base = baseline(rig)
    rig.http.health_ok = False
    rc, o, e, rep = verify(rig, base)
    assert rc == 1 and level(rep, "health-public") == ["STOP"]
    rig.http.health_ok = True
    txt = rig.env_path.read_text().replace("https://claude.ai/api/mcp/auth_callback", "https://elsewhere.example/cb")
    rig.env_path.write_text(txt)
    rc, o, e, rep = verify(rig, base)
    assert level(rep, "config") == ["STOP"]


def test_verify_features_must_be_off(rig):
    base = baseline(rig)
    rig.env_path.write_text(rig.env_path.read_text() + "BRAIN_ENRICHMENT=on\n")
    rc, o, e, rep = verify(rig, base)
    assert level(rep, "features") == ["STOP"]


# ================================================================== rollback
def applied_rig(rig, **plan_kw):
    rc, o, e, plan_path = rig.plan(**plan_kw)
    assert rc == 0, o
    work = rig.root / "work"
    assert rig.live_apply(plan_path, work)[0] == 0
    return plan_path, work


def rb(rig, plan_path, work, *extra, level=2):
    return rig.run("rollback", "--wiki", rig.wiki, "--level", level, "--plan", plan_path, "--work-dir", work, *extra)


def rollback_exec(rig, plan_path, work, level=2, *extra):
    rc, o, e = rb(rig, plan_path, work, *extra, level=level)
    assert rc == 0 and "DRY RUN" in o, (o, e)
    return rb(rig, plan_path, work, *extra, "--execute", "--confirm", rig.token(o), level=level)


def test_rollback_level2_restores_exact_pre_release_state_and_keeps_data(rig):
    before_all = tree_hashes(rig.wiki)
    plan_path, work = applied_rig(rig)
    # post-upgrade activity that MUST survive: a capture, an ingest, an appended proposal, a store, auth refresh
    cap_after = wc.capture_text("capture made after the upgrade, before rollback", "mcp", "en")
    from support import add_held_file
    held = add_held_file(rig.wiki, "zb-src-dddddddddddd", "after.txt", b"after bytes\n", "# Extracted\n\n> method: fixture\n\nafter upgrade text\n")
    with open(rig.wiki / "_proposals/proposals.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"id": "prop-post-upgrade", "kind": "note", "status": "new", "body": "post"}) + "\n")
    con = make_store(rig.wiki, rows=3)
    (rig.state / "auth/oauth_state.json").write_text(json.dumps({"clients": {}, "marker": SENTINEL_AUTH, "rotated": True}))
    preserved = {k: v for k, v in tree_hashes(rig.wiki, rig.state, rig.proj).items()
                 if not any(k.split(":", 1)[1].startswith(p) for p in [*RELEASE_ADDS, "CLAUDE.md", ".env.example", ".gitignore",
                                                                       "scripts/brain_enrichment/config.py", "scripts/remote_mcp/adapter.py"])
                 and not k.split(":", 1)[1].startswith(("docs/enrichment", "tests/enrichment", "harness", ".git/"))}
    rc, o, e = rb(rig, plan_path, work)
    assert rc == 0 and "DRY RUN" in o and "restore-preimage" in o and "remove-new-file" in o
    unchanged_by_dry = tree_hashes(rig.wiki, rig.state, rig.proj)
    assert {k: v for k, v in unchanged_by_dry.items() if k in preserved} == preserved
    ups_before = len(rig.up_cmds())
    rc, o, e = rb(rig, plan_path, work, "--execute", "--confirm", rig.token(o))
    assert rc == 0 and "ROLLBACK OK" in o, (o, e)
    # code and shared files are back to the exact pre-release bytes
    plan = json.loads(plan_path.read_text())
    for x in plan["paths"]:
        live = rig.wiki / x["path"]
        if x["before_sha256"] is None:
            assert not live.exists(), x["path"]
        else:
            assert hashlib.sha256(live.read_bytes()).hexdigest() == x["before_sha256"], x["path"]
    assert not (rig.wiki / "docs").exists() and not (rig.wiki / "harness").exists() and not (rig.wiki / "tests").exists()
    # everything else (corpus, post-upgrade writes, queue, registers, auth, overrides, store) is untouched by the rollback
    after = tree_hashes(rig.wiki, rig.state, rig.proj)
    con.close()
    changed = {k for k in preserved if after.get(k) != preserved[k]}
    assert not changed, changed
    assert len(rig.up_cmds()) == ups_before + 1 and rig.up_cmds()[-1][-1] == "brain"
    # both the pre-upgrade corpus and the post-upgrade inputs stay readable through the real surface
    from brain_surface import BrainSurface
    from brain_surface.backend import WikiBackend
    s = BrainSurface(WikiBackend(rig.wiki))
    for ref in (rig.seeded.refs["fa_short"], f"cap:{Path(cap_after['path']).stem}", f"rec:{held['rid']}"):
        r = s.call("brain_read", {"ref": ref})
        assert r.get("ok") is True, (ref, r)
    assert (rig.state / "auth/oauth_state.json").read_text().count("rotated") == 1
    names_after = {k.split(":", 1)[1] for k in tree_hashes(rig.wiki)}
    assert {k.split(":", 1)[1] for k in before_all if ".git/" not in k.split(":", 1)[1][:5] and not k.split(":", 1)[1].startswith(".git/")} <= names_after, \
        "a file that existed before the release is gone after the rollback"


def test_rollback_level2_refuses_when_any_file_changed_after_release(rig):
    plan_path, work = applied_rig(rig)
    cfg = rig.wiki / "scripts/brain_enrichment/config.py"
    cfg.write_text(cfg.read_text() + "# later change by the owner\n")
    new_file = rig.wiki / "scripts/brain_enrichment/store.py"
    new_file.write_text(new_file.read_text() + "# later edit of a release-created file\n")
    snapshot = tree_hashes(rig.wiki, rig.state, rig.proj)
    rc, o, e = rb(rig, plan_path, work)
    assert rc == 3 and "STOP" in o and "changed after the release" in o and "NOTHING was changed" in o
    assert "scripts/brain_enrichment/config.py" in o and "scripts/brain_enrichment/store.py" in o
    assert tree_hashes(rig.wiki, rig.state, rig.proj) == snapshot
    # no token is offered and a guessed one is not accepted
    assert "CONFIRM-" not in o
    rc, o, e = rb(rig, plan_path, work, "--execute", "--confirm", "CONFIRM-0123456789ABCDEF")
    assert rc == 3 and tree_hashes(rig.wiki, rig.state, rig.proj) == snapshot
    assert len(rig.up_cmds()) == 1                                    # only the apply restart; no rollback restart


def test_rollback_level2_instance_marker_files_use_bounded_hunk_revert(rig):
    plan_path, work = applied_rig(rig)
    claude = rig.wiki / "CLAUDE.md"
    text = claude.read_text()
    later = text.replace("- kit version: `1.2.0`", "- kit version: `1.2.0` (owner annotated later)")
    assert later != text
    claude.write_text(later)                                           # a later, non-overlapping owner edit
    rc, o, e = rb(rig, plan_path, work)
    assert "reverse-merge" in o and rc == 0
    rc, o, e = rollback_exec(rig, plan_path, work)
    assert rc == 0, (o, e)
    final = claude.read_text()
    assert "owner annotated later" in final, "later owner edit must survive"
    assert "Enrichment is a derived sidecar" not in final, "the release hunk must be reverted"
    assert "Registered source artifacts: 7 (instance" in final


def test_rollback_level2_marker_overlap_is_a_stop(rig):
    plan_path, work = applied_rig(rig)
    claude = rig.wiki / "CLAUDE.md"
    claude.write_text(claude.read_text().replace("- Enrichment is a derived sidecar (docs/enrichment/CONTRACT-E1.md).",
                                                 "- Enrichment is a derived sidecar (owner rewrote this line)."))
    snap = tree_hashes(rig.wiki)
    rc, o, e = rb(rig, plan_path, work)
    assert rc == 3 and "cannot be reverted cleanly" in o and tree_hashes(rig.wiki) == snap


def test_rollback_level2_restores_the_previous_image_tag(rig):
    plan_path, work = applied_rig(rig, new_image_tag="0xbrain/brain:r1-test")
    assert EB.parse_env_file(rig.env_path)["BRAIN_IMAGE"] == "0xbrain/brain:r1-test"
    rc, o, e = rollback_exec(rig, plan_path, work)
    assert rc == 0, (o, e)
    assert EB.parse_env_file(rig.env_path)["BRAIN_IMAGE"] == "0xbrain/brain:local"
    assert "0xbrain/brain:r1-test" in rig.docker.images and "0xbrain/brain:local" in rig.docker.images, "no image is ever deleted"
    assert EB.parse_env_file(rig.env_path)["BRAIN_OWNER_SECRET"] == OWNER_SECRET and (rig.env_path.stat().st_mode & 0o777) == 0o600
    assert rig.brain().image_tag == "0xbrain/brain:local"


def test_rollback_level2_leaves_an_operator_changed_image_pin_alone(rig):
    plan_path, work = applied_rig(rig, new_image_tag="0xbrain/brain:r1-test")
    rig.env_path.write_text(rig.env_path.read_text().replace("0xbrain/brain:r1-test", "0xbrain/brain:operator-choice"))
    rc, o, e = rollback_exec(rig, plan_path, work)
    assert rc == 0 and "was changed after the release; left as is" in o
    assert EB.parse_env_file(rig.env_path)["BRAIN_IMAGE"] == "0xbrain/brain:operator-choice"


def test_rollback_level1_turns_features_off_and_restarts_only_brain(rig):
    rc, o, e, plan_path = rig.plan()
    rig.live_apply(plan_path, rig.root / "work")
    rig.env_path.write_text(rig.env_path.read_text() + "BRAIN_ENRICHMENT=on\nBRAIN_ENRICHMENT_WRITES=1\nBRAIN_ENRICHMENT_RETRIEVAL=1\n")
    before_lines = rig.env_path.read_text().splitlines()
    code_before = tree_hashes(rig.wiki)
    n_ups = len(rig.up_cmds())
    rc, o, e = rb(rig, plan_path, rig.root / "work", level=1)
    assert rc == 0 and "set BRAIN_ENRICHMENT" in o and "DRY RUN" in o
    rc, o, e = rb(rig, plan_path, rig.root / "work", "--execute", "--confirm", rig.token(o), level=1)
    assert rc == 0 and "FEATURES OFF" in o, (o, e)
    env = EB.parse_env_file(rig.env_path)
    assert (env["BRAIN_ENRICHMENT"], env["BRAIN_ENRICHMENT_WRITES"], env["BRAIN_ENRICHMENT_RETRIEVAL"]) == ("off", "0", "0")
    after_lines = rig.env_path.read_text().splitlines()
    assert [l for l in after_lines if not l.startswith("BRAIN_ENRICHMENT")] == [l for l in before_lines if not l.startswith("BRAIN_ENRICHMENT")]
    assert (rig.env_path.stat().st_mode & 0o777) == 0o600
    assert tree_hashes(rig.wiki) == code_before, "level 1 must not touch the wiki"
    assert len(rig.up_cmds()) == n_ups + 1 and rig.up_cmds()[-1][-5:] == ["up", "-d", "--no-deps", "--force-recreate", "brain"]
    assert leaked(o) == []


def test_rollback_level3_is_explicit_preserves_inputs_first_and_never_touches_the_stack(rig, tmp_path):
    con = make_store(rig.wiki, rows=4)
    dest = rig.root / "bk"
    run_backup(rig, dest)
    arch = next(dest.glob("*.tar.gz"))
    post = wc.capture_text("input arriving AFTER the backup and before the disaster", "mcp", "en")
    from support import add_held_file
    add_held_file(rig.wiki, "zb-src-eeeeeeeeeeee", "post.txt", b"post bytes\n", "# Extracted\n\n> method: fixture\n\nbody\n")
    con.execute("INSERT INTO annotation_revisions(source_ref, body) VALUES ('cap:post','y')")
    con.commit()
    plan_path = rig.plan()[3]
    restore = rig.root / "restored"
    work = rig.root / "w3"
    base_args = ["rollback", "--wiki", rig.wiki, "--level", 3, "--work-dir", work]
    # level 3 without the explicit flags is refused
    assert rig.run(*base_args)[0] == 2
    args = [*base_args, "--disaster-restore", "--archive", arch, "--restore-dir", restore]
    live_before = tree_hashes(rig.wiki, rig.state, rig.proj)
    n_runner = len(rig.runner.log)
    rc, o, e = rig.run(*args)
    assert rc == 0 and "DRY RUN" in o and "SALVAGED" in o and not restore.exists()
    plain_tok = rig.token(o)
    rc, o, e = rig.run(*args, "--replay-inputs")
    replay_tok = rig.token(o)
    assert replay_tok != plain_tok
    assert rig.run(*args, "--replay-inputs", "--execute", "--confirm", plain_tok)[0] == 2        # token is bound to the exact flags
    assert not restore.exists()
    rc, o, e = rig.run(*args, "--replay-inputs", "--execute", "--confirm", replay_tok)
    assert rc == 0 and "DISASTER RESTORE OK" in o, (o, e)
    assert "replayed" in o
    live_after = tree_hashes(rig.wiki, rig.state, rig.proj)
    con.close()
    assert {k: v for k, v in live_after.items() if "_enrichment" not in k} == {k: v for k, v in live_before.items() if "_enrichment" not in k}, "level 3 touched the live tree"
    assert (rig.wiki / post["path"]).exists()
    # salvage holds the post-backup inputs (including a fresh store snapshot)
    salv = next(work.glob("post-backup-inputs-*"))
    assert (salv / "files" / post["path"]).read_bytes() == (rig.wiki / post["path"]).read_bytes()
    snaps = list(salv.glob("enrichment-*.sqlite3"))
    assert snaps and sqlite3.connect(snaps[0]).execute("SELECT count(*) FROM annotation_revisions").fetchone()[0] == 5
    # the restored tree is the backup's state plus the add-only replay of the salvaged inputs
    assert (restore / ".git/HEAD").exists() and (restore / post["path"]).read_bytes() == (rig.wiki / post["path"]).read_bytes()
    # no stack command was issued by level 3
    new_cmds = rig.runner.log[n_runner:]
    assert not [c for c in new_cmds if c[:2] == ["docker", "compose"]]
    # refusing to restore over the live wiki or an existing dir
    snap_work = sorted(p.name for p in work.iterdir())
    rc, o, e = rig.run(*base_args, "--disaster-restore", "--archive", arch, "--restore-dir", rig.wiki)
    assert rc == EB.EXIT_STOP and "not empty" in e
    rc, o, e = rig.run(*base_args, "--disaster-restore", "--archive", arch, "--restore-dir", rig.wiki / "inside-live")
    assert rc == EB.EXIT_STOP and "separate from the live wiki" in e and not (rig.wiki / "inside-live").exists()
    rc, o, e = rig.run(*base_args, "--disaster-restore", "--archive", arch, "--restore-dir", restore)
    assert rc == EB.EXIT_STOP and "not empty" in e
    assert sorted(p.name for p in work.iterdir()) == snap_work, "a refused level 3 must not salvage or write anything"


# ================================================================== end-to-end rehearsal
def test_end_to_end_rehearsal_on_a_copy_capture_apply_capture_rollback_both_readable(rig):
    from brain_surface import BrainSurface
    from brain_surface.backend import WikiBackend
    from support import add_held_file
    s = BrainSurface(WikiBackend(rig.wiki))
    store = make_store(rig.wiki, rows=2)
    # 1. preflight baseline + backup, then capture + ingest BEFORE the upgrade
    def validators():
        r = subprocess.run([sys.executable, "scripts/validate_repo.py", "--full"], cwd=rig.wiki, capture_output=True, text=True,
                           env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        return r.returncode, sorted(l for l in r.stdout.splitlines() if l.startswith("ERROR"))

    validators_before = validators()           # the seeded fixture has its own baseline errors; the point is: no NEW ones
    base = baseline(rig)
    dest = rig.root / "bk"
    run_backup(rig, dest)
    cap_before = wc.capture_text("e2e capture BEFORE the upgrade", "mcp", "en")
    held_before = add_held_file(rig.wiki, "zb-src-111111111111", "before.txt", b"before bytes\n", "# Extracted\n\n> method: fixture\n\nbefore body\n")
    ref_before = f"cap:{Path(cap_before['path']).stem}"
    # 2. plan, copy-first rehearsal, live apply
    rc, o, e, plan_path = rig.plan()
    assert rc == 0
    assert rig.run("apply", "--wiki", rig.wiki, "--plan", plan_path, "--copy-dir", rig.root / "copy")[0] == 0
    work = rig.root / "work"
    assert rig.live_apply(plan_path, work)[0] == 0
    # 3. verify, then capture + ingest AFTER the upgrade (new code deployed, features off)
    rc, o, e, rep = verify(rig, base, "--applied", work / "applied.json")
    assert rc == 0, o
    cap_after = wc.capture_text("e2e capture AFTER the upgrade", "mcp", "en")
    held_after = add_held_file(rig.wiki, "zb-src-222222222222", "after.txt", b"after bytes\n", "# Extracted\n\n> method: fixture\n\nafter body\n")
    ref_after = f"cap:{Path(cap_after['path']).stem}"
    # 4. rollback of code only
    rc, o, e = rollback_exec(rig, plan_path, work)
    assert rc == 0, (o, e)
    store.close()
    # 5. both captures and both ingests are still readable, bytes unchanged
    for ref in (ref_before, ref_after, f"rec:{held_before['rid']}", f"rec:{held_after['rid']}"):
        r = s.call("brain_read", {"ref": ref})
        assert r.get("ok") is True, (ref, r)
    assert "e2e capture BEFORE the upgrade" in json.dumps(s.call("brain_read", {"ref": ref_before}), ensure_ascii=False)
    assert "e2e capture AFTER the upgrade" in json.dumps(s.call("brain_read", {"ref": ref_after}), ensure_ascii=False)
    assert (rig.wiki / held_after["orig"]).read_bytes() == b"after bytes\n"
    # 6. the code is the pre-release code again, the enrichment store and its data were never touched
    plan = json.loads(plan_path.read_text())
    for x in plan["paths"]:
        if x["before_sha256"]:
            assert hashlib.sha256((rig.wiki / x["path"]).read_bytes()).hexdigest() == x["before_sha256"]
        else:
            assert not (rig.wiki / x["path"]).exists()
    c = sqlite3.connect(rig.wiki / "_enrichment/store-v1.sqlite3")
    assert c.execute("SELECT count(*) FROM annotation_revisions").fetchone()[0] == 2
    c.close()
    # 7. the repo validators answer exactly as they did before the rehearsal (no new errors)
    assert validators() == validators_before
    # 8. across the whole rehearsal no forbidden command was ever generated
    assert_no_forbidden_commands(rig.runner.log)
    assert rig.docker.unexpected == []


# ================================================================== command generation / static guarantees
def assert_no_forbidden_commands(log_):
    for argv in log_:
        flat = " ".join(argv)
        exe = os.path.basename(argv[0])
        assert exe in ("git", "docker", "ss") or argv[0] == sys.executable, argv
        assert "chown" not in flat and "-R" not in argv and "--recursive" not in argv
        if exe == "git":
            sub = next(a for a in argv[1:] if not a.startswith("-") and a not in ("safe.directory=*", "core.quotepath=off") and not a.startswith("/"))
            assert sub not in ("reset", "clean", "stash", "checkout", "revert", "rebase", "merge", "pull", "push", "commit", "restore"), argv
        if exe == "docker" and argv[1] == "compose":
            sub = next(a for a in argv[2:] if a in ("config", "ps", "up", "build", "down", "restart", "stop", "rm", "run", "kill", "exec"))
            assert sub in ("config", "ps", "up", "build"), argv
            if sub == "up":
                assert "--no-deps" in argv and "-d" in argv and argv[-1] == "brain" and "--remove-orphans" not in argv
                assert not any(a in ("-p", "--publish") or re.fullmatch(r"\d+:\d+", a) for a in argv[argv.index("up"):])
        if exe == "docker" and argv[1] != "compose":
            assert argv[1] in ("info", "ps", "inspect", "image", "pause", "unpause", "exec"), argv


def test_full_flow_generates_only_allowed_commands(rig):
    base = baseline(rig)
    run_backup(rig, rig.root / "bk")
    rc, o, e, plan_path = rig.plan()
    rig.live_apply(plan_path, rig.root / "work")
    verify(rig, base)
    rollback_exec(rig, plan_path, rig.root / "work")
    rb(rig, plan_path, rig.root / "work", level=1)
    assert rig.runner.log, "nothing ran"
    assert_no_forbidden_commands(rig.runner.log)
    assert all(c[-1] == "brain" for c in rig.up_cmds())
    assert not [c for c in rig.runner.log if c[:2] == ["docker", "compose"] and "down" in c]


DANGEROUS_LITERALS = {"reset", "clean", "stash", "down", "--hard", "--force", "-R", "-rf", "prune", "rm", "chown -R"}


@pytest.mark.parametrize("fname", ["upgrade-enrichment.py", "enrichment-backup.py"])
def test_forbidden_operations_are_absent_from_the_source(fname):
    src = (SCRIPTS / fname).read_text()
    tree = ast.parse(src)
    guard_ranges = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in ("_forbidden", "guard_command"):
            guard_ranges.append((node.lineno, node.end_lineno))
    outside = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in DANGEROUS_LITERALS:
            if not any(a <= node.lineno <= b for a, b in guard_ranges):
                outside.append((node.lineno, node.value))
    assert outside == [], f"dangerous literals used outside the guard's refusal table: {outside}"
    # os.chown only in single-path helpers; nothing walks a tree to chown it
    chown_in = set()
    for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef,))]:
        for n in ast.walk(fn):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "chown":
                chown_in.add(fn.name)
    assert chown_in <= {"ensure_dirs", "atomic_write", "extract_archive"}, chown_in
    assert not re.search(r"shell\s*=\s*True|os\.system|os\.popen", src)
    assert "--force\"" not in src.replace("--force-recreate", "")


def test_no_force_flag_on_any_restore_surface():
    for parser in (EB.build_parser(), UP.build_parser()):
        text = []

        def walk(p):
            for a in p._actions:
                text.extend(a.option_strings)
                if hasattr(a, "choices") and isinstance(a.choices, dict):
                    for sp in a.choices.values():
                        walk(sp)
        walk(parser)
        assert "--force" not in text and "--hard" not in text and "-f" not in text


def test_scripts_import_nothing_beyond_the_standard_library():
    std = set(sys.stdlib_module_names)
    for fname in ("upgrade-enrichment.py", "enrichment-backup.py"):
        tree = ast.parse((SCRIPTS / fname).read_text())
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.level == 0:
                mods.add(n.module.split(".")[0])
        extra = mods - std - {"brain_enrichment"}                 # lazily imported, optional (EnrichmentConfig)
        assert not extra, (fname, extra)


def test_baseline_tool_names_match_the_frozen_surface():
    from brain_surface import contract as C
    from brain_enrichment import tools_spec
    assert sorted(UP.BASELINE_TOOLS) == sorted(C.TOOL_NAMES)
    assert sorted(UP.ENRICHMENT_TOOLS) == sorted(t["name"] for t in tools_spec.EXTENSION_TOOLS)
    from brain_enrichment.config import EnrichmentConfig
    assert EnrichmentConfig.from_env({}).mode == "off"
    for k, v in UP.FEATURE_OFF.items():
        assert EnrichmentConfig.from_env({k: v}).mode == "off"
    assert set(UP.ENV_WHITELIST) == {"BRAIN_ENRICHMENT", "BRAIN_ENRICHMENT_WRITES", "BRAIN_ENRICHMENT_RETRIEVAL", "BRAIN_ENRICHMENT_BATCH_SIZE",
                                     "BRAIN_ENRICHMENT_MAX_INPUT_CHARS", "BRAIN_ENRICHMENT_MAX_OUTPUT_BYTES"}


# ----- documented hazards: the cited file:line evidence must be true (docs cannot drift from the scripts)
HAZARDS = [
    ("scripts/upgrade.sh", 28, "chown -R"),
    ("scripts/upgrade.sh", 30, "dc up -d"),
    ("scripts/upgrade.sh", 44, "revert"),
    ("scripts/upgrade.sh", 46, "revert"),
    ("scripts/upgrade.sh", 50, "dc up -d"),
    ("scripts/upgrade.sh", 66, "dc up -d"),
    ("scripts/restore-backup.sh", 32, "down"),
    ("scripts/restore-backup.sh", 38, "mv "),
    ("scripts/restore-backup.sh", 52, "chown -R"),
    ("scripts/restore-backup.sh", 63, "dc up -d"),
    ("install.sh", 113, "chown -R"),
    ("Dockerfile", 2, "Code and data are NOT baked in"),
    ("Dockerfile", 3, "bind-mounted at /wiki"),
    ("deploy/entrypoint.sh", 51, 'exec python "$WIKI/scripts/remote_mcp/server.py"'),
    ("compose.yaml", 51, ":/wiki"),
    ("scripts/create-backup.sh", 28, "BACKUP_INFO.txt"),
    ("scripts/create-backup.sh", 40, "dc pause brain"),
    ("scripts/create-backup.sh", 43, "--exclude='./_search'"),
    ("scripts/create-backup.sh", 44, "-cf"),
    ("scripts/create-backup.sh", 46, "caddy/data caddy/config auth"),
    ("deploy/lib.sh", 32, "docker compose --project-directory"),
    ("deploy/lib.sh", 61, "public_health()"),
    ("deploy/lib.sh", 63, "BRAIN_DOMAIN"),
    ("deploy/lib.sh", 66, "--resolve"),
]


def test_documented_hazard_evidence_is_literally_true():
    doc = (ROOT / "docs/enrichment/upgrade.md").read_text()
    for path, line, needle in HAZARDS:
        lines = (ROOT / path).read_text().splitlines()
        assert needle in lines[line - 1], f"{path}:{line} no longer contains {needle!r}: {lines[line - 1]!r}"
        assert f"{path}:{line}" in doc, f"docs/enrichment/upgrade.md does not cite {path}:{line}"


def test_docs_and_template_exist_and_cover_required_topics():
    up = (ROOT / "docs/enrichment/upgrade.md").read_text()
    for need in ("baseline", "code-off", "shadow", "pilot", "backfill", "stop condition", "10", "50", "preflight", "rollback", "no-touch"):
        assert need.lower() in up.lower(), need
    rb_ = (ROOT / "docs/enrichment/runbook.md").read_text()
    for need in ("install", "pilot", "backfill", "rollback"):
        assert need in rb_.lower()
    tpl = (ROOT / "docs/enrichment/compat-report.template.md").read_text()
    for need in ("wiki path", "compose", "public URL", "callbacks", "Hermes", "YAML", "OAuth", "/upload", "Telegram", "UNVERIFIED"):
        assert need.lower() in tpl.lower(), need
    for f in (up, rb_, tpl):
        assert leaked(f) == []


# ================================================================== real MCP listing against the REAL server (loopback)
def test_mcp_client_lists_tools_against_the_real_remote_server(tmp_path):
    pytest.importorskip("mcp")
    uvicorn = pytest.importorskip("uvicorn")
    import socket
    from remote_mcp.server import build_app
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    app = build_app(public_url=url, owner_secret="correct horse battery staple 42", state_dir=tmp_path / "st", allow_loopback_redirects=True)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started
    try:
        tok = app.state.auth_provider._issue("upgrade-test-client", ["brain"], url + "/mcp", "owner")
        deps = UP.Deps()
        ok, why = UP.check_health(deps, url, False)
        assert ok, why
        client = UP.McpClient(deps, url, tok.access_token)
        tools = client.list_tools()
        assert sorted(tools) == sorted(UP.BASELINE_TOOLS), sorted(tools)
        with pytest.raises(EB.ToolError):
            UP.McpClient(deps, url, "not-a-token").list_tools()
    finally:
        server.should_exit = True
        th.join(timeout=10)


# ================================================================== opt-in: real Docker (throwaway project, no published ports)
def _docker_ok() -> tuple[bool, str]:
    if not shutil.which("docker"):
        return False, "docker CLI not installed"
    r = subprocess.run(["docker", "info"], capture_output=True, text=True)
    return (r.returncode == 0, "" if r.returncode == 0 else "docker daemon not reachable")


@pytest.mark.skipif(os.environ.get("BRAIN_E2E_DOCKER") != "1",
                    reason="SKIP real-docker proof: opt-in only (set BRAIN_E2E_DOCKER=1). The fake-docker tests above are NOT a deployment proof.")
def test_real_docker_up_touches_only_the_brain_service(tmp_path):
    ok, why = _docker_ok()
    if not ok:
        pytest.skip(f"SKIP real-docker proof: {why}; fake/static compose results are not a deployment proof")
    image = os.environ.get("BRAIN_E2E_IMAGE", "busybox:latest")
    if subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode != 0:
        pytest.skip(f"SKIP real-docker proof: image {image} not present locally and tests never pull")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "compose.yaml").write_text(
        f"name: e2e-{os.getpid()}\nservices:\n  brain:\n    image: {image}\n    command: sleep 300\n    environment:\n      STAMP: ${{STAMP:-a}}\n"
        f"  other:\n    image: {image}\n    command: sleep 300\n")                       # no ports anywhere
    (proj / ".env").write_text("")
    ctx = UP.ComposeCtx(project=f"e2e-{os.getpid()}", project_dir=str(proj), files=[str(proj / "compose.yaml")], env_file=str(proj / ".env"))
    base = ["docker", "compose", "--project-name", ctx.project, "--project-directory", str(proj), "-f", str(proj / "compose.yaml")]

    def cid(svc):
        return subprocess.run([*base, "ps", "-q", svc], capture_output=True, text=True).stdout.strip()

    try:
        subprocess.run([*base, "up", "-d"], check=True, capture_output=True)                 # test-local bootstrap (throwaway project only)
        other0, brain0 = cid("other"), cid("brain")
        started0 = subprocess.run(["docker", "inspect", "-f", "{{.State.StartedAt}}", other0], capture_output=True, text=True).stdout
        runner = EB.Runner()
        r = runner.run(ctx.up(), cwd=str(proj), timeout=120)
        assert r.rc == 0, r.errtext
        assert cid("brain") != brain0 and cid("other") == other0
        assert subprocess.run(["docker", "inspect", "-f", "{{.State.StartedAt}}", other0], capture_output=True, text=True).stdout == started0
        pub = subprocess.run(["docker", "inspect", "-f", "{{json .NetworkSettings.Ports}}", cid("brain")], capture_output=True, text=True).stdout
        assert '"HostPort"' not in pub, "a port got published"
    finally:
        subprocess.run([*base, "down", "-v", "--remove-orphans"], capture_output=True)       # cleanup of the throwaway project only


def test_real_docker_availability_is_reported_honestly_in_this_sandbox():
    ok, why = _docker_ok()
    if not ok:
        pytest.skip(f"SKIP: {why} -> no container-level proof in this environment; only fake-docker/command-generation proofs ran")
