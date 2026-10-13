# Enrichment runbook (one page)

Placeholders: `W` = live wiki checkout, `R` = a clone of the release repository that already
contains the target commit (fetch it yourself first; the tools never fetch), `SHA` = the full
40-hex target commit, `OUT` = a private directory **outside** `W` (reports are mode 0600),
`T` = a confirmation token printed by the preceding dry run. Read `docs/enrichment/upgrade.md`
for what each step guarantees. Nothing below changes anything until a step says `--execute`.

```
U=scripts/upgrade-enrichment.py     # run from a checkout of the release (R), as an operator with Docker access
```

## 1. Install (code deployed, every feature off)

1. Discovery + gates (read-only):
   `python $U preflight --wiki W --release-repo R --target-sha SHA --out OUT/preflight.json --token-file OUT/token`
   Any `STOP` ends the procedure. Fill `docs/enrichment/compat-report.template.md`.
2. Backup (dry run, review, then execute with the printed token):
   `python $U backup --wiki W --dest OUT/backups [--extra-path /etc/nginx/...]`
   `python $U backup ... --execute --confirm T` then `python scripts/enrichment-backup.py verify --archive OUT/backups/<archive>`
3. Plan + rehearsal on a copy:
   `python $U plan --wiki W --release-repo R --target-sha SHA --out OUT/plan.json`
   `python $U apply --wiki W --plan OUT/plan.json --copy-dir OUT/rehearsal`
   `python $U rollback --wiki W --level 2 --plan OUT/plan.json --work-dir OUT/rehearsal.work --copy-dir OUT/rehearsal`
4. Live (restarts **only** `brain`):
   `python $U apply --wiki W --plan OUT/plan.json --work-dir OUT/work --execute --confirm T`
5. Gates:
   `python $U verify --wiki W --baseline OUT/preflight.json --applied OUT/work/applied.json --out OUT/verify.json --token-file OUT/token`
   Expect tools = the baseline 7, public `/healthz` OK, auth unchanged, no-touch equal.
   Then run the consented probes yourself (`scripts/remote_smoke.py`): old-ref read, probe capture, fixture ingest.
6. Commit the code files as the repository owner (paths in `OUT/work/applied.json`); the tool never commits.

## 2. Pilot (10 / 50 / all)

* Shadow first: set `BRAIN_ENRICHMENT=shadow` in the env file, recreate only `brain`
  (`python $U rollback --wiki W --level 1 ... ` dry run prints the exact command), run the CLI scan
  (`docs/enrichment/usage.md`), `verify --expect-features off`, `backup`.
* `BRAIN_ENRICHMENT=on`, `WRITES=0` -> `verify --expect-features on` (7 + 4 tools).
* `WRITES=1`, `BATCH_SIZE=1`: the harness annotates **10**, you review. Then **50**. Then all existing.
  `verify` + `backup` between steps. `RETRIEVAL=1` last.
* Stop and set features off on: a source hash moved, a forged quote accepted, duplicate revisions,
  health/latency regression, unexplained results, port/other-service change.

## 3. Backfill

Bounded batches through the inventory cursor; `verify` + `backup` per milestone; keep one verified
archive from before the first batch; never loop on a failing source (explicit reprocess after a fix).

## 4. Rollback

| Need | Command (dry run first, then `--execute --confirm T`) |
|---|---|
| Features off, keep code and data | `python $U rollback --wiki W --level 1 --plan OUT/plan.json --work-dir OUT/work` |
| Remove the new code (and previous image pin) | `python $U rollback --wiki W --level 2 --plan OUT/plan.json --work-dir OUT/work` |
| Disaster (explicit) | `python $U rollback --wiki W --level 3 --disaster-restore --archive OUT/backups/<a> --restore-dir NEWDIR --work-dir OUT/w3 [--replay-inputs]` |

Level 2 STOPs and changes nothing if any released file changed after the release: resolve those
files by hand, or use level 1. Level 2 keeps `_enrichment/` (it is data), but the restored pre-R1
`.gitignore` no longer ignores it: the tool warns; never `git add -A` on the live tree, and add
`_enrichment/` to `.git/info/exclude` if you want it hidden locally. After level 3 the cutover is yours (point `BRAIN_DATA_DIR` at the
restored tree, recreate only `brain`). Never use `docker compose down`, `git reset --hard`,
`git clean`, a stash, `restore-backup.sh --force` or a recursive `chown` on the live deployment.
