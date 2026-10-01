# Cat VisionLink History Snapshot

Append-only Cloud Run Job that copies the current Cat VisionLink fleet into
Cat VisionLink History. Each weekday snapshot answers questions such as where
a machine was, which job it was on, what its hours were, and when VisionLink
last reported, at a specific copy time.

Version: `0.01.00`

## The only write path

Read **Cat VisionLink** (database `3db284de-cb43-80ed-9b6f-fc20d6cc20eb`).
Append new pages to **Cat VisionLink History** (looked up by exact title).

This job does not update, archive, or delete anything. It does not write to
Machines. It does not write to Works Manager Project List. It does not change
the source database. It does not delete historical rows, including when a
retry finds rows it already created.

Relation properties are never written. That includes source **Assigned
Contact** and **Works Manager Project**, and history **Machine** and
**Related to Projects (VisionLink History)**. Those history relations are
two-way, so setting them would change Machines or the project list.

## Snapshot Date and Last Reported

Business timezone: `America/Los_Angeles` (IANA, so Pacific Daylight Time and
Pacific Standard Time both resolve correctly).

- **Snapshot Date** is when this Cloud Run execution copied current state. It
  is the process clock in `America/Los_Angeles`, stored as a Notion datetime
  with that time zone. It is not the slot boundary and it is not Last Reported.
- **Last Reported** is copied from the source page unchanged.

## Idempotency

Unique key: **Machine ID + Snapshot Run ID**.

A retry of the same scheduled slot does not create another history row. A
later slot always does, even when the machine data is unchanged. Previous
rows are left as they are.

Run id rules:

1. `SNAPSHOT_RUN_ID`, when set, is used as-is. Leave it unset in production.
   It exists for a controlled rerun or a test.
2. Scheduled runs set `SNAPSHOT_SLOT` to `06:00` or `14:00`. The run id is
   that slot's civil time on the latest occurrence that is not in the future:
   `2026-10-01T06:00:00_America-Los_Angeles` or
   `2026-10-01T14:00:00_America-Los_Angeles`.
   The zone name is part of the id. A fixed UTC offset is not. A task retry
   later the same day still produces the same id, so rows already written are
   skipped and rows that failed can be created. The 6:00 AM and 2:00 PM slots
   are different ids.
3. A manual execution does not set `SNAPSHOT_SLOT`. Its run id is
   `manual-{CLOUD_RUN_EXECUTION}_America-Los_Angeles`. Cloud Run keeps
   `CLOUD_RUN_EXECUTION` stable across task retries and assigns a new value
   for a later execution, so a manual retry does not duplicate itself and
   does not collide with either weekday slot. Being close to 6:00 AM or
   2:00 PM does not turn a manual run into a slot run.
4. Without `CLOUD_RUN_EXECUTION` (a local process), a manual id uses the
   civil timestamp. Local retries are stable only when `SNAPSHOT_RUN_ID` or
   `CLOUD_RUN_EXECUTION` is set.

Cloud Scheduler is split into two jobs so each invocation passes its own
`SNAPSHOT_SLOT`. A single cron cannot attach a different slot to 6:00 AM and
2:00 PM. Task retries are capped (`--max-retries=3`) so a failed attempt
cannot run long enough to adopt the next day's slot id.

## Schedule

Twice each weekday, America/Los_Angeles:

- 6:00 AM, `SNAPSHOT_SLOT=06:00`
- 2:00 PM, `SNAPSHOT_SLOT=14:00`

Equivalent cron: `0 6,14 * * 1-5`. The scheduler timezone is
`America/Los_Angeles`, not a fixed offset.

The scheduler is created only after a production `DRY_RUN=true` execution is
clean. Until then it stays uncreated.

## Dry run

`DRY_RUN=true` connects to Notion, reads the whole source database, loads the
history schema, builds the history pages, validates them, and logs what it
would create. It does not create or modify pages.

```bash
export NOTION_TOKEN=...   # do not commit this
export DRY_RUN=true
python -m src.main
```

The job's deployed default is `DRY_RUN=false`. A dry-run execution overrides
that variable for one execution only.

## Tests

```bash
python -m pip install -r requirements-dev.txt
python -m pytest
```

## Deploy

Project `work-projects-486912`, region `us-west1`, job
`notion-visionlink-history-snapshot`.

The Notion integration token lives in Secret Manager as `NOTION_TOKEN`. It is
mounted as an environment variable. It is not stored in source, the
Dockerfile, logs, or git.

Runtime service account `notion-visionlink-history-snapshot` gets Secret
Manager accessor on that secret and log writing. A separate scheduler service
account receives `run.jobs.run` on this job only.

```bash
gcloud config set project work-projects-486912

# Deploy with DRY_RUN=true. Do not flip it to false until a dry run is clean.
gcloud run jobs deploy notion-visionlink-history-snapshot \
  --source . \
  --region us-west1 \
  --service-account notion-visionlink-history-snapshot@work-projects-486912.iam.gserviceaccount.com \
  --set-secrets NOTION_TOKEN=NOTION_TOKEN:latest \
  --set-env-vars ^@^DRY_RUN=true@SOURCE_DATABASE_ID=3db284de-cb43-80ed-9b6f-fc20d6cc20eb@DESTINATION_DATABASE_TITLE=Cat VisionLink History@DESTINATION_DATABASE_ID=0357c6bd-2650-4dfc-affb-72430beaca84@BUSINESS_TIMEZONE=America/Los_Angeles \
  --tasks 1 \
  --parallelism 1 \
  --max-retries 3 \
  --task-timeout 1800 \
  --memory 512Mi \
  --cpu 1

gcloud run jobs execute notion-visionlink-history-snapshot \
  --region us-west1 \
  --wait
```

`DESTINATION_DATABASE_ID` is the history database found via the Notion API.
Every run still checks that its title is exactly `Cat VisionLink History`.

Create the two weekday triggers only when the dry run accessed both
databases, mappings validated, failed count was 0, and writes performed was
0. At that point set the job's `DRY_RUN` to `false` and create the scheduler.
If the dry run is not clean, leave `DRY_RUN=true` and do not create the
scheduler.

## Configuration

| Variable | Purpose |
| --- | --- |
| `NOTION_TOKEN` | Notion integration secret. Required. Never log it. |
| `DRY_RUN` | `true` proposes pages and writes nothing. Default `false`. |
| `SOURCE_DATABASE_ID` | Source database. Default is Cat VisionLink. |
| `DESTINATION_DATABASE_TITLE` | Exact history title. Default `Cat VisionLink History`. |
| `DESTINATION_DATABASE_ID` | Optional pin. The title must still match. |
| `SNAPSHOT_SLOT` | `06:00` or `14:00` for a scheduled slot. Unset for manual runs. |
| `SNAPSHOT_RUN_ID` | Optional explicit run id. Unset in production. |
| `BUSINESS_TIMEZONE` | Default `America/Los_Angeles`. |
| `CLOUD_RUN_EXECUTION` | Set by Cloud Run. Used for manual run ids. |

## Safety rules

- Read Cat VisionLink. Append to Cat VisionLink History. Nothing else.
- Do not write to Machines or Works Manager Project List.
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
src/models.py          result and mapping records
tests/                 unit tests
Dockerfile             Python 3.12
```
