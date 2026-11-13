#!/usr/bin/env bash
# ==============================================================================
# Purpose: Phase 7B Token Management Policy provisioning module—renders and
#          applies `TokenAggregationPolicy`, `TokenBudgetPolicy`, and
#          `CostPerTokenPolicy` (`networksecurity/v1alpha1`) in strict creation
#          order (`Aggregation` -> `Budget` -> `CostPerToken`).
# Architecture/Context: Sourced by `install.sh` after `agents_runtime.sh` so both
#          SPIFFE workload principals (`TIER1_PRINCIPAL`, `SPECIALIST_PRINCIPAL`)
#          and `EGRESS_GATEWAY` exist. Automatically detects whether the target
#          GCP project is enrolled in the `TM_PRE` Early Preview allowlist:
#            - Enrolled (`HTTP 200`): Imports all three policies onto the Egress
#              Agent Gateway via `gcloud alpha network-security ... import`.
#            - Not yet allowlisted (`HTTP 404` / `403`): Saves rendered validated
#              YAML manifests under `.state/<PROJECT_ID>.token-policies/` and
#              activates `HYBRID_GATEWAY_PREVIEW` enforcement in `cymbal-portal`.
# Dependencies/Side Effects: Reads `gateway/token_management/*.yaml.tmpl` and
#          updates `.state/<PROJECT_ID>.{state,outputs}`.
# Author: Pradeep Singh
# License: Apache-2.0
# ==============================================================================

#######################################
# Summary: Renders all three Token Management YAML templates and imports them in
#          strict dependency order when `networksecurity/v1alpha1` Token
#          Management Early Preview (`TM_PRE`) is active on `$PROJECT_ID`.
# Parameters: None.
# Return Value: 0; exports `ACTIVE_TOKEN_MANAGEMENT_MODE`.
# Exceptions/Errors: Propagates template rendering errors; gracefully handles
#                    Early Preview allowlist HTTP 404/403 responses.
#######################################
provision_token_management_policies() {
  phase_banner "Phase 7B — Agent Gateway Token Management Policies (Aggregation, Budget & Cost)"

  local rendered_dir="${STATE_DIR}/${PROJECT_ID}.token-policies"
  mkdir -p "${rendered_dir}"

  local agg_yaml="${rendered_dir}/token-aggregation-policy.yaml"
  local budget_yaml="${rendered_dir}/token-budget-policy.yaml"
  local cost_yaml="${rendered_dir}/cost-per-token-policy.yaml"

  render_template "${ROOT_DIR}/gateway/token_management/token-aggregation-policy.yaml.tmpl" \
    "${agg_yaml}" \
    PROJECT_NUMBER REGION EGRESS_GATEWAY TIER1_PRINCIPAL SPECIALIST_PRINCIPAL

  render_template "${ROOT_DIR}/gateway/token_management/token-budget-policy.yaml.tmpl" \
    "${budget_yaml}" \
    PROJECT_NUMBER REGION EGRESS_GATEWAY TM_SANDBOX_MAX_TOKENS MODEL_NAME \
    TM_SHARED_OPS_MAX_TOKENS TM_SPECIALIST_MAX_TOKENS TM_DEFAULT_MAX_TOKENS

  render_template "${ROOT_DIR}/gateway/token_management/cost-per-token-policy.yaml.tmpl" \
    "${cost_yaml}" \
    PROJECT_NUMBER REGION EGRESS_GATEWAY MODEL_NAME \
    TM_INPUT_TOKEN_COST_USD TM_OUTPUT_TOKEN_COST_USD TM_CACHED_TOKEN_COST_USD TM_PRO_COST_DISCOUNT

  local tm_probe_url="https://networksecurity.googleapis.com/v1alpha1/projects/${PROJECT_ID}/locations/${REGION}/tokenAggregationPolicies"
  local probe_status
  probe_status="$(api_status "${tm_probe_url}")"

  if [[ "${probe_status}" == "200" ]]; then
    log_info "Token Management v1alpha1 API active on ${PROJECT_ID}; importing policies in strict order..."

    # 1. TokenAggregationPolicy MUST be created first.
    gc alpha network-security token-aggregation-policies import "${TOKEN_AGGREGATION_POLICY_ID}" \
      --location="${REGION}" \
      --source="${agg_yaml}" \
      --format=none
    state_record token_aggregation_policy "${TOKEN_AGGREGATION_POLICY_ID}" "${REGION}" CREATED

    # 2. TokenBudgetPolicy MUST be created second (references groups from step 1).
    gc alpha network-security token-budget-policies import "${TOKEN_BUDGET_POLICY_ID}" \
      --location="${REGION}" \
      --source="${budget_yaml}" \
      --format=none
    state_record token_budget_policy "${TOKEN_BUDGET_POLICY_ID}" "${REGION}" CREATED

    # 3. CostPerTokenPolicy MUST be created third (requires TokenBudgetPolicy on scope).
    gc alpha network-security cost-per-token-policies import "${COST_PER_TOKEN_POLICY_ID}" \
      --location="${REGION}" \
      --source="${cost_yaml}" \
      --format=none
    state_record cost_per_token_policy "${COST_PER_TOKEN_POLICY_ID}" "${REGION}" CREATED

    export ACTIVE_TOKEN_MANAGEMENT_MODE="GCP_NETWORK_SECURITY_V1ALPHA1"
    log_info "Imported TokenAggregationPolicy, TokenBudgetPolicy, and CostPerTokenPolicy onto ${EGRESS_GATEWAY}."
  else
    export ACTIVE_TOKEN_MANAGEMENT_MODE="HYBRID_GATEWAY_PREVIEW"
    log_warn "networksecurity/v1alpha1 TokenManagementService returned HTTP ${probe_status} (Early Preview 'TM_PRE' allowlist required)."
    log_info "Rendered validated YAML manifests to ${rendered_dir}/ and activated '${ACTIVE_TOKEN_MANAGEMENT_MODE}' two-phase enforcement."
  fi

  output_set ACTIVE_TOKEN_MANAGEMENT_MODE "${ACTIVE_TOKEN_MANAGEMENT_MODE}"
  output_set TOKEN_AGGREGATION_POLICY_ID "${TOKEN_AGGREGATION_POLICY_ID}"
  output_set TOKEN_BUDGET_POLICY_ID "${TOKEN_BUDGET_POLICY_ID}"
  output_set COST_PER_TOKEN_POLICY_ID "${COST_PER_TOKEN_POLICY_ID}"
}
