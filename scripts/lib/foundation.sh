#!/usr/bin/env bash
# ==============================================================================
# Purpose: Phase 1 & Phase 2 provisioning module—enables required Google Cloud
#          APIs, initializes service agents with least-privilege IAM roles,
#          provisions Firestore Native `(default)` database, and seeds Cymbal
#          Bank baseline data.
# Architecture/Context: Sourced by `install.sh` after `scripts/lib/common.sh`.
# Dependencies/Side Effects: Modifies project IAM policy bindings and Firestore.
# Author: Pradeep Singh
# License: Apache-2.0
# ==============================================================================

#######################################
# Summary: Idempotently grants a project-level IAM role to a member.
# Parameters: $1 principal member string; $2 IAM role.
# Return Value: 0.
# Exceptions/Errors: Propagates `gcloud` failures after retries.
#######################################
grant_project_iam() {
  local member="$1" role="$2"
  retry 3 10 gc projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="${member}" \
    --role="${role}" \
    --condition=None \
    --format=none >/dev/null
}

#######################################
# Summary: Creates or undeletes the custom IAM role required for Vertex AI Agent
#          Runtime service agents to validate and bind to Agent Gateways.
# Parameters: None.
# Return Value: 0.
# Exceptions/Errors: Propagates `gcloud` errors.
#######################################
ensure_gateway_user_custom_role() {
  local role_id="${PREFIX//-/_}_agent_gateway_user"
  local role_res="projects/${PROJECT_ID}/roles/${role_id}"
  local perms="networkservices.agentGateways.get,networkservices.agentGateways.use,networkservices.operations.get"
  local deleted
  deleted="$(gc iam roles describe "${role_id}" --project="${PROJECT_ID}" --format='value(deleted)' 2>/dev/null || printf 'missing')"

  case "${deleted}" in
    missing)
      gc iam roles create "${role_id}" \
        --project="${PROJECT_ID}" \
        --title="Cymbal Agent Gateway Runtime User" \
        --description="Allows Vertex AI service agents to bind Reasoning Engines to Agent Gateway." \
        --permissions="${perms}" \
        --stage=GA \
        --format=none
      state_record iam_custom_role "${role_id}" global CREATED
      ;;
    True | true)
      gc iam roles undelete "${role_id}" --project="${PROJECT_ID}" --format=none
      gc iam roles update "${role_id}" --project="${PROJECT_ID}" --permissions="${perms}" --format=none
      state_record iam_custom_role "${role_id}" global CREATED
      ;;
    *)
      state_record iam_custom_role "${role_id}" global REUSED
      ;;
  esac

  grant_project_iam "serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-aiplatform.iam.gserviceaccount.com" "${role_res}"
  grant_project_iam "serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-aiplatform-re.iam.gserviceaccount.com" "${role_res}"
}

