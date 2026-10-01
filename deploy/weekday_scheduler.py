#!/usr/bin/env python3
"""Weekday 10:00 AM Pacific scheduler for the VisionLink history snapshot.

Cloud Scheduler POSTs to the Cloud Run Admin API run method. The body is an
execution override, not a change to the job template. Cloud Run merges the
override environment with the job environment and replaces same-named
variables only:

https://cloud.google.com/run/docs/reference/rest/v2/projects.locations.jobs/run

The resting job stays DRY_RUN=true and does not set SNAPSHOT_SLOT. A scheduled
execution sets DRY_RUN=false and SNAPSHOT_SLOT=10:00 for that execution only.
"""

from __future__ import annotations

import base64
import json
import re
import shlex
import sys
from pathlib import Path

SCHEDULER_NAME = "notion-visionlink-history-weekday-10am"
SCHEDULE = "0 10 * * 1-5"
TIME_ZONE = "America/Los_Angeles"
PROJECT = "work-projects-486912"
PROJECT_NUMBER = "564809734796"
REGION = "us-west1"
CLOUD_RUN_JOB = "notion-visionlink-history-snapshot"
OAUTH_SERVICE_ACCOUNT = (
    "github-visionlink-deployer@work-projects-486912.iam.gserviceaccount.com"
)
SCHEDULER_AGENT = (
    f"service-{PROJECT_NUMBER}@gcp-sa-cloudscheduler.iam.gserviceaccount.com"
)
OAUTH_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
DESCRIPTION = (
    "Weekday 10:00 America/Los_Angeles production snapshot. "
    "Execution override sets DRY_RUN=false and SNAPSHOT_SLOT=10:00. "
    "The Cloud Run job stays DRY_RUN=true."
)
RUN_URI = (
    f"https://run.googleapis.com/v2/projects/{PROJECT}/locations/{REGION}"
    f"/jobs/{CLOUD_RUN_JOB}:run"
)

# Only these two variables are overridden. Database ids, the runtime service
# account, and NOTION_TOKEN stay on the job template.
OVERRIDE_ENV = (
    {"name": "DRY_RUN", "value": "false"},
    {"name": "SNAPSHOT_SLOT", "value": "10:00"},
)


class SchedulerMismatch(Exception):
    pass


def override_body() -> dict:
    return {
        "overrides": {
            "containerOverrides": [
                {"env": [dict(item) for item in OVERRIDE_ENV]},
            ]
        }
    }


def message_body() -> str:
    return json.dumps(override_body(), separators=(",", ":"))


def write_export(env_path: Path, body_path: Path) -> None:
    body_path.write_text(message_body(), encoding="utf-8")
    values = {
        "SCHEDULER_NAME": SCHEDULER_NAME,
        "SCHEDULE": SCHEDULE,
        "TIME_ZONE": TIME_ZONE,
        "PROJECT": PROJECT,
        "PROJECT_NUMBER": PROJECT_NUMBER,
        "REGION": REGION,
        "CLOUD_RUN_JOB": CLOUD_RUN_JOB,
        "OAUTH_SERVICE_ACCOUNT": OAUTH_SERVICE_ACCOUNT,
        "SCHEDULER_AGENT": SCHEDULER_AGENT,
        "OAUTH_SCOPE": OAUTH_SCOPE,
        "DESCRIPTION": DESCRIPTION,
        "RUN_URI": RUN_URI,
        "BODY_FILE": str(body_path),
    }
    lines = [f"{key}={shlex.quote(value)}" for key, value in values.items()]
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _resource_parts(job: dict) -> tuple[str, str]:
    parts = str(job.get("name") or "").split("/")
    if len(parts) < 6 or parts[-2] != "jobs":
        raise SchedulerMismatch(f"scheduler name {job.get('name')!r} is not a full resource name")
    return parts[-3], parts[-1]


def targets_history_job(job: dict) -> bool:
    uri = str((job.get("httpTarget") or {}).get("uri") or "")
    return f"/jobs/{CLOUD_RUN_JOB}:run" in uri


def legacy_schedule_name(job_id: str) -> bool:
    """Old 6 AM or 2 PM VisionLink history schedulers, if any still exist."""
    folded = job_id.lower().replace("_", "-")
    if "visionlink" not in folded:
        return False
    return re.search(
        r"(?:^|[^a-z0-9])(?:0?6-?am|0?2-?pm|0600|1400|14pm)(?:$|[^a-z0-9])",
        folded,
    ) is not None


def schedulers_to_delete(jobs: list[dict]) -> list[tuple[str, str]]:
    """Every VisionLink history scheduler except the one weekday 10:00 job."""
    found: list[tuple[str, str]] = []
    for job in jobs:
        if not isinstance(job, dict) or "name" not in job:
            continue
        location, job_id = _resource_parts(job)
        canonical = job_id == SCHEDULER_NAME and location == REGION
        if canonical:
            continue
        if targets_history_job(job) or legacy_schedule_name(job_id):
            found.append((location, job_id))
    return found


