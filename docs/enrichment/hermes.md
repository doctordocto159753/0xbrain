# Hermes harness for the enrichment layer (R1, WP-B)

This describes the **external** harness side of annotation: a Hermes skill and
a disabled job template that call the archive's enrichment tools over the
remote connector. It ships in `harness/hermes/`. The archive server never calls
a model and never holds a model API key (CONTRACT-E1 section 10); the model
provider, its key and its billing live entirely in the Hermes host.

Status: the skill, prompts, template, installer and reference loop are
tested in CI against the in-repo `AnnotationService` and a fake store with a
rule-based fake annotator. **Nothing here has been run against a real Hermes
installation, a real model provider or the real enrichment store.** The target
directory layout of `install.sh` and the key names of `job.template.yaml` are
assumptions that you must check against your Hermes version (see the
compatibility checklist).

## What is in the package

| file | purpose |
|---|---|
| `harness/hermes/SKILL.md` | the installable skill: the periodic loop, stop rules, hard rules |
| `harness/hermes/prompts/annotate.md` | exact JSON shape, limits, exactness rules, worked Persian example |
| `harness/hermes/prompts/capture_policy.md` | voice / text / file capture rules |
| `harness/hermes/prompts/retrieval_policy.md` | how to retrieve and cite |
| `harness/hermes/job.template.yaml` | the job, `enabled: false`, concurrency 1, batch 1, caps, no credential fields |
| `harness/hermes/install.sh` | dry-run-by-default installer |
| `harness/hermes/loop.py` | reference driver `run_turn(tool_call, annotator, budget)` (tested; not the scheduler) |
| `harness/hermes/fake_annotator.py` | deterministic rule-based annotator for CI (no model) |

## Server prerequisites

The annotation tools exist only when the server runs with
`BRAIN_ENRICHMENT=on`, and submissions are accepted only with
`BRAIN_ENRICHMENT_WRITES=1` (otherwise `E_DISABLED`). Neither is set by this
package. With the feature off, the seven base tools are unchanged.

## Install (dry run first)

```
bash harness/hermes/install.sh --hermes-home /abs/path/to/hermes --account owner-1
```

This is a **dry run**: it prints `DRY-RUN` and the files it would write and
changes nothing. `--hermes-home` and `--account` are mandatory (nothing is
guessed). To install, repeat with `--apply`:

```
bash harness/hermes/install.sh --hermes-home /abs/path/to/hermes --account owner-1 --apply
```

It copies only the skill (`skills/0xbrain-annotator/`) and the **disabled** job
(`accounts/<account>/jobs/0xbrain-annotator.yaml`). It does not pip-install
anything, does not touch Docker or the archive, does not read or write any
credential, never overwrites an existing file unless `--force`, and never
replaces a job file that is already `enabled: true`.

## Enabling (manual, in this order)

1. The server side is deployed and `BRAIN_ENRICHMENT=on` is active, as confirmed by the
   owner (`docs/enrichment/usage.md` once it lands). Writes stay off until step 4.
2. In Hermes, sign the owner in to the 0xBrain connector (OAuth, done by the
   owner in the Hermes UI; this package stores nothing).
3. Fill in the job: adapt the key names to your Hermes job format, set the
   budgets below, keep concurrency 1 and batch size 1.
4. Set `BRAIN_ENRICHMENT_WRITES=1` on the server and restart it (owner action).
5. Run **one** turn by hand and check the receipt and `brain_artifact_read`.
6. Only then set `enabled: true`. Watch the first day.

To stop: set `enabled: false` (annotation stops; captures and ingestion are
unaffected) or `BRAIN_ENRICHMENT_WRITES=0` (the server refuses submissions).

## Budgets

