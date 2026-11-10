#!/usr/bin/env bash
# ==============================================================================
# Purpose: Phase 3 & Phase 8 Cloud Run container build/deployment library for
#          `cymbal-banking-mcp`, `cymbal-rogue-ext`, and `cymbal-portal`.
# Architecture/Context: Builds hardened non-root images via Cloud Build into
#          Artifact Registry and deploys IAM-protected (`--no-allow-unauthenticated`)
#          Cloud Run services compatible with enterprise org policies.
# Dependencies/Side Effects: Requires `artifactregistry`, `cloudbuild`, and `run`.
# Author: Pradeep Singh
# License: Apache-2.0
# ==============================================================================

#######################################
# Summary: Ensures the regional Docker Artifact Registry repository exists.
# Parameters: None.
# Return Value: 0.
# Exceptions/Errors: Propagates `gcloud` failures.
#######################################
ensure_artifact_registry() {
  if gc artifacts repositories describe "${AR_REPO}" --location="${REGION}" --format='value(name)' >/dev/null 2>&1; then
    state_record artifact_repo "${AR_REPO}" "${REGION}" REUSED
  else
    gc artifacts repositories create "${AR_REPO}" \
      --location="${REGION}" \
      --repository-format=docker \
      --description="Cymbal Bank Agent Gateway Demo containers" \
      --format=none
    state_record artifact_repo "${AR_REPO}" "${REGION}" CREATED
  fi
}

#######################################
# Summary: Submits a root-context Cloud Build using a service-specific Dockerfile.
# Parameters: $1 image name; $2 relative Dockerfile path.
# Return Value: Prints full Artifact Registry image URI to stdout.
# Exceptions/Errors: Propagates `gcloud builds submit` failures.
#######################################
build_service_image() {
  local image_name="$1" dockerfile_rel="$2"
  local image_uri="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/${image_name}:latest"
  local cb_cfg
  cb_cfg="$(mktemp)"
  cat >"${cb_cfg}" <<EOF
steps:
  - name: 'gcr.io/cloud-builders/docker'
    args: ['build', '-f', '${dockerfile_rel}', '-t', '${image_uri}', '.']
images:
  - '${image_uri}'
EOF
  log_info "Building container image ${image_uri} via Cloud Build..."
  gc builds submit "${ROOT_DIR}" --config="${cb_cfg}" --region="${REGION}" >/dev/null
  rm -f "${cb_cfg}"
  printf '%s' "${image_uri}"
}

#######################################
# Summary: Ensures a dedicated least-privilege Cloud Run service account exists
#          for `cymbal-banking-mcp` and `cymbal-portal` so Firestore, Vertex AI
#          Reasoning Engine invocation, and Cloud Logging audit streaming work
#          reliably even when default Compute SA grants are disabled by org policy.
# Parameters: None.
# Return Value: Prints service account email to stdout.
# Exceptions/Errors: Propagates `gcloud iam` errors.
#######################################
ensure_cloudrun_runtime_sa() {
  local sa_id="${PREFIX}-runtime-sa"
  local sa_email="${sa_id}@${PROJECT_ID}.iam.gserviceaccount.com"
  if gc iam service-accounts describe "${sa_email}" --format='value(email)' >/dev/null 2>&1; then
    state_record service_account "${sa_email}" global REUSED
  else
    gc iam service-accounts create "${sa_id}" \
      --display-name="Cymbal Bank Cloud Run Runtime SA" \
      --description="Least-privilege runtime SA for Cymbal Banking MCP and Portal services." \
      --format=none >&2
    state_record service_account "${sa_email}" global CREATED
    sleep 5
  fi

  local role_id="${PREFIX//-/_}_agent_gateway_user"
  local role
  for role in \
    roles/datastore.user \
    roles/aiplatform.user \
    roles/logging.viewer \
    roles/logging.logWriter \
    roles/cloudtrace.agent \
    "projects/${PROJECT_ID}/roles/${role_id}"; do
    gc projects add-iam-policy-binding "${PROJECT_ID}" \
      --member="serviceAccount:${sa_email}" \
      --role="${role}" \
      --condition=None \
      --format=none >/dev/null 2>&1 || true
  done
  printf '%s' "${sa_email}"
}

