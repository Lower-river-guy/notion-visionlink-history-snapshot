#!/usr/bin/env bash
# Deploy the VisionLink history snapshot Cloud Run job and run it once.
#
# This script always deploys DRY_RUN=true. It does not set DRY_RUN=false,
# does not create Cloud Scheduler, and does not print secret values.
#
# Application environment variable: NOTION_TOKEN
# Secret Manager secret:            Notion_Google_Cloud_Sync
# Cloud Run mapping:                NOTION_TOKEN=Notion_Google_Cloud_Sync:latest
#
# Project: work-projects-486912
# Region:  us-west1
# Job:     notion-visionlink-history-snapshot
# Runner:  visionlink-history-runner@work-projects-486912.iam.gserviceaccount.com

set -euo pipefail

PROJECT="work-projects-486912"
REGION="us-west1"
JOB="notion-visionlink-history-snapshot"
SA_EMAIL="visionlink-history-runner@${PROJECT}.iam.gserviceaccount.com"
SECRET_RESOURCE="Notion_Google_Cloud_Sync"
SOURCE_DATABASE_ID="3db284de-cb43-80ed-9b6f-fc20d6cc20eb"
DESTINATION_DATABASE_ID="0357c6bd-2650-4dfc-affb-72430beaca84"
DESTINATION_TITLE="Cat VisionLink History"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ "${1:-}" == "--help" ]]; then
  cat <<EOF
Usage: deploy/deploy-dry-run.sh

Deploys ${JOB} in ${PROJECT} / ${REGION} with DRY_RUN=true, executes it once,
and prints redacted logs. Requires an active gcloud account.

Mounts Secret Manager ${SECRET_RESOURCE} as NOTION_TOKEN
(NOTION_TOKEN=${SECRET_RESOURCE}:latest).

Does not set DRY_RUN=false and does not create Cloud Scheduler.
Does not create a service account. The runner ${SA_EMAIL} must already exist.
EOF
  exit 0
fi

if [[ "$#" -gt 0 ]]; then
  echo "ERROR: This script takes no arguments. It only deploys DRY_RUN=true." >&2
  exit 1
fi

if [[ "${DRY_RUN:-true}" != "true" ]]; then
  echo "ERROR: Refusing to deploy because DRY_RUN=${DRY_RUN}. This script only deploys DRY_RUN=true." >&2
  exit 1
fi

GCLOUD=""
if command -v gcloud >/dev/null 2>&1; then
  GCLOUD="$(command -v gcloud)"
else
  for candidate in \
    "${HOME}/google-cloud-sdk/bin/gcloud" \
    /usr/lib/google-cloud-sdk/bin/gcloud \
    /usr/bin/gcloud
  do
    if [[ -x "${candidate}" ]]; then
      GCLOUD="${candidate}"
      break
    fi
  done
fi
if [[ -z "${GCLOUD}" ]]; then
  echo "ERROR: gcloud is not installed." >&2
  exit 1
fi

ACTIVE_ACCOUNT="$("${GCLOUD}" auth list --filter='status:ACTIVE' --format='value(account)' 2>/dev/null || true)"
if [[ -z "${ACTIVE_ACCOUNT}" ]]; then
  echo "ERROR: No credentialed gcloud account." >&2
  echo "gcloud auth list reports no active account. Run: gcloud auth login" >&2
  exit 1
fi

echo "Deploying ${JOB} as ${ACTIVE_ACCOUNT} with DRY_RUN=true"
echo "Secret mapping: NOTION_TOKEN=${SECRET_RESOURCE}:latest"
"${GCLOUD}" config set project "${PROJECT}" >/dev/null

"${GCLOUD}" services enable \
  run.googleapis.com \
  secretmanager.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  logging.googleapis.com \
  iam.googleapis.com \
  --project="${PROJECT}"

if ! "${GCLOUD}" iam service-accounts describe "${SA_EMAIL}" --project="${PROJECT}" >/dev/null 2>&1; then
  echo "ERROR: Service account ${SA_EMAIL} does not exist." >&2
  echo "Refusing to create a service account. The runner must already exist." >&2
  exit 1
fi

# Confirm the secret exists. Do not read or print its value, and do not create one.
if ! "${GCLOUD}" secrets describe "${SECRET_RESOURCE}" --project="${PROJECT}" >/dev/null; then
  echo "ERROR: Secret ${SECRET_RESOURCE} was not found in ${PROJECT}. Refusing to create a replacement." >&2
  exit 1
fi

"${GCLOUD}" secrets add-iam-policy-binding "${SECRET_RESOURCE}" \
  --project="${PROJECT}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/secretmanager.secretAccessor" \
  --quiet >/dev/null

