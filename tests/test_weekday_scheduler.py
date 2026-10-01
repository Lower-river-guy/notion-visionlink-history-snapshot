"""The one weekday scheduler overrides a single execution and leaves the job dry-run."""

from __future__ import annotations

import base64
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _module():
    path = ROOT / "deploy" / "weekday_scheduler.py"
    spec = importlib.util.spec_from_file_location("weekday_scheduler", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _scheduler(module, **overrides):
    body = overrides.pop("body", module.override_body())
    job = {
        "name": (
            f"projects/{module.PROJECT}/locations/{module.REGION}/jobs/{module.SCHEDULER_NAME}"
        ),
        "schedule": module.SCHEDULE,
        "timeZone": module.TIME_ZONE,
        "state": "ENABLED",
        "scheduleTime": "2026-10-02T17:00:00Z",
        "retryConfig": {"retryCount": 0},
        "httpTarget": {
            "uri": module.RUN_URI,
            "httpMethod": "POST",
            "headers": {"Content-Type": "application/json"},
            "body": base64.b64encode(json.dumps(body).encode()).decode(),
            "oauthToken": {
                "serviceAccountEmail": module.OAUTH_SERVICE_ACCOUNT,
                "scope": module.OAUTH_SCOPE,
            },
        },
    }
    job.update(overrides)
    return job


def _run_check(job: dict) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "deploy" / "weekday_scheduler.py")],
        input=json.dumps(job),
        text=True,
        capture_output=True,
        check=False,
    )


def test_weekday_schedule_is_ten_am_pacific_only():
    module = _module()
    assert module.SCHEDULE == "0 10 * * 1-5"
    assert module.TIME_ZONE == "America/Los_Angeles"
    assert module.SCHEDULER_NAME == "notion-visionlink-history-weekday-10am"
    assert module.CLOUD_RUN_JOB == "notion-visionlink-history-snapshot"
    assert module.REGION == "us-west1"
    assert module.RUN_URI.endswith(
        "/jobs/notion-visionlink-history-snapshot:run"
    )
    env = module.override_body()["overrides"]["containerOverrides"][0]["env"]
    assert env == [
        {"name": "DRY_RUN", "value": "false"},
        {"name": "SNAPSHOT_SLOT", "value": "10:00"},
    ]
    encoded = json.dumps(module.override_body())
    assert "Notion_Google_Cloud_Sync" not in encoded
    assert "3db284de-cb43-80ed-9b6f-fc20d6cc20eb" not in encoded
    assert "0357c6bd-2650-4dfc-affb-72430beaca84" not in encoded


def test_checker_accepts_enabled_override_and_prints_next_run():
    module = _module()
    result = _run_check(_scheduler(module))
    assert result.returncode == 0, result.stderr
    assert "confirmed: schedule 0 10 * * 1-5" in result.stdout
    assert "confirmed: timeZone America/Los_Angeles" in result.stdout
    assert "confirmed: execution override DRY_RUN=false" in result.stdout
    assert "confirmed: execution override SNAPSHOT_SLOT=10:00" in result.stdout
    assert "confirmed: state ENABLED" in result.stdout
    assert "confirmed: next run 2026-10-02T17:00:00Z" in result.stdout
    assert "confirmed: resting Cloud Run job was not modified" in result.stdout
    assert module.OAUTH_SERVICE_ACCOUNT in result.stdout


def test_checker_rejects_paused_wrong_cron_and_dry_run_true_override():
    module = _module()
    paused = _scheduler(module, state="PAUSED")
    paused_result = _run_check(paused)
    assert paused_result.returncode == 1
    assert "ENABLED" in paused_result.stderr

    wrong_cron = _scheduler(module, schedule="0 6 * * 1-5")
    cron_result = _run_check(wrong_cron)
    assert cron_result.returncode == 1
    assert "0 6 * * 1-5" in cron_result.stderr

    wrong_zone = _scheduler(module, timeZone="UTC")
    zone_result = _run_check(wrong_zone)
    assert zone_result.returncode == 1

    dry = _scheduler(
        module,
        body={
            "overrides": {
                "containerOverrides": [
                    {"env": [{"name": "DRY_RUN", "value": "true"}]}
                ]
            }
        },
    )
    dry_result = _run_check(dry)
    assert dry_result.returncode == 1
    assert "DRY_RUN" in dry_result.stderr


def test_only_the_weekday_scheduler_is_kept():
    module = _module()
    canonical = _scheduler(module)
    six_am = {
        "name": f"projects/{module.PROJECT}/locations/us-west1/jobs/visionlink-history-6am",
        "httpTarget": {"uri": module.RUN_URI},
    }
    two_pm = {
        "name": f"projects/{module.PROJECT}/locations/us-central1/jobs/visionlink-history-2pm",
        "httpTarget": {"uri": "https://example.invalid/unused"},
    }
    other = {
        "name": f"projects/{module.PROJECT}/locations/us-west1/jobs/payroll-digest",
        "httpTarget": {"uri": "https://example.invalid/other"},
    }
    noon = {
        "name": f"projects/{module.PROJECT}/locations/us-west1/jobs/visionlink-history-12pm",
        "httpTarget": {"uri": "https://example.invalid/noon"},
    }
    deletes = module.schedulers_to_delete([canonical, six_am, two_pm, other, noon])
    assert deletes == [
        ("us-west1", "visionlink-history-6am"),
        ("us-central1", "visionlink-history-2pm"),
    ]


def test_ensure_script_does_not_execute_and_uses_the_override():
    script = (ROOT / "deploy" / "ensure_weekday_scheduler.sh").read_text()
    workflow = (ROOT / ".github" / "workflows" / "deploy-dry-run.yml").read_text()
    diagnostic = (ROOT / ".github" / "workflows" / "diagnostic-run.yml").read_text()
    subprocess.run(["bash", "-n", str(ROOT / "deploy" / "ensure_weekday_scheduler.sh")], check=True)
    assert "deploy/weekday_scheduler.py" in script
    assert "gcloud scheduler jobs create http" in script
    assert "gcloud scheduler jobs update http" in script
    assert "gcloud scheduler jobs delete" in script
    assert "gcloud scheduler jobs run" not in script
    assert "gcloud run jobs" not in script
    assert "--set-env-vars" not in script
    assert "--update-env-vars" not in script
    assert "bash deploy/ensure_weekday_scheduler.sh" in workflow
    assert "gcloud scheduler" not in diagnostic
    assert "workflow_dispatch:" in diagnostic
    assert "--update-env-vars=DRY_RUN=true" in diagnostic
