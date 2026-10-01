#!/usr/bin/env bash
# Create or update the one weekday 10:00 AM Pacific scheduler.
#
# The Cloud Run job template is DRY_RUN=false. Cloud Scheduler still calls the
# Cloud Run jobs.run API with an execution override of DRY_RUN=false and
# SNAPSHOT_SLOT=10:00. This script does not execute the job and does not
# call Cloud Scheduler's run command.

set -euo pipefail
shopt -s nullglob

if [[ "$#" -gt 0 ]]; then
  echo "ERROR: This script takes no arguments." >&2
  exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT
python3 deploy/weekday_scheduler.py --export "${TMP}/env" --body-file "${TMP}/body.json"
# shellcheck disable=SC1091
source "${TMP}/env"

if [[ "${SCHEDULE}" != "0 10 * * 1-5" || "${TIME_ZONE}" != "America/Los_Angeles" ]]; then
  echo "ERROR: scheduler export is not the weekday 10:00 Pacific schedule." >&2
  exit 1
fi

echo "Ensuring Cloud Scheduler ${SCHEDULER_NAME} in ${REGION}."
echo "Resting Cloud Run job ${CLOUD_RUN_JOB} is not modified and is not executed."
echo "Scheduled runs call the Run API as ${OAUTH_SERVICE_ACCOUNT}."
echo "That requires ${SCHEDULER_AGENT} to have roles/iam.serviceAccountUser on ${OAUTH_SERVICE_ACCOUNT}."
echo "This step does not grant or read that binding. The deployer lacks iam.serviceAccounts.getIamPolicy."
echo "An admin applies it with deploy/bootstrap-gcp-auth.sh."

mkdir -p "${TMP}/lists"
while IFS= read -r location; do
  [[ -z "${location}" ]] && continue
  gcloud scheduler jobs list \
    --project="${PROJECT}" \
    --location="${location}" \
    --format=json > "${TMP}/lists/${location}.json"
done < <(gcloud scheduler locations list --project="${PROJECT}" --format='value(locationId)')

delete_extras() {
  local listed
  listed="$(python3 deploy/weekday_scheduler.py --print-deletes "${TMP}/lists/"*.json)"
  if [[ -z "${listed}" ]]; then
    return 0
  fi
  while IFS=$'\t' read -r location job_id; do
    [[ -z "${job_id}" ]] && continue
    echo "Deleting extra scheduler ${location}/${job_id}"
    gcloud scheduler jobs delete "${job_id}" \
      --project="${PROJECT}" \
      --location="${location}" \
      --quiet
  done <<< "${listed}"
}

delete_extras
while IFS= read -r location; do
  [[ -z "${location}" ]] && continue
  gcloud scheduler jobs list \
    --project="${PROJECT}" \
    --location="${location}" \
    --format=json > "${TMP}/lists/${location}.json"
done < <(gcloud scheduler locations list --project="${PROJECT}" --format='value(locationId)')
remaining="$(python3 deploy/weekday_scheduler.py --print-deletes "${TMP}/lists/"*.json)"
if [[ -n "${remaining}" ]]; then
  echo "ERROR: More than one VisionLink history scheduler remains:" >&2
  echo "${remaining}" >&2
  exit 1
fi

COMMON=(
  --project="${PROJECT}"
  --location="${REGION}"
  --schedule="${SCHEDULE}"
  --time-zone="${TIME_ZONE}"
  --uri="${RUN_URI}"
  --http-method=POST
  --message-body-from-file="${BODY_FILE}"
  --oauth-service-account-email="${OAUTH_SERVICE_ACCOUNT}"
  --oauth-token-scope="${OAUTH_SCOPE}"
  --attempt-deadline=180s
  --max-retry-attempts=0
  --description="${DESCRIPTION}"
)

if gcloud scheduler jobs describe "${SCHEDULER_NAME}" \
  --project="${PROJECT}" \
  --location="${REGION}" >/dev/null 2>&1; then
  gcloud scheduler jobs update http "${SCHEDULER_NAME}" \
    "${COMMON[@]}" \
    --update-headers="Content-Type=application/json"
else
  gcloud scheduler jobs create http "${SCHEDULER_NAME}" \
    "${COMMON[@]}" \
    --headers="Content-Type=application/json"
fi

state="$(gcloud scheduler jobs describe "${SCHEDULER_NAME}" \
  --project="${PROJECT}" \
  --location="${REGION}" \
  --format='value(state)')"
if [[ "${state}" != "ENABLED" ]]; then
  gcloud scheduler jobs resume "${SCHEDULER_NAME}" \
    --project="${PROJECT}" \
    --location="${REGION}"
fi

gcloud scheduler jobs describe "${SCHEDULER_NAME}" \
  --project="${PROJECT}" \
  --location="${REGION}" \
  --format=json \
  | python3 deploy/weekday_scheduler.py

echo "Did not execute the Cloud Run job. Did not trigger the scheduler."
echo "Did not change IAM. Scheduler service-agent actAs is applied only by deploy/bootstrap-gcp-auth.sh."
