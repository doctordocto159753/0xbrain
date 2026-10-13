# Enrichment upgrade, backup, rollback and QA (WP-E)

Operator guide for putting the R1 enrichment layer (`docs/enrichment/CONTRACT-E1.md`) onto a
running 0xBrain deployment **without touching the corpus, the auth state, the public endpoint,
the callbacks or any other service**, and for getting back out again.

Two stdlib-only tools carry this (no network, no secrets printed, nothing runs unless you ask):

| Tool | Purpose |
|---|---|
| `scripts/upgrade-enrichment.py` | `preflight`, `plan`, `backup`, `apply`, `verify`, `rollback` |
| `scripts/enrichment-backup.py` | `create`, `verify`, `list`, `snapshot-store`, `restore` (new/empty directory only) |

One-page command sheet: `docs/enrichment/runbook.md`. Discovery record to fill in and keep:
`docs/enrichment/compat-report.template.md`.

## 1. Rules the tools enforce

* **Dry run first, always.** Every command that changes anything is a dry run until you pass
  `--execute` **and** `--confirm TOKEN`. The token is printed by the dry run and is bound to that
  exact plan (a different plan, flags, archive or per-file state gives a different token). `apply`
  is copy-first: without `--execute` it writes into a **copy** (`--copy-dir`), never the live tree.
* **Discovery, not assumption.** The wiki path, DATA/STATE, compose project, the exact `-f` files
  *and their order*, the env file, the container, image id/digest, uid/gid and the public URL are
  discovered from container labels, mounts, the env file and `docker compose config`. Nothing is
  hard-coded to `/opt/0xbrain/wiki`, Caddy, or a `compose.override.yaml`. Anything that cannot be
  evidenced is reported `UNVERIFIED`, never guessed silently. If two containers match, the tool
  stops and asks for `--project` / `--container`.
* **A guarded command runner.** Every external command passes `guard_command` (in
  `scripts/enrichment-backup.py`). It permits read-only `git`/`docker`/`ss`, the two repository
  validators, `docker pause|unpause <id>`, and exactly one write form:
  `docker compose [-p/--project-directory/-f.../--env-file] up -d --no-deps [--force-recreate] <one service>`
  (plus `build <one service>`). Everything else is refused before it runs. So these cannot be
  generated, by construction (and the tests assert it on every flow): `compose down`, `compose up`
  without a service name, `git reset --hard`, `git clean`, any stash, `git checkout/merge/revert`
  on the archive repo, recursive `chown`/`chmod`, `rm -rf`, `restore --force`, `docker system prune`,
  `docker run -p` (a new published port).
* **Allowlisted code only.** A release may touch only `scripts/brain_enrichment/**`,
  `harness/hermes/**`, `docs/enrichment/**`, `tests/enrichment/**`, the two new scripts, and the
  bounded hunks of the shared files (`.gitignore`, `.env.example`, `AGENTS.md`, the two validators,
  `scripts/remote_mcp/adapter.py`, `docs/UPGRADE.md`, `tests/conftest.py`) and instance-marker files (`README.md`, `HOME.md`,
  `SYSTEM_DESIGN.md`, `CLAUDE.md`). Anything else in the diff (`compose*.yaml`, `Dockerfile`,
  `deploy/`, `install.sh`, `scripts/*.sh`, `requirements*`, `_originals/`, `01-inbox/` ... `07-genesis/`,
  `00-system/`, `_proposals/`, `_captures/`, `_enrichment/`, `_search/`, `.env*`) is a **STOP**.
* **Instance markers are merged, never copied.** `CLAUDE.md`, `SYSTEM_DESIGN.md`, `README.md`,
  `HOME.md` differ per instance (counts, names). The release hunk is applied with a 3-way merge
  (`git merge-file -p`) from the exact base release; an overlap is a STOP, and the merge is refused
  if it would drop an instance-specific line. The empty-kit text is never written over yours.
* **Pinned releases.** `--target-sha` must be a full 40-hex commit that already exists in the
  release repository (this tool never fetches). Branch names, `HEAD`, tags and abbreviations are
  refused. Per-path `before`/`after` sha256 are recorded in the plan; `apply` re-derives every
  `after` hash and refuses to write if the live file is no longer the one that was planned.
* **Features stay off.** No env value is edited by `apply` (absent keys mean default off). The plan
  STOPs if the release would add a non-whitelisted env key or enable one by default, or if the live
  `.env` already switches enrichment on.
