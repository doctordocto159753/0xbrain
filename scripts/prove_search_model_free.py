#!/usr/bin/env python3
"""Clean-room proof that indexing and search need no model artefact and no network.

Outer run: builds an empty HOME and a materialised fixture wiki, then re-runs
itself as `--inner` under `unshare -rn` (a network namespace with only a down
loopback) and `strace -f`, so any model download, model file open or outbound
connection is either impossible or recorded. Exit 0 only if:

  * configure, lexical search (canonical/captures/all) and exact search all
    complete with correct results;
  * no *.gguf / models directory exists under the QMD home or HOME;
  * strace saw no open of a model file and no non-local connect().

Requires qmd on PATH; `unshare` and `strace` are used when available and the
report says which isolation layers were actually active.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import search_lexical as sl  # noqa: E402
import run_lexical_eval as ev  # noqa: E402

MODEL_OPEN = re.compile(r'openat\(.*"[^"]*(\.gguf|/models/|\.safetensors|\.onnx)[^"]*"')
CONNECT = re.compile(r'connect\(\d+, \{sa_family=AF_INET6?, .*sin6?_addr[^}]*"([^"]+)"|connect\(\d+, \{sa_family=AF_INET, sin_port=htons\((\d+)\), sin_addr=inet_addr\("([^"]+)"\)')


def inner(wiki: Path) -> int:
    sl.configure(wiki)
    checks = {}
    r = sl.search("حافظه جمعی", "lexical", "canonical", 5, wiki)
    checks["lexical canonical (fa)"] = any(x["path"] == "03-objects/OBJ-0001.md" for x in r["groups"]["canonical"]["results"])
    r = sl.search("provenance tracing", "lexical", "canonical", 5, wiki)
    checks["lexical canonical (en)"] = any(x["path"] == "03-objects/OBJ-0007.md" for x in r["groups"]["canonical"]["results"])
    r = sl.search("ferry schedule", "lexical", "all", 5, wiki)
    cap = [x["path"] for x in r["groups"]["captures"]["results"]]
    can = [x["path"] for x in r["groups"]["canonical"]["results"]]
    checks["scope=all separate groups"] = (cap == ["01-inbox/captures/CAP-0002.md"]
                                           and "02-sources/text/DER-0001.md" in can
                                           and not any(p.startswith("01-inbox/") for p in can))
    r = sl.search("محمد", "exact", "canonical", 5, wiki)
    checks["exact folds harakat"] = any(x["path"] == "02-sources/text/DER-0003.md" for x in r["groups"]["canonical"]["results"])
    for bad in (["embed"], ["vsearch", "x"], ["query", "bare question"], ["query", "lex: x"]):
        try:
            sl._run_qmd(bad, wiki, sl.load_config(wiki))
            checks[f"guard refuses {bad[0]}"] = False
        except sl.ModelFreeViolation:
            checks[f"guard refuses {bad[0]}"] = True
    ev_state = sl.verify_model_free(wiki)
    print(json.dumps({"checks": checks, "qmd_home_files": ev_state["files"],
                      "model_artifacts": ev_state["model_artifacts"]}, ensure_ascii=False))
    return 0 if all(checks.values()) and ev_state["model_free"] else 1


def outer() -> int:
    if not shutil.which("qmd"):
        print("ERROR: qmd not on PATH", file=sys.stderr)
        return 2
    work = Path(tempfile.mkdtemp(prefix="wiki-model-free-proof-"))
    home = work / "home"
    home.mkdir()
    wiki = work / "wiki"
    ev.materialise(wiki)
    env = dict(os.environ, HOME=str(home), XDG_CACHE_HOME=str(home / ".cache"),
               XDG_CONFIG_HOME=str(home / ".config"), WIKI_QMD_HOME=str(work / "qmd"))
    cmd = [sys.executable, str(Path(__file__).resolve()), "--inner", str(wiki)]
    layers = {"empty_home": True, "network_namespace": False, "strace": False}
    if shutil.which("unshare") and subprocess.run(["unshare", "-rn", "true"]).returncode == 0:
        cmd = ["unshare", "-rn", *cmd]
        layers["network_namespace"] = True
    trace = work / "strace.log"
    if shutil.which("strace"):
        cmd = ["strace", "-f", "-qq", "-e", "trace=openat,connect,execve", "-o", str(trace), *cmd]
        layers["strace"] = True
    proc = subprocess.run(cmd, env=env, text=True, capture_output=True)
    report = {"isolation": layers, "inner_returncode": proc.returncode,
              "inner_stdout": proc.stdout.strip()[-1500:], "inner_stderr": proc.stderr.strip()[-500:]}
    model_opens, remote_connects, llama_libs = [], [], set()
    if trace.exists():
        for line in trace.read_text(errors="replace").splitlines():
            if MODEL_OPEN.search(line) and "ENOENT" not in line:
                model_opens.append(line[:200])
            if "connect(" in line and "AF_UNIX" not in line and "127.0.0.1" not in line and "::1" not in line:
                remote_connects.append(line[:200])
            m = re.search(r'openat\([^"]*"([^"]*llama[^"]*\.node)"', line)
            if m and "ENOENT" not in line:
                llama_libs.add(m.group(1).split("/node_modules/")[-1])
        report["strace_lines"] = sum(1 for _ in trace.open(errors="replace"))
    report.update({"model_file_opens": model_opens, "non_local_connects": remote_connects,
                   "native_llama_binding_loaded": sorted(llama_libs)})
    home_files = [str(p.relative_to(work)) for p in work.rglob("*")
                  if p.is_file() and p.suffix in {".gguf", ".safetensors", ".onnx"}]
    report["model_files_on_disk"] = home_files
    report["cache_contents"] = sorted(str(p.relative_to(work / "qmd")) for p in (work / "qmd").rglob("*") if p.is_file())
    ok = (proc.returncode == 0 and not model_opens and not remote_connects and not home_files)
    report["result"] = "PASS" if ok else "FAIL"
    shutil.rmtree(work, ignore_errors=True)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--inner", metavar="WIKI")
    a = ap.parse_args()
    sys.exit(inner(Path(a.inner)) if a.inner else outer())
