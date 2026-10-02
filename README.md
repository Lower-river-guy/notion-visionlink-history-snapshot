# Cat VisionLink History Snapshot

Append-only Cloud Run Job that copies the current Cat VisionLink fleet into
Cat VisionLink History. Each weekday snapshot answers questions such as where
a machine was, which job it was on, what its hours were, and when VisionLink
last reported, at a specific copy time.

Version: `0.01.04`

## The only write path

Read **Cat VisionLink** (database `3db284de-cb43-80ed-9b6f-fc20d6cc20eb`).
Append new pages to **Cat VisionLink History** (looked up by exact title).

History rows are still append-only. After the snapshot, the same run writes
three number properties on each Machines page: **Hours This Week**, **Hours
Last Week**, and **Hours This Month**. It does not update any other Machines
property. It does not write to Works Manager Project List. It does not change
the source database. It does not delete historical rows, including when a
retry finds rows it already created. `DRY_RUN=true` logs the three values
and does not patch Machines.

Each new history page sets **Machine** when Machines
(database `8248e735-8458-4a00-9b41-cbe1eff6b975`) has exactly one record whose
**Machine ID** title equals the snapshot Machine ID. The Machines data source
id `241a2acd-f833-410a-9c0a-99e376add55e` is not the API query target. Notion
fills **VisionLink History** on that Machines page because the relation is
two-way. The utilization step then sets only the three hour-delta numbers
on that Machines page. It does not set **Related to
Projects (VisionLink History)**. Source **Assigned Contact** and **Works
Manager Project** are not copied. A missing or duplicate Machines match fails
that record only.

## Machine hour deltas

The three Machines numbers are hour-meter deltas from **Cat VisionLink
History**, not sums of the readings and not Notion formulas. Periods use
`America/Los_Angeles` and are half-open:

- **Hours This Week**: Monday 00:00 through the next Monday 00:00
- **Hours Last Week**: the previous Monday 00:00 through this Monday 00:00
- **Hours This Month**: the first of the month 00:00 through the next month 00:00

Each value is the latest meter inside the period minus the baseline meter.
The baseline is a snapshot exactly at the period start (Monday 00:00, or
the 1st at 00:00) when one exists, and otherwise the nearest reading before
that start. It is blank when either reading is missing, when two rows that
share a timestamp or Snapshot Run ID disagree, when the meter decreases,
or when the meter gains more hours than the wall-clock time between the
readings (plus 0.1 hour). That last case is logged with the machine id,
both readings, the delta, and the elapsed hours. A zero delta is written
as `0`. Results are rounded half up to one decimal place.

`DRY_RUN=true` still calculates the values and logs a sample. It does not
patch Machines.

Notion also has formula and rollup properties from an earlier pass (`Hours
Gained`, `Days Observed`, `Hours per Day`, `Utilization`, `Latest Snapshot`,
`First Snapshot`, `Latest Hours`, `Earliest Hours`, `Snapshot Count`). The
job does not read or write those. There is no `Weekly Hours Added` property
in this repository. That name was a Notion placeholder and was renamed to
the formula `Hours Gained`; it was left in place.

## Snapshot Date and Last Reported

Business timezone: `America/Los_Angeles` (IANA, so Pacific Daylight Time and
Pacific Standard Time both resolve correctly).

- **Snapshot Date** is when this Cloud Run execution copied current state. It
  is the process clock in `America/Los_Angeles`, stored as a Notion datetime
  with that time zone. It is not the slot boundary and it is not Last Reported.
- **Last Reported** is copied from the source page unchanged.

## Idempotency

Unique key: **Machine ID + Snapshot Run ID**.

A retry of the same 10:00 AM slot does not create another history row. The
next weekday's 10:00 AM run does, even when the machine data is unchanged.
Previous rows are left as they are.

Run id rules:

1. `SNAPSHOT_RUN_ID`, when set, is used as-is. Leave it unset in production.
   It exists for a controlled rerun or a test.
2. Scheduled runs set `SNAPSHOT_SLOT` to `10:00`. The run id is that slot's
   civil time on the latest occurrence that is not in the future:
   `2026-10-01T10:00:00_America-Los_Angeles`.
   The zone name is part of the id. A fixed UTC offset is not. A task retry
   later the same day still produces the same id, so rows already written are
   skipped and rows that failed can be created.
