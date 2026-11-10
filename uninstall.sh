#!/usr/bin/env bash
# ==============================================================================
# Purpose: Reverse-dependency deprovisioning script cleanly tearing down all
#          Cymbal Bank Agent Gateway Demo resources in a target GCP project.
# Architecture/Context: Deletes Semantic Governance Policies & Engine, Reasoning
#          Engines, IAM UAP bindings, AuthzPolicies, Agent Gateways, Service
#          Extensions, Connectivity Templates, Agent Registry entries, Cloud Run
#          services, Model Armor/SDP templates, VPC/DNS/PSC resources, and `.state`
#          files.
# Dependencies/Side Effects: Irreversibly removes demo resources prefixed with
#          `$PREFIX` (default `cbgw`) in the selected GCP project.
# Author: Pradeep Singh
# License: Apache-2.0
# ==============================================================================

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export ROOT_DIR

# shellcheck source=scripts/lib/common.sh
source "${ROOT_DIR}/scripts/lib/common.sh"

#######################################
# Summary: Executes a deletion command, logging warnings instead of aborting if
#          a resource was already deleted.
# Parameters: $1 description; $@ command and arguments.
# Return Value: 0.
# Exceptions/Errors: None.
#######################################
best_effort() {
  local label="$1"
  shift
  log_info "Removing ${label}..."
  if ! "$@" >/dev/null 2>&1; then
    log_warn "Skipped or already absent: ${label}"
  fi
}

