# Real Docker rehearsal (R1 upgrade -> features on -> rollback 1/2/3)

`docker_rehearsal.py` builds a throwaway **pre-R1** deployment (the base release, instantiated like
`install.sh` does, image built from the base `Dockerfile`), fills it with real inputs through the
real OAuth/MCP endpoint, then runs every step of `docs/enrichment/runbook.md` with the real tools
against it and asserts the outcome of each step. It is for a disposable host or VM with a Docker
daemon; it never touches an existing deployment:

* its own compose project (`--project`, default `rh0xbrain`), its own data/state under `--work`;
* `brain` is published on **loopback only** (`127.0.0.1:<port>`); the bundled caddy is disabled by
  an override (the production layout behind a host proxy); a separate `music-other` container on
  another loopback port is the no-touch witness;
* `--teardown` removes only what this script created (its project and the witness container).

```
python docs/enrichment/rehearsal/docker_rehearsal.py --work /srv/rehearsal \
    --release-repo /path/to/0xbrain --target-sha <40-hex> --base-sha <40-hex> \
    [--python-base python:3.12-bookworm] [--build-ca /path/ca.crt --build-proxy URL]
```

Needs: Docker + compose v2, git, openssl, Python with `requirements-remote.txt` (mcp). Output:
`<work>/out/summary.json` (every check, PASS/FAIL) and one log per step. A rehearsal on a fresh
throwaway instance is **not** the compatibility preflight of your real instance (see
`compat-report.template.md`).