3. A manual execution does not set `SNAPSHOT_SLOT`. Its run id is
   `manual-{CLOUD_RUN_EXECUTION}_America-Los_Angeles`. Cloud Run keeps
   `CLOUD_RUN_EXECUTION` stable across task retries and assigns a new value
   for a later execution, so a manual retry does not duplicate itself and
   does not collide with the weekday slot. Being close to 10:00 AM does not
   turn a manual run into a slot run.
4. Without `CLOUD_RUN_EXECUTION` (a local process), a manual id uses the
   civil timestamp. Local retries are stable only when `SNAPSHOT_RUN_ID` or
   `CLOUD_RUN_EXECUTION` is set.

The weekday scheduler passes `SNAPSHOT_SLOT=10:00` only as an execution
override. Task retries are capped (`--max-retries=3`) so a failed attempt
cannot run long enough to adopt the next day's slot id.
`deploy/deploy-dry-run.sh` does not create that scheduler job.

## Schedule

One Cloud Scheduler job, `notion-visionlink-history-weekday-10am`:

- 10:00 AM America/Los_Angeles, Monday–Friday
- Cron: `0 10 * * 1-5`
- Time zone: `America/Los_Angeles` (not a fixed UTC offset)
- Target: Cloud Run job `notion-visionlink-history-snapshot` in `us-west1`
- Execution override: `DRY_RUN=false` and `SNAPSHOT_SLOT=10:00`

The Cloud Run job template is `DRY_RUN=false` with `SNAPSHOT_SLOT` unset.
Cloud Run merges the scheduler override into that execution only, so the
weekday run still sets `SNAPSHOT_SLOT=10:00` and states `DRY_RUN=false`.
There is no second Cloud Run job and no other VisionLink history schedule.

The scheduler authenticates as
`github-visionlink-deployer@work-projects-486912.iam.gserviceaccount.com`.
That call works only after
`service-564809734796@gcp-sa-cloudscheduler.iam.gserviceaccount.com` has
`roles/iam.serviceAccountUser` on the deployer. `deploy/bootstrap-gcp-auth.sh`
grants it from an admin `gcloud` session. The deploy workflow cannot: the
deployer lacks `iam.serviceAccounts.getIamPolicy`.

## Dry run

`DRY_RUN=true` connects to Notion, reads the whole source database, loads the
history schema, builds the history pages, validates them, and logs what it
would create. It does not create or modify pages.

The manual workflow `.github/workflows/diagnostic-run.yml` is
`workflow_dispatch` only. It runs the deployed job once with an execution
override of `DRY_RUN=true`, then checks that the execution spec is
`DRY_RUN=true` before waiting for the container. If that check fails, it
cancels the execution. It does not update the job template and does not
trigger Cloud Scheduler. The resting job stays `DRY_RUN=false`.

```bash
export NOTION_TOKEN=...   # do not commit this
export DRY_RUN=true
python -m src.main
```

The Cloud Run job is deployed with `DRY_RUN=false`. A local process treats an
unset `DRY_RUN` as false, so set `DRY_RUN=true` before any local run that
must not write.

## Tests

```bash
python -m pip install -r requirements-dev.txt
python -m pytest
```

## Deploy

Project `work-projects-486912`, region `us-west1`, job
`notion-visionlink-history-snapshot`.

Requires an active `gcloud` account that can administer this project. The
script deploys `DRY_RUN=false` and does not execute the job. It does not
create Cloud Scheduler. GitHub Actions deploy is the path that updates the
live job.

```bash
deploy/deploy-dry-run.sh
```

The application reads the environment variable `NOTION_TOKEN`. The token
already lives in Secret Manager as `Notion_Google_Cloud_Sync`. Cloud Run
mounts that secret with:

`NOTION_TOKEN=Notion_Google_Cloud_Sync:latest`

The script refuses to create a replacement secret and never prints the secret
value. The token is not stored in source, the Dockerfile, logs, or git.

Runtime service account
`visionlink-history-runner@work-projects-486912.iam.gserviceaccount.com`
must already exist. The script does not create a service account. It receives
only:

- `roles/secretmanager.secretAccessor` on secret `Notion_Google_Cloud_Sync`
- `roles/logging.logWriter` on the project
- `roles/run.invoker` on this Cloud Run job

The deploying principal and
`${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com` receive
`roles/iam.serviceAccountUser` on that runner so they can deploy the job as
it. That Cloud Build service account also receives `roles/run.developer` and
`roles/artifactregistry.writer` so a later build can update this job. The
runner is not granted Owner or Editor.