* **Secrets.** Secret values (owner secret, tokens, anything in an env var whose name contains
  SECRET/TOKEN/PASSWORD/KEY...) are registered with a redactor that scrubs stdout, stderr, logs,
  error text and reports; only key *names* and presence are ever recorded. Tokens are read from a
  file or an environment variable, never from argv. Reports are written mode 0600 and only outside
  the wiki.

## 2. Hazards in the current scripts (and what this tooling does instead)

Evidence is the file and line in this repository at the base commit. The test
`test_documented_hazard_evidence_is_literally_true` fails if a cited line stops saying this.

| # | Hazard | Evidence | What WP-E does instead |
|---|---|---|---|
| H1 | `dc up -d` with **no service name** recreates/starts every service in the project (caddy, any added service) - on upgrade, after a conflicting merge, and after a failed revert | `scripts/upgrade.sh:30`, `scripts/upgrade.sh:50`, `scripts/upgrade.sh:66` | Only `up -d --no-deps --force-recreate brain`, with the discovered project/files/env; the guard refuses `up` without exactly one service and `--no-deps`. `--force-recreate` is needed because a bind-mounted code change does not alter the compose config, so plain `up -d` would not restart the container at all. |
| H2 | Recursive `chown -R` over the whole DATA tree (the archive) on every upgrade, restore and install: slow, races with the writer, rewrites ownership of originals | `scripts/upgrade.sh:28`, `scripts/restore-backup.sh:52`, `install.sh:113` | No recursive ownership change anywhere. New files/dirs get the owner of their parent via a per-path `os.chown` (only when root); existing files keep their owner. The preflight reports uid/gid mismatches instead of "fixing" them. |
| H3 | Restore can `down` the **whole stack** and then `mv` the live repo and state dirs aside (`--force`) before extracting | `scripts/restore-backup.sh:32`, `scripts/restore-backup.sh:38`, `scripts/restore-backup.sh:63` | `enrichment-backup.py restore` writes only into a **new or empty** directory that is separate from the live wiki, has no force flag, never runs compose, never moves anything. Level-3 rollback salvages post-backup inputs first, restores beside the live tree, and leaves the cutover to you. |
| H4 | The image holds **no application code**: the server runs from the repository bind-mounted at `/wiki`. Restoring an old image alone does **not** restore old code | `Dockerfile:2`, `Dockerfile:3`, `deploy/entrypoint.sh:51`, `compose.yaml:51` | Rollback restores the *code files* (from recorded pre-images, hash-guarded) **and** the previous image tag together. A new image is only ever built under a **new** versioned tag; the old tag is never overwritten or deleted. R1 needs no image rebuild (stdlib only), and the plan says so. |
| H5 | The backup **excludes `_search`** and tars `_enrichment` as plain files. A raw copy of a WAL SQLite database is not a consistent backup, and the persistent store must not live under `_search` | `scripts/create-backup.sh:43`, `scripts/create-backup.sh:44` | The store lives at `_enrichment/store-v1.sqlite3` (CONTRACT-E1 section 1), outside `_search`. `enrichment-backup.py` snapshots every SQLite file under `_enrichment/` through the SQLite online-backup API (integrity-checked, single file) and archives **no** raw `*.sqlite3*` / `-wal` / `-shm` files. `_search/` stays excluded. |
| H6 | `public_health` assumes Caddy + `BRAIN_DOMAIN` + `--resolve ...:127.0.0.1` | `deploy/lib.sh:61`, `deploy/lib.sh:63`, `deploy/lib.sh:66` | `verify` checks `GET <discovered BRAIN_PUBLIC_URL>/healthz` (or `--public-url`), with no Caddy assumptions; works behind Nginx or any front. |
| H7 | `upgrade.sh` rolls back with `git revert` over the commit range it merged, in the repository that **also holds the archive** (captures, proposals) | `scripts/upgrade.sh:44`, `scripts/upgrade.sh:46` | Rollback never touches git history: it restores exactly the manifest's paths from pre-images, only where the file still has the release's `after` hash. |
| H8 | `dc` passes `--project-directory "$REPO"` and no `-f`/`--env-file`: it cannot know a production stack's explicit file list, order or env file | `deploy/lib.sh:32` | The compose context (project, directory, files in order, env file, service) is discovered from the running container's labels and re-checked for **drift** before every live step. |
| H9 | The backup archives only `auth`, `caddy/data`, `caddy/config` and the data dir; compose overrides or env files outside DATA, and a reverse proxy's TLS state, are not captured | `scripts/create-backup.sh:46` | Compose files / env file outside DATA are archived separately (`compose/N-...`); `--extra-path` adds reverse-proxy config or TLS directories (`extra/N/...`). |
| H10 | The backup only *freezes* the container (`docker pause`) around a plain `tar`, with no check that a file was stable while read, and its `BACKUP_INFO.txt` records neither the image nor a code manifest | `scripts/create-backup.sh:40`, `scripts/create-backup.sh:28` | `docker pause <id>` (or your own hook) around the archive, always resumed (even on failure); each file is hashed *while written* and an "unstable file" aborts instead of archiving a half-written copy; the old image id/tag/digest and a sha256 code manifest are in the archive manifest; the archive is proven by a scratch restore. |