#######################################
# Summary: Builds and deploys `cymbal-banking-mcp` and `cymbal-rogue-ext` on
#          Cloud Run (`--no-allow-unauthenticated`) and grants `roles/run.invoker`
#          on `cymbal-banking-mcp` to the project's Agent Identity principalSet.
# Parameters: None.
# Return Value: 0; sets `MCP_SERVER_URL` and `ROGUE_WEBHOOK_URL`.
# Exceptions/Errors: Propagates Cloud Build / Cloud Run errors.
#######################################
provision_backend_cloudrun_services() {
  phase_banner "Phase 3 — Deploying Cloud Run MCP Server & Unregistered Rogue Endpoint"

  ensure_artifact_registry
  local runtime_sa
  runtime_sa="$(ensure_cloudrun_runtime_sa)"

  if [[ "${FORCE_REBUILD:-false}" != "true" ]] &&
    gc run services describe "${MCP_SERVICE_NAME}" --region="${REGION}" --format='value(status.url)' >/dev/null 2>&1 &&
    gc run services describe "${ROGUE_SERVICE_NAME}" --region="${REGION}" --format='value(status.url)' >/dev/null 2>&1; then
    log_info "Reusing existing Cloud Run services '${MCP_SERVICE_NAME}' and '${ROGUE_SERVICE_NAME}' (set FORCE_REBUILD=true to rebuild)"
    local cur_mcp_sa
    cur_mcp_sa="$(gc run services describe "${MCP_SERVICE_NAME}" --region="${REGION}" --format='value(spec.template.spec.serviceAccountName)' || true)"
    if [[ "${cur_mcp_sa}" != "${runtime_sa}" ]]; then
      gc run services update "${MCP_SERVICE_NAME}" --region="${REGION}" --service-account="${runtime_sa}" --format=none
      gc run services update "${ROGUE_SERVICE_NAME}" --region="${REGION}" --service-account="${runtime_sa}" --format=none
    fi
    state_record cloud_run "${MCP_SERVICE_NAME}" "${REGION}" REUSED
    state_record cloud_run "${ROGUE_SERVICE_NAME}" "${REGION}" REUSED
  else
    local mcp_img rogue_img
    mcp_img="$(build_service_image "cymbal-banking-mcp" "services/cymbal_banking_mcp/Dockerfile")"
    rogue_img="$(build_service_image "cymbal-rogue-ext" "services/cymbal_rogue_ext/Dockerfile")"

    gc run deploy "${MCP_SERVICE_NAME}" \
      --image="${mcp_img}" \
      --region="${REGION}" \
      --service-account="${runtime_sa}" \
      --no-allow-unauthenticated \
      --cpu=1 \
      --memory=512Mi \
      --min-instances=0 \
      --max-instances=5 \
      --set-env-vars="PROJECT_ID=${PROJECT_ID},STORAGE_BACKEND=firestore,FIRESTORE_DATABASE=${FIRESTORE_DATABASE},FIRESTORE_COLLECTION_PREFIX=${FIRESTORE_COLLECTION_PREFIX},LOG_LEVEL=INFO" \
      --format=none
    state_record cloud_run "${MCP_SERVICE_NAME}" "${REGION}" CREATED

    gc run deploy "${ROGUE_SERVICE_NAME}" \
      --image="${rogue_img}" \
      --region="${REGION}" \
      --service-account="${runtime_sa}" \
      --no-allow-unauthenticated \
      --cpu=1 \
      --memory=512Mi \
      --min-instances=0 \
      --max-instances=2 \
      --set-env-vars="LOG_LEVEL=INFO" \
      --format=none
    state_record cloud_run "${ROGUE_SERVICE_NAME}" "${REGION}" CREATED
  fi

  MCP_SERVER_URL="$(gc run services describe "${MCP_SERVICE_NAME}" --region="${REGION}" --format='value(status.url)')"
  ROGUE_SERVICE_URL="$(gc run services describe "${ROGUE_SERVICE_NAME}" --region="${REGION}" --format='value(status.url)')"
  export MCP_SERVER_URL
  export ROGUE_WEBHOOK_URL="${ROGUE_SERVICE_URL}/collect"
  output_set MCP_SERVER_URL "${MCP_SERVER_URL}"
  output_set ROGUE_WEBHOOK_URL "${ROGUE_WEBHOOK_URL}"

  # Allow Agent Identity SPIFFE workloads in this project to invoke the IAM-protected MCP server.
  local principal_set="principalSet://${AGENT_IDENTITY_TRUST_DOMAIN}/attribute.platformContainer/aiplatform/projects/${PROJECT_NUMBER}"
  retry 3 10 gc run services add-iam-policy-binding "${MCP_SERVICE_NAME}" \
    --region="${REGION}" \
    --member="${principal_set}" \
    --role="roles/run.invoker" \
    --format=none >/dev/null

  log_info "MCP Server deployed: ${MCP_SERVER_URL}"
  log_info "Unregistered Rogue Endpoint deployed (omitted from Agent Registry): ${ROGUE_WEBHOOK_URL}"
}