## Continuous deploy

Pushes run the test suite. When the repository Actions secrets
`GCP_WORKLOAD_IDENTITY_PROVIDER` and `GCP_SERVICE_ACCOUNT` are set,
`.github/workflows/deploy-dry-run.yml` authenticates with Workload Identity
Federation as
`github-visionlink-deployer@work-projects-486912.iam.gserviceaccount.com`
and submits `cloudbuild.yaml`. That build updates this Cloud Run job and keeps
`DRY_RUN=false` and `NOTION_TOKEN=Notion_Google_Cloud_Sync:latest`. It does
not execute the job. The same workflow then runs
`deploy/ensure_weekday_scheduler.sh`, which creates or updates the one
weekday 10:00 AM Pacific scheduler. A scheduled execution overrides
`DRY_RUN=false` and `SNAPSHOT_SLOT=10:00`. The resting job is `DRY_RUN=false`.

The runtime account is not the GitHub deployment identity. One-time setup is
`deploy/bootstrap-gcp-auth.sh`. It does not deploy the application and does
not create the scheduler. It grants the deployer permission to create,
update, and delete Cloud Scheduler jobs. It also grants the Cloud Scheduler
service agent `roles/iam.serviceAccountUser` on the deployer. Scheduled runs
cannot mint an OAuth token without that binding, and the GitHub deployer
cannot set or read it.

`DESTINATION_DATABASE_ID` is `0357c6bd-2650-4dfc-affb-72430beaca84`, the
history database found via the Notion API. Every run still checks that its
title is exactly `Cat VisionLink History`.

## Configuration

| Variable | Purpose |
| --- | --- |
| `NOTION_TOKEN` | Notion integration secret. Required. Cloud Run maps it from Secret Manager `Notion_Google_Cloud_Sync`. Never log it. |
| `DRY_RUN` | `true` proposes pages and writes nothing. Default `false`. |
| `SOURCE_DATABASE_ID` | Source database. Default is Cat VisionLink. |
| `DESTINATION_DATABASE_TITLE` | Exact history title. Default `Cat VisionLink History`. |
| `DESTINATION_DATABASE_ID` | Optional pin. The title must still match. |
| `SNAPSHOT_SLOT` | `10:00` on the weekday scheduler execution only. Unset on the resting job and on manual runs. |
| `SNAPSHOT_RUN_ID` | Optional explicit run id. Unset in production. |
| `BUSINESS_TIMEZONE` | Default `America/Los_Angeles`. |
| `CLOUD_RUN_EXECUTION` | Set by Cloud Run. Used for manual run ids. |

## Safety rules

- Read Cat VisionLink. Append to Cat VisionLink History. Then set only
  **Hours This Week**, **Hours Last Week**, and **Hours This Month** on
  Machines.
- Do not write any other Machines property, and do not write Works Manager
  Project List.
- Do not edit, archive, delete, or rename anything on the source.
- Do not delete or update historical snapshots.
- Do not put the Notion token in source, images, logs, or git.
- One machine failure is logged with its Machine ID and a token-free error.
  Remaining machines continue. The process exits non-zero if any machine
  failed or the snapshot could not be read.
- Notion HTTP 429, 500, 502, 503, 504, timeouts, and connection errors retry
  with bounded exponential backoff and honor `Retry-After` up to 60 seconds.
- Select values that are not options on the history property fail that
  machine with no page written, so a retry can still create a complete row.
  Empty optional fields are omitted.
- Latitude and longitude are not separate history properties. They are
  preserved inside the copied **Map** URL (`query=latitude,longitude`).

## Layout

```
src/main.py            entrypoint, structured logs, exit status
src/config.py          environment and version
src/notion_client.py   pagination, retries, write guards
src/snapshot.py        mapping, run id, dry run
src/utilization.py     week and month hour-meter deltas
src/models.py          result and mapping records
tests/                 unit tests
deploy/deploy-dry-run.sh
deploy/bootstrap-gcp-auth.sh
deploy/assert_job_config.py
deploy/weekday_scheduler.py
deploy/ensure_weekday_scheduler.sh
cloudbuild.yaml
.github/workflows/deploy-dry-run.yml
.github/workflows/diagnostic-run.yml
Dockerfile             Python 3.12
```