#######################################
# Summary: Parses CLI flags, confirms project deletion unless `--yes` is passed,
#          and removes all demo resources in reverse dependency order.
# Parameters: $@ CLI flags (`--project`, `--region`, `--prefix`, `--yes`).
# Return Value: 0.
# Exceptions/Errors: Exits 1 if operator declines confirmation.
#######################################
main() {
  local auto_yes="false"

  while (($# > 0)); do
    case "$1" in
      --project)
        [[ $# -ge 2 ]] || die "--project requires a value"
        export PROJECT_ID="$2"
        shift 2
        ;;
      --region)
        [[ $# -ge 2 ]] || die "--region requires a value"
        export REGION="$2"
        shift 2
        ;;
      --prefix)
        [[ $# -ge 2 ]] || die "--prefix requires a value"
        export PREFIX="$2"
        shift 2
        ;;
      --yes | -y)
        auto_yes="true"
        shift
        ;;
      -h | --help)
        printf 'Usage: ./uninstall.sh [--project <PROJECT_ID>] [--region <REGION>] [--prefix <PREFIX>] [--yes]\n'
        exit 0
        ;;
      *)
        die "Unknown flag: $1"
        ;;
    esac
  done

  select_gcp_project
  init_environment_context

  if [[ "${auto_yes}" != "true" ]]; then
    local confirm=""
    read -r -p "Tear down all '${PREFIX}-*' Agent Gateway Demo resources in '${PROJECT_ID}'? [y/N]: " confirm </dev/tty
    [[ "${confirm}" =~ ^[Yy]$ ]] || die "Teardown cancelled by operator."
  fi

  phase_banner "Deprovisioning Cymbal Bank Agent Gateway Demo (${PROJECT_ID})"

  # 1. Delete Semantic Governance Policies & deprovision engine.
  best_effort "Semantic Governance Policy ${SGP_REFUND_POLICY_ID}" \
    api_call DELETE "https://${REGION}-aiplatform.googleapis.com/v1beta1/projects/${PROJECT_ID}/locations/${REGION}/semanticGovernancePolicies/${SGP_REFUND_POLICY_ID}"
  best_effort "Semantic Governance Policy Engine" \
    api_call POST "https://${REGION}-aiplatform.googleapis.com/v1/projects/${PROJECT_ID}/locations/${REGION}/semanticGovernancePolicyEngine:deprovision" '{"force":true}'

  # 2. Delete Reasoning Engines (must unbind/delete before deleting Agent Gateways).
  local eng
  for eng in "$(output_get TIER1_ENGINE_RESOURCE)" "$(output_get SPECIALIST_ENGINE_RESOURCE)"; do
    if [[ -n "${eng}" ]]; then
      best_effort "Reasoning Engine ${eng}" \
        api_call DELETE "https://${REGION}-aiplatform.googleapis.com/v1/${eng}?force=true"
    fi
  done

  # 3. Delete IAM v3 Unified Access Policy binding & policy.
  best_effort "IAM UAP Binding ${UAP_BINDING_ID}" \
    api_call DELETE "https://iam.googleapis.com/v3beta/projects/${PROJECT_ID}/locations/global/policyBindings/${UAP_BINDING_ID}"
  best_effort "IAM UAP Policy ${UAP_POLICY_ID}" \
    api_call DELETE "https://iam.googleapis.com/v3beta/projects/${PROJECT_ID}/locations/global/accessPolicies/${UAP_POLICY_ID}"

  # 4. Delete AuthzPolicies before AuthzExtensions and AgentGateways.
  local pol
  for pol in "${IAP_AUTHZ_POLICY}" "${MA_INGRESS_POLICY}" "${MA_EGRESS_POLICY}" "${SGP_AUTHZ_POLICY}"; do
    best_effort "AuthzPolicy ${pol}" \
      gc network-security authz-policies delete "${pol}" --location="${REGION}"
  done

  local ext
  for ext in "${IAP_AUTHZ_EXTENSION}" "${MA_AUTHZ_EXTENSION}" "${SGP_AUTHZ_EXTENSION}"; do
    best_effort "AuthzExtension ${ext}" \
      gc beta service-extensions authz-extensions delete "${ext}" --location="${REGION}"
  done

  # 5. Delete Ingress & Egress Agent Gateways and Connectivity Template.
  best_effort "Ingress Agent Gateway ${INGRESS_GATEWAY}" \
    gc network-services agent-gateways delete "${INGRESS_GATEWAY}" --location="${REGION}"
  best_effort "Egress Agent Gateway ${EGRESS_GATEWAY}" \
    gc network-services agent-gateways delete "${EGRESS_GATEWAY}" --location="${REGION}"
  best_effort "Agent Connectivity Template ${CONNECTIVITY_TEMPLATE}" \
    gc network-services agent-connectivity-templates delete "${CONNECTIVITY_TEMPLATE}" --location="${REGION}"

  # 6. Delete Agent Registry services.
  local reg
  for reg in "${REG_TIER1_AGENT}" "${REG_SPECIALIST_AGENT}" "${REG_MCP_UNIFIED}" "${REG_MCP_READONLY}" "${REG_MCP_PRIVILEGED}" "${REG_GOOGLE_APIS}"; do
    best_effort "Agent Registry Service ${reg}" \
      gc agent-registry services delete "${reg}" --location="${REGION}"
  done

  # 7. Delete Cloud Run services, runtime SA & Artifact Registry repository.
  local svc
  for svc in "${PORTAL_SERVICE_NAME}" "${MCP_SERVICE_NAME}" "${ROGUE_SERVICE_NAME}"; do
    best_effort "Cloud Run service ${svc}" \
      gc run services delete "${svc}" --region="${REGION}"
  done
  best_effort "Cloud Run Runtime SA ${PREFIX}-runtime-sa" \
    gc iam service-accounts delete "${PREFIX}-runtime-sa@${PROJECT_ID}.iam.gserviceaccount.com"
  best_effort "Artifact Registry ${AR_REPO}" \
    gc artifacts repositories delete "${AR_REPO}" --location="${REGION}"

  # 8. Delete Model Armor & Cloud SDP templates.
  best_effort "Model Armor Template ${MODEL_ARMOR_TEMPLATE_ID}" \
    api_call DELETE "https://modelarmor.${REGION}.rep.googleapis.com/v1/projects/${PROJECT_ID}/locations/${REGION}/templates/${MODEL_ARMOR_TEMPLATE_ID}"
  best_effort "SDP Deidentify Template ${SDP_DEIDENTIFY_TEMPLATE_ID}" \
    api_call DELETE "https://dlp.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/deidentifyTemplates/${SDP_DEIDENTIFY_TEMPLATE_ID}"
  best_effort "SDP Inspect Template ${SDP_INSPECT_TEMPLATE_ID}" \
    api_call DELETE "https://dlp.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/inspectTemplates/${SDP_INSPECT_TEMPLATE_ID}"

  # 9. Delete PSC Network Attachment, Private DNS Zone (and records), Subnets, and VPC.
  best_effort "PSC Network Attachment ${NETWORK_ATTACHMENT}" \
    gc compute network-attachments delete "${NETWORK_ATTACHMENT}" --region="${REGION}"
  if gc dns managed-zones describe "${DNS_ZONE_NAME}" >/dev/null 2>&1; then
    gc dns record-sets list --zone="${DNS_ZONE_NAME}" --format=json |
      jq -r '.[] | select(.type != "NS" and .type != "SOA") | "\(.name) \(.type)"' |
      while read -r rname rtype; do
        [[ -n "${rname}" ]] && gc dns record-sets delete "${rname}" --type="${rtype}" --zone="${DNS_ZONE_NAME}" >/dev/null 2>&1 || true
      done
    best_effort "Private DNS Zone ${DNS_ZONE_NAME}" \
      gc dns managed-zones delete "${DNS_ZONE_NAME}"
  fi
  best_effort "Proxy Subnet ${VPC_PROXY_SUBNET}" \
    gc compute networks subnets delete "${VPC_PROXY_SUBNET}" --region="${REGION}"
  best_effort "Primary Subnet ${VPC_SUBNET}" \
    gc compute networks subnets delete "${VPC_SUBNET}" --region="${REGION}"
  best_effort "VPC Network ${VPC_NETWORK}" \
    gc compute networks delete "${VPC_NETWORK}"

  rm -f "${STATE_FILE}" "${OUTPUTS_FILE}" \
    "${STATE_DIR}/${PROJECT_ID}".*.metadata.json \
    "${ROOT_DIR}/agents/tier1_support_agent/deployment_metadata.json" \
    "${ROOT_DIR}/agents/risk_specialist_agent/deployment_metadata.json"

  log_info "Deprovisioning completed cleanly for project '${PROJECT_ID}'."
}

main "$@"