## 3. Command reference

All commands take `--wiki PATH` (the live checkout). Discovery needs read access to the Docker
socket for container facts; without it the tool says so and marks those facts `UNVERIFIED`.
Exit codes: `0` ok, `1` failure, `2` usage / confirmation, `3` **STOP** (reason printed),
`7` archive unverified.

```
upgrade-enrichment.py preflight --wiki W --out REPORT.json [--target-sha SHA --base-sha SHA --release-repo R]
                                [--token-file F | --token-env VAR] [--public-url U] [--compose-file F ...]
upgrade-enrichment.py plan      --wiki W --release-repo R --target-sha SHA [--base-sha SHA] --out PLAN.json
                                [--new-image-tag name:tag] [--shared-hunks AGENT0.json]
upgrade-enrichment.py backup    --wiki W --dest DIR [--extra-path P ...] [--execute --confirm TOKEN]
upgrade-enrichment.py apply     --wiki W --plan PLAN.json --copy-dir NEWDIR                     # rehearsal on a copy
upgrade-enrichment.py apply     --wiki W --plan PLAN.json --work-dir DIR --execute --confirm T  # live
upgrade-enrichment.py verify    --wiki W --baseline REPORT.json --out VERIFY.json [--applied DIR/applied.json]
                                [--token-file F] [--expect-features off|on] [--read-ref REF]
upgrade-enrichment.py rollback  --wiki W --level 1|2|3 --plan PLAN.json --work-dir DIR [--execute --confirm T]
enrichment-backup.py  create|verify|list|snapshot-store|restore ...
```

* **preflight** (read-only; proven by hashing every file before/after in the tests) records: repo
  HEAD/branch/remotes (credentials stripped), ancestry of base/target, dirty/untracked **names**
  only, DATA/STATE and the enrichment/`_search` locations, compose project + files + order +
  env file, container id, image id/digest, uid/gid (env vs container vs filesystem), public URL and
  callbacks (count + fingerprint, never values), auth-state file existence/mode/sha256 (never
  content), internal and public health, the real MCP `tools/list` when a token is supplied (else
  `SKIP`), a sha256 manifest of `_originals`, captures, sources, derivatives, canonical zones,
  `00-system`, `_proposals` and `.githooks/known-baseline-errors.txt`, the proposals count and
  uncommitted-writes inventory, validator baseline results (rc and error counts; no baseline file
  is ever edited), free RAM/disk, every **other** container with its port mappings and the
  listening sockets (the no-touch baseline), and the release allowlist check. Output: a private
  JSON report (0600) and a redacted summary. Any STOP means: do not proceed.
* **plan** builds `PLAN.json` (0600): pinned `target_sha`, per-path before/after sha256 and method
  (`add`, `copy-target`, `merge3`, `delete`), the config delta (new whitelisted env keys, all off),
  the image decision (`rebuild_required: false` unless you pass `--new-image-tag`), the discovered
  compose context, and the allowlist and forbidden zones. It prints the token for the live apply.
* **backup** wraps `enrichment-backup.py create` with discovery. Contents: `repo/` (the whole
  working tree **including `.git`**, `_originals`, dirty/uncommitted captures and proposals, `.env`
  and instance config, `_enrichment/` with the store as a consistent snapshot; **without** `_search`,
  caches, raw SQLite files), `state/` (`auth`, `caddy/data`, `caddy/config`), `compose/` (compose
  files and env file that live outside DATA), `extra/` (your `--extra-path`s), and
  `BACKUP_MANIFEST.json` (git head/branch/dirty names, old image id/tag/digest, compose files in
  order, code manifest, snapshot integrity results, writer-pause record, per-member sha256). Archive
  and sidecars are mode 0600, written to a scratch name then renamed, and **always verified by
  restoring to a scratch directory** and comparing every hash and every snapshot (`PRAGMA
  integrity_check`). The destination must be outside the wiki. For off-box copies encrypt the
  archive yourself (it contains `.env`); this tool does not manage passphrases.
* **apply** (see section 5). **verify** (section 6). **rollback** (section 7).

## 4. Stages

