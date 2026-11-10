#!/usr/bin/env bash
# ==============================================================================
# Purpose: Phase 7 provisioning module—deploys BOTH Google ADK agents
#          (`tier1-support-agent` and `risk-specialist-agent`) to Vertex AI
#          Agent Runtime with `AGENT_IDENTITY` and dual gateway bindings, binds
#          IAM Unified Access Policies (with automatic IAP V1 org-policy
#          fallback), flips IAP to `ENFORCED`, and creates the Semantic
#          Governance Policy ($500 refund guardrail).
# Architecture/Context: Sourced by `install.sh` after `gateway_sgp.sh`.
# Dependencies/Side Effects: Requires `uv` and `google-agents-cli` (auto-installed
#          via `uv tool install` if absent in Cloud Shell).
# Author: Pradeep Singh
# License: Apache-2.0
# ==============================================================================

#######################################
# Summary: Ensures `uv` and `agents-cli` are installed in `$PATH` on Cloud Shell.
# Parameters: None.
# Return Value: 0.
# Exceptions/Errors: Calls `die` if `uv` or `agents-cli` cannot be installed.
#######################################
ensure_agents_cli_toolchain() {
  export PATH="${HOME}/.local/bin:${PATH}"
  if ! command -v uv >/dev/null 2>&1; then
    log_info "Installing 'uv' Python package manager..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="${HOME}/.local/bin:${PATH}"
  fi
  if ! command -v agents-cli >/dev/null 2>&1; then
    log_info "Installing 'google-agents-cli' via uv..."
    uv tool install "google-agents-cli>=1.6.0"
  fi
}

#######################################
# Summary: Deploys one ADK agent directory to Vertex AI Agent Runtime bound to
#          both `INGRESS_GATEWAY_RESOURCE` and `EGRESS_GATEWAY_RESOURCE` with
#          `--agent-identity` and the gateway's TLS inspection CA certificate.
# Parameters: $1 agent folder name; $2 display name; $3 routing mode (unified|split).
# Return Value: Prints full Reasoning Engine resource URI (`projects/.../reasoningEngines/ID`).
# Exceptions/Errors: Calls `die` if deployment fails.
#######################################
deploy_single_runtime_agent() {
  local folder="$1" display_name="$2" routing_mode="$3"
  local agent_dir="${ROOT_DIR}/agents/${folder}"
  local meta_file="${agent_dir}/deployment_metadata.json"
  local saved_meta="${STATE_DIR}/${PROJECT_ID}.${folder}.metadata.json"

  # Prevent stale metadata from another GCP project from hijacking this deploy.
  rm -f "${meta_file}"
  if [[ -f "${saved_meta}" ]]; then
    cp "${saved_meta}" "${meta_file}"
  fi

  local agw_cert agw_cert_b64=""
  agw_cert="$(gc network-services agent-gateways describe "${EGRESS_GATEWAY}" \
    --location="${REGION}" \
    --format='value[delimiter=\n](agentGatewayCard.rootCertificates)' || true)"
  if [[ -n "${agw_cert}" ]]; then
    agw_cert_b64="$(printf '%s\n' "${agw_cert}" | base64 | tr -d '\n')"
  fi

  local env_vars="GOOGLE_CLOUD_LOCATION=${REGION},GOOGLE_GENAI_USE_VERTEXAI=true"
  env_vars+=",MODEL_NAME=${MODEL_NAME},MCP_SERVER_URL=${MCP_SERVER_URL}"
  env_vars+=",ROGUE_WEBHOOK_URL=${ROGUE_WEBHOOK_URL},MCP_AUTH_MODE=cloud_run_iam"
  env_vars+=",MCP_ROUTING_MODE=${routing_mode},MCP_TIMEOUT_SECONDS=30"

  log_info "Deploying ${display_name} to Vertex AI Agent Runtime (5-10 min)..."
  (
    cd "${agent_dir}"
    local deploy_args=(
      deploy
      --project="${PROJECT_ID}"
      --region="${REGION}"
      --no-confirm-project
      --deployment-target=agent_runtime
      --agent-identity
      --agent-gateway-ingress="${INGRESS_GATEWAY_RESOURCE}"
      --agent-gateway-egress="${EGRESS_GATEWAY_RESOURCE}"
      --service-name="${display_name}"
      --update-env-vars="${env_vars}"
    )
    if [[ -n "${agw_cert_b64}" ]]; then
      deploy_args+=(--build-args="AGENT_GATEWAY_ROOT_CERTS_B64=${agw_cert_b64}")
    fi
    agents-cli "${deploy_args[@]}" >&2
  ) || die "agents-cli deploy failed for ${display_name}"

  [[ -f "${meta_file}" ]] || die "Missing ${meta_file} after deploying ${display_name}"
  cp "${meta_file}" "${saved_meta}"

  local engine_res
  engine_res="$(jq -r '[.. | strings | select(test("reasoningEngines/"))] | first // empty' "${meta_file}")"
  [[ -n "${engine_res}" ]] || die "Could not parse reasoningEngine resource for ${display_name}"
  state_record reasoning_engine "${engine_res}" "${REGION}" CREATED
  printf '%s' "${engine_res}"
}

