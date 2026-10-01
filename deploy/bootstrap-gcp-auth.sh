#!/usr/bin/env bash
# Idempotent Workload Identity Federation setup for GitHub Actions.
#
# Creates the GitHub deployer, a repository-scoped identity pool, and the
# IAM bindings that let this repository deploy later. It does not deploy or
# execute the Cloud Run job, does not create a Cloud Scheduler job, does not
# create a service-account key, and does not print secret values.
#
# Runtime identity stays visionlink-history-runner. GitHub impersonates
# github-visionlink-deployer only.

set -euo pipefail

PROJECT="work-projects-486912"
EXPECTED_PROJECT_NUMBER="564809734796"
POOL="github-visionlink"
PROVIDER="github"
REPO="Lower-river-guy/notion-visionlink-history-snapshot"
DEPLOYER_NAME="github-visionlink-deployer"
DEPLOYER="github-visionlink-deployer@work-projects-486912.iam.gserviceaccount.com"
RUNNER="visionlink-history-runner@work-projects-486912.iam.gserviceaccount.com"
SCHEDULER_ROLE="visionlinkHistorySchedulerDeployer"
BUILD_SUBMIT_ROLE="visionlinkHistoryBuildSubmitter"
AR_LOCATION="us-west1"
AR_REPO="cloud-run-source-deploy"
GITHUB_REPO="Lower-river-guy/notion-visionlink-history-snapshot"

# Display names only. Resource IDs above stay as-is.
# Limits: pool <=32, provider <=32, service account <=100,
# custom role title <=100, custom role description <=256,
# Artifact Registry description <=256.
POOL_DISPLAY_NAME="VisionLink GitHub"
PROVIDER_DISPLAY_NAME="GitHub Actions"
DEPLOYER_DISPLAY_NAME="GitHub Actions deployer for VisionLink history snapshot"
SCHEDULER_ROLE_TITLE="VisionLink history scheduler deploy"
SCHEDULER_ROLE_DESCRIPTION="Create and update Cloud Scheduler jobs for the VisionLink history snapshot. Does not run or delete them."
BUILD_SUBMIT_ROLE_TITLE="VisionLink history build submit"
BUILD_SUBMIT_ROLE_DESCRIPTION="List Cloud Storage buckets so the deployer can stage Cloud Build source."
AR_DESCRIPTION="Images for notion-visionlink-history-snapshot"

require_max_length() {
  local label="$1"
  local value="$2"
  local limit="$3"
  local length="${#value}"
  if (( length > limit )); then
    echo "ERROR: ${label} is ${length} characters; GCP allows at most ${limit}." >&2
    exit 1
  fi
}

require_max_length "Workload Identity Pool display name" "${POOL_DISPLAY_NAME}" 32
require_max_length "Workload Identity Provider display name" "${PROVIDER_DISPLAY_NAME}" 32
require_max_length "Deployer service account display name" "${DEPLOYER_DISPLAY_NAME}" 100
require_max_length "Custom role title" "${SCHEDULER_ROLE_TITLE}" 100
require_max_length "Custom role description" "${SCHEDULER_ROLE_DESCRIPTION}" 256
require_max_length "Build submit role title" "${BUILD_SUBMIT_ROLE_TITLE}" 100
require_max_length "Build submit role description" "${BUILD_SUBMIT_ROLE_DESCRIPTION}" 256
require_max_length "Artifact Registry description" "${AR_DESCRIPTION}" 256

# IDs are already valid, so they are not renamed to shorten a display name.
# Pool and provider: 4-32 chars, start with a letter, end with a letter or digit.
# Service account id: 6-30 chars, same character rules.
# Custom role id: 3-64 letters, digits, underscores, or periods.
require_id() {
  local label="$1"
  local value="$2"
  local pattern="$3"
  if [[ ! "${value}" =~ ${pattern} ]]; then
    echo "ERROR: ${label} '${value}' is not a valid GCP id." >&2
    exit 1
  fi
}