Each stage ends with `verify`, and every stage has stop conditions. Moving on needs your explicit
decision; nothing here advances by itself.

### Stage 0 - baseline (nothing changes)

1. `preflight` with `--target-sha` and a token file. Resolve every STOP. Read every WARN.
2. Fill `compat-report.template.md` from the report; unevidenced items stay `UNVERIFIED`.
3. `backup` (dry run, review, execute). Confirm `verify` of the archive passes and the printed
   counts match `git status` (dirty paths) and the store row counts.
4. `plan`, then `apply --copy-dir` on a copy and `rollback --level 2 --copy-dir` on that copy.
   This rehearses the exact hashes and the merge of your instance-marker files without touching live.

**Stop if:** preflight STOP; unexpected container ambiguity; `UNVERIFIED` compose files; plan STOP
(out-of-allowlist path, merge conflict, non-whitelisted env key); backup verification fails;
validators already fail *and* you cannot explain the baseline; less than the required free disk.

### Stage 1 - code deployed, features off ("code-off")

`apply --execute` (it restarts **only** `brain`), then `verify --expect-features off`.
Expected: tool list = the baseline 7, `/healthz` OK on the public URL, auth state unchanged,
callbacks unchanged, source hashes equal to baseline (apart from legitimate new captures/appends),
no-touch comparison equal. Soak (a day of normal capture/ingest use) before continuing.
Operator-consented probes (they write data): old-ref read (`--read-ref`), a probe capture and a
fixture ingest (`scripts/remote_smoke.py`). `verify` reports them `SKIP` until run.

**Stop and roll back (level 2) if:** `verify` FAIL; the tool list is not the baseline 7;
`/healthz` unhealthy after the 4-minute wait; a published port appeared; any other container's id,
image, start time or ports changed; an original or capture changed; auth refresh stops working.

### Stage 2 - shadow

Set `BRAIN_ENRICHMENT=shadow` in the env file by hand, then recreate only `brain` with the exact
`up -d --no-deps --force-recreate brain` command that `rollback --level 1` prints in its dry run.
Shadow builds the sidecar through the enrichment CLI only and exposes **no** tool
(CONTRACT-E1 section 10). Run the CLI projection scan (see `docs/enrichment/usage.md`) in bounded
passes, then `verify --expect-features off` (tool list is still 7) and take a new `backup`.

**Stop if:** `verify` shows any source change; a projection pass reports parse failures you cannot
explain; the store file is not under `_enrichment/` or `_search/` grew a store; RAM or disk
headroom drops below the preflight minimums; store `integrity_check` fails.

### Stage 3 - pilot: 10, then 50, then all existing

1. `BRAIN_ENRICHMENT=on`, `WRITES=0`, `RETRIEVAL=0`: the seven plus four read-only tools appear;
   `verify --expect-features on` must list exactly 7 + 4. Check an annotation page by hand.
2. `WRITES=1`, `BATCH_SIZE=1`. The external harness (Hermes) annotates **10** sources; you review
   them in the CLI. Check the same checklist as Stage 1 plus: zero source mutation, no forged quote
   accepted, no duplicate revision from a retry, no unreviewed transcript auto-captured.
3. **50**, then **all** current captures/ingests, in batches, `verify` + `backup` between steps.
4. `RETRIEVAL=1` only after annotations look right and the index builds incrementally.

**Stop (set features off, level 1) if:** any annotation is accepted with a quote not in the source;
a source hash moves; an error rate you did not expect; health or latency degrades; a second
writer appears; disk grows unexpectedly; you cannot explain a single result.

### Stage 4 - backfill

Continue the same bounded loop for the remaining history (proposals and history refs are not
projected in R1). Respect the inventory cursor; never loop on a failing source (stable failures
need an explicit reprocess after a fix). Re-run `backup` + `verify` at each batch milestone, and
keep one verified archive from before the backfill started.

## 5. Apply

`apply` verifies every precondition before writing anything: the plan digest, the pinned commit
in the release repository, compose-discovery **drift**, a reachable Docker and the discovered
brain container, the planned new image tag still free, the work dir outside the wiki, and for
each path that the live file still has the planned *before* hash and that the release content
reproduces the planned *after* hash. Then, all-or-nothing: pre-images are saved (0600, outside
the wiki), files are written atomically (temp file + `fsync` + rename, mode and per-path owner
preserved), all hashes are re-checked, and on any failure the files already written are put back.
Then (live only) optionally `compose build brain` under the new tag, the single non-secret
`BRAIN_IMAGE` env edit, and `up -d --no-deps --force-recreate brain`, followed by a health wait.
It reports (and fails on) a newly published port of the brain container, and warns on any change
to other containers, the auth-state hash or the corpus fingerprint. Code files remain
**uncommitted working-tree changes**: the tool does not commit (a root-run commit would leave
root-owned objects in `.git`); commit them as the repository owner using the paths in
`applied.json`.