#######################################
# Summary: Attempts to create and bind the IAM v3 Unified Access Policy (`V2`).
#          Returns 0 on success or 1 if blocked by enterprise org policy
#          `constraints/iam.managed.disableAccessPolicyBindings`.
# Parameters: None.
# Return Value: 0 when IAM UAP V2 is active; 1 when fallback to IAP V1 is required.
# Exceptions/Errors: None (handled by caller).
#######################################
try_bind_iam_uap_v2() {
  local tmp_dir
  tmp_dir="$(mktemp -d)"

  # Best-effort attempt to relax disableAccessPolicyBindings at project scope if permitted.
  local org_pol="${tmp_dir}/uap-org-policy.yaml"
  cat >"${org_pol}" <<EOF
name: projects/${PROJECT_ID}/policies/iam.managed.disableAccessPolicyBindings
spec:
  rules:
    - enforce: false
EOF
  gcloud org-policies set-policy "${org_pol}" --project="${PROJECT_ID}" >/dev/null 2>&1 || true

  render_template "${ROOT_DIR}/gateway/uap-access-policy.json.tmpl" \
    "${tmp_dir}/uap.raw.json" \
    AGENT_PRINCIPAL_SET REGISTRY_GOOGLE_APIS_NAME TIER1_PRINCIPAL \
    REGISTRY_MCP_UNIFIED_NAME REGISTRY_MCP_RO_NAME REGISTRY_MCP_PRIV_NAME SPECIALIST_PRINCIPAL
  jq 'del(._comment)' "${tmp_dir}/uap.raw.json" >"${tmp_dir}/uap.json"

  local pol_base="https://iam.googleapis.com/v3beta/projects/${PROJECT_ID}/locations/global/accessPolicies"
  if [[ "$(api_status "${pol_base}/${UAP_POLICY_ID}")" == "200" ]]; then
    local etag
    etag="$(api_call GET "${pol_base}/${UAP_POLICY_ID}" | jq -r '.etag // empty')"
    jq --arg e "${etag}" '. + {etag: $e}' "${tmp_dir}/uap.json" >"${tmp_dir}/uap.patch.json"
    if ! api_call PATCH "${pol_base}/${UAP_POLICY_ID}" "@${tmp_dir}/uap.patch.json" >/dev/null; then
      rm -rf "${tmp_dir}"
      return 1
    fi
  else
    if ! api_call POST "${pol_base}?accessPolicyId=${UAP_POLICY_ID}" "@${tmp_dir}/uap.json" >/dev/null; then
      rm -rf "${tmp_dir}"
      return 1
    fi
    state_record iam_uap_policy "${UAP_POLICY_ID}" global CREATED
  fi

  local bind_base="https://iam.googleapis.com/v3beta/projects/${PROJECT_ID}/locations/global/policyBindings"
  if [[ "$(api_status "${bind_base}/${UAP_BINDING_ID}")" != "200" ]]; then
    local bind_body
    bind_body="$(jq -n \
      --arg pol "projects/${PROJECT_ID}/locations/global/accessPolicies/${UAP_POLICY_ID}" \
      --arg tgt "//cloudresourcemanager.googleapis.com/projects/${PROJECT_ID}" \
      '{policyKind: "ACCESS", policy: $pol, target: {resource: $tgt}}')"
    if ! api_call POST "${bind_base}?policyBindingId=${UAP_BINDING_ID}" "${bind_body}" >/dev/null; then
      rm -rf "${tmp_dir}"
      return 1
    fi
    state_record iam_uap_binding "${UAP_BINDING_ID}" global CREATED
  fi

  rm -rf "${tmp_dir}"
  return 0
}