def _decode_body(raw: object) -> dict:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        raise SchedulerMismatch("scheduler request body is empty")
    text = raw.strip()
    if text.startswith("{"):
        parsed = json.loads(text)
    else:
        parsed = json.loads(base64.b64decode(text))
    if not isinstance(parsed, dict):
        raise SchedulerMismatch("scheduler request body is not a JSON object")
    return parsed


def check(job: dict) -> str:
    location, job_id = _resource_parts(job)
    if job_id != SCHEDULER_NAME or location != REGION:
        raise SchedulerMismatch(
            f"scheduler is {location}/{job_id}, expected {REGION}/{SCHEDULER_NAME}"
        )
    if job.get("schedule") != SCHEDULE:
        raise SchedulerMismatch(f"schedule is {job.get('schedule')!r}, expected {SCHEDULE!r}")
    if job.get("timeZone") != TIME_ZONE:
        raise SchedulerMismatch(
            f"timeZone is {job.get('timeZone')!r}, expected {TIME_ZONE!r}"
        )
    state = job.get("state")
    if state != "ENABLED":
        raise SchedulerMismatch(f"scheduler state is {state!r}, expected 'ENABLED'")

    http = job.get("httpTarget") or {}
    if http.get("uri") != RUN_URI:
        raise SchedulerMismatch(f"target uri is {http.get('uri')!r}, expected {RUN_URI!r}")
    method = str(http.get("httpMethod") or "POST").upper()
    if method != "POST":
        raise SchedulerMismatch(f"http method is {method!r}, expected POST")

    headers = {str(key).lower(): value for key, value in (http.get("headers") or {}).items()}
    content_type = str(headers.get("content-type") or "")
    if "application/json" not in content_type.lower():
        raise SchedulerMismatch(f"Content-Type is {content_type!r}, expected application/json")

    body = _decode_body(http.get("body"))
    if body != override_body():
        raise SchedulerMismatch(f"execution override is {body!r}, expected {override_body()!r}")

    oauth = http.get("oauthToken") or {}
    email = oauth.get("serviceAccountEmail")
    if email != OAUTH_SERVICE_ACCOUNT:
        raise SchedulerMismatch(
            f"oauth service account is {email!r}, expected {OAUTH_SERVICE_ACCOUNT}"
        )
    scope = oauth.get("scope")
    if scope not in (None, "", OAUTH_SCOPE):
        raise SchedulerMismatch(f"oauth scope is {scope!r}, expected {OAUTH_SCOPE}")

    retry_count = (job.get("retryConfig") or {}).get("retryCount", 0)
    if retry_count not in (0, "0", None):
        raise SchedulerMismatch(f"retryCount is {retry_count!r}, expected 0")

    next_run = str(job.get("scheduleTime") or "").strip()
    if not next_run:
        raise SchedulerMismatch("scheduleTime is empty; the next run time is not set")
    return next_run


def confirm(job: dict) -> None:
    next_run = check(job)
    print(f"confirmed: scheduler {REGION}/{SCHEDULER_NAME}")
    print("confirmed: state ENABLED")
    print(f"confirmed: schedule {SCHEDULE}")
    print(f"confirmed: timeZone {TIME_ZONE}")
    print(f"confirmed: target {RUN_URI}")
    print("confirmed: execution override DRY_RUN=false")
    print("confirmed: execution override SNAPSHOT_SLOT=10:00")
    print(f"confirmed: oauth {OAUTH_SERVICE_ACCOUNT}")
    print("confirmed: retryCount 0")
    print(f"confirmed: next run {next_run}")
    print("confirmed: resting Cloud Run job was not modified")


def _load_jobs(paths: list[str]) -> list[dict]:
    jobs: list[dict] = []
    for path in paths:
        text = Path(path).read_text(encoding="utf-8").strip()
        if not text:
            continue
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            parsed = [parsed]
        if not isinstance(parsed, list):
            raise SchedulerMismatch(f"{path} is not a scheduler list")
        jobs.extend(item for item in parsed if isinstance(item, dict))
    return jobs


def main(argv: list[str]) -> int:
    if "--export" in argv:
        export_at = argv.index("--export")
        body_at = argv.index("--body-file")
        write_export(Path(argv[export_at + 1]), Path(argv[body_at + 1]))
        return 0
    if "--print-deletes" in argv:
        at = argv.index("--print-deletes")
        for location, job_id in schedulers_to_delete(_load_jobs(argv[at + 1 :])):
            print(f"{location}\t{job_id}")
        return 0
    try:
        job = json.load(sys.stdin)
        confirm(job)
    except (json.JSONDecodeError, SchedulerMismatch, KeyError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