require_id "Workload Identity Pool id" "${POOL}" '^[a-z][a-z0-9-]{2,30}[a-z0-9]$'
require_id "Workload Identity Provider id" "${PROVIDER}" '^[a-z][a-z0-9-]{2,30}[a-z0-9]$'
require_id "Deployer service account id" "${DEPLOYER_NAME}" '^[a-z][a-z0-9-]{4,28}[a-z0-9]$'
require_id "Custom role id" "${SCHEDULER_ROLE}" '^[a-zA-Z][a-zA-Z0-9_.]{2,63}$'
require_id "Build submit role id" "${BUILD_SUBMIT_ROLE}" '^[a-zA-Z][a-zA-Z0-9_.]{2,63}$'

if [[ "$#" -gt 0 ]]; then
  echo "ERROR: This script takes no arguments. It only configures Workload Identity Federation." >&2
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

echo "Configuring GitHub Workload Identity as ${ACTIVE_ACCOUNT}"
"${GCLOUD}" config set project "${PROJECT}" >/dev/null

PROJECT_NUMBER="$("${GCLOUD}" projects describe "${PROJECT}" --format='value(projectNumber)')"
if [[ "${PROJECT_NUMBER}" != "${EXPECTED_PROJECT_NUMBER}" ]]; then
  echo "ERROR: ${PROJECT} project number is ${PROJECT_NUMBER}, expected ${EXPECTED_PROJECT_NUMBER}." >&2
  exit 1
fi

"${GCLOUD}" services enable \
  iam.googleapis.com \
  iamcredentials.googleapis.com \
  cloudresourcemanager.googleapis.com \
  sts.googleapis.com \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  logging.googleapis.com \
  cloudscheduler.googleapis.com \
  --project="${PROJECT}"

if ! "${GCLOUD}" iam service-accounts describe "${RUNNER}" --project="${PROJECT}" >/dev/null 2>&1; then
  echo "ERROR: Runtime service account ${RUNNER} does not exist. Refusing to create it." >&2
  exit 1
fi

if ! "${GCLOUD}" iam service-accounts describe "${DEPLOYER}" --project="${PROJECT}" >/dev/null 2>&1; then
  "${GCLOUD}" iam service-accounts create "${DEPLOYER_NAME}" \
    --project="${PROJECT}" \
    --display-name="${DEPLOYER_DISPLAY_NAME}"
else
  echo "Reusing deployer service account ${DEPLOYER}"
fi

if ! "${GCLOUD}" iam workload-identity-pools describe "${POOL}" \
  --project="${PROJECT}" \
  --location=global >/dev/null 2>&1; then
  "${GCLOUD}" iam workload-identity-pools create "${POOL}" \
    --project="${PROJECT}" \
    --location=global \
    --display-name="${POOL_DISPLAY_NAME}"
else
  echo "Reusing Workload Identity Pool ${POOL}"
  "${GCLOUD}" iam workload-identity-pools update "${POOL}" \
    --project="${PROJECT}" \
    --location=global \
    --display-name="${POOL_DISPLAY_NAME}"
fi

ATTRIBUTE_CONDITION="assertion.repository=='Lower-river-guy/notion-visionlink-history-snapshot'"
ATTRIBUTE_MAPPING="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref"
if ! "${GCLOUD}" iam workload-identity-pools providers describe "${PROVIDER}" \
  --project="${PROJECT}" \
  --location=global \
  --workload-identity-pool="${POOL}" >/dev/null 2>&1; then
  "${GCLOUD}" iam workload-identity-pools providers create-oidc "${PROVIDER}" \
    --project="${PROJECT}" \
    --location=global \
    --workload-identity-pool="${POOL}" \
    --display-name="${PROVIDER_DISPLAY_NAME}" \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="${ATTRIBUTE_MAPPING}" \
    --attribute-condition="${ATTRIBUTE_CONDITION}"