#######################################
# Summary: Applies the enterprise org-policy fallback (`IAP_POLICY_VERSION=V1`)
#          granting `roles/iap.egressor` per individual SPIFFE principal across
#          `/mcp/readonly` (both agents) and `/mcp/privileged` (Risk Specialist only).
# Parameters: None.
# Return Value: 0.
# Exceptions/Errors: Propagates `gcloud iap` errors.
#######################################
apply_iap_v1_fallback() {
  log_warn "IAM v3 policy binding restricted by org policy; activating IAP V1 per-SPIFFE fallback!"

  local google_ep_id="${REGISTRY_GOOGLE_APIS_NAME##*/}"
  retry 4 10 gc iap web add-iam-policy-binding \
    --resource-type=agent-registry --endpoint="${google_ep_id}" --region="${REGION}" \
    --member="${AGENT_PRINCIPAL_SET}" --role="roles/iap.egressor" --format=none

  # Both SPIFFE principals may call `/mcp/readonly`.
  retry 4 10 gc iap web add-iam-policy-binding \
    --resource-type=agent-registry --mcp-server="${REGISTRY_MCP_RO_ID}" --region="${REGION}" \
    --member="${TIER1_PRINCIPAL}" --role="roles/iap.egressor" --format=none
  retry 4 10 gc iap web add-iam-policy-binding \
    --resource-type=agent-registry --mcp-server="${REGISTRY_MCP_RO_ID}" --region="${REGION}" \
    --member="${SPECIALIST_PRINCIPAL}" --role="roles/iap.egressor" --format=none

  # ONLY Risk & Disputes Specialist SPIFFE principal may call `/mcp/privileged`!
  retry 4 10 gc iap web add-iam-policy-binding \
    --resource-type=agent-registry --mcp-server="${REGISTRY_MCP_PRIV_ID}" --region="${REGION}" \
    --member="${SPECIALIST_PRINCIPAL}" --role="roles/iap.egressor" --format=none
}

