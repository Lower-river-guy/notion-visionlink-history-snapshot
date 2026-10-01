"""Deploy config stays on the live dry-run mapping."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "3db284de-cb43-80ed-9b6f-fc20d6cc20eb"
DESTINATION = "0357c6bd-2650-4dfc-affb-72430beaca84"
RUNNER = "visionlink-history-runner@work-projects-486912.iam.gserviceaccount.com"
MAPPING = "NOTION_TOKEN=Notion_Google_Cloud_Sync:latest"


def _run_assert(job: dict) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "deploy" / "assert_job_config.py")],
        input=json.dumps(job),
        text=True,
        capture_output=True,
        check=False,
    )


def _job(secret_item: dict | None = None, service_account: str = RUNNER) -> dict:
    if secret_item is None:
        secret_item = {
            "name": "NOTION_TOKEN",
            "valueSource": {
                "secretKeyRef": {
                    "secret": "Notion_Google_Cloud_Sync",
                    "version": "latest",
                }
            },
        }
    env = [
        {"name": "DRY_RUN", "value": "true"},
        {"name": "SOURCE_DATABASE_ID", "value": SOURCE},
        {"name": "DESTINATION_DATABASE_ID", "value": DESTINATION},
        {"name": "DESTINATION_DATABASE_TITLE", "value": "Cat VisionLink History"},
        {"name": "BUSINESS_TIMEZONE", "value": "America/Los_Angeles"},
        secret_item,
    ]
    return {
        "spec": {
            "template": {
                "spec": {
                    "template": {
                        "spec": {
                            "serviceAccountName": service_account,
                            "containers": [{"image": "example", "env": env}],
                        }
                    }
                }
            }
        }
    }


def test_assert_accepts_live_secret_mapping():
    result = _run_assert(_job())
    assert result.returncode == 0, result.stderr
    assert "confirmed: DRY_RUN=true" in result.stdout
    assert f"confirmed: {MAPPING}" in result.stdout
    assert f"confirmed: service account {RUNNER}" in result.stdout
    assert "read-only" in result.stdout
    assert "append-only" in result.stdout


def test_assert_accepts_value_from_secret_shape():
    job = _job(
        {
            "name": "NOTION_TOKEN",
            "valueFrom": {
                "secretKeyRef": {
                    "name": "Notion_Google_Cloud_Sync",
                    "key": "latest",
                }
            },
        }
    )
    result = _run_assert(job)
    assert result.returncode == 0, result.stderr


def test_assert_rejects_secret_renamed_over_env_var():
    job = _job(
        {
            "name": "Notion_Google_Cloud_Sync",
            "valueSource": {
                "secretKeyRef": {
                    "secret": "Notion_Google_Cloud_Sync",
                    "version": "latest",
                }
            },
        }
    )
    result = _run_assert(job)
    assert result.returncode == 1
    assert MAPPING in result.stderr


def test_assert_rejects_env_var_pointing_at_secret_named_notion_token():
    job = _job(
        {
            "name": "NOTION_TOKEN",
            "valueSource": {
                "secretKeyRef": {"secret": "NOTION_TOKEN", "version": "latest"}
            },
        }
    )
    result = _run_assert(job)
    assert result.returncode == 1
    assert MAPPING in result.stderr


def test_assert_rejects_literal_token_without_echoing_it():
    literal = "super-secret-token-value"
    job = _job({"name": "NOTION_TOKEN", "value": literal})
    result = _run_assert(job)
    assert result.returncode == 1
    assert literal not in result.stdout
    assert literal not in result.stderr


def test_assert_rejects_dry_run_false_and_wrong_runner():
    wrong_runner = _job(
        service_account="notion-visionlink-history-runner@work-projects-486912.iam.gserviceaccount.com"
    )
    runner_result = _run_assert(wrong_runner)
    assert runner_result.returncode == 1
    assert "service account" in runner_result.stderr

    dry_run_false = _job()
    dry_run_false["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"] = "false"
    dry_result = _run_assert(dry_run_false)
    assert dry_result.returncode == 1
    assert "DRY_RUN" in dry_result.stderr


def test_deploy_files_keep_live_mapping():
    script = (ROOT / "deploy" / "deploy-dry-run.sh").read_text()
    build = (ROOT / "cloudbuild.yaml").read_text()
    workflow = (ROOT / ".github" / "workflows" / "deploy-dry-run.yml").read_text()
    config = (ROOT / "src" / "config.py").read_text()

    for text in (script, build):
        assert MAPPING in text
        assert "DRY_RUN=true" in text
        assert RUNNER.split("@")[0] in text
        assert "@cloudbuild.gserviceaccount.com" in text
        assert "notion-visionlink-history-runner" not in text
        assert "@clou.gserviceaccount.com" not in text
        assert "@cloudservices.gserviceaccount.com" not in text
        assert "gcloud scheduler" not in text
        assert "service-accounts create" not in text

    assert "@cloudbuild.gserviceaccount.com" in script
    assert "@cloudbuild.gserviceaccount.com" in workflow
    assert "developer.gserviceaccount.com" not in script
    assert "_DRY_RUN: \"false\"" not in build
    assert "_DRY_RUN=false" not in workflow
    assert "gcloud scheduler" not in workflow
    assert "gcloud builds submit --config cloudbuild.yaml --project work-projects-486912" in workflow

    env_lines = [line for line in script.splitlines() if "ENV_VARS=" in line or "set-env-vars" in line]
    assert env_lines
    for line in env_lines:
        assert "DRY_RUN=false" not in line
    assert '!= "true"' in script

    assert 'env.get("NOTION_TOKEN")' in config
    assert "Notion_Google_Cloud_Sync" not in config