else
  echo "Reusing Workload Identity Provider ${PROVIDER}"
  "${GCLOUD}" iam workload-identity-pools providers update-oidc "${PROVIDER}" \
    --project="${PROJECT}" \
    --location=global \
    --workload-identity-pool="${POOL}" \
    --display-name="${PROVIDER_DISPLAY_NAME}" \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="${ATTRIBUTE_MAPPING}" \
    --attribute-condition="${ATTRIBUTE_CONDITION}"
fi

# Repository principal only. attribute.repository_owner would cover the whole org.
PRINCIPAL="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/attribute.repository/${REPO}"
"${GCLOUD}" iam service-accounts add-iam-policy-binding "${DEPLOYER}" \
  --project="${PROJECT}" \
  --member="${PRINCIPAL}" \
  --role="roles/iam.workloadIdentityUser" \
  --quiet >/dev/null

# The deployer submits Cloud Build and is also the build identity, so it must
# be allowed to act as itself. It must also act as the runtime account.
"${GCLOUD}" iam service-accounts add-iam-policy-binding "${DEPLOYER}" \
  --project="${PROJECT}" \
  --member="serviceAccount:${DEPLOYER}" \
  --role="roles/iam.serviceAccountUser" \
  --quiet >/dev/null
"${GCLOUD}" iam service-accounts add-iam-policy-binding "${RUNNER}" \
  --project="${PROJECT}" \
  --member="serviceAccount:${DEPLOYER}" \
  --role="roles/iam.serviceAccountUser" \
  --quiet >/dev/null

BUILD_AGENT="service-${PROJECT_NUMBER}@gcp-sa-cloudbuild.iam.gserviceaccount.com"
if ! "${GCLOUD}" iam service-accounts describe "${BUILD_AGENT}" --project="${PROJECT}" >/dev/null 2>&1; then
  "${GCLOUD}" beta services identity create \
    --service=cloudbuild.googleapis.com \
    --project="${PROJECT}" >/dev/null \
    || echo "Cloud Build service agent was not created by this command. Continuing with the IAM binding."
fi
"${GCLOUD}" iam service-accounts add-iam-policy-binding "${DEPLOYER}" \
  --project="${PROJECT}" \
  --member="serviceAccount:${BUILD_AGENT}" \
  --role="roles/iam.serviceAccountUser" \
  --quiet >/dev/null

for ROLE in \
  roles/run.developer \
  roles/cloudbuild.builds.editor \
  roles/artifactregistry.writer \
  roles/logging.logWriter \
  roles/serviceusage.serviceUsageConsumer
do
  "${GCLOUD}" projects add-iam-policy-binding "${PROJECT}" \
    --member="serviceAccount:${DEPLOYER}" \
    --role="${ROLE}" \
    --quiet >/dev/null
done

SCHEDULER_PERMISSIONS="cloudscheduler.jobs.create,cloudscheduler.jobs.get,cloudscheduler.jobs.list,cloudscheduler.jobs.update,cloudscheduler.locations.get,cloudscheduler.locations.list"
if "${GCLOUD}" iam roles describe "${SCHEDULER_ROLE}" --project="${PROJECT}" >/dev/null 2>&1; then
  echo "Reusing custom role ${SCHEDULER_ROLE}"
  "${GCLOUD}" iam roles update "${SCHEDULER_ROLE}" \
    --project="${PROJECT}" \
    --title="${SCHEDULER_ROLE_TITLE}" \
    --description="${SCHEDULER_ROLE_DESCRIPTION}" \
    --permissions="${SCHEDULER_PERMISSIONS}" \
    --stage=GA >/dev/null
else
  "${GCLOUD}" iam roles create "${SCHEDULER_ROLE}" \
    --project="${PROJECT}" \
    --title="${SCHEDULER_ROLE_TITLE}" \
    --description="${SCHEDULER_ROLE_DESCRIPTION}" \
    --permissions="${SCHEDULER_PERMISSIONS}" \
    --stage=GA >/dev/null