"${GCLOUD}" projects add-iam-policy-binding "${PROJECT}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/logging.logWriter" \
  --quiet >/dev/null

# The runner does not deploy itself. These actAs bindings let the caller and
# Cloud Build start a job that runs as the runner. They are not Owner/Editor.
if [[ "${ACTIVE_ACCOUNT}" == *.gserviceaccount.com ]]; then
  DEPLOYER_MEMBER="serviceAccount:${ACTIVE_ACCOUNT}"
else
  DEPLOYER_MEMBER="user:${ACTIVE_ACCOUNT}"
fi
"${GCLOUD}" iam service-accounts add-iam-policy-binding "${SA_EMAIL}" \
  --project="${PROJECT}" \
  --member="${DEPLOYER_MEMBER}" \
  --role="roles/iam.serviceAccountUser" \
  --quiet >/dev/null

# This project's working Cloud Build identity uses the cloudbuild domain.
PROJECT_NUMBER="$("${GCLOUD}" projects describe "${PROJECT}" --format='value(projectNumber)')"
BUILD_SA="${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com"
"${GCLOUD}" iam service-accounts add-iam-policy-binding "${SA_EMAIL}" \
  --project="${PROJECT}" \
  --member="serviceAccount:${BUILD_SA}" \
  --role="roles/iam.serviceAccountUser" \
  --quiet >/dev/null
"${GCLOUD}" projects add-iam-policy-binding "${PROJECT}" \
  --member="serviceAccount:${BUILD_SA}" \
  --role="roles/run.developer" \
  --quiet >/dev/null
"${GCLOUD}" projects add-iam-policy-binding "${PROJECT}" \
  --member="serviceAccount:${BUILD_SA}" \
  --role="roles/artifactregistry.writer" \
  --quiet >/dev/null

ENV_VARS="^@^DRY_RUN=true@SOURCE_DATABASE_ID=${SOURCE_DATABASE_ID}@DESTINATION_DATABASE_TITLE=${DESTINATION_TITLE}@DESTINATION_DATABASE_ID=${DESTINATION_DATABASE_ID}@BUSINESS_TIMEZONE=America/Los_Angeles"

"${GCLOUD}" run jobs deploy "${JOB}" \
  --project="${PROJECT}" \
  --source="${ROOT}" \
  --region="${REGION}" \
  --service-account="${SA_EMAIL}" \
  --set-secrets="NOTION_TOKEN=Notion_Google_Cloud_Sync:latest" \
  --set-env-vars="${ENV_VARS}" \
  --tasks=1 \
  --parallelism=1 \
  --max-retries=3 \
  --task-timeout=1800 \
  --memory=512Mi \
  --cpu=1

"${GCLOUD}" run jobs add-iam-policy-binding "${JOB}" \
  --project="${PROJECT}" \
  --region="${REGION}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/run.invoker" \
  --quiet >/dev/null

assert_dry_run() {
  local phase="$1"
  echo "Checking job configuration (${phase})"
  "${GCLOUD}" run jobs describe "${JOB}" \
    --project="${PROJECT}" \
    --region="${REGION}" \
    --format=json \
    | python3 "${ROOT}/deploy/assert_job_config.py"
}

assert_dry_run "before execute"

EXECUTION_NAME="$("${GCLOUD}" run jobs execute "${JOB}" \
  --project="${PROJECT}" \
  --region="${REGION}" \
  --wait \
  --format='value(metadata.name)')"

assert_dry_run "after execute"

echo "Execution: ${EXECUTION_NAME}"
echo "Fetching logs. Secret-like strings are redacted."

"${GCLOUD}" logging read \
  "resource.type=\"cloud_run_job\" AND resource.labels.job_name=\"${JOB}\" AND labels.\"run.googleapis.com/execution_name\"=\"${EXECUTION_NAME}\"" \
  --project="${PROJECT}" \
  --freshness=6h \
  --limit=1000 \
  --format=json \
  | python3 -c '
import json, re, sys
secret = re.compile(r"(?i)(bearer\s+)\S+|((?:secret_|ntn_)[A-Za-z0-9_\-]+)")
def redact(value):
    if isinstance(value, str):
        return secret.sub(lambda m: (m.group(1) or "") + "[redacted]" if m.group(1) else "[redacted]", value)
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    return value
entries = json.load(sys.stdin)
for entry in reversed(entries):
    payload = entry.get("jsonPayload") or entry.get("textPayload") or ""
    print(json.dumps(redact(payload), ensure_ascii=False))
'

echo
echo "Stopped after one dry run."
echo "DRY_RUN remains true. Cloud Scheduler was not created. No production Notion writes were enabled."
