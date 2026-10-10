#!/usr/bin/env python3
"""Bounded, rehearsable, reversible code upgrade for the 0xBrain enrichment layer (WP-E, stdlib only).

    preflight   read-only discovery of the real deployment (never hard-coded paths) -> private
                JSON report + redacted operator summary; STOPs on anything outside the allowlist
    plan        pinned release manifest: per-path before/after sha256 for the CODE allowlist only,
                expected config delta (all default off), image tag, instance-marker-aware merges
    backup      (scripts/enrichment-backup.py) repo + dirty writes + auth/TLS + compose overrides +
                SQLite-backup-API snapshot of the enrichment store, verified by restoring to scratch
    apply       copy-first: applies the allowlist to a COPY; live tree only with --execute + token;
                restarts ONLY the brain service (`up -d --no-deps --force-recreate brain`)
    verify      post-cutover gates (health through the discovered public URL, tool list, auth state,
                source hashes, no-touch of other services/ports); unrun consented steps are SKIP
    rollback    level 1 feature off; level 2 code+image of THIS release only, hash-guarded;
                level 3 disaster restore (explicit, preserves post-backup inputs first)

Every command that changes anything is a dry run unless `--execute` AND `--confirm TOKEN` (printed
by the dry run for exactly that plan) are given. No network except the optional HTTP checks against
the URL you pass. Every external command goes through a guard (enrichment-backup.guard_command):
global compose down, git reset/clean/stash, recursive chown, restore --force, `up` without a
service name are impossible by construction. Secrets are never printed, logged or reported.

Exit codes: 0 ok, 1 failure, 2 usage/confirmation, 3 STOP (reason printed), 7 unverified.
"""
from __future__ import annotations

import argparse
import difflib
import importlib.util
import json
import logging
import os
import re
import shutil
import ssl
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent


def _load_backup_module():
    """scripts/enrichment-backup.py has a hyphen in its name; load it by path (shared classes)."""
    existing = sys.modules.get("enrichment_backup")
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location("enrichment_backup", HERE / "enrichment-backup.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["enrichment_backup"] = mod
    spec.loader.exec_module(mod)
    return mod


eb = _load_backup_module()
ToolError, UnsafeCommand = eb.ToolError, eb.UnsafeCommand
EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_STOP, EXIT_UNVERIFIED = eb.EXIT_OK, eb.EXIT_FAIL, eb.EXIT_USAGE, eb.EXIT_STOP, eb.EXIT_UNVERIFIED
log = logging.getLogger("upgrade_enrichment")
log.addFilter(eb.RedactingFilter())

TOOL = "upgrade-enrichment.py"

# --------------------------------------------------------------------------
# release policy (what an R1 release may touch)
BASELINE_TOOLS = ("brain_search", "brain_read", "brain_capture", "brain_ingest_file",
                  "brain_reconcile_context", "brain_propose", "brain_status")
ENRICHMENT_TOOLS = ("brain_enrichment_pending", "brain_enrichment_submit", "brain_artifact_read", "brain_retrieve")
CODE_PREFIXES = ("scripts/brain_enrichment/", "harness/hermes/", "docs/enrichment/", "tests/enrichment/")
CODE_FILES = ("scripts/upgrade-enrichment.py", "scripts/enrichment-backup.py")
# Shared-file hunks (CONTRACT-E1 section 13). Merge-aware, line-bounded; extend only with --shared-hunks.
SHARED_FILES = (".gitignore", ".env.example", "AGENTS.md", "scripts/validate_repo.py",
                "scripts/validate_content_release.py", "scripts/remote_mcp/adapter.py",
                # R1 integration hunks: the enrichment pointer in the legacy upgrade guide and the
                # suite-level fixture (tests/enrichment/conftest.py shadowed tests/conftest.py)
                "docs/UPGRADE.md", "tests/conftest.py")
MARKER_FILES = ("README.md", "HOME.md", "SYSTEM_DESIGN.md", "CLAUDE.md")   # instance-edited: merge, never copy
FORBIDDEN_TOPS = tuple(eb.CORPUS_ZONES) + ("_enrichment", "_search", ".git")
ENV_WHITELIST = {"BRAIN_ENRICHMENT": ("off",), "BRAIN_ENRICHMENT_WRITES": ("0",), "BRAIN_ENRICHMENT_RETRIEVAL": ("0",),
                 "BRAIN_ENRICHMENT_BATCH_SIZE": None, "BRAIN_ENRICHMENT_MAX_INPUT_CHARS": None,
                 "BRAIN_ENRICHMENT_MAX_OUTPUT_BYTES": None}     # None = any value (limits, not switches)
FEATURE_OFF = {"BRAIN_ENRICHMENT": "off", "BRAIN_ENRICHMENT_WRITES": "0", "BRAIN_ENRICHMENT_RETRIEVAL": "0"}
EDITABLE_ENV_KEYS = set(ENV_WHITELIST) | {"BRAIN_IMAGE"}
MAX_SHARED_LINES = 400
PIN_RE = re.compile(r"^[0-9a-f]{40}$")
TAG_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]*:[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
DEFAULT_REDIRECTS = ("https://claude.ai/api/mcp/auth_callback", "https://claude.com/api/mcp/auth_callback")
AUTH_FILE = "oauth_state.json"
COMPOSE_LABELS = ("com.docker.compose.project", "com.docker.compose.service", "com.docker.compose.project.working_dir",
                  "com.docker.compose.project.config_files", "com.docker.compose.project.environment_file")
NONCANONICAL_PREFIXES = ("_proposals/", "01-inbox/captures/", "_captures/")


# --------------------------------------------------------------------------
# dependency injection (tests inject a fake runner / http / clock)
def _default_sysinfo(paths: list[Path]) -> dict:
    ram = None
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                ram = int(line.split()[1]) // 1024
    except (OSError, ValueError):
        pass
    disk = {}
    for p in paths:
        try:
            disk[str(p)] = shutil.disk_usage(p).free // (1024 * 1024)
        except OSError:
            disk[str(p)] = None
    return {"ram_available_mb": ram, "disk_free_mb": disk}


class NetworkError(Exception):
    pass


def real_http(method: str, url: str, headers: dict, body: bytes | None, timeout: float, insecure: bool = False):
    """urllib, proxies deliberately ignored (the check must reach the endpoint itself)."""
    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ctx))
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()
    except Exception as exc:                                       # noqa: BLE001
        raise NetworkError(type(exc).__name__) from exc


@dataclass
class Deps:
    runner: object = field(default_factory=eb.Runner)
    http: Callable = real_http
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    sysinfo: Callable[[list], dict] = _default_sysinfo
    geteuid: Callable[[], int] = os.geteuid
    out: object = None
    err: object = None
    redactor: object = field(default_factory=eb.Redactor)

    def say(self, line: str = "") -> None:
        print(self.redactor.text(line), file=self.out or sys.stdout)

    def run(self, argv, **kw):
        log.debug("run: %s", " ".join(map(str, argv)))
        return self.runner.run(argv, **kw)


class Findings:
    """STOP blocks the upgrade; WARN needs a human look; SKIP = not run (with reason); INFO = fact."""

    def __init__(self):
        self.items: list[dict] = []

    def add(self, level: str, area: str, msg: str) -> None:
        self.items.append({"level": level, "area": area, "msg": msg})

    stop = lambda self, a, m: self.add("STOP", a, m)        # noqa: E731
    warn = lambda self, a, m: self.add("WARN", a, m)        # noqa: E731
    skip = lambda self, a, m: self.add("SKIP", a, m)        # noqa: E731
    info = lambda self, a, m: self.add("INFO", a, m)        # noqa: E731
    ok = lambda self, a, m: self.add("PASS", a, m)          # noqa: E731

    def has(self, level: str) -> bool:
        return any(i["level"] == level for i in self.items)

    @property
    def status(self) -> str:
        return "STOP" if self.has("STOP") else "WARN" if self.has("WARN") else "PASS"


# --------------------------------------------------------------------------
# small helpers
def short(h) -> str:
    return h[:12] if h else "-"


def canonical_digest(obj) -> str:
    return eb.sha256_bytes(eb.canonical(obj))


def utc_iso(deps: Deps) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(deps.clock()))


def write_report(deps: Deps, path: Path, obj, wiki: Path) -> None:
    path = Path(path)
    if eb.inside(path, wiki):
        raise ToolError("reports/work files must be written outside the wiki tree (it is the archive)", EXIT_USAGE)
    eb.write_private(path, json.dumps(deps.redactor.obj(obj), sort_keys=True, indent=1, ensure_ascii=False))