fi
"${GCLOUD}" projects add-iam-policy-binding "${PROJECT}" \
  --member="serviceAccount:${DEPLOYER}" \
  --role="projects/${PROJECT}/roles/${SCHEDULER_ROLE}" \
  --quiet >/dev/null

# gcloud builds submit reports a forbidden Cloud Build bucket when the caller
# lacks serviceusage.services.use. It also needs to list buckets and write
# source objects in PROJECT_cloudbuild. This is not project storage admin.
BUILD_SUBMIT_PERMISSIONS="storage.buckets.get,storage.buckets.list"
if "${GCLOUD}" iam roles describe "${BUILD_SUBMIT_ROLE}" --project="${PROJECT}" >/dev/null 2>&1; then
  echo "Reusing custom role ${BUILD_SUBMIT_ROLE}"
  "${GCLOUD}" iam roles update "${BUILD_SUBMIT_ROLE}" \
    --project="${PROJECT}" \
    --title="${BUILD_SUBMIT_ROLE_TITLE}" \
    --description="${BUILD_SUBMIT_ROLE_DESCRIPTION}" \
    --permissions="${BUILD_SUBMIT_PERMISSIONS}" \
    --stage=GA >/dev/null
else
  "${GCLOUD}" iam roles create "${BUILD_SUBMIT_ROLE}" \
    --project="${PROJECT}" \
    --title="${BUILD_SUBMIT_ROLE_TITLE}" \
    --description="${BUILD_SUBMIT_ROLE_DESCRIPTION}" \
    --permissions="${BUILD_SUBMIT_PERMISSIONS}" \
    --stage=GA >/dev/null
fi
"${GCLOUD}" projects add-iam-policy-binding "${PROJECT}" \
  --member="serviceAccount:${DEPLOYER}" \
  --role="projects/${PROJECT}/roles/${BUILD_SUBMIT_ROLE}" \
  --quiet >/dev/null

CLOUDBUILD_BUCKET="gs://${PROJECT}_cloudbuild"
if ! "${GCLOUD}" storage buckets describe "${CLOUDBUILD_BUCKET}" >/dev/null 2>&1; then
  echo "ERROR: Cloud Build staging bucket ${CLOUDBUILD_BUCKET} does not exist." >&2
  exit 1
fi
"${GCLOUD}" storage buckets add-iam-policy-binding "${CLOUDBUILD_BUCKET}" \
  --member="serviceAccount:${DEPLOYER}" \
  --role="roles/storage.objectAdmin"

if ! "${GCLOUD}" artifacts repositories describe "${AR_REPO}" \
  --location="${AR_LOCATION}" \
  --project="${PROJECT}" >/dev/null 2>&1; then
  "${GCLOUD}" artifacts repositories create "${AR_REPO}" \
    --repository-format=docker \
    --location="${AR_LOCATION}" \
    --project="${PROJECT}" \
    --description="${AR_DESCRIPTION}"
fi

PROVIDER_RESOURCE="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/providers/${PROVIDER}"

echo
echo "Workload Identity is configured. No Cloud Run job was deployed or executed."
echo "No Cloud Scheduler job was created."
echo
echo "GCP_WORKLOAD_IDENTITY_PROVIDER=${PROVIDER_RESOURCE}"
echo "GCP_SERVICE_ACCOUNT=${DEPLOYER}"

if command -v gh >/dev/null 2>&1; then
  if printf '%s' "${PROVIDER_RESOURCE}" | gh secret set GCP_WORKLOAD_IDENTITY_PROVIDER --repo "${GITHUB_REPO}" \
    && printf '%s' "${DEPLOYER}" | gh secret set GCP_SERVICE_ACCOUNT --repo "${GITHUB_REPO}"; then
    echo "GitHub Actions secrets GCP_WORKLOAD_IDENTITY_PROVIDER and GCP_SERVICE_ACCOUNT were set."
  else
    echo "GitHub Actions secrets were not set. Store the two values printed above as repository Actions secrets."
  fi
else
  echo "gh is not installed. Store the two values printed above as repository Actions secrets."
fi