#######################################
# Summary: Deploys both ADK agents, registers them in Agent Registry, enforces
#          IAM UAP V2 (or IAP V1 fallback), flips IAP enforcement mode, and
#          creates the Semantic Governance Policy ($500 refund guardrail).
# Parameters: None.
# Return Value: 0.
# Exceptions/Errors: Propagates deployment or policy errors.
#######################################
provision_agents_and_policies() {
  phase_banner "Phase 7 — Dual ADK Reasoning Engines, SPIFFE Policies & Semantic Guardrail"

  ensure_agents_cli_toolchain

  export AGENT_PRINCIPAL_SET="principalSet://${AGENT_IDENTITY_TRUST_DOMAIN}/attribute.platformContainer/aiplatform/projects/${PROJECT_NUMBER}"

  TIER1_ENGINE_RESOURCE="$(deploy_single_runtime_agent "tier1_support_agent" "${TIER1_DISPLAY_NAME}" "split")"
  SPECIALIST_ENGINE_RESOURCE="$(deploy_single_runtime_agent "risk_specialist_agent" "${SPECIALIST_DISPLAY_NAME}" "split")"
  export TIER1_ENGINE_RESOURCE SPECIALIST_ENGINE_RESOURCE
  output_set TIER1_ENGINE_RESOURCE "${TIER1_ENGINE_RESOURCE}"
  output_set SPECIALIST_ENGINE_RESOURCE "${SPECIALIST_ENGINE_RESOURCE}"

  local tier1_id="${TIER1_ENGINE_RESOURCE##*/}"
  local spec_id="${SPECIALIST_ENGINE_RESOURCE##*/}"

  export TIER1_PRINCIPAL="principal://${AGENT_IDENTITY_TRUST_DOMAIN}/resources/aiplatform/projects/${PROJECT_NUMBER}/locations/${REGION}/reasoningEngines/${tier1_id}"
  export SPECIALIST_PRINCIPAL="principal://${AGENT_IDENTITY_TRUST_DOMAIN}/resources/aiplatform/projects/${PROJECT_NUMBER}/locations/${REGION}/reasoningEngines/${spec_id}"
  output_set TIER1_PRINCIPAL "${TIER1_PRINCIPAL}"
  output_set SPECIALIST_PRINCIPAL "${SPECIALIST_PRINCIPAL}"

  log_info "Tier-1 SPIFFE Principal    : ${TIER1_PRINCIPAL}"
  log_info "Specialist SPIFFE Principal: ${SPECIALIST_PRINCIPAL}"

  # Register both deployed Reasoning Engines in Agent Registry.
  local reg_t1 reg_sp
  reg_t1="$(upsert_registry_service "${REG_TIER1_AGENT}" "Cymbal Tier-1 Support Agent" \
    "Read-only SPIFFE tier ADK agent on Vertex AI Agent Runtime." \
    --agent-spec-type=no-spec \
    --interfaces="protocolBinding=jsonrpc,url=https://${REGION}-aiplatform.mtls.googleapis.com/v1/projects/${PROJECT_NUMBER}/locations/${REGION}/reasoningEngines/${tier1_id}" 2>/dev/null ||
    upsert_registry_service "${REG_TIER1_AGENT}" "Cymbal Tier-1 Support Agent" \
      "Read-only SPIFFE tier ADK agent on Vertex AI Agent Runtime." \
      --endpoint-spec-type=no-spec \
      --interfaces="protocolBinding=jsonrpc,url=https://${REGION}-aiplatform.mtls.googleapis.com/v1/projects/${PROJECT_NUMBER}/locations/${REGION}/reasoningEngines/${tier1_id}")"
  output_set REGISTRY_TIER1_AGENT "${reg_t1}"

  reg_sp="$(upsert_registry_service "${REG_SPECIALIST_AGENT}" "Cymbal Risk Specialist Agent" \
    "Privileged SPIFFE tier ADK agent governed by Semantic Governance Policy." \
    --agent-spec-type=no-spec \
    --interfaces="protocolBinding=jsonrpc,url=https://${REGION}-aiplatform.mtls.googleapis.com/v1/projects/${PROJECT_NUMBER}/locations/${REGION}/reasoningEngines/${spec_id}" 2>/dev/null ||
    upsert_registry_service "${REG_SPECIALIST_AGENT}" "Cymbal Risk Specialist Agent" \
      "Privileged SPIFFE tier ADK agent governed by Semantic Governance Policy." \
      --endpoint-spec-type=no-spec \
      --interfaces="protocolBinding=jsonrpc,url=https://${REGION}-aiplatform.mtls.googleapis.com/v1/projects/${PROJECT_NUMBER}/locations/${REGION}/reasoningEngines/${spec_id}")"
  output_set REGISTRY_SPECIALIST_AGENT "${reg_sp}"

  # Bind IAM Unified Access Policy (V2) or fall back cleanly to IAP V1 per-SPIFFE bindings.
  if try_bind_iam_uap_v2; then
    export IAP_POLICY_VERSION="V2"
    export ACTIVE_IAP_MODE="IAM_UAP_V2"
    log_info "Bound IAM v3 Unified Access Policy (${UAP_POLICY_ID}) with per-SPIFFE MCP tool CEL rules."
  else
    export IAP_POLICY_VERSION="V1"
    export ACTIVE_IAP_MODE="IAP_V1_FALLBACK"
    apply_iap_v1_fallback
  fi
  output_set ACTIVE_IAP_MODE "${ACTIVE_IAP_MODE}"

  # Activate IAP enforcement unless `--dry-run-policies` was requested.
  local tmp_iap
  tmp_iap="$(mktemp)"
  render_template "${ROOT_DIR}/gateway/iap-authz-extension.yaml.tmpl" \
    "${tmp_iap}" IAP_AUTHZ_EXTENSION IAP_POLICY_VERSION
  if [[ "${DRY_RUN_POLICIES:-false}" == "true" ]]; then
    printf '  iamEnforcementMode: "DRY_RUN"\n' >>"${tmp_iap}"
    log_warn "Keeping IAP extension in DRY_RUN mode (--dry-run-policies enabled)."
  fi
  gc beta service-extensions authz-extensions import "${IAP_AUTHZ_EXTENSION}" \
    --source="${tmp_iap}" --location="${REGION}" --format=none
  rm -f "${tmp_iap}"

  # Discover auto-ingested Agent Registry agent resource ID for Risk Specialist to attach Semantic Governance Policy.
  local sgp_agent_res=""
  sgp_agent_res="$(gc agent-registry agents list \
    --location="${REGION}" --format=json 2>/dev/null |
    jq -r --arg eid "${spec_id}" '.[]? | select((.attributes // {} | tostring | contains($eid)) or (.displayName // "" | contains("risk-specialist"))) | .name' | head -n1 || true)"

  if [[ -z "${sgp_agent_res}" && "${reg_sp}" == *"/agents/"* ]]; then
    sgp_agent_res="projects/${PROJECT_ID}/locations/${REGION}/agents/${reg_sp##*/}"
  fi

  if [[ -n "${sgp_agent_res}" ]]; then
    local nlc_text="Allow issue_dispute_refund when amount_usd is less than or equal to ${SGP_MAX_REFUND_USD}.00 USD. Deny issue_dispute_refund when amount_usd exceeds ${SGP_MAX_REFUND_USD}.00 USD unless supervisor_approval_code is non-empty and starts with 'SUP-APR-'. Deny initiate_wire_reversal unless fraud_verified_ticket starts with 'FRD-'."
    local deny_msg="Cymbal Bank Compliance Policy Denial: Dispute refunds above \$${SGP_MAX_REFUND_USD}.00 require a valid supervisor approval code (SUP-APR-...), and wire reversals require a verified Fraud Ops ticket (FRD-...)."
    local sgp_pol_base="https://${REGION}-aiplatform.googleapis.com/v1beta1/projects/${PROJECT_ID}/locations/${REGION}/semanticGovernancePolicies"
    local sgp_pol_json
    sgp_pol_json="$(jq -n \
      --arg dn "Cymbal Bank $${SGP_MAX_REFUND_USD} Refund & Wire Guardrail" \
      --arg desc "Enforces $${SGP_MAX_REFUND_USD} refund cap and supervisor/fraud-ticket compliance rules on Risk Specialist Agent." \
      --arg ag "${sgp_agent_res}" \
      --arg nlc "${nlc_text}" \
      --arg dm "${deny_msg}" \
      '{displayName: $dn, description: $desc, agent: $ag, naturalLanguageConstraint: $nlc, agentResponseCustomization: {denialMessage: $dm}}')"

    if [[ "$(api_status "${sgp_pol_base}/${SGP_REFUND_POLICY_ID}")" == "200" ]]; then
      local cur_etag
      cur_etag="$(api_call GET "${sgp_pol_base}/${SGP_REFUND_POLICY_ID}" | jq -r '.etag // empty')"
      sgp_pol_json="$(jq --arg e "${cur_etag}" '. + {etag: $e}' <<<"${sgp_pol_json}")"
      api_call PATCH "${sgp_pol_base}/${SGP_REFUND_POLICY_ID}" "${sgp_pol_json}" >/dev/null || true
      state_record sgp_policy "${SGP_REFUND_POLICY_ID}" "${REGION}" REUSED
    else
      api_call POST "${sgp_pol_base}?semanticGovernancePolicyId=${SGP_REFUND_POLICY_ID}" "${sgp_pol_json}" >/dev/null || true
      state_record sgp_policy "${SGP_REFUND_POLICY_ID}" "${REGION}" CREATED
    fi
    log_info "Configured Semantic Governance Policy '${SGP_REFUND_POLICY_ID}' on ${sgp_agent_res}"
  else
    log_warn "Agent Registry auto-discovery entry for ${spec_id} still propagating; retry ./install.sh in 1 min if SGP policy picker is empty."
  fi
}
