#!/usr/bin/env bash
# Deploy the VisionLink history snapshot Cloud Run job and run it once.
#
# This script always deploys DRY_RUN=true. It does not set DRY_RUN=false,
# does not create Cloud Scheduler, and does not print NOTION_TOKEN.
#
# Project: work-projects-486912
# Region:  us-west1
# Job:     notion-visionlink-history-snapshot
# Runner:  notion-visionlink-history-runner

set -euo pipefail

PROJECT="work-projects-486912"
REGION="us-west1"
JOB="notion-visionlink-history-snapshot"
SA_NAME="notion-visionlink-history-runner"
SA_EMAIL="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
SECRET_NAME="NOTION_TOKEN"
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

Does not set DRY_RUN=false and does not create Cloud Scheduler.
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
  "${GCLOUD}" iam service-accounts create "${SA_NAME}" \
    --project="${PROJECT}" \
    --display-name="VisionLink history snapshot runner"
fi

# Confirm the secret exists. Do not read or print its value, and do not create one.
if ! "${GCLOUD}" secrets describe "${SECRET_NAME}" --project="${PROJECT}" >/dev/null; then
  echo "ERROR: Secret ${SECRET_NAME} was not found in ${PROJECT}. Refusing to create a replacement." >&2
  exit 1
fi

"${GCLOUD}" secrets add-iam-policy-binding "${SECRET_NAME}" \
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

PROJECT_NUMBER="$("${GCLOUD}" projects describe "${PROJECT}" --format='value(projectNumber)')"
for BUILD_SA in \
  "${PROJECT_NUMBER}@clou.gserviceaccount.com" \
  "${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
do
  "${GCLOUD}" iam service-accounts add-iam-policy-binding "${SA_EMAIL}" \
    --project="${PROJECT}" \
    --member="serviceAccount:${BUILD_SA}" \
    --role="roles/iam.serviceAccountUser" \
    --quiet >/dev/null
done

ENV_VARS="^@^DRY_RUN=true@SOURCE_DATABASE_ID=${SOURCE_DATABASE_ID}@DESTINATION_DATABASE_TITLE=${DESTINATION_TITLE}@DESTINATION_DATABASE_ID=${DESTINATION_DATABASE_ID}@BUSINESS_TIMEZONE=America/Los_Angeles"

"${GCLOUD}" run jobs deploy "${JOB}" \
  --project="${PROJECT}" \
  --source="${ROOT}" \
  --region="${REGION}" \
  --service-account="${SA_EMAIL}" \
  --set-secrets="${SECRET_NAME}=${SECRET_NAME}:latest" \
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
  local description
  description="$("${GCLOUD}" run jobs describe "${JOB}" \
    --project="${PROJECT}" \
    --region="${REGION}" \
    --format=json)"
  DRY_PHASE="${phase}" python3 -c '
import json, os, sys
phase = os.environ["DRY_PHASE"]
job = json.loads(sys.stdin.read())
containers = (
    job.get("spec", {})
    .get("template", {})
    .get("spec", {})
    .get("template", {})
    .get("spec", {})
    .get("containers", [])
)
if not containers:
    containers = job.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
env = containers[0].get("env", []) if containers else []
value = None
for item in env:
    if item.get("name") == "DRY_RUN":
        value = item.get("value")
if value != "true":
    print(f"ERROR: {phase}: job DRY_RUN is {value!r}, expected true. Not executing.", file=sys.stderr)
    sys.exit(1)
print(f"{phase}: DRY_RUN=true")
' <<<"${description}"
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