| key in the template | meaning | default |
|---|---|---|
| `timeout_seconds` | wall-clock cap of one turn | 240 |
| `max_pages_per_turn` | pages (submissions) per turn | 3 |
| `cost.max_usd_per_turn` | stop when the turn's estimated spend would pass it | 0.25 |
| `cost.max_usd_per_day` | the daily cost cap, summed over turns | 1.00 |
| `cost.est_usd_per_call` | estimate used for the caps (set to your model's real cost) | 0.02 |
| `retry.max_attempts_transient` | attempts for 429 / 503 / timeout, in total | 2 |
| `retry.spacing_seconds` | wait between those attempts | 30 |

Costs are estimates owned by the harness; the server never sees them. A failed
provider call is charged, because it may still bill. The daily ledger must be
persisted by the harness between turns (`Budget.daily_spent` in `loop.py`).

## Failure modes

| situation | behaviour |
|---|---|
| credit exhausted, HTTP 402, auth failure | stop at once, one attempt, no retry |
| 429, 503, timeout | at most 2 attempts in total, spaced; then stop |
| any provider failure | nothing is submitted, **nothing is written to the archive** (no error text becomes a capture), capture and file ingestion keep working because they do not use the provider |
| lost response after a submit | retried with the **same** `idempotency_key`; the server answers `idempotent_replay: true`; no second revision |
| `E_CONFLICT`, `E_STALE_SOURCE` | the revision or the text moved: re-fetch once, a second time stops the turn |
| `E_VALIDATION` | reasons are field and code only; the same JSON is never resent; a unit rejected twice is skipped until the owner looks (`blocked` ledger) |
| `E_IDEMPOTENCY_CONFLICT` | stop; a new key is never invented to force a write |
| `E_TOO_LARGE` | the answer exceeded `BRAIN_ENRICHMENT_MAX_OUTPUT_BYTES`; send less |
| `E_DISABLED` | server writes are off; stop |
| model returns non-JSON | stop; no retry of the model |

What the server proves about a submission: every `chunk_ref` belongs to that
source at that `source_version`; every mention and quote is an exact substring
of the chunk text (no normalisation: Arabic `ي` is not Persian `ی`, a missing
zero-width non-joiner or `\n` for `\r\n` fails); link targets resolve; no
server-owned field was supplied. It does **not** prove that a summary, tag,
entity reading or a `supports` / `contradicts` link is true. All of it is
candidate tier (authority level 7), `unreviewed`, and never evidence.

## Compatibility checklist (Telegram file bridge and the harness host)

The repository contains a Telegram capture hook (`scripts/capture/telegram_capture_hook.py`)
and the remote ingest path. **Nothing below has been verified, and this
document does not claim that the existing plugin works** with the enrichment
skill or with any particular Hermes version. Check each item on the real host
before relying on it; record the answer.

| # | item | what to check | result |
|---|---|---|---|
| 1 | runtime Python | the Python that Hermes uses for plugins: version (the repo is developed on 3.13), and that it can import the hook's modules without installing anything into the system interpreter | not verified |
| 2 | YAML loader | which YAML library Hermes uses to read job and skill files, whether it accepts the quoted cron string and nested `cost` / `retry` / `tools` maps in `job.template.yaml`, and whether it reads `enabled: false` as a boolean | not verified |
| 3 | job key names | the real names Hermes expects for schedule, concurrency, timeout and tool allow-lists; map them and keep `enabled: false` until mapped | not verified |
| 4 | skill location and format | where Hermes loads skills from per account, and that `SKILL.md` front matter (`name`, `description`) is accepted | not verified |
| 5 | OAuth helper | how the connector sign-in is done and refreshed for the owner (client registration, token storage, expiry behaviour, what happens in an unattended job when the sign-in has expired: it must fail closed, not prompt) | not verified |
| 6 | tool allow-list enforcement | that Hermes really denies shell, Docker and file access to the annotator job, and exposes only the three enrichment tools | not verified |
| 7 | voice flow | that a voice message reaches the owner as a transcript for confirmation **before** any `brain_capture` call (see `prompts/capture_policy.md`); the existing hook is zero-LLM and captures media directly, so reconcile this with the confirm-then-capture rule | not verified |
| 8 | file flow | the confirm, upload, `upload_ref`, `brain_ingest_file` path end to end, including the upload page's owner authentication | not verified |
| 9 | secrets | the model key and the connector credentials are in the Hermes secret store only; grep the installed skill and job for credentials (none are shipped) | not verified |
| 10 | spend control | provider-side hard limit configured in addition to the template caps | not verified |

Repairing the Telegram file/voice bridge is out of scope for R1 (CONTRACT-E1
section 14).

## Testing without a model

`python -m pytest -q -p no:cacheprovider tests/enrichment/test_harness_contract.py`
runs the loop against the real `AnnotationService` and a fake store with the
rule-based `fake_annotator.py`: provider outages, retry without duplicates,
budgets, rejections, the install script (in a temp directory) and the policy
files. These tests do not show model quality or real-Hermes compatibility.