#######################################
# Summary: Builds and deploys the IAM-protected Cymbal Bank Operations Portal
#          (`cymbal-portal`) configured with the deployed Reasoning Engine IDs.
# Parameters: None.
# Return Value: 0; sets `PORTAL_URL`.
# Exceptions/Errors: Propagates Cloud Build / Cloud Run errors.
#######################################
provision_portal_cloudrun() {
  phase_banner "Phase 8 — Deploying Cymbal Bank Operations Portal (Cloud Run)"

  local runtime_sa portal_img
  runtime_sa="$(ensure_cloudrun_runtime_sa)"
  portal_img="$(build_service_image "cymbal-portal" "services/cymbal_portal/Dockerfile")"

  local env_list="PROJECT_ID=${PROJECT_ID},REGION=${REGION},STORAGE_BACKEND=firestore"
  env_list+=",FIRESTORE_DATABASE=${FIRESTORE_DATABASE},FIRESTORE_COLLECTION_PREFIX=${FIRESTORE_COLLECTION_PREFIX}"
  env_list+=",AGENT_INVOCATION_MODE=agent_runtime"
  env_list+=",INGRESS_GATEWAY_NAME=${INGRESS_GATEWAY},EGRESS_GATEWAY_NAME=${EGRESS_GATEWAY}"
  env_list+=",IAP_POLICY_MODE=${ACTIVE_IAP_MODE}"
  env_list+=",TIER1_REASONING_ENGINE=${TIER1_ENGINE_RESOURCE}"
  env_list+=",SPECIALIST_REASONING_ENGINE=${SPECIALIST_ENGINE_RESOURCE}"
  env_list+=",TIER1_SPIFFE_ID=${TIER1_PRINCIPAL}"
  env_list+=",SPECIALIST_SPIFFE_ID=${SPECIALIST_PRINCIPAL}"

  gc run deploy "${PORTAL_SERVICE_NAME}" \
    --image="${portal_img}" \
    --region="${REGION}" \
    --service-account="${runtime_sa}" \
    --no-allow-unauthenticated \
    --cpu=1 \
    --memory=1Gi \
    --min-instances=0 \
    --max-instances=5 \
    --set-env-vars="${env_list}" \
    --format=none
  state_record cloud_run "${PORTAL_SERVICE_NAME}" "${REGION}" CREATED

  PORTAL_URL="$(gc run services describe "${PORTAL_SERVICE_NAME}" --region="${REGION}" --format='value(status.url)')"
  export PORTAL_URL
  output_set PORTAL_URL "${PORTAL_URL}"
  log_info "Cymbal Bank Operations Portal deployed: ${PORTAL_URL}"
}