#######################################
# Summary: Enables required GCP APIs and provisions Google-managed service agents
#          plus IAM roles for Model Armor, Service Extensions, and Agent Platform.
# Parameters: None.
# Return Value: 0.
# Exceptions/Errors: Propagates `gcloud` failures.
#######################################
provision_foundation_apis_and_iam() {
  phase_banner "Phase 1 — Enabling Google Cloud APIs & Service Agent IAM"

  if [[ "${FORCE_IAM:-false}" != "true" ]] &&
    grep -Fq "iam_custom_role|${PREFIX//-/_}_agent_gateway_user|" "${STATE_FILE}" 2>/dev/null; then
    log_info "Reusing previously configured GCP APIs & service agent IAM (set FORCE_IAM=true to reapply)."
    return 0
  fi

  local apis=(
    compute.googleapis.com
    dns.googleapis.com
    run.googleapis.com
    cloudbuild.googleapis.com
    artifactregistry.googleapis.com
    firestore.googleapis.com
    dlp.googleapis.com
    modelarmor.googleapis.com
    iap.googleapis.com
    iam.googleapis.com
    iamcredentials.googleapis.com
    networkservices.googleapis.com
    networksecurity.googleapis.com
    agentregistry.googleapis.com
    aiplatform.googleapis.com
    logging.googleapis.com
    monitoring.googleapis.com
    cloudtrace.googleapis.com
    telemetry.googleapis.com
  )
  log_info "Enabling ${#apis[@]} Google Cloud APIs on ${PROJECT_ID}..."
  gc services enable "${apis[@]}"

  local svc
  for svc in aiplatform.googleapis.com networkservices.googleapis.com modelarmor.googleapis.com iap.googleapis.com; do
    gcloud beta services identity create --service="${svc}" --project="${PROJECT_ID}" --quiet >/dev/null 2>&1 || true
  done

  # Model Armor service agent needs Cloud SDP roles to execute advanced PII de-identification.
  local ma_sa="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-modelarmor.iam.gserviceaccount.com"
  grant_project_iam "${ma_sa}" "roles/dlp.user"
  grant_project_iam "${ma_sa}" "roles/dlp.reader"

  # Service Extensions (`gcp-sa-dep`) needs Model Armor callout permissions on the gateway project.
  local dep_sa="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-dep.iam.gserviceaccount.com"
  grant_project_iam "${dep_sa}" "roles/modelarmor.calloutUser"
  grant_project_iam "${dep_sa}" "roles/serviceusage.serviceUsageConsumer"
  grant_project_iam "${dep_sa}" "roles/modelarmor.user"

  # Agent Gateway service agent (`gcp-sa-agentgateway`) needs VPC & DNS roles for connectivity templates.
  local agw_sa="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-agentgateway.iam.gserviceaccount.com"
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" --member="${agw_sa}" --role="roles/compute.networkViewer" --condition=None --quiet >/dev/null 2>&1 || true
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" --member="${agw_sa}" --role="roles/compute.networkUser" --condition=None --quiet >/dev/null 2>&1 || true
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" --member="${agw_sa}" --role="roles/compute.networkAdmin" --condition=None --quiet >/dev/null 2>&1 || true
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" --member="${agw_sa}" --role="roles/dns.peer" --condition=None --quiet >/dev/null 2>&1 || true
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" --member="${agw_sa}" --role="roles/dns.reader" --condition=None --quiet >/dev/null 2>&1 || true

  # Vertex AI P4SA & Reasoning Engine SA need network/DNS admin + Model Armor callout roles.
  local ai_sa="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-aiplatform.iam.gserviceaccount.com"
  local re_sa="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-aiplatform-re.iam.gserviceaccount.com"
  grant_project_iam "${ai_sa}" "roles/compute.networkAdmin"
  grant_project_iam "${ai_sa}" "roles/dns.admin"
  grant_project_iam "${ai_sa}" "roles/cloudtrace.agent"
  grant_project_iam "${ai_sa}" "roles/modelarmor.calloutUser"
  grant_project_iam "${ai_sa}" "roles/modelarmor.user"
  grant_project_iam "${re_sa}" "roles/modelarmor.calloutUser"
  grant_project_iam "${re_sa}" "roles/modelarmor.user"

  # Grant Agent Identity project-wide principalSet Vertex AI + telemetry permissions.
  local principal_set="principalSet://${AGENT_IDENTITY_TRUST_DOMAIN}/attribute.platformContainer/aiplatform/projects/${PROJECT_NUMBER}"
  grant_project_iam "${principal_set}" "roles/aiplatform.user"
  grant_project_iam "${principal_set}" "roles/serviceusage.serviceUsageConsumer"
  grant_project_iam "${principal_set}" "roles/logging.logWriter"
  grant_project_iam "${principal_set}" "roles/monitoring.metricWriter"
  grant_project_iam "${principal_set}" "roles/cloudtrace.agent"

  ensure_gateway_user_custom_role
  log_info "Foundation APIs and least-privilege service agent IAM bindings ready."
}

#######################################
# Summary: Ensures the Firestore Native `(default)` database exists and seeds the
#          baseline Cymbal Bank accounts, transactions, and disputes.
# Parameters: None.
# Return Value: 0.
# Exceptions/Errors: Propagates `gcloud firestore` or Python seed errors.
#######################################
provision_firestore_and_seed() {
  phase_banner "Phase 2 — Serverless Firestore Ledger & Cymbal Bank Seed Data"

  if gc firestore databases describe --database="${FIRESTORE_DATABASE}" --format='value(name)' >/dev/null 2>&1; then
    log_info "Reusing existing Firestore database '${FIRESTORE_DATABASE}'"
    state_record firestore_db "${FIRESTORE_DATABASE}" "${REGION}" REUSED
  else
    log_info "Creating Firestore Native database '${FIRESTORE_DATABASE}' in ${REGION}..."
    gc firestore databases create \
      --database="${FIRESTORE_DATABASE}" \
      --location="${REGION}" \
      --type=firestore-native \
      --format=none
    state_record firestore_db "${FIRESTORE_DATABASE}" "${REGION}" CREATED
  fi

  log_info "Seeding Cymbal Bank accounts, transactions, and dispute scenarios into Firestore..."
  GOOGLE_OAUTH_ACCESS_TOKEN="$(gcloud auth print-access-token)" \
    STORAGE_BACKEND=firestore \
    PROJECT_ID="${PROJECT_ID}" \
    FIRESTORE_DATABASE="${FIRESTORE_DATABASE}" \
    FIRESTORE_COLLECTION_PREFIX="${FIRESTORE_COLLECTION_PREFIX}" \
    uv run python "${ROOT_DIR}/scripts/seed_firestore.py"
}