## 6. Verify

`verify --baseline REPORT.json` re-discovers and compares. Gates: container running/healthy,
internal healthcheck probe, public `/healthz` via the discovered URL, the real `tools/list`
(exactly the baseline 7 when features are off; 7 + 4 with `--expect-features on`; tool input
schemas compared with the baseline), auth-state file (unchanged hash = PASS; changed hash = WARN,
because a server-side refresh-token rotation is legitimate and this tooling never writes it;
missing = FAIL), public URL and callbacks fingerprint unchanged, enrichment effective config is
off, code files equal their release hashes, source hashes versus baseline (new files and
append-only `.jsonl` growth are legitimate; a changed `_originals` file or any removed file is a
FAIL; another modified file is a WARN for review), and the no-touch comparison of every other
container and port mapping. Steps that write data or need your consent are reported `SKIP` with
the exact command until you run them.

## 7. Rollback

| Level | When | What it does | What it never does |
|---|---|---|---|
| **1 - feature off** | enrichment misbehaves; code is fine | sets `BRAIN_ENRICHMENT=off`, `..._WRITES=0`, `..._RETRIEVAL=0` (only those lines of the env file; mode and every other line preserved), recreates only `brain` | touches code, data, store, auth, other services |
| **2 - software** | the new code must go | restores exactly the manifest's code paths from pre-images **only if each file's current hash equals the release's `after` hash**; removes release-created files only if unchanged since the release, then empty release-created dirs; restores the previous `BRAIN_IMAGE` pin only if still the one the release set; instance-marker/shared files use a bounded reverse-merge of the release hunk (a later non-overlapping edit survives, an overlap is a STOP); recreates only `brain`. **All or nothing: if any path changed after the release it STOPs, lists them, and changes nothing** | overwrite a later change; touch corpus, queue, registers, auth state, compose overrides, the enrichment store, `.git`; delete an image; use `git reset/clean/stash/revert` |
| **3 - disaster restore** | only on explicit request (`--disaster-restore`) | salvages every post-backup input (changed/new corpus files, a fresh store snapshot) into the work dir **first**, then restores the archive into a **new** directory (`--restore-dir`), optionally replays the salvaged inputs add-only (`--replay-inputs`) | restore over or beside-by-moving the live wiki, run any compose command, or decide the cutover for you |

Data written *after* the upgrade (captures, ingests, proposals, annotations, the store) stays
readable after a level 1 or 2 rollback: those files are outside the manifest by construction, and
the end-to-end rehearsal test reads both a pre-upgrade and a post-upgrade capture and ingest back
through the real `brain_read` after the rollback.

## 8. What is proven, and what is not

Proven by `tests/enrichment/test_upgrade.py` (real git repos, real SQLite, real file operations):
preflight read-only (content and mtime of wiki, project dir, state dir), secret redaction on
stdout/stderr/logs/reports/command logs, compose-override discovery and ordering, STOP on
out-of-allowlist diffs, pinned-SHA planning, 3-way marker merges, backup contents and scratch
restore with identical hashes, WAL-store snapshot integrity, apply all-or-nothing, rollback
refusal on later changes, level 1/2/3 semantics, the command guard, and a full rehearsal
(capture, apply, capture, rollback, both captures readable). The MCP tool-list client is also
exercised against the **real** remote MCP server on loopback.

**Not proven here:** anything against a real Docker daemon or real VPS. The compose/Docker side
uses a command-runner double, so it proves the commands this tooling *generates* and its logic,
not that your Docker accepts them. A real-Docker test exists (`BRAIN_E2E_DOCKER=1`, optional
`BRAIN_E2E_IMAGE`) that starts a throwaway two-service project with no published ports and shows
`up -d --no-deps --force-recreate brain` recreates only that service; it is SKIPPED when no
daemon is reachable. Also unverified: Nginx front specifics, real TLS/ACME state, Hermes, Telegram
bridge, behaviour under real load, and the container's real `/state` ownership.

## 9. Housekeeping

* Keep the private reports and `applied.json` (they contain file names and hashes, no secrets).
* Never edit `.githooks/known-baseline-errors.txt` to get a gate green.
* A backup on the same disk does not survive the disk: copy archives off the box (encrypted).
* Clean-up of rehearsal copies, `*.work` dirs and old archives is manual; the tools delete nothing
  they did not create in the same run.