def load_json(path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ToolError(f"cannot read JSON {path}: {type(exc).__name__}", EXIT_USAGE) from exc


def jparse(text: str):
    try:
        return json.loads(text)
    except ValueError:
        return None


def posix_rel(p: str) -> str | None:
    if not p or p.startswith("/") or "\\" in p or "\0" in p or ".." in p.split("/") or p.startswith("./"):
        return None
    return p


# --------------------------------------------------------------------------
# path policy
def classify_path(rel: str, extra_shared: tuple[str, ...] = ()) -> tuple[str, str]:
    """-> (class, reason). class in code|shared|marker|forbidden|outside."""
    if posix_rel(rel) is None:
        return "forbidden", "unsafe path"
    top = rel.split("/", 1)[0]
    if top in FORBIDDEN_TOPS:
        return "forbidden", f"corpus/runtime path ({top}) is never part of a code release"
    if rel == ".env" or (rel.startswith(".env") and rel != ".env.example"):
        return "forbidden", "environment/secret file"
    if "__pycache__" in rel.split("/") or rel.endswith((".pyc", ".sqlite3", ".sqlite", ".db")):
        return "outside", "build/runtime artifact"
    if rel in CODE_FILES or rel.startswith(CODE_PREFIXES):
        return "code", "code allowlist"
    if rel in MARKER_FILES:
        return "marker", "instance-marker file (merge-aware)"
    if rel in SHARED_FILES or rel in extra_shared:
        return "shared", "shared-file hunk (merge-aware, line-bounded)"
    return "outside", "not on the release allowlist"


# --------------------------------------------------------------------------
# discovery
def parse_inspect(raw: dict, deps: Deps) -> dict:
    cfg, st, net = raw.get("Config") or {}, raw.get("State") or {}, raw.get("NetworkSettings") or {}
    env_keys = []
    for e in cfg.get("Env") or []:
        k, _, v = e.partition("=")
        env_keys.append(k)
        if eb.is_secret_key(k):
            deps.redactor.add(v)
    ports = []
    for cport, binds in (net.get("Ports") or {}).items():
        for b in binds or []:
            ports.append(f"{b.get('HostIp') or ''}:{b.get('HostPort')}->{cport}")
    labels = cfg.get("Labels") or {}
    return {
        "id": raw.get("Id", ""), "name": (raw.get("Name") or "").lstrip("/"), "image_id": raw.get("Image", ""),
        "image_tag": cfg.get("Image", ""), "user": cfg.get("User", ""), "state": st.get("Status", ""),
        "health": (st.get("Health") or {}).get("Status"), "started_at": st.get("StartedAt", ""),
        "restart_policy": ((raw.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name"),
        "mounts": [{"source": m.get("Source", ""), "destination": m.get("Destination", ""), "rw": m.get("RW")}
                   for m in raw.get("Mounts") or []],
        "ports": sorted(ports), "env_keys": sorted(env_keys),
        "labels": {k: labels[k] for k in COMPOSE_LABELS if k in labels},
    }


def docker_inspect(deps: Deps, ids: list[str]) -> list[dict]:
    if not ids:
        return []
    r = deps.run(["docker", "inspect", *ids])
    data = jparse(r.text) if r.rc == 0 else None
    return [parse_inspect(x, deps) for x in data] if isinstance(data, list) else []


def docker_available(deps: Deps) -> bool:
    return deps.run(["docker", "info", "--format", "{{.ServerVersion}}"]).rc == 0


@dataclass
class ComposeCtx:
    project: str | None = None
    project_dir: str | None = None
    files: list[str] = field(default_factory=list)
    env_file: str | None = None
    service: str = "brain"
    container: str | None = None
    source: str = "unknown"

    def signature(self) -> dict:
        return {"project": self.project, "project_dir": self.project_dir, "files": list(self.files),
                "env_file": self.env_file, "service": self.service}

    def base(self) -> list[str]:
        cmd = ["docker", "compose"]
        if self.project:
            cmd += ["--project-name", self.project]
        if self.project_dir:
            cmd += ["--project-directory", self.project_dir]
        for f in self.files:
            cmd += ["-f", f]
        if self.env_file:
            cmd += ["--env-file", self.env_file]
        return cmd

    def up(self) -> list[str]:
        """The ONLY command that starts anything: recreate the one service, touch no dependency."""
        return self.base() + ["up", "-d", "--no-deps", "--force-recreate", self.service]

    def build(self) -> list[str]:
        return self.base() + ["build", self.service]

    def config(self) -> list[str]:
        return self.base() + ["config", "--format", "json"]

    def ps_q(self) -> list[str]:
        return self.base() + ["ps", "-q", self.service]


def _file_services(path: Path) -> list[str]:
    """Heuristic: top-level keys under `services:` (UNVERIFIED parse; `compose config` is authoritative)."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out, in_services, indent = [], False, None
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if re.match(r"^services:\s*$", line):
            in_services, indent = True, None
            continue
        if in_services:
            if re.match(r"^\S", line):
                in_services = False
                continue
            m = re.match(r"^(\s+)([A-Za-z0-9_.-]+):\s*$", line)
            if m and (indent is None or len(m.group(1)) == indent):
                indent = len(m.group(1))
                out.append(m.group(2))
    return out


def file_info(path: str, index: int) -> dict:
    p = Path(path)
    ex = p.is_file()
    return {"path": path, "exists": ex, "index": index, "sha256": eb.sha256_file(p) if ex else None,
            "services_defined": _file_services(p) if ex else [], "looks_like_override": "override" in p.name}


def discover_compose(deps: Deps, F: Findings, a, wiki: Path, rep: dict) -> ComposeCtx:
    svc = a.service
    ctx = ComposeCtx(service=svc)
    explicit = [str(Path(f).resolve()) for f in (a.compose_file or [])]
    cands: list[dict] = []
    docker_ok = docker_available(deps)
    rep["docker"] = {"available": docker_ok}
    if not docker_ok:
        F.warn("docker", "docker is not reachable: container, image, compose-label and port facts are UNVERIFIED")
    else:
        r = deps.run(["docker", "ps", "-a", "--filter", f"label=com.docker.compose.service={svc}", "--format", "{{.ID}}"])
        ids = [x for x in r.text.split() if x]
        for c in docker_inspect(deps, ids):
            lab = c["labels"]
            wd = lab.get("com.docker.compose.project.working_dir", "")
            mounted = any(m["destination"] == "/wiki" and os.path.realpath(m["source"]) == str(wiki) for m in c["mounts"])
            if a.container and not (c["id"].startswith(a.container) or c["name"] == a.container):
                continue
            if a.project and lab.get("com.docker.compose.project") != a.project:
                continue
            if a.container or mounted or (wd and os.path.realpath(wd) == str(wiki)):
                cands.append(c)
        if len(cands) > 1:
            F.stop("compose", f"ambiguous: {len(cands)} containers of service {svc!r} match this wiki "
                              f"({', '.join(c['name'] for c in cands)}); pass --project or --container")
        elif not cands:
            F.warn("compose", f"no container of service {svc!r} found for {wiki}; falling back to files (container facts UNVERIFIED)")
    cont = cands[0] if len(cands) == 1 else None
    if cont:
        lab = cont["labels"]
        ctx.container, ctx.project = cont["id"], lab.get("com.docker.compose.project")
        ctx.project_dir = lab.get("com.docker.compose.project.working_dir") or None
        label_files = [f for f in lab.get("com.docker.compose.project.config_files", "").split(",") if f]
        label_files = [f if os.path.isabs(f) else str(Path(ctx.project_dir or wiki) / f) for f in label_files]
        envf = lab.get("com.docker.compose.project.environment_file", "").split(",")[0]
        ctx.env_file = envf or None
        if explicit and label_files and explicit != label_files:
            F.warn("compose", "operator --compose-file list differs from the running container's labels (labels = what is actually up); "
                              "using the operator list is UNVERIFIED")
            ctx.files, ctx.source = explicit, "operator-flag (differs from labels)"
        else:
            ctx.files, ctx.source = (explicit or label_files), ("operator-flag" if explicit else "container-labels")
    if not ctx.files:
        if explicit:
            ctx.files, ctx.source = explicit, "operator-flag"
        else:
            envmap = eb.parse_env_file(wiki / ".env")
            cf = envmap.get("COMPOSE_FILE")
            if cf:
                sep = envmap.get("COMPOSE_PATH_SEPARATOR", ":")
                ctx.files, ctx.source = [str((wiki / p).resolve()) for p in cf.split(sep) if p], "COMPOSE_FILE in .env"
            else:
                names = [n for n in ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml") if (wiki / n).is_file()]
                ov = [n for n in ("compose.override.yaml", "compose.override.yml", "docker-compose.override.yaml", "docker-compose.override.yml") if (wiki / n).is_file()]
                ctx.files = [str(wiki / n) for n in names[:1] + ov[:1]]
                ctx.source = "default file names in the wiki (UNVERIFIED: not confirmed against a running project)"
                if ctx.files:
                    F.warn("compose", "compose files were guessed from default names; confirm with --compose-file or a running container")
    ctx.project_dir = ctx.project_dir or (str(Path(ctx.files[0]).parent) if ctx.files else str(wiki))
    if not ctx.env_file:
        for cand in (a.env_file, str(Path(ctx.project_dir) / ".env"), str(wiki / ".env")):
            if cand and Path(cand).is_file():
                ctx.env_file = str(Path(cand).resolve())
                break
    if a.env_file:
        ctx.env_file = str(Path(a.env_file).resolve())
    infos = [file_info(f, i) for i, f in enumerate(ctx.files)]
    rep["compose"] = {"project": ctx.project, "project_dir": ctx.project_dir, "service": svc, "env_file": ctx.env_file,
                      "source": ctx.source, "files": infos,
                      "order_note": "compose merges -f files left to right; later files override earlier ones"}
    for i in infos:
        if not i["exists"]:
            F.stop("compose", f"compose file listed but missing on disk: {i['path']}")
    overrides = [i for i in infos if i["looks_like_override"]]
    if overrides and infos and infos[0]["looks_like_override"]:
        F.warn("compose", "an override file is listed BEFORE the base file; verify the -f order")
    both = [i["path"] for i in infos if svc in i["services_defined"]]
    if len(both) > 1:
        F.info("compose", f"service {svc!r} is defined in {len(both)} files; the later one overrides the earlier")
    rep["compose"]["override_after_base"] = bool(overrides) and not infos[0]["looks_like_override"] if infos else None
    if cont:
        rep["container"] = {k: cont[k] for k in ("id", "name", "image_id", "image_tag", "user", "state", "health",
                                                  "started_at", "restart_policy", "mounts", "ports", "env_keys")}
        img = deps.run(["docker", "image", "inspect", cont["image_id"]])
        d = jparse(img.text) if img.rc == 0 else None
        if isinstance(d, list) and d:
            rep["container"]["repo_digests"] = d[0].get("RepoDigests") or []
            rep["container"]["repo_tags"] = d[0].get("RepoTags") or []
    if docker_ok and ctx.files:
        r = deps.run(ctx.config(), cwd=ctx.project_dir)
        cfg = jparse(r.text) if r.rc == 0 else None
        if isinstance(cfg, dict):
            services = cfg.get("services") or {}
            for s in services.values():
                for k, v in (s.get("environment") or {}).items() if isinstance(s.get("environment"), dict) else []:
                    if eb.is_secret_key(str(k)) and isinstance(v, str):
                        deps.redactor.add(v)
            rep["compose"]["merged_services"] = sorted(services)
            rep["compose"]["published_ports"] = {n: sorted(f"{p.get('published')}->{p.get('target')}/{p.get('protocol', 'tcp')}"
                                                           for p in (s.get("ports") or []) if isinstance(p, dict))
                                                 for n, s in services.items()}
            b = services.get(svc) or {}
            rep["compose"]["service_image"] = b.get("image")
            rep["compose"]["service_binds"] = [{"source": v.get("source"), "target": v.get("target")}
                                               for v in b.get("volumes") or [] if isinstance(v, dict)]
            if svc not in services:
                F.stop("compose", f"merged compose config has no service named {svc!r}")
            if rep["compose"]["published_ports"].get(svc):
                F.info("compose", f"service {svc!r} publishes ports through the compose files (not touched by this release)")
        else:
            F.warn("compose", "`docker compose config` failed; merged service/port view is UNVERIFIED")
    return ctx


def discover_paths(deps: Deps, F: Findings, a, wiki: Path, ctx: ComposeCtx, rep: dict) -> dict:
    """DATA/STATE from .env, container mounts and compose binds; cross-checked, never assumed."""
    envp = Path(ctx.env_file) if ctx.env_file else wiki / ".env"
    env = eb.parse_env_file(envp)
    for v in eb.secret_values_of(env):
        deps.redactor.add(v)
    cb = [x.strip() for x in env.get("BRAIN_ALLOWED_REDIRECTS", "").split(",") if x.strip()]
    effective = sorted(cb) if cb else sorted(DEFAULT_REDIRECTS)
    rep["config"] = {
        "env_file": str(envp), "env_file_exists": envp.is_file(),
        "env_file_mode": oct(envp.stat().st_mode & 0o777) if envp.is_file() else None,
        "keys_present": sorted(env), "secret_keys_present": sorted(k for k in env if eb.is_secret_key(k)),
        "values": {k: env[k] for k in eb.SAFE_ENV_KEYS if k in env},
        "callbacks": {"explicit": bool(cb), "count": len(effective), "sha256": canonical_digest(effective)},
        "public_url_present": bool(env.get("BRAIN_PUBLIC_URL")),
    }
    if envp.is_file() and (envp.stat().st_mode & 0o077):
        F.warn("config", f"{envp.name} is group/world accessible (expected 0600)")
    if not env.get("BRAIN_OWNER_SECRET"):
        F.warn("config", "BRAIN_OWNER_SECRET is not present in the env file (UNVERIFIED where the server gets it)")
    try:
        sys.path.insert(0, str(HERE))
        from brain_enrichment.config import EnrichmentConfig                 # type: ignore
        c = EnrichmentConfig.from_env(env)
        rep["config"]["enrichment_effective"] = {"mode": c.mode, "writes": c.writes_enabled, "retrieval": c.retrieval,
                                                 "warnings": list(c.warnings)}
        if c.mode != "off" or c.writes or c.retrieval:
            F.info("config", f"enrichment is already enabled in the live env (mode={c.mode})")
    except Exception:                                                       # noqa: BLE001 - repo may predate the module
        rep["config"]["enrichment_effective"] = {"mode": env.get("BRAIN_ENRICHMENT", "off"), "note": "config module not importable (pre-release code)"}
    finally:
        if sys.path and sys.path[0] == str(HERE):
            sys.path.pop(0)
    data = state = None
    cont = (rep.get("container") or {})
    for m in cont.get("mounts", []):
        if m["destination"] == "/wiki":
            data = m["source"]
        if m["destination"] == "/state/auth":
            state = str(Path(m["source"]).parent)
    mount_data, mount_state = data, state
    data = env.get("BRAIN_DATA_DIR") or data
    state = a.state_dir or env.get("BRAIN_STATE_DIR") or state
    if data and os.path.realpath(data) != str(wiki):
        F.stop("paths", f"--wiki ({wiki}) differs from the configured data dir; refuse to operate on a mismatched tree")
    if mount_data and os.path.realpath(mount_data) != str(wiki):
        F.stop("paths", "the container's /wiki mount is not the --wiki tree")
    if mount_state and state and os.path.realpath(mount_state) != os.path.realpath(state):
        F.stop("paths", "BRAIN_STATE_DIR in the env file differs from the container's /state/auth mount parent")
    rep["paths"] = {"data": str(wiki), "state": state, "container_data_mount": mount_data, "container_state_mount": mount_state,
                    "search_dir": str(wiki / "_search"), "enrichment_dir": str(wiki / eb.ENRICHMENT_DIR),
                    "enrichment_store": str(wiki / "_enrichment" / "store-v1.sqlite3")}
    if not state:
        F.warn("paths", "state directory could not be discovered (BRAIN_STATE_DIR missing): auth/TLS state UNVERIFIED")
    elif not Path(state).is_dir():
        F.stop("paths", "the discovered state directory does not exist")
    return {"env": env, "state": Path(state) if state else None}


def auth_state_info(state: Path | None) -> dict:
    if state is None:
        return {"exists": False, "reason": "no state dir"}
    p = state / "auth" / AUTH_FILE
    d = state / "auth"
    info = {"dir_exists": d.is_dir(), "dir_mode": oct(d.stat().st_mode & 0o777) if d.is_dir() else None,
            "exists": p.is_file()}
    if p.is_file():
        s = p.stat()
        info.update(mode=oct(s.st_mode & 0o777), size=s.st_size, uid=s.st_uid, gid=s.st_gid, sha256=eb.sha256_file(p))
    return info


def corpus_manifest(wiki: Path) -> dict:
    files: dict[str, dict] = {}
    for zone in eb.CORPUS_ZONES:
        root = wiki / zone
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if d not in eb.EXCLUDED_DIRNAMES and not (Path(dirpath) / d).is_symlink())
            for f in sorted(filenames):
                p = Path(dirpath) / f
                rel = p.relative_to(wiki).as_posix()
                if p.is_symlink():
                    files[rel] = {"sha256": "symlink:" + os.readlink(p), "size": 0}
                    continue
                files[rel] = {"sha256": eb.sha256_file(p), "size": p.stat().st_size}
    base = wiki / ".githooks" / "known-baseline-errors.txt"
    if base.is_file():
        files[".githooks/known-baseline-errors.txt"] = {"sha256": eb.sha256_file(base), "size": base.stat().st_size}
    return {"count": len(files), "root_sha256": canonical_digest({k: v["sha256"] for k, v in files.items()}), "files": files}


def io_null():
    import io
    return io.BytesIO(b"")


def corpus_fingerprint(wiki: Path) -> str:
    """stat-only fingerprint (cheap) of the corpus zones: size + mtime_ns."""
    rows = []
    for zone in eb.CORPUS_ZONES:
        root = wiki / zone
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if d not in eb.EXCLUDED_DIRNAMES)
            for f in sorted(filenames):
                p = Path(dirpath) / f
                try:
                    s = p.lstat()
                except OSError:
                    continue
                rows.append((p.relative_to(wiki).as_posix(), s.st_size, s.st_mtime_ns))
    return canonical_digest(rows)


def code_hashes(wiki: Path, rels: list[str]) -> dict:
    return {r: (eb.sha256_file(wiki / r) if (wiki / r).is_file() else None) for r in rels}


def discover_git(deps: Deps, F: Findings, wiki: Path, rep: dict) -> None:
    g = lambda *args: eb.git(deps.runner, wiki, *args)           # noqa: E731
    head = g("rev-parse", "HEAD")
    if head.rc != 0:
        F.stop("git", f"{wiki} is not a readable git checkout")
        rep["git"] = {}
        return
    rems = g("remote", "-v").text
    rems = "\n".join(sorted(set(re.sub(r"://[^/@\s]+@", "://", ln) for ln in rems.splitlines())))
    st = g("status", "--porcelain=v1", "-z", "--untracked-files=all")
    entries = eb.parse_status_z(st.text) if st.rc == 0 else []
    gd = g("rev-parse", "--absolute-git-dir").text.strip()
    noncanon = [p for _, p in entries if p.startswith(NONCANONICAL_PREFIXES)]
    other_dirty = [p for _, p in entries if not p.startswith(NONCANONICAL_PREFIXES)]
    journal = {}
    if gd:
        jp = Path(gd) / "brain-uncommitted.json"
        if jp.is_file():
            j = jparse(jp.read_text(encoding="utf-8", errors="replace")) or {}
            journal = {"present": True, "stage": j.get("stage"), "paths": j.get("paths", [])}
    rep["git"] = {"head": head.text.strip(), "branch": g("rev-parse", "--abbrev-ref", "HEAD").text.strip(),
                  "remotes": rems.splitlines(), "git_dir": gd, "dirty_names_noncanonical": sorted(noncanon),
                  "dirty_names_other": sorted(other_dirty), "write_lock_present": bool(gd and (Path(gd) / "brain-write.lock").exists()),
                  "index_lock_present": bool(gd and (Path(gd) / "index.lock").exists()), "k9_failure_journal": journal}
    if rep["git"]["index_lock_present"]:
        F.stop("git", "a git index.lock exists: a git operation is in progress; do not proceed")
    if rep["git"]["write_lock_present"]:
        F.warn("git", "brain-write.lock is present: a capture/proposal write may be in flight (backup pauses the writer)")
    if other_dirty:
        F.warn("git", f"{len(other_dirty)} dirty path(s) outside the capture/proposal zones (names in the private report)")


def discover_other_services(deps: Deps, F: Findings, rep: dict, brain_id: str | None) -> None:
    """No-touch baseline: every container except the brain one, plus listening ports."""
    snap = []
    if rep.get("docker", {}).get("available"):
        ids = deps.run(["docker", "ps", "-a", "--format", "{{.ID}}"]).text.split()
        for c in docker_inspect(deps, ids):
            if brain_id and c["id"].startswith(brain_id[:12]):
                continue
            snap.append({k: c[k] for k in ("name", "id", "image_id", "image_tag", "state", "started_at", "ports")}
                        | {"project": c["labels"].get("com.docker.compose.project"), "service": c["labels"].get("com.docker.compose.service")})
    rep["others"] = sorted(snap, key=lambda x: x["name"])
    ss = deps.run(["ss", "-H", "-ltn"])
    ports = sorted({ln.split()[3] for ln in ss.text.splitlines() if len(ln.split()) >= 4}) if ss.rc == 0 else None
    rep["listening"] = ports
    if ports is None:
        F.warn("no-touch", "`ss` unavailable: listening-port baseline UNVERIFIED")


def run_validators(deps: Deps, F: Findings, wiki: Path) -> dict:
    out = {}
    for name, script, extra in (("validate_repo", "scripts/validate_repo.py", ["--full"]),
                                ("validate_content_release", "scripts/validate_content_release.py", [])):
        if not (wiki / script).is_file():
            out[name] = {"status": "SKIP", "reason": "script missing"}
            continue
        r = deps.run([sys.executable, script, *extra], cwd=str(wiki), timeout=300)
        lines = (r.text + r.errtext).splitlines()
        errs = [ln for ln in lines if ln.startswith(("ERROR", "FAIL"))]
        out[name] = {"rc": r.rc, "error_lines": len(errs), "last_line": lines[-1][:200] if lines else ""}
        if r.rc != 0:
            F.warn("validators", f"{name} already fails at baseline (rc={r.rc}, {len(errs)} error lines); recorded for comparison, nothing edited")
    return out


# --------------------------------------------------------------------------
# MCP over plain HTTP (stdlib), used for the real tool listing
class McpClient:
    def __init__(self, deps: Deps, url: str, token: str, insecure: bool = False):
        self.deps, self.url, self.token, self.insecure = deps, url.rstrip("/"), token, insecure
        self.session: str | None = None
        self._id = 0
        deps.redactor.add(token)

    def _post(self, payload: dict, expect_reply: bool = True):
        self._id += 1
        if expect_reply:
            payload = {**payload, "id": self._id}
        hdr = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
               "Authorization": f"Bearer {self.token}", "MCP-Protocol-Version": "2025-06-18"}
        if self.session:
            hdr["Mcp-Session-Id"] = self.session
        status, headers, body = self.deps.http("POST", self.url + "/mcp", hdr, json.dumps(payload).encode(), 30, self.insecure)
        if headers.get("mcp-session-id"):
            self.session = headers["mcp-session-id"]
        if status in (401, 403):
            raise ToolError(f"MCP endpoint refused the token (HTTP {status})", EXIT_FAIL)
        if status >= 400:
            raise ToolError(f"MCP endpoint answered HTTP {status}", EXIT_FAIL)
        if not expect_reply:
            return None
        text = body.decode("utf-8", errors="replace")
        msgs = []
        if "text/event-stream" in headers.get("content-type", ""):
            for ln in text.splitlines():
                if ln.startswith("data:"):
                    m = jparse(ln[5:].strip())
                    if isinstance(m, dict):
                        msgs.append(m)
        else:
            m = jparse(text)
            msgs = [m] if isinstance(m, dict) else []
        for m in msgs:
            if m.get("id") == self._id:
                if "error" in m:
                    raise ToolError(f"MCP error {m['error'].get('code')}", EXIT_FAIL)
                return m.get("result")
        raise ToolError("MCP reply not understood", EXIT_FAIL)

    def initialize(self) -> None:
        self._post({"jsonrpc": "2.0", "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "upgrade-enrichment", "version": "1"}}})
        try:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, expect_reply=False)
        except ToolError:
            pass

    def list_tools(self) -> dict:
        self.initialize()
        res = self._post({"jsonrpc": "2.0", "method": "tools/list", "params": {}}) or {}
        return {t["name"]: canonical_digest(t.get("inputSchema") or t.get("input_schema") or {}) for t in res.get("tools", [])}

    def call(self, name: str, arguments: dict) -> dict:
        self.initialize()
        res = self._post({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": name, "arguments": arguments}}) or {}
        txt = "".join(c.get("text", "") for c in res.get("content", []) if isinstance(c, dict))
        return jparse(txt) or {"ok": False, "raw_len": len(txt)}


def load_token(a, deps: Deps) -> str | None:
    tok = None
    if getattr(a, "token_env", None):
        tok = os.environ.get(a.token_env)
    elif getattr(a, "token_file", None):
        raw = Path(a.token_file).read_text(encoding="utf-8").strip()
        j = jparse(raw)
        if isinstance(j, dict):
            tok = j.get("access_token") or (j.get("tokens") or {}).get("access_token") or (j.get("token") if isinstance(j.get("token"), str) else None)
        else:
            tok = raw
    if tok:
        deps.redactor.add(tok)
    return tok or None


def check_health(deps: Deps, base_url: str, insecure: bool) -> tuple[bool, str]:
    try:
        status, _, body = deps.http("GET", base_url.rstrip("/") + "/healthz", {"Accept": "application/json"}, None, 10, insecure)
    except NetworkError as exc:
        return False, f"unreachable ({exc})"
    j = jparse(body.decode("utf-8", errors="replace")) if status == 200 else None
    return (status == 200 and isinstance(j, dict) and j.get("ok") is True), f"HTTP {status}"


# --------------------------------------------------------------------------
# release diff / plan
@dataclass
class Blobs:
    deps: Deps
    repo: Path

    def get(self, oid: str) -> bytes | None:
        if not oid or set(oid) == {"0"}:
            return None
        r = eb.git(self.deps.runner, self.repo, "cat-file", "blob", oid)
        if r.rc != 0:
            raise ToolError(f"release object {oid[:12]} is not in the release repository (fetch the target first)", EXIT_STOP)
        return r.out


def merge3(deps: Deps, cur: bytes, base: bytes, other: bytes) -> tuple[bytes, int]:
    """git merge-file -p: apply (base -> other) onto cur. Returns (merged, conflict_count)."""
    if b"\0" in cur + base + other:
        return b"", 1
    td = Path(tempfile.mkdtemp(prefix="up-merge-"))
    try:
        paths = []
        for n, data in (("cur", cur), ("base", base), ("other", other)):
            p = td / n
            p.write_bytes(data)
            paths.append(str(p))
        r = eb.git(deps.runner, td, "merge-file", "-p", "-L", "current", "-L", "base", "-L", "release", *paths)
    finally:
        shutil.rmtree(td, ignore_errors=True)
    if r.rc < 0 or r.rc > 127:
        return b"", 1
    return r.out, r.rc


def compute_after(deps: Deps, cur: bytes | None, base: bytes | None, tgt: bytes | None) -> tuple[bytes | None, str, str | None]:
    if tgt is None:
        if cur is None:
            return None, "noop", None
        if base is not None and cur == base:
            return None, "delete", None
        return None, "conflict", "the file was modified locally but the release deletes it"
    if cur == tgt:
        return tgt, "noop", None
    if cur is None:
        return (tgt, "add", None) if base is None else (None, "conflict", "the file was deleted locally but the release modifies it")
    if base is None:
        return None, "conflict", "the file exists locally with different content but the release adds it"
    if cur == base:
        return tgt, "copy-target", None
    merged, conflicts = merge3(deps, cur, base, tgt)
    if conflicts:
        return None, "conflict", "binary or conflicting local edits; 3-way merge refused (resolve by hand on a branch)"
    return merged, "merge3", None


def changed_lines(base: bytes | None, tgt: bytes | None) -> int:
    if (base and b"\0" in base) or (tgt and b"\0" in tgt):
        return 0
    a = (base or b"").decode("utf-8", "replace").splitlines()
    b = (tgt or b"").decode("utf-8", "replace").splitlines()
    return sum(1 for ln in difflib.unified_diff(a, b, lineterm="", n=0) if ln[:1] in "+-" and ln[:3] not in ("+++", "---"))


def parse_raw_diff(text: str) -> list[dict]:
    parts, out, i = text.split("\0"), [], 0
    while i < len(parts) and parts[i]:
        meta = parts[i]
        if not meta.startswith(":"):
            break
        om, nm, ooid, noid, status = meta[1:].split()[:5]
        out.append({"old_mode": om, "new_mode": nm, "old_oid": ooid, "new_oid": noid, "status": status[0], "path": parts[i + 1]})
        i += 2
    return out


def env_keys_of(blob: bytes | None) -> dict[str, tuple[bool, str]]:
    out = {}
    for ln in (blob or b"").decode("utf-8", "replace").splitlines():
        m = re.match(r"^\s*(#\s*)?([A-Z][A-Z0-9_]*)=(.*)$", ln)
        if m:
            out[m.group(2)] = (m.group(1) is None, m.group(3).split("#", 1)[0].strip())
    return out


def pinned(deps: Deps, repo: Path, sha: str, what: str, F: Findings) -> str | None:
    if not PIN_RE.match(sha or ""):
        F.stop("release", f"{what} must be a full 40-hex commit SHA, not a branch/tag/abbreviation ({sha!r}); floating refs are refused")
        return None
    r = eb.git(deps.runner, repo, "rev-parse", "--verify", f"{sha}^{{commit}}")
    if r.rc != 0 or r.text.strip() != sha:
        F.stop("release", f"{what} {sha[:12]} is not a commit in the release repository (fetch it first; this tool never fetches)")
        return None
    return sha


def build_release_entries(deps: Deps, F: Findings, wiki: Path, repo: Path, base: str, target: str,
                          extra_shared: tuple[str, ...], max_lines: int, env_live: dict) -> tuple[list[dict], dict]:
    r = eb.git(deps.runner, repo, "diff", "--raw", "-z", "--no-renames", "--no-abbrev", base, target)
    if r.rc != 0:
        F.stop("release", "cannot diff base..target in the release repository")
        return [], {}
    blobs = Blobs(deps, repo)
    entries, delta = [], {"new_env_keys": [], "active_values_ok": True}
    for d in parse_raw_diff(r.text):
        rel = d["path"]
        cls, why = classify_path(rel, extra_shared)
        if cls in ("forbidden", "outside"):
            F.stop("allowlist", f"{rel}: {why} -> STOP (a code release may not touch it)")
            entries.append({"path": rel, "class": cls, "action": "refused", "reason": why})
            continue
        if d["new_mode"] in ("120000", "160000") or d["old_mode"] in ("120000", "160000"):
            F.stop("allowlist", f"{rel}: symlink/submodule entries are refused")
            continue
        live = wiki / rel
        resolved_ok = not any(p.is_symlink() for p in [live, *live.parents] if eb.inside(p, wiki) and p != wiki)
        if not resolved_ok:
            F.stop("allowlist", f"{rel}: a path component in the live tree is a symlink; refused")
            continue
        if live.exists() and not live.is_file():
            F.stop("allowlist", f"{rel}: the live path exists but is not a regular file; refused")
            continue
        cur = live.read_bytes() if live.is_file() else None
        base_b, tgt_b = blobs.get(d["old_oid"]), blobs.get(d["new_oid"])
        after, method, problem = compute_after(deps, cur, base_b, tgt_b)
        lines = changed_lines(base_b, tgt_b)
        e = {"path": rel, "class": cls, "action": ("delete" if method == "delete" else "add" if method == "add" else
                                                    "noop" if method == "noop" else "conflict" if method == "conflict" else "modify"),
             "method": method, "mode": 0o755 if d["new_mode"] == "100755" else 0o644,
             "before_sha256": eb.sha256_bytes(cur) if cur is not None else None,
             "after_sha256": eb.sha256_bytes(after) if after is not None else None,
             "base_oid": d["old_oid"], "target_oid": d["new_oid"], "changed_lines": lines}
        if problem:
            e["problem"] = problem
            F.stop("merge", f"{rel}: {problem}")
        if cls in ("shared", "marker") and lines > max_lines:
            F.stop("allowlist", f"{rel}: the release hunk is {lines} changed lines (> {max_lines}); not a bounded shared-file hunk")
        if cls == "marker" and method == "merge3" and cur is not None and base_b is not None and after is not None:
            lost = (set(cur.decode("utf-8", "replace").splitlines()) - set(base_b.decode("utf-8", "replace").splitlines())) \
                   - set(after.decode("utf-8", "replace").splitlines())
            e["instance_lines_preserved"] = not lost
            if lost:
                F.stop("merge", f"{rel}: the merge would drop {len(lost)} instance-specific line(s); refused")
        if cls == "marker" and method == "copy-target" and cur is not None and base_b is not None and cur != base_b:
            F.stop("merge", f"{rel}: marker file would be overwritten with release text")      # unreachable by construction; belt and braces
        if rel == ".env.example" and tgt_b is not None:
            old_k, new_k = env_keys_of(base_b), env_keys_of(tgt_b)
            for k, (active, val) in new_k.items():
                if k not in old_k:
                    delta["new_env_keys"].append(k)
                    if k not in ENV_WHITELIST:
                        F.stop("config", f"release adds env key {k} which is not on the enrichment whitelist")
                    elif ENV_WHITELIST[k] is not None and active and val not in ENV_WHITELIST[k]:
                        F.stop("config", f"release enables {k}={val!r} by default; every new key must default off")
                        delta["active_values_ok"] = False
        entries.append(e)
    for k in FEATURE_OFF:
        if env_live.get(k) not in (None, "", FEATURE_OFF[k]):
            F.stop("config", f"the live env already sets {k} to a non-off value; the new code would activate immediately. Set it off first")
    delta["new_env_keys"] = sorted(set(delta["new_env_keys"]))
    delta["env_changes_required_in_live_env"] = []
    delta["note"] = "absent keys mean default off; this release edits no env value unless --new-image-tag is used (BRAIN_IMAGE)"
    return entries, delta


def plan_token(plan: dict, wiki: Path) -> str:
    return eb.confirm_token("apply", canonical_digest({"digest": plan["digest"], "wiki": str(wiki)}))


# --------------------------------------------------------------------------
# commands: preflight
def common_setup(a, deps: Deps) -> tuple[Path, Findings, dict]:
    wiki = Path(a.wiki).resolve()
    if not (wiki / ".git").exists():
        raise ToolError(f"{wiki} is not a git checkout", EXIT_USAGE)
    return wiki, Findings(), {"schema": None, "created_utc": utc_iso(deps), "wiki": str(wiki)}


def discover_runtime(a, deps: Deps, F: Findings, wiki: Path, rep: dict) -> tuple[ComposeCtx, dict]:
    discover_git(deps, F, wiki, rep)
    ctx = discover_compose(deps, F, a, wiki, rep)
    paths = discover_paths(deps, F, a, wiki, ctx, rep)
    return ctx, paths


def render_summary(deps: Deps, title: str, rep: dict, F: Findings, extra: list[str] | None = None) -> None:
    deps.say(f"== {title}: {F.status} ==")
    for line in extra or []:
        deps.say(line)
    for it in F.items:
        if it["level"] != "INFO":
            deps.say(f"[{it['level']}] {it['area']}: {it['msg']}")
    for it in F.items:
        if it["level"] == "INFO":
            deps.say(f"[INFO] {it['area']}: {it['msg']}")


def cmd_preflight(a, deps: Deps) -> int:
    wiki, F, rep = common_setup(a, deps)
    rep["schema"] = "enrichment-preflight-1"
    ctx, paths = discover_runtime(a, deps, F, wiki, rep)
    state, env = paths["state"], paths["env"]
    # identity: uid/gid
    ws, ids = wiki.stat(), {"wiki_owner": f"{wiki.stat().st_uid}:{wiki.stat().st_gid}", "env": f"{env.get('BRAIN_UID')}:{env.get('BRAIN_GID')}"}
    cont = rep.get("container") or {}
    if cont.get("user"):
        ids["container_user"] = cont["user"]
    if state and (state / "auth").is_dir():
        ids["auth_dir_owner"] = f"{(state / 'auth').stat().st_uid}:{(state / 'auth').stat().st_gid}"
    enr = wiki / eb.ENRICHMENT_DIR
    if enr.is_dir():
        ids["enrichment_dir_owner"] = f"{enr.stat().st_uid}:{enr.stat().st_gid}"
    rep["identity"] = ids
    if cont.get("user") and env.get("BRAIN_UID") and cont["user"].split(":")[0] != env["BRAIN_UID"]:
        F.warn("identity", "container user differs from BRAIN_UID in the env file")
    if env.get("BRAIN_UID") and str(ws.st_uid) != env["BRAIN_UID"]:
        F.warn("identity", "the wiki is not owned by BRAIN_UID (the server writes captures as BRAIN_UID); no chown is ever performed by this tooling")
    # auth state (never values)
    rep["auth_state"] = auth_state_info(state)
    if state and not rep["auth_state"]["exists"]:
        F.warn("auth", f"{AUTH_FILE} not found under the state dir (no connector registered yet, or UNVERIFIED path)")
    elif rep["auth_state"].get("mode") not in (None, "0o600"):
        F.warn("auth", f"auth state file mode is {rep['auth_state']['mode']} (expected 0o600)")
    # enrichment store (stat only; never opened: opening a WAL db is not read-only)
    store = wiki / "_enrichment" / "store-v1.sqlite3"
    rep["enrichment_store"] = {"exists": store.is_file(), "size": store.stat().st_size if store.is_file() else None,
                               "wal_present": Path(str(store) + "-wal").exists(), "shm_present": Path(str(store) + "-shm").exists(),
                               "search_dir_exists": (wiki / "_search").is_dir()}
    # endpoint, health, tools
    url = a.public_url or env.get("BRAIN_PUBLIC_URL")
    rep["endpoint"] = {"public_url": url, "scheme": urlparse(url or "").scheme or None, "callbacks": rep["config"]["callbacks"]}
    tok = load_token(a, deps)
    if url:
        ok, why = check_health(deps, url, a.insecure)
        rep["health_public"] = {"ok": ok, "detail": why}
        (F.ok if ok else F.warn)("health", f"public /healthz via the configured BRAIN_PUBLIC_URL: {why}")
    else:
        rep["health_public"] = {"ok": None, "detail": "SKIP"}
        F.skip("health", "no BRAIN_PUBLIC_URL / --public-url: public health not checked")
    if cont.get("id") and rep["docker"]["available"]:
        r = deps.run(["docker", "exec", cont["id"], "python", "/usr/local/lib/brain/healthcheck.py"])
        rep["health_internal"] = {"ok": r.rc == 0}
        (F.ok if r.rc == 0 else F.warn)("health", f"internal healthcheck probe rc={r.rc}")
    if url and tok:
        try:
            tools = McpClient(deps, a.mcp_url or url, tok, a.insecure).list_tools()
            rep["tools"] = {"names": sorted(tools), "schema_sha256": tools, "baseline_equal": sorted(tools) == sorted(BASELINE_TOOLS)}
            if sorted(tools) != sorted(BASELINE_TOOLS):
                F.warn("tools", f"tool list is not the baseline 7 (got {len(tools)}): {sorted(set(tools) ^ set(BASELINE_TOOLS))}")
            else:
                F.ok("tools", "MCP tools/list returned exactly the baseline 7 tools")
        except ToolError as exc:
            rep["tools"] = {"error": str(exc)}
            F.warn("tools", f"tool listing failed: {exc}")
    else:
        rep["tools"] = {"status": "SKIP"}
        F.skip("tools", "no --token-file/--token-env (and URL): real MCP tool listing not run")
    # corpus, proposals, uncommitted
    rep["corpus"] = corpus_manifest(wiki)
    prop = wiki / "_proposals" / "proposals.jsonl"
    with open(prop, "rb") if prop.is_file() else io_null() as fh:
        n_prop = sum(1 for _ in fh)
    rep["proposals"] = {"exists": prop.is_file(), "lines": n_prop,
                        "uncommitted_noncanonical": rep["git"].get("dirty_names_noncanonical", [])}
    if rep["proposals"]["uncommitted_noncanonical"]:
        F.info("inventory", f"{len(rep['proposals']['uncommitted_noncanonical'])} uncommitted capture/proposal path(s); the backup archives them")
    # validators, resources
    rep["validators"] = run_validators(deps, F, wiki) if not a.skip_validators else {"status": "SKIP", "reason": "--skip-validators"}
    res = deps.sysinfo([wiki, state] if state else [wiki])
    rep["resources"] = res
    free = min([v for v in res["disk_free_mb"].values() if v is not None] or [10**9])
    if free < a.min_free_mb:
        F.stop("resources", f"only {free} MB free on a data/state filesystem (< {a.min_free_mb} MB required for backup + rehearsal)")
    if res.get("ram_available_mb") is not None and res["ram_available_mb"] < a.min_free_ram_mb:
        F.warn("resources", f"only {res['ram_available_mb']} MB RAM available (< {a.min_free_ram_mb})")
    discover_other_services(deps, F, rep, cont.get("id"))
    # release allowlist
    rep["release"] = {"checked": False}
    if a.target_sha:
        repo = Path(a.release_repo or wiki).resolve()
        tgt = pinned(deps, repo, a.target_sha, "--target-sha", F)
        base = None
        if tgt:
            base = pinned(deps, repo, a.base_sha, "--base-sha", F) if a.base_sha else None
            if not a.base_sha:
                mb = eb.git(deps.runner, repo, "merge-base", "HEAD", tgt)
                base = mb.text.strip() if mb.rc == 0 and PIN_RE.match(mb.text.strip()) else None
                if not base:
                    F.stop("release", "cannot determine the base release SHA (pass --base-sha)")
        if tgt and base:
            ents, delta = build_release_entries(deps, F, wiki, repo, base, tgt, tuple(a.extra_shared or ()), a.max_shared_lines, env)
            rep["release"] = {"checked": True, "base": base, "target": tgt, "entries": ents, "config_delta": delta}
            anc = lambda repo_, x, y: {0: True, 1: False}.get(eb.git(deps.runner, repo_, "merge-base", "--is-ancestor", x, y).rc)    # noqa: E731
            rep["release"]["ancestry"] = {"base_is_ancestor_of_target": anc(repo, base, tgt),
                                          "head_contains_base": anc(wiki, base, "HEAD"),
                                          "head_contains_target": anc(wiki, tgt, "HEAD")}
            if rep["release"]["ancestry"]["base_is_ancestor_of_target"] is False:
                F.stop("release", "the base SHA is not an ancestor of the target SHA")
            if rep["release"]["ancestry"]["head_contains_base"] is None:
                F.info("release", "the live repository does not contain the base commit object (separate lineage); hashes decide, not history")
            if rep["release"]["ancestry"]["head_contains_target"]:
                F.info("release", "the target commit is already part of the live history")
            dirty_code = [e["path"] for e in ents if e["path"] in rep["git"].get("dirty_names_other", [])]
            if dirty_code:
                F.stop("release", f"release paths are dirty in the live tree: {dirty_code}")
            F.info("release", f"{sum(1 for e in ents if e['class'] in ('code','shared','marker'))} path(s) in scope; "
                              f"{sum(1 for e in ents if e['class'] in ('forbidden','outside'))} outside the allowlist")
    else:
        F.skip("release", "no --target-sha: allowlist check not run")
    rep["status"], rep["findings"] = F.status, F.items
    write_report(deps, Path(a.out), rep, wiki)
    render_summary(deps, "PREFLIGHT", rep, F, [
        f"wiki={wiki} head={short(rep['git'].get('head'))} branch={rep['git'].get('branch')}",
        f"compose project={ctx.project} files={len(ctx.files)} source={ctx.source}",
        f"container={'found' if cont else 'not found'} image={short(cont.get('image_id', '').replace('sha256:', ''))}",
        f"corpus files={rep['corpus']['count']} root={short(rep['corpus']['root_sha256'])} proposals_lines={rep['proposals']['lines']}",
        f"others (no-touch baseline)={len(rep['others'])} containers; listening={len(rep['listening'] or [])}",
        f"private report: {a.out} (0600)  [read-only: nothing under the wiki or state dir was written]"])
    return EXIT_STOP if F.has("STOP") else EXIT_OK


# --------------------------------------------------------------------------
# commands: plan
def cmd_plan(a, deps: Deps) -> int:
    wiki, F, rep = common_setup(a, deps)
    ctx, paths = discover_runtime(a, deps, F, wiki, rep)
    repo = Path(a.release_repo or wiki).resolve()
    tgt = pinned(deps, repo, a.target_sha, "--target-sha", F)
    base = None
    if tgt:
        if a.base_sha:
            base = pinned(deps, repo, a.base_sha, "--base-sha", F)
        else:
            mb = eb.git(deps.runner, repo, "merge-base", "HEAD", tgt)
            base = mb.text.strip() if mb.rc == 0 and PIN_RE.match(mb.text.strip()) else None
            if not base:
                F.stop("release", "cannot determine the base release SHA from history (pass --base-sha)")
    extra = ()
    if a.shared_hunks:
        j = load_json(a.shared_hunks)
        extra = tuple(p for p in j.get("allow", []) if classify_path(p)[0] not in ("forbidden",))
        bad = [p for p in j.get("allow", []) if classify_path(p)[0] == "forbidden"]
        if bad:
            F.stop("allowlist", f"--shared-hunks lists forbidden path(s): {bad}")
    entries, delta = ([], {})
    if tgt and base:
        entries, delta = build_release_entries(deps, F, wiki, repo, base, tgt, extra, a.max_shared_lines, paths["env"])
    image = {"current_tag": (rep.get("container") or {}).get("image_tag"), "current_id": (rep.get("container") or {}).get("image_id"),
             "rebuild_required": False, "new_tag": None,
             "note": "the image holds runtimes only; app code is bind-mounted at /wiki, so restoring an old image does NOT restore old code"}
    if a.new_image_tag:
        t = a.new_image_tag
        if not TAG_RE.match(t) or t.endswith((":latest", ":local")):
            F.stop("image", f"--new-image-tag must be a new versioned name:tag (not latest/local): {t!r}")
        elif t == image["current_tag"]:
            F.stop("image", "the new image tag equals the running tag; an existing tag is never overwritten")
        elif deps.run(["docker", "image", "inspect", t]).rc == 0:
            F.stop("image", f"image tag {t} already exists locally; choose a new one (never overwrite)")
        else:
            image["new_tag"] = t
            image["build_command"] = ctx.build()
            image["build_env"] = {"BRAIN_IMAGE": t}
    plan = {
        "schema": "enrichment-release-1", "release_id": f"r1-{(tgt or 'none')[:12]}",
        "base_release_sha": base, "target_sha": tgt, "target_pinned": bool(tgt),
        "base_deployed_head": (rep.get("git") or {}).get("head"), "release_repo": str(repo),
        "wiki": str(wiki), "compose": ctx.signature(),
        "paths": entries, "config_delta": delta, "image": image,
        "features": {"after_apply": dict(FEATURE_OFF), "note": "no env value is changed by apply; features stay off"},
        "forbidden_zones": list(FORBIDDEN_TOPS), "allowlist": {"code": list(CODE_PREFIXES) + list(CODE_FILES),
                                                               "shared_hunks": list(SHARED_FILES) + list(extra), "instance_markers": list(MARKER_FILES)},
        "stop_reasons": [i["msg"] for i in F.items if i["level"] == "STOP"],
    }
    plan["digest"] = canonical_digest(plan)
    if not F.has("STOP"):
        plan["created_utc"] = utc_iso(deps)
        write_report(deps, Path(a.out), plan, wiki)
    changes = [e for e in entries if e["action"] in ("add", "modify", "delete")]
    lines = [f"release {plan['release_id']}: base={short(base)} target={short(tgt)} (pinned)",
             f"{len(changes)} path(s) to change, {sum(1 for e in entries if e['action'] == 'noop')} already current"]
    for e in entries:
        if e["action"] in ("add", "modify", "delete", "noop"):
            lines.append(f"  {e['action']:7} {e['class']:6} {e['path']}  {short(e['before_sha256'])} -> {short(e['after_sha256'])}"
                         f"{'  [' + e['method'] + ']' if e.get('method') not in (None, 'noop') else ''}")
    lines.append(f"config delta: new env keys {delta.get('new_env_keys')} (all default off; no env value is edited)")
    lines.append(f"image: {'new tag ' + image['new_tag'] if image['new_tag'] else 'unchanged (code is bind-mounted; no rebuild needed)'}")
    if not F.has("STOP"):
        lines.append(f"plan written to {a.out} (0600). Rehearse on a copy (no token): apply --plan {a.out} --copy-dir DIR")
        lines.append(f"live apply needs: --execute --confirm {plan_token(plan, wiki)}")
    render_summary(deps, "PLAN", plan, F, lines)
    return EXIT_STOP if F.has("STOP") else EXIT_OK


# --------------------------------------------------------------------------
# apply
def owner_of(path: Path) -> tuple[int, int] | None:
    p = path
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        s = p.stat()
        return s.st_uid, s.st_gid
    except OSError:
        return None


def ensure_dirs(root: Path, rel_parent: str, created: list[str], deps: Deps) -> None:
    cur = root
    for part in Path(rel_parent).parts if rel_parent else ():
        cur = cur / part
        if cur.is_symlink():
            raise ToolError(f"{cur.name}: a symlinked directory in the release path; refused", EXIT_STOP)
        if not cur.exists():
            own = owner_of(cur.parent)
            cur.mkdir(mode=0o755)
            if deps.geteuid() == 0 and own:
                os.chown(cur, *own)                       # this one directory only (never recursive)
            created.append(cur.relative_to(root).as_posix())


def atomic_write(deps: Deps, path: Path, data: bytes, mode: int, owner: tuple[int, int] | None) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".upg-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        if deps.geteuid() == 0 and owner:
            os.chown(tmp, *owner)                         # this one file only
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def edit_env(deps: Deps, path: Path, key: str, value: str) -> str | None:
    """Replace/append one NON-SECRET key atomically; returns the previous value (None if absent)."""
    if key not in EDITABLE_ENV_KEYS:
        raise ToolError(f"refusing to edit env key {key}", EXIT_STOP)
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    st = path.stat() if path.is_file() else None
    lines, old, hit = text.splitlines(keepends=True), None, None
    for i, ln in enumerate(lines):
        m = re.match(rf"^(?:export\s+)?{re.escape(key)}=(.*?)(\r?\n)?$", ln)
        if m:
            hit, old = i, m.group(1).strip().strip("\"'")
    nl = "\r\n" if text.count("\r\n") > text.count("\n") / 2 and text else "\n"
    new_line = f"{key}={value}{nl}"
    if hit is None:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += nl
        lines.append(new_line)
    else:
        lines[hit] = new_line
    atomic_write(deps, path, "".join(lines).encode("utf-8"), stat.S_IMODE(st.st_mode) if st else 0o600,
                 (st.st_uid, st.st_gid) if st else None)
    return old


def wait_healthy(deps: Deps, ctx: ComposeCtx, timeout: int = 240) -> tuple[bool, str, str | None]:
    t0, cid, last = deps.clock(), None, "no container"
    while deps.clock() - t0 < timeout:
        cid = deps.run(ctx.ps_q(), cwd=ctx.project_dir).text.strip().splitlines()
        cid = cid[0] if cid else None
        if cid:
            c = docker_inspect(deps, [cid])
            if c:
                last = c[0]["health"] or c[0]["state"]
                if last == "healthy":
                    return True, last, cid
                if c[0]["state"] in ("exited", "dead"):
                    return False, c[0]["state"], cid
        deps.sleep(2)
    return False, f"timeout ({last})", cid


def check_ctx_drift(deps: Deps, a, wiki: Path, plan_or_applied: dict, F: Findings) -> ComposeCtx:
    rep: dict = {}
    ctx = discover_compose(deps, F, a, wiki, rep)
    if not rep["docker"]["available"] or not ctx.container:
        raise ToolError("docker is not reachable or the running brain container was not found: refusing to change anything that "
                        "would need a restart (nothing was written)", EXIT_STOP)
    want = plan_or_applied["compose"]
    if ctx.signature() != want:
        raise ToolError("compose discovery drifted since the plan was made "
                        f"(then {json.dumps(want, sort_keys=True)[:300]}, now {json.dumps(ctx.signature(), sort_keys=True)[:300]}); re-run preflight/plan", EXIT_STOP)
    if F.has("STOP"):
        raise ToolError("; ".join(i["msg"] for i in F.items if i["level"] == "STOP"), EXIT_STOP)
    return ctx


def load_plan(path) -> dict:
    plan = load_json(path)
    body = {k: v for k, v in plan.items() if k not in ("digest", "created_utc")}
    if plan.get("schema") != "enrichment-release-1" or canonical_digest(body) != plan.get("digest"):
        raise ToolError("plan file is not a valid, unmodified plan (digest mismatch)", EXIT_USAGE)
    if plan.get("stop_reasons"):
        raise ToolError("the plan carries STOP reasons; it cannot be applied", EXIT_STOP)
    return plan


def materialize(deps: Deps, blobs: Blobs, e: dict, cur: bytes | None) -> bytes | None:
    base_b, tgt_b = blobs.get(e["base_oid"]), blobs.get(e["target_oid"])
    after, method, problem = compute_after(deps, cur, base_b, tgt_b)
    if problem:
        raise ToolError(f"{e['path']}: {problem}", EXIT_STOP)
    return after


def apply_files(deps: Deps, plan: dict, root: Path, blobs: Blobs, work: Path) -> dict:
    """Check, back up pre-images, write atomically, verify. All-or-nothing: on any failure the
    files already written are restored from their pre-images."""
    todo = [e for e in plan["paths"] if e["action"] in ("add", "modify", "delete")]
    staged = []
    for e in todo:                                                     # phase 1: pure checks, no writes
        live = root / e["path"]
        cur = live.read_bytes() if live.is_file() else None
        have = eb.sha256_bytes(cur) if cur is not None else None
        if have != e["before_sha256"]:
            raise ToolError(f"{e['path']}: changed since the plan was made (hash {short(have)} != {short(e['before_sha256'])}); re-plan", EXIT_STOP)
        after = materialize(deps, blobs, e, cur)
        got = eb.sha256_bytes(after) if after is not None else None
        if got != e["after_sha256"]:
            raise ToolError(f"{e['path']}: release content does not reproduce the planned hash", EXIT_STOP)
        staged.append((e, cur, after))
    (work / "preimage").mkdir(parents=True, exist_ok=True, mode=0o700)
    for e, cur, _ in staged:                                           # phase 2: pre-images (and merge inputs for markers)
        if cur is not None:
            eb.write_private(work / "preimage" / e["path"], cur)
        if e["class"] in ("marker", "shared"):
            for kind, oid in (("base", e["base_oid"]), ("target", e["target_oid"])):
                b = blobs.get(oid)
                if b is not None:
                    eb.write_private(work / "blobs" / kind / e["path"], b)
    created: list[str] = []
    done: list[dict] = []
    try:
        for e, cur, after in staged:                                   # phase 3: writes
            live = root / e["path"]
            if after is None:
                live.unlink()
            else:
                ensure_dirs(root, str(Path(e["path"]).parent) if str(Path(e["path"]).parent) != "." else "", created, deps)
                mode = stat.S_IMODE(live.stat().st_mode) if live.exists() else e["mode"]
                atomic_write(deps, live, after, mode, owner_of(live if live.exists() else live.parent))
            done.append(e)
        for e in todo:                                                 # phase 4: verify
            live = root / e["path"]
            got = eb.sha256_file(live) if live.is_file() else None
            if got != e["after_sha256"]:
                raise ToolError(f"{e['path']}: post-write hash mismatch", EXIT_FAIL)
    except BaseException:
        for e in reversed(done):                                       # undo exactly what this run wrote
            pre = work / "preimage" / e["path"]
            live = root / e["path"]
            if pre.is_file():
                atomic_write(deps, live, pre.read_bytes(), e["mode"], owner_of(live.parent))
            elif live.exists() and e["before_sha256"] is None:
                live.unlink()
        for d in sorted(created, reverse=True):
            try:
                (root / d).rmdir()
            except OSError:
                pass
        raise
    rec = {"schema": "enrichment-applied-1", "release_id": plan["release_id"], "plan_digest": plan["digest"],
           "applied_utc": utc_iso(deps), "root": str(root), "compose": plan["compose"],
           "entries": [{"path": e["path"], "class": e["class"], "action": e["action"], "before_sha256": e["before_sha256"],
                        "after_sha256": e["after_sha256"], "mode": e["mode"], "base_oid": e["base_oid"], "target_oid": e["target_oid"],
                        "preimage": (f"preimage/{e['path']}" if e["before_sha256"] else None)} for e in todo],
           "created_dirs": created, "env_edits": [], "image": {}}
    return rec


def make_copy(wiki: Path, plan: dict, copy_dir: Path) -> None:
    copy_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    for e in plan["paths"]:
        if e["action"] in ("add", "modify", "delete", "noop"):
            src = wiki / e["path"]
            if src.is_file():
                dst = copy_dir / e["path"]
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)


def cmd_apply(a, deps: Deps) -> int:
    wiki, F, _ = common_setup(a, deps)
    plan = load_plan(a.plan)
    if Path(plan["wiki"]).resolve() != wiki:
        raise ToolError("the plan was made for a different wiki path", EXIT_USAGE)
    token = plan_token(plan, wiki)
    do_live = eb.require_confirmation("apply", canonical_digest({"digest": plan["digest"], "wiki": str(wiki)}), a.execute, a.confirm)
    repo = Path(plan["release_repo"])
    blobs = Blobs(deps, repo)
    pin = eb.git(deps.runner, repo, "cat-file", "-e", f"{plan['target_sha']}^{{commit}}")
    if pin.rc != 0:
        raise ToolError("the pinned target commit is not available in the release repository", EXIT_STOP)
    if not do_live:
        copy_dir = Path(a.copy_dir) if a.copy_dir else None
        if copy_dir is None:
            raise ToolError("copy-first mode needs --copy-dir (a NEW directory, outside the wiki)", EXIT_USAGE)
        if eb.inside(copy_dir, wiki) or eb.inside(wiki, copy_dir):
            raise ToolError("--copy-dir must be separate from the wiki", EXIT_USAGE)
        if copy_dir.exists() and any(copy_dir.iterdir()):
            raise ToolError("--copy-dir must be empty or new", EXIT_USAGE)
        make_copy(wiki, plan, copy_dir)
        work = copy_dir.parent / (copy_dir.name + ".work")
        rec = apply_files(deps, plan, copy_dir, blobs, work)
        eb.write_private(work / "applied.json", json.dumps(rec, indent=1, sort_keys=True))
        deps.say(f"COPY-FIRST OK: {len(rec['entries'])} path(s) applied to {copy_dir}; every after-hash verified; live tree untouched.")
        deps.say(f"to apply for real: --execute --confirm {token}")
        return EXIT_OK
    # ------------------------------------------------------------------ live
    work = Path(a.work_dir) if a.work_dir else None
    if work is None:
        st = (eb.parse_env_file(Path(plan["compose"]["env_file"])) if plan["compose"].get("env_file") else {}).get("BRAIN_STATE_DIR")
        if not st:
            raise ToolError("cannot derive the work dir; pass --work-dir (outside the wiki)", EXIT_USAGE)
        work = Path(st) / "upgrades" / plan["release_id"]
    if eb.inside(work, wiki):
        raise ToolError("--work-dir must be outside the wiki tree", EXIT_USAGE)
    if (work / "applied.json").exists():
        raise ToolError("this release was already applied (applied.json exists); use rollback or a new work dir", EXIT_STOP)
    ctx = check_ctx_drift(deps, a, wiki, plan, F)
    if plan["image"].get("new_tag") and deps.run(["docker", "image", "inspect", plan["image"]["new_tag"]]).rc == 0:
        raise ToolError("the planned new image tag now exists; refusing to overwrite", EXIT_STOP)
    state_dir = eb.parse_env_file(Path(ctx.env_file)).get("BRAIN_STATE_DIR") if ctx.env_file else None
    auth_before = auth_state_info(Path(state_dir) if state_dir else None)
    fp_before = corpus_fingerprint(wiki)
    rec = apply_files(deps, plan, wiki, blobs, work)
    rec["image"] = {"previous_tag": plan["image"].get("current_tag"), "previous_id": plan["image"].get("current_id"),
                    "new_tag": plan["image"].get("new_tag")}
    others_before = snapshot_others(deps, ctx)
    brain_ports_before = (docker_inspect(deps, [ctx.container]) or [{}])[0].get("ports")
    eb.write_private(work / "applied.json", json.dumps(rec, indent=1, sort_keys=True))
    steps = []
    try:
        if plan["image"].get("new_tag"):
            r = deps.run(ctx.build(), cwd=ctx.project_dir, env={"BRAIN_IMAGE": plan["image"]["new_tag"]}, timeout=1800)
            if r.rc != 0:
                raise ToolError(f"image build failed (rc={r.rc}); code files are applied but the old image is still in use: {r.errtext[-300:]}", EXIT_FAIL)
            ii = deps.run(["docker", "image", "inspect", plan["image"]["new_tag"]])
            built = jparse(ii.text) if ii.rc == 0 else None
            rec["image"]["new_id"] = built[0].get("Id") if isinstance(built, list) and built else None
            rec["image"]["new_digests"] = (built[0].get("RepoDigests") or []) if isinstance(built, list) and built else []
            old = edit_env(deps, Path(ctx.env_file), "BRAIN_IMAGE", plan["image"]["new_tag"])
            rec["env_edits"].append({"key": "BRAIN_IMAGE", "old": old, "new": plan["image"]["new_tag"]})
            eb.write_private(work / "applied.json", json.dumps(rec, indent=1, sort_keys=True))
            steps.append("image built, BRAIN_IMAGE updated")
        r = deps.run(ctx.up(), cwd=ctx.project_dir, timeout=600)
        if r.rc != 0:
            raise ToolError(f"`{' '.join(ctx.up()[-5:])}` failed (rc={r.rc}): {r.errtext[-300:]}", EXIT_FAIL)
        ok, st, cid = wait_healthy(deps, ctx)
        steps.append(f"brain {st}")
    except ToolError:
        deps.say(f"APPLY FAILED after writing code files; roll back with: rollback --level 2 --plan {a.plan} --work-dir {work}")
        raise
    problems = []
    new_brain = docker_inspect(deps, [cid]) if cid else []
    if new_brain and new_brain[0]["ports"] != brain_ports_before:
        problems.append(f"published ports of {ctx.service} changed ({brain_ports_before} -> {new_brain[0]['ports']}): NEW PORT EXPOSURE, roll back and investigate")
        ok = False
    now_others = snapshot_others(deps, ctx)
    if now_others != others_before:
        problems.append("other containers/ports changed during apply (no-touch violation)")
    if auth_state_info(Path(state_dir) if state_dir else None).get("sha256") != auth_before.get("sha256"):
        problems.append("auth state file hash changed during apply (server-side token rotation or a real fault; run verify)")
    if corpus_fingerprint(wiki) != fp_before:
        problems.append("corpus fingerprint changed during apply (new captures are legitimate; compare with verify)")
    deps.say(f"APPLY {'OK' if ok else 'UNHEALTHY'}: {len(rec['entries'])} path(s) written + verified; {'; '.join(steps)}")
    for p in problems:
        deps.say(f"[WARN] {p}")
    deps.say("Files are uncommitted working-tree changes; this tool never commits as root. To commit as the repo owner use the pathspec in "
             f"{work}/applied.json (paths only), e.g. `git add -- <paths>`.")
    deps.say(f"next: verify --baseline <preflight.json> --applied {work}/applied.json ...")
    return EXIT_OK if ok else EXIT_FAIL


def snapshot_others(deps: Deps, ctx: ComposeCtx) -> list[dict]:
    ids = deps.run(["docker", "ps", "-a", "--format", "{{.ID}}"]).text.split()
    out = []
    for c in docker_inspect(deps, ids):
        if ctx.container and c["id"].startswith(ctx.container[:12]):
            continue
        if c["labels"].get("com.docker.compose.service") == ctx.service and c["labels"].get("com.docker.compose.project") == ctx.project:
            continue                                                # the recreated brain container
        out.append({k: c[k] for k in ("name", "id", "image_id", "state", "started_at", "ports")})
    return sorted(out, key=lambda x: x["name"])


# --------------------------------------------------------------------------
# verify
def cmd_verify(a, deps: Deps) -> int:
    wiki, F, rep = common_setup(a, deps)
    rep["schema"] = "enrichment-verify-1"
    base = load_json(a.baseline)
    ctx, paths = discover_runtime(a, deps, F, wiki, rep)
    state, env = paths["state"], paths["env"]
    cont = rep.get("container") or {}
    bcont = base.get("container") or {}
    # 1 container
    if cont:
        ok = cont.get("state") == "running" and cont.get("health") in ("healthy", None)
        F.add("PASS" if ok else "STOP", "container", f"state={cont.get('state')} health={cont.get('health')} "
              f"image {'unchanged' if cont.get('image_id') == bcont.get('image_id') else 'CHANGED (expected only with --new-image-tag)'}")
        if cont.get("id") and rep["docker"]["available"]:
            r = deps.run(["docker", "exec", cont["id"], "python", "/usr/local/lib/brain/healthcheck.py"])
            F.add("PASS" if r.rc == 0 else "STOP", "health-internal", f"container healthcheck probe rc={r.rc}")
    else:
        F.skip("container", "no container discovered (docker unavailable?): UNVERIFIED")
    # 2 public health via discovered URL (not the Caddy helper)
    url = a.public_url or env.get("BRAIN_PUBLIC_URL") or (base.get("endpoint") or {}).get("public_url")
    if url:
        ok, why = check_health(deps, url, a.insecure)
        F.add("PASS" if ok else "STOP", "health-public", f"GET /healthz on the discovered BRAIN_PUBLIC_URL: {why}")
    else:
        F.skip("health-public", "no public URL discovered")
    # 3 tools
    tok = load_token(a, deps)
    want = sorted(BASELINE_TOOLS) if a.expect_features == "off" else sorted(BASELINE_TOOLS + ENRICHMENT_TOOLS)
    client = None
    if url and tok:
        client = McpClient(deps, a.mcp_url or url, tok, a.insecure)
        try:
            tools = client.list_tools()
            got = sorted(tools)
            F.add("PASS" if got == want else "STOP", "tools", f"tools/list = {len(got)} tools; expected {len(want)} for features={a.expect_features}"
                  + ("" if got == want else f"; difference {sorted(set(got) ^ set(want))}"))
            bsch = (base.get("tools") or {}).get("schema_sha256") or {}
            drift = [n for n in got if n in bsch and bsch[n] != tools[n]]
            if drift:
                F.warn("tools", f"input schema changed for {drift} vs baseline")
        except ToolError as exc:
            F.stop("tools", f"tool listing failed: {exc}")
    else:
        F.skip("tools", "no URL/token supplied: real MCP tool listing not run")
    # 4 auth state
    now_auth, was = auth_state_info(state), base.get("auth_state") or {}
    if not now_auth.get("exists"):
        F.add("STOP" if was.get("exists") else "SKIP", "auth", "auth state file missing" if was.get("exists") else "auth state file absent at baseline too")
    elif now_auth.get("sha256") == was.get("sha256"):
        F.ok("auth", "auth state file unchanged (same sha256, mode %s)" % now_auth.get("mode"))
    else:
        F.warn("auth", "auth state file hash differs from baseline. This tooling never writes it; a server-side refresh-token rotation is legitimate. "
                       "Confirm connectors still work (operator step) before closing the change")
    # 5 config unchanged
    cfg0 = base.get("config") or {}
    if cfg0:
        same_cb = rep["config"]["callbacks"] == cfg0.get("callbacks")
        same_url = (rep["config"]["values"].get("BRAIN_PUBLIC_URL") == (cfg0.get("values") or {}).get("BRAIN_PUBLIC_URL"))
        F.add("PASS" if same_cb and same_url else "STOP", "config", f"public URL {'unchanged' if same_url else 'CHANGED'}; callbacks {'unchanged' if same_cb else 'CHANGED'}")
    eff = (rep["config"].get("enrichment_effective") or {})
    if a.expect_features == "off":
        off = eff.get("mode", "off") == "off" and not eff.get("writes") and not eff.get("retrieval")
        F.add("PASS" if off else "STOP", "features", f"enrichment effective config {'is off' if off else 'is NOT off: ' + json.dumps(eff)}")
    # 6 code == applied hashes
    if a.applied:
        ap = load_json(a.applied)
        bad = [e["path"] for e in ap["entries"] if (eb.sha256_file(wiki / e["path"]) if (wiki / e["path"]).is_file() else None) != e["after_sha256"]]
        F.add("PASS" if not bad else "STOP", "code", f"{len(ap['entries'])} release path(s) match their AFTER hash" if not bad else f"changed/missing since release: {bad}")
    else:
        F.skip("code", "no --applied record given: code hashes not compared")
    # 7 sources vs baseline
    cmp = compare_corpus((base.get("corpus") or {}).get("files") or {}, corpus_manifest(wiki)["files"], wiki)
    rep["sources"] = {k: (v if not isinstance(v, list) else len(v)) for k, v in cmp.items()}
    rep["sources_names"] = {k: v for k, v in cmp.items() if isinstance(v, list) and k != "unchanged_count"}
    if cmp["removed"] or cmp["originals_changed"]:
        F.stop("sources", f"removed: {len(cmp['removed'])}, original/immutable changed: {len(cmp['originals_changed'])} (names in the private report)")
    elif cmp["modified"]:
        F.warn("sources", f"{len(cmp['modified'])} existing non-original file(s) differ from baseline (legit only for e.g. a capture transcript); review names in the report")
    else:
        F.ok("sources", f"unchanged {cmp['unchanged_count']}, new writes {len(cmp['added'])}, append-only {len(cmp['appended'])}; no removals, no modifications")
    # 8 no-touch
    discover_other_services(deps, F, rep, cont.get("id"))
    bo = base.get("others")
    if bo is not None and rep["docker"]["available"]:
        key = lambda xs: [(x["name"], x["id"], x["image_id"], x["started_at"], tuple(x["ports"])) for x in xs]       # noqa: E731
        same = key(bo) == key(rep["others"])
        F.add("PASS" if same else "STOP", "no-touch", "other containers/ports identical to baseline (ids, images, start times, port mappings)" if same
              else "other containers or port mappings differ from the baseline")
        if base.get("listening") is not None and rep.get("listening") is not None and base["listening"] != rep["listening"]:
            F.warn("no-touch", f"listening sockets differ: {sorted(set(base['listening']) ^ set(rep['listening']))}")
    else:
        F.skip("no-touch", "no baseline or docker unavailable")
    # 9 operator-consented steps
    if a.read_ref and client:
        r = client.call("brain_read", {"ref": a.read_ref})
        F.add("PASS" if r.get("ok") else "STOP", "old-ref-read", f"brain_read {a.read_ref} ok={r.get('ok')}")
    else:
        F.skip("old-ref-read", "operator-consented step not run (--read-ref REF with URL+token)")
    F.skip("capture-probe", "writes a probe capture; run `python scripts/remote_smoke.py --url URL` with consent")
    F.skip("fixture-ingest", "uploads a fixture file; run `python scripts/remote_smoke.py --url URL --ingest-file FILE` with consent")
    rep["status"], rep["findings"] = ("FAIL" if F.has("STOP") else F.status), F.items
    write_report(deps, Path(a.out), rep, wiki)
    render_summary(deps, "VERIFY", rep, F, [f"private report: {a.out}"])
    return EXIT_FAIL if F.has("STOP") else EXIT_OK


def compare_corpus(base: dict, now: dict, wiki: Path) -> dict:
    out = {"added": [], "removed": [], "modified": [], "appended": [], "originals_changed": [], "unchanged_count": 0}
    for rel, b in base.items():
        n = now.get(rel)
        if n is None:
            out["removed"].append(rel)
        elif n["sha256"] == b["sha256"]:
            out["unchanged_count"] += 1
        elif rel.endswith(".jsonl") and n["size"] >= b["size"] > 0 and _prefix_hash(wiki / rel, b["size"]) == b["sha256"]:
            out["appended"].append(rel)
        elif rel.startswith("_originals/"):
            out["originals_changed"].append(rel)
        else:
            out["modified"].append(rel)
    out["added"] = sorted(r for r in now if r not in base)
    for k in ("removed", "modified", "appended", "originals_changed"):
        out[k].sort()
    return out


def _prefix_hash(path: Path, n: int) -> str | None:
    try:
        with open(path, "rb") as fh:
            return eb.sha256_bytes(fh.read(n))
    except OSError:
        return None


# --------------------------------------------------------------------------
# rollback
def classify_for_rollback(deps: Deps, root: Path, work: Path, e: dict) -> dict:
    live = root / e["path"]
    cur = live.read_bytes() if live.is_file() else None
    have = eb.sha256_bytes(cur) if cur is not None else None
    r = {"path": e["path"], "class": e["class"], "current_sha256": have}
    if have == e["after_sha256"]:
        r["action"] = "remove-new-file" if e["before_sha256"] is None else "restore-preimage"
        if e["before_sha256"] is not None and not (work / "preimage" / e["path"]).is_file():
            r["action"], r["reason"] = "STOP", "pre-image missing from the work dir"
        return r
    if have == e["before_sha256"]:
        r["action"] = "already-original"
        return r
    if e["class"] in ("marker", "shared") and cur is not None and e["after_sha256"] is not None:
        base_p, tgt_p = work / "blobs" / "base" / e["path"], work / "blobs" / "target" / e["path"]
        if base_p.is_file() and tgt_p.is_file():
            merged, conflicts = merge3(deps, cur, tgt_p.read_bytes(), base_p.read_bytes())     # apply (target -> base) onto current
            if not conflicts:
                r["action"], r["merged_sha256"], r["_merged"] = "reverse-merge", eb.sha256_bytes(merged), merged
                return r
            r["action"], r["reason"] = "STOP", "release hunk cannot be reverted cleanly (later edits overlap it)"
            return r
    r["action"], r["reason"] = "STOP", ("file changed after the release (current hash differs from the release's AFTER hash); never overwritten"
                                        if cur is not None else "file is missing but the release expects it")
    return r


def cmd_rollback(a, deps: Deps) -> int:
    wiki, F, _ = common_setup(a, deps)
    if a.level == 3:
        return rollback_level3(a, deps, wiki)
    if not a.plan:
        raise ToolError("levels 1 and 2 need --plan", EXIT_USAGE)
    plan = load_plan(a.plan)
    work = Path(a.work_dir)
    if a.level == 1:
        return rollback_level1(a, deps, wiki, plan, F)
    root = wiki if not a.copy_dir else Path(a.copy_dir)
    applied = load_json(Path(a.applied) if a.applied else work / "applied.json")
    if applied.get("plan_digest") != plan["digest"]:
        raise ToolError("applied record does not belong to this plan", EXIT_USAGE)
    rows = [classify_for_rollback(deps, root, work, e) for e in applied["entries"]]
    stops = [r for r in rows if r["action"] == "STOP"]
    digest = canonical_digest({"plan": plan["digest"], "wiki": str(root), "rows": [[r["path"], r["action"], r["current_sha256"]] for r in rows]})
    deps.say(f"== ROLLBACK level 2 (software) {plan['release_id']}: {'STOP' if stops else 'PLAN'} ==")
    for r in rows:
        deps.say(f"  {r['action']:16} {r['class']:6} {r['path']}  now={short(r['current_sha256'])}" + (f"  [{r['reason']}]" if r.get("reason") else ""))
    if stops:
        deps.say(f"STOP: {len(stops)} file(s) changed after the release or cannot be restored safely; NOTHING was changed. "
                 "Later changes are never overwritten. Resolve those files by hand, or use level 1 (feature off).")
        return EXIT_STOP
    do = eb.require_confirmation("rollback-2", digest, a.execute, a.confirm)
    ctx = None
    if not a.copy_dir:
        ctx = check_ctx_drift(deps, a, wiki, applied, F)
    if not do:
        deps.say(f"DRY RUN: nothing changed. Corpus, queue, registers, auth state, compose overrides and the enrichment store are not part of this rollback. "
                 f"To execute: --execute --confirm {eb.confirm_token('rollback-2', digest)}")
        return EXIT_OK
    for r, e in zip(rows, applied["entries"]):
        live = root / e["path"]
        if r["action"] == "restore-preimage":
            atomic_write(deps, live, (work / "preimage" / e["path"]).read_bytes(), e["mode"], owner_of(live))
        elif r["action"] == "reverse-merge":
            atomic_write(deps, live, r["_merged"], stat.S_IMODE(live.stat().st_mode), owner_of(live))
        elif r["action"] == "remove-new-file":
            live.unlink()
    for d in sorted(applied.get("created_dirs", []), reverse=True):
        try:
            (root / d).rmdir()                                   # only if empty; never rmtree
        except OSError:
            pass
    for ed in reversed(applied.get("env_edits", [])):
        if ctx and ctx.env_file:
            cur = eb.parse_env_file(Path(ctx.env_file)).get(ed["key"])
            if cur == ed["new"] and ed["old"] is not None:
                edit_env(deps, Path(ctx.env_file), ed["key"], ed["old"])
            elif cur == ed["new"]:
                deps.say(f"[WARN] {ed['key']} was added by the release; remove that line by hand (this tool does not delete env lines)")
            else:
                deps.say(f"[WARN] {ed['key']} was changed after the release; left as is")
    bad = [e["path"] for r, e in zip(rows, applied["entries"])
           if r["action"] in ("restore-preimage", "remove-new-file") and (eb.sha256_file(root / e["path"]) if (root / e["path"]).is_file() else None) != e["before_sha256"]]
    if bad:
        raise ToolError(f"post-rollback hash check failed for {bad}", EXIT_FAIL)
    deps.say(f"ROLLBACK OK: {sum(1 for r in rows if r['action'] != 'already-original')} path(s) restored, hashes verified.")
    if ctx:
        r = deps.run(ctx.up(), cwd=ctx.project_dir, timeout=600)
        ok, st, _ = wait_healthy(deps, ctx)
        deps.say(f"brain restarted alone (rc={r.rc}), state={st}. The previous image tag was "
                 f"{'restored in the env file' if applied.get('env_edits') else 'never changed'}; the old image was never deleted.")
        return EXIT_OK if ok else EXIT_FAIL
    return EXIT_OK


def rollback_level1(a, deps: Deps, wiki: Path, plan: dict, F: Findings) -> int:
    ctx = check_ctx_drift(deps, a, wiki, plan, F)
    envp = Path(ctx.env_file)
    cur = eb.parse_env_file(envp)
    edits = [(k, cur.get(k), v) for k, v in FEATURE_OFF.items() if cur.get(k) != v]
    digest = canonical_digest({"plan": plan["digest"], "edits": edits, "env": str(envp)})
    do = eb.require_confirmation("rollback-1", digest, a.execute, a.confirm)
    deps.say("== ROLLBACK level 1 (feature off) ==")
    for k, old, new in edits:
        deps.say(f"  set {k}: {'<unset>' if old is None else old} -> {new}")
    deps.say(f"  then: {' '.join(ctx.up())}")
    if not do:
        deps.say(f"DRY RUN. To execute: --execute --confirm {eb.confirm_token('rollback-1', digest)}")
        return EXIT_OK
    for k, _, v in edits:
        edit_env(deps, envp, k, v)
    r = deps.run(ctx.up(), cwd=ctx.project_dir, timeout=600)
    ok, st, _ = wait_healthy(deps, ctx)
    deps.say(f"FEATURES OFF: env edited ({len(edits)} key(s)), only {ctx.service} recreated (rc={r.rc}), state={st}. Data, store and code untouched.")
    return EXIT_OK if ok else EXIT_FAIL


def rollback_level3(a, deps: Deps, wiki: Path) -> int:
    """Disaster restore. Explicit only. Preserves post-backup inputs FIRST, restores into a NEW
    directory, never stops/starts a stack and never touches the live wiki."""
    if not (a.archive and a.restore_dir and a.disaster_restore):
        raise ToolError("level 3 needs --disaster-restore --archive A --restore-dir NEW_DIR --work-dir W", EXIT_USAGE)
    work = Path(a.work_dir)
    if eb.inside(work, wiki):
        raise ToolError("--work-dir must be outside the wiki", EXIT_USAGE)
    manifest = eb.read_manifest(Path(a.archive))
    eb.check_restore_targets(manifest, Path(a.restore_dir), Path(a.state_restore_dir) if a.state_restore_dir else None, wiki)
    digest = canonical_digest({"archive": eb.sha256_file(Path(a.archive)), "restore": str(Path(a.restore_dir).resolve()), "wiki": str(wiki),
                               "state": a.state_restore_dir, "replay": bool(a.replay_inputs), "owner": bool(a.preserve_owner)})
    do = eb.require_confirmation("rollback-3", digest, a.execute, a.confirm)
    inputs = []
    for rel, rec in corpus_manifest(wiki)["files"].items():
        arc = manifest["files"].get(f"repo/{rel}")
        if arc is None or arc.get("sha256") != rec["sha256"]:
            inputs.append(rel)
    store = wiki / eb.ENRICHMENT_DIR
    deps.say("== ROLLBACK level 3 (disaster restore, explicit) ==")
    deps.say(f"  {len(inputs)} corpus path(s) are new/different since the backup and will be SALVAGED before anything else"
             f"{'; enrichment store will be snapshotted too' if eb.enrichment_sqlite_files(wiki) else ''}")
    deps.say(f"  then the archive is restored into {a.restore_dir} (must be new/empty); no compose command is run")
    if not do:
        deps.say(f"DRY RUN. To execute: --execute --confirm {eb.confirm_token('rollback-3', digest)}")
        return EXIT_OK
    stamp, n = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(deps.clock())), 0
    salv = work / f"post-backup-inputs-{stamp}"
    while salv.exists():
        n += 1
        salv = work / f"post-backup-inputs-{stamp}-{n}"
    (salv / "files").mkdir(parents=True, mode=0o700)
    for rel in inputs:
        dst = salv / "files" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(wiki / rel, dst)
    snaps = []
    for src in eb.enrichment_sqlite_files(wiki):
        snaps.append(src.name)
        eb.snapshot_sqlite(src, salv / f"enrichment-{src.name}")
    eb.write_private(salv / "SALVAGE.json", json.dumps({"paths": inputs, "enrichment_snapshots": snaps, "archive": Path(a.archive).name}, indent=1))
    res = eb.restore_archive(Path(a.archive), Path(a.restore_dir), Path(a.state_restore_dir) if a.state_restore_dir else None,
                             live_wiki=wiki, preserve_owner=a.preserve_owner)
    replayed, kept = [], []
    if a.replay_inputs:
        for rel in inputs:
            dst = Path(res["repo"]) / rel
            if dst.exists():
                kept.append(rel)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(salv / "files" / rel, dst)
                replayed.append(rel)
    deps.say(f"DISASTER RESTORE OK into {res['repo']} (head {short(res['head'])}). Salvage: {salv}")
    deps.say(f"  replayed {len(replayed)} input(s) add-only; {len(kept)} differing file(s) kept from the archive (the salvage copy has yours)")
    deps.say("  Cutover is manual: point BRAIN_DATA_DIR at the restored tree and recreate ONLY the brain service. Nothing was stopped by this tool.")
    return EXIT_OK


# --------------------------------------------------------------------------
# backup glue
def cmd_backup(a, deps: Deps) -> int:
    wiki, F, rep = common_setup(a, deps)
    ctx, paths = discover_runtime(a, deps, F, wiki, rep)
    if F.has("STOP"):
        raise ToolError("; ".join(i["msg"] for i in F.items if i["level"] == "STOP"), EXIT_STOP)
    cont = rep.get("container") or {}
    cfg = eb.BackupConfig(
        wiki=wiki, dest=Path(a.dest), state_dir=paths["state"], compose_files=list(ctx.files), env_file=ctx.env_file,
        extra_paths=list(a.extra_path or []),
        image={"id": cont.get("image_id"), "tag": cont.get("image_tag"), "digest": (cont.get("repo_digests") or [None])[0]},
        container=ctx.container, compose={"project": ctx.project, "project_dir": ctx.project_dir, "service": ctx.service},
        writer_pause=a.writer_pause, pause_hook=a.pause_hook, resume_hook=a.resume_hook)
    plan = eb.plan_backup(cfg)
    do = eb.require_confirmation("backup-create", plan["digest"], a.execute, a.confirm)
    deps.say(f"backup plan: {plan['files_in_repo_part']} files / {plan['bytes_in_repo_part']} bytes; _enrichment included={plan['enrichment_included']}; "
             f"sqlite snapshots={plan['sqlite_snapshots']}; state parts={plan['state_parts']}; outside-data config files={len(plan['outside_data_config_files'])}; "
             f"extras={len(plan['extra_paths'])}; excluded={plan['excluded']['top_level']}; writer pause={cfg.writer_pause}"
             f"{' (container ' + ctx.container[:12] + ')' if ctx.container else ' (no container: writer NOT paused)'}")
    if not do:
        deps.say("DRY RUN: nothing was written. To execute: " + f"--execute --confirm {eb.confirm_token('backup-create', plan['digest'])}")
        return EXIT_OK
    res = eb.create_backup(cfg, deps.runner, clock=deps.clock)
    deps.say("BACKUP OK: " + json.dumps(res, sort_keys=True))
    return EXIT_OK


# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=TOOL, description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, out=True):
        sp.add_argument("--wiki", required=True, help="the live (or throwaway) wiki checkout")
        sp.add_argument("--service", default="brain")
        sp.add_argument("--project", help="compose project name (disambiguation)")
        sp.add_argument("--container", help="container id/name (disambiguation)")
        sp.add_argument("--compose-file", action="append", help="repeat in compose merge order (default: discovered)")
        sp.add_argument("--env-file")
        sp.add_argument("--state-dir")
        sp.add_argument("--public-url")
        sp.add_argument("--mcp-url", help="base URL of the MCP server (default: the public URL)")
        sp.add_argument("--token-file", help="file holding an access token (raw or JSON); never put a token on the command line")
        sp.add_argument("--token-env", help="name of an environment variable holding an access token")
        sp.add_argument("--insecure", action="store_true", help="skip TLS verification (self-signed tests only)")
        if out:
            sp.add_argument("--out", required=True, help="private JSON report path (outside the wiki), mode 0600")

    def gate(sp):
        sp.add_argument("--execute", action="store_true")
        sp.add_argument("--confirm", metavar="TOKEN")

    def release(sp):
        sp.add_argument("--release-repo", help="a git repo that contains the target commit (default: the wiki itself)")
        sp.add_argument("--target-sha", help="pinned 40-hex commit to deploy")
        sp.add_argument("--base-sha", help="40-hex upstream commit the deployed code came from (default: merge-base)")
        sp.add_argument("--shared-hunks", help="JSON {\"allow\": [paths]} from AGENT-0 extending the shared-file list")
        sp.add_argument("--extra-shared", action="append")
        sp.add_argument("--max-shared-lines", type=int, default=MAX_SHARED_LINES)

    pf = sub.add_parser("preflight", help="read-only discovery -> private report + redacted summary")
    common(pf)
    release(pf)
    pf.add_argument("--skip-validators", action="store_true")
    pf.add_argument("--min-free-mb", type=int, default=1024)
    pf.add_argument("--min-free-ram-mb", type=int, default=256)

    pl = sub.add_parser("plan", help="pinned release manifest (code allowlist only)")
    common(pl)
    release(pl)
    pl.add_argument("--new-image-tag", help="build a NEW versioned image tag (never overwrites the old one)")

    bk = sub.add_parser("backup", help="backup via enrichment-backup.py (dry run unless --execute --confirm)")
    common(bk, out=False)
    bk.add_argument("--dest", required=True)
    bk.add_argument("--extra-path", action="append", help="reverse-proxy config / TLS state outside the data dir")
    bk.add_argument("--writer-pause", default="auto", choices=("auto", "docker", "hook", "none"))
    bk.add_argument("--pause-hook")
    bk.add_argument("--resume-hook")
    gate(bk)

    ap = sub.add_parser("apply", help="copy-first; live only with --execute --confirm")
    common(ap, out=False)
    ap.add_argument("--plan", required=True)
    ap.add_argument("--copy-dir", help="NEW directory for the copy-first rehearsal")
    ap.add_argument("--work-dir", help="pre-images + applied.json (outside the wiki; default <state>/upgrades/<release>)")
    gate(ap)

    vf = sub.add_parser("verify", help="post-cutover gates")
    common(vf)
    vf.add_argument("--baseline", required=True, help="the private preflight report")
    vf.add_argument("--applied", help="applied.json from apply")
    vf.add_argument("--expect-features", default="off", choices=("off", "on"))
    vf.add_argument("--read-ref", help="operator-consented: read this existing ref through brain_read")

    rb = sub.add_parser("rollback", help="level 1 feature off | 2 software (code+image) | 3 disaster restore")
    common(rb, out=False)
    rb.add_argument("--level", type=int, choices=(1, 2, 3), required=True)
    rb.add_argument("--plan")
    rb.add_argument("--applied")
    rb.add_argument("--work-dir", required=True)
    rb.add_argument("--copy-dir", help="rehearse level 2 against a copy produced by apply")
    rb.add_argument("--disaster-restore", action="store_true", help="level 3: explicit request")
    rb.add_argument("--archive")
    rb.add_argument("--restore-dir")
    rb.add_argument("--state-restore-dir")
    rb.add_argument("--replay-inputs", action="store_true")
    rb.add_argument("--preserve-owner", action="store_true")
    gate(rb)
    return p


def main(argv=None, deps: Deps | None = None) -> int:
    a = build_parser().parse_args(argv)
    deps = deps or Deps()
    eb.set_active_redactor(deps.redactor)
    try:
        return {"preflight": cmd_preflight, "plan": cmd_plan, "backup": cmd_backup, "apply": cmd_apply,
                "verify": cmd_verify, "rollback": cmd_rollback}[a.cmd](a, deps)
    except ToolError as exc:
        print(deps.redactor.text(f"{'STOP' if exc.code == EXIT_STOP else 'ERROR'}: {exc}"), file=deps.err or sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
