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
AR_LOCATION="us-west1"
AR_REPO="cloud-run-source-deploy"
GITHUB_REPO="Lower-river-guy/notion-visionlink-history-snapshot"

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
    --display-name="GitHub Actions deployer for VisionLink history snapshot"
fi

if ! "${GCLOUD}" iam workload-identity-pools describe "${POOL}" \
  --project="${PROJECT}" \
  --location=global >/dev/null 2>&1; then
  "${GCLOUD}" iam workload-identity-pools create "${POOL}" \
    --project="${PROJECT}" \
    --location=global \
    --display-name="GitHub VisionLink history snapshot"
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
    --display-name="GitHub Actions" \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="${ATTRIBUTE_MAPPING}" \
    --attribute-condition="${ATTRIBUTE_CONDITION}"
else
  "${GCLOUD}" iam workload-identity-pools providers update-oidc "${PROVIDER}" \
    --project="${PROJECT}" \
    --location=global \
    --workload-identity-pool="${POOL}" \
    --display-name="GitHub Actions" \
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
  roles/logging.logWriter
do
  "${GCLOUD}" projects add-iam-policy-binding "${PROJECT}" \
    --member="serviceAccount:${DEPLOYER}" \
    --role="${ROLE}" \
    --quiet >/dev/null
done

SCHEDULER_PERMISSIONS="cloudscheduler.jobs.create,cloudscheduler.jobs.get,cloudscheduler.jobs.list,cloudscheduler.jobs.update,cloudscheduler.locations.get,cloudscheduler.locations.list"
if "${GCLOUD}" iam roles describe "${SCHEDULER_ROLE}" --project="${PROJECT}" >/dev/null 2>&1; then
  "${GCLOUD}" iam roles update "${SCHEDULER_ROLE}" \
    --project="${PROJECT}" \
    --title="VisionLink history scheduler deploy" \
    --description="Create and update Cloud Scheduler jobs for the VisionLink history snapshot. Does not run or delete them." \
    --permissions="${SCHEDULER_PERMISSIONS}" \
    --stage=GA >/dev/null
else
  "${GCLOUD}" iam roles create "${SCHEDULER_ROLE}" \
    --project="${PROJECT}" \
    --title="VisionLink history scheduler deploy" \
    --description="Create and update Cloud Scheduler jobs for the VisionLink history snapshot. Does not run or delete them." \
    --permissions="${SCHEDULER_PERMISSIONS}" \
    --stage=GA >/dev/null
fi
"${GCLOUD}" projects add-iam-policy-binding "${PROJECT}" \
  --member="serviceAccount:${DEPLOYER}" \
  --role="projects/${PROJECT}/roles/${SCHEDULER_ROLE}" \
  --quiet >/dev/null

if ! "${GCLOUD}" artifacts repositories describe "${AR_REPO}" \
  --location="${AR_LOCATION}" \
  --project="${PROJECT}" >/dev/null 2>&1; then
  "${GCLOUD}" artifacts repositories create "${AR_REPO}" \
    --repository-format=docker \
    --location="${AR_LOCATION}" \
    --project="${PROJECT}" \
    --description="Images for notion-visionlink-history-snapshot"
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
