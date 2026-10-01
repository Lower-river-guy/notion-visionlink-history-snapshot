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
    assert "github-visionlink-deployer@work-projects-486912.iam.gserviceaccount.com" in workflow
    assert "google-github-actions/auth@v2" in workflow
    assert "id-token: write" in workflow
    assert "environment: gcp" in workflow
    assert "developer.gserviceaccount.com" not in script
    assert "_DRY_RUN: \"false\"" not in build
    assert "_DRY_RUN=false" not in workflow
    assert "deploy/ensure_weekday_scheduler.sh" in workflow
    assert "gcloud run jobs execute" not in workflow
    assert "gcloud run jobs describe notion-visionlink-history-snapshot" in workflow
    assert "gcloud builds submit --config cloudbuild.yaml --project work-projects-486912" in workflow

    env_lines = [line for line in script.splitlines() if "ENV_VARS=" in line or "set-env-vars" in line]
    assert env_lines
    for line in env_lines:
        assert "DRY_RUN=false" not in line
    assert '!= "true"' in script

    assert 'env.get("NOTION_TOKEN")' in config
    assert "Notion_Google_Cloud_Sync" not in config


def test_bootstrap_wif_is_repository_scoped_and_does_not_deploy():
    script = (ROOT / "deploy" / "bootstrap-gcp-auth.sh").read_text()
    provider = (
        "projects/564809734796/locations/global/workloadIdentityPools/"
        "github-visionlink/providers/github"
    )
    deployer = "github-visionlink-deployer@work-projects-486912.iam.gserviceaccount.com"
    runner = "visionlink-history-runner@work-projects-486912.iam.gserviceaccount.com"
    assert "GCP_WORKLOAD_IDENTITY_PROVIDER=" in script
    assert provider in script or "providers/${PROVIDER}" in script
    assert deployer in script or 'DEPLOYER_NAME="github-visionlink-deployer"' in script
    assert runner in script
    assert "assertion.repository=='Lower-river-guy/notion-visionlink-history-snapshot'" in script
    assert "attribute.repository/${REPO}" in script
    assert "roles/iam.workloadIdentityUser" in script
    assert "roles/iam.serviceAccountUser" in script
    assert "roles/run.developer" in script
    assert "roles/cloudbuild.builds.editor" in script
    assert "roles/serviceusage.serviceUsageConsumer" in script
    assert "roles/storage.objectAdmin" in script
    assert "gs://${PROJECT}_cloudbuild" in script
    assert 'BUILD_SUBMIT_ROLE="visionlinkHistoryBuildSubmitter"' in script
    assert 'LOG_READER_ROLE="visionlinkHistoryLogReader"' in script
    assert "logging.logEntries.list" in script
    assert "roles/storage.admin" not in script
    assert "roles/owner" not in script
    assert "roles/editor" not in script
    assert "keys create" not in script
    assert "gcloud scheduler jobs" not in script
    assert "gcloud run jobs" not in script
    assert "DRY_RUN=false" not in script
    assert "cloudscheduler.jobs.delete" in script
    assert "service-${PROJECT_NUMBER}@gcp-sa-cloudscheduler.iam.gserviceaccount.com" in script
    assert "--service=cloudscheduler.googleapis.com" in script
    assert 'member="serviceAccount:${SCHEDULER_AGENT}"' in script
    assert "Scheduled runs need that binding" in script
    assert "iam.serviceAccounts.getIamPolicy" in script
    assert "attribute.repository_owner/" not in script
    assert 'POOL="github-visionlink"' in script
    assert 'PROVIDER="github"' in script
    assert 'DEPLOYER_NAME="github-visionlink-deployer"' in script
    assert 'SCHEDULER_ROLE="visionlinkHistorySchedulerDeployer"' in script
    assert "service-accounts describe" in script
    assert "workload-identity-pools update" in script
    assert "GitHub VisionLink history snapshot" not in script

    names = {
        'POOL_DISPLAY_NAME="VisionLink GitHub"': 32,
        'PROVIDER_DISPLAY_NAME="GitHub Actions"': 32,
        'DEPLOYER_DISPLAY_NAME="GitHub Actions deployer for VisionLink history snapshot"': 100,
        'SCHEDULER_ROLE_TITLE="VisionLink history scheduler deploy"': 100,
        'SCHEDULER_ROLE_DESCRIPTION="Create, update, and delete Cloud Scheduler jobs for the VisionLink history snapshot. Does not execute them."': 256,
        'BUILD_SUBMIT_ROLE_TITLE="VisionLink history build submit"': 100,
        'BUILD_SUBMIT_ROLE_DESCRIPTION="List Cloud Storage buckets so the deployer can stage Cloud Build source."': 256,
        'LOG_READER_ROLE_TITLE="VisionLink history log reader"': 100,
        'LOG_READER_ROLE_DESCRIPTION="Read Cloud Logging entries for one VisionLink history execution."': 256,
        'AR_DESCRIPTION="Images for notion-visionlink-history-snapshot"': 256,
    }
    for assignment, limit in names.items():
        assert assignment in script
        value = assignment.split('="', 1)[1][:-1]
        assert len(value) <= limit


def test_diagnostic_workflow_is_manual_and_restores_dry_run():
    text = (ROOT / ".github" / "workflows" / "diagnostic-run.yml").read_text()
    assert "workflow_dispatch:" in text
    assert "\n  push:" not in text
    assert "pull_request:" not in text
    assert "schedule:" not in text
    assert "cancel-in-progress: false" in text
    assert "environment: gcp" in text
    assert "google-github-actions/auth@v2" in text
    assert "GCP_WORKLOAD_IDENTITY_PROVIDER" in text
    assert "GCP_SERVICE_ACCOUNT" in text
    assert "work-projects-486912" in text
    assert "us-west1" in text
    assert "notion-visionlink-history-snapshot" in text
    assert text.count("gcloud run jobs execute") == 1
    assert "--update-env-vars=DRY_RUN=false" in text
    assert "--update-env-vars=DRY_RUN=true" in text
    assert "--max-retries=0" in text
    assert "--max-retries=3" in text
    assert "if: always()" in text
    assert "run.googleapis.com/execution_name" in text
    assert "upload-artifact@v4" in text
    assert "gcloud scheduler" not in text
    assert "--image" not in text
    assert "--set-secrets" not in text
    assert "--set-env-vars" not in text
    assert "--service-account" not in text
    assert "roles/owner" not in text
    assert "roles/editor" not in text
    assert text.index("Cloud Logging read failed") < text.index("gcloud run jobs execute")
