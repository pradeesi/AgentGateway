#!/usr/bin/env bash
# ==============================================================================
# Purpose: Phase 5 & Phase 6 provisioning module—sets up VPC/DNS/PSC networking,
#          provisions the Semantic Governance Policy Engine, registers approved
#          Google APIs and `cymbal-banking-mcp` tool specs in Agent Registry,
#          and creates both Ingress (`CLIENT_TO_AGENT`) and Egress
#          (`AGENT_TO_ANYWHERE`) Agent Gateways with Model Armor & SGP policies.
# Architecture/Context: Sourced by `install.sh`. Initially deploys the IAP
#          extension in `DRY_RUN` mode while Reasoning Engines are created in
#          Phase 7, then locks IAP to `ENFORCED` once SPIFFE IDs are bound.
# Dependencies/Side Effects: Creates VPC, Cloud DNS, PSC NetworkAttachment,
#          AgentConnectivityTemplate, AgentRegistry services, AgentGateways, and
#          Network Security AuthzPolicies.
# Author: Pradeep Singh
# License: Apache-2.0
# ==============================================================================

#######################################
# Summary: Extracts the trailing resource ID segment from a full resource URI.
# Parameters: $1 resource path.
# Return Value: Prints trailing segment.
# Exceptions/Errors: None.
#######################################
last_segment() {
  printf '%s' "${1##*/}"
}

#######################################
# Summary: Provisions the VPC, subnets, private Cloud DNS zone, PSC network
#          attachment, and Semantic Governance Policy Engine, polling until
#          `state == ACTIVE` and capturing `SGP_DNS_HOSTNAME`.
# Parameters: None.
# Return Value: 0; exports `SGP_DNS_HOSTNAME`.
# Exceptions/Errors: Calls `die` if Semantic Governance engine fails to activate.
#######################################
provision_vpc_and_semantic_engine() {
  phase_banner "Phase 5 — VPC PSC Networking & Semantic Governance Policy Engine"

  if gc compute networks describe "${VPC_NETWORK}" --format='value(name)' >/dev/null 2>&1; then
    state_record vpc_network "${VPC_NETWORK}" global REUSED
  else
    gc compute networks create "${VPC_NETWORK}" --subnet-mode=custom --format=none
    state_record vpc_network "${VPC_NETWORK}" global CREATED
  fi

  if gc compute networks subnets describe "${VPC_SUBNET}" --region="${REGION}" --format='value(name)' >/dev/null 2>&1; then
    state_record vpc_subnet "${VPC_SUBNET}" "${REGION}" REUSED
  else
    # Avoids Agent Gateway internal reserved ranges (10.0.0.0/24..10.0.2.0/24).
    gc compute networks subnets create "${VPC_SUBNET}" \
      --network="${VPC_NETWORK}" \
      --region="${REGION}" \
      --range="10.24.10.0/24" \
      --format=none
    state_record vpc_subnet "${VPC_SUBNET}" "${REGION}" CREATED
  fi

  if gc compute networks subnets describe "${VPC_PROXY_SUBNET}" --region="${REGION}" --format='value(name)' >/dev/null 2>&1; then
    state_record vpc_subnet "${VPC_PROXY_SUBNET}" "${REGION}" REUSED
  else
    gc compute networks subnets create "${VPC_PROXY_SUBNET}" \
      --network="${VPC_NETWORK}" \
      --region="${REGION}" \
      --range="10.24.11.0/24" \
      --purpose=REGIONAL_MANAGED_PROXY \
      --role=ACTIVE \
      --format=none
    state_record vpc_subnet "${VPC_PROXY_SUBNET}" "${REGION}" CREATED
  fi

  if gc dns managed-zones describe "${DNS_ZONE_NAME}" --format='value(name)' >/dev/null 2>&1; then
    state_record dns_zone "${DNS_ZONE_NAME}" global REUSED
  else
    gc dns managed-zones create "${DNS_ZONE_NAME}" \
      --description="Private DNS zone for Cymbal Bank Semantic Governance Policy Engine" \
      --dns-name="${SGP_DNS_ZONE_SUFFIX}" \
      --visibility=private \
      --networks="${VPC_NETWORK}" \
      --format=none
    state_record dns_zone "${DNS_ZONE_NAME}" global CREATED
  fi

  if gc compute network-attachments describe "${NETWORK_ATTACHMENT}" --region="${REGION}" --format='value(name)' >/dev/null 2>&1; then
    state_record network_attachment "${NETWORK_ATTACHMENT}" "${REGION}" REUSED
  else
    gc compute network-attachments create "${NETWORK_ATTACHMENT}" \
      --region="${REGION}" \
      --subnets="${VPC_SUBNET}" \
      --connection-preference=ACCEPT_AUTOMATIC \
      --format=none
    state_record network_attachment "${NETWORK_ATTACHMENT}" "${REGION}" CREATED
  fi

  # Provision Semantic Governance Policy Engine with Google-managed PSC/DNS binding.
  local sgp_url="https://${REGION}-aiplatform.googleapis.com/v1beta1/projects/${PROJECT_ID}/locations/${REGION}/semanticGovernancePolicyEngine"
  local existing_sgp existing_state existing_dns
  existing_sgp="$(api_call GET "${sgp_url}" 2>/dev/null || true)"
  existing_state="$(jq -r '.state // empty' <<<"${existing_sgp}")"
  existing_dns="$(jq -r --arg gw "${EGRESS_GATEWAY}" '.gatewayConfigs[$gw].dnsRecord // empty' <<<"${existing_sgp}")"

  if [[ "${existing_state}" == "ACTIVE" && -n "${existing_dns}" ]]; then
    SGP_DNS_HOSTNAME="${existing_dns%.}"
    state_record sgp_engine "semanticGovernancePolicyEngine" "${REGION}" REUSED
    log_info "Reusing ACTIVE Semantic Governance Policy Engine at ${SGP_DNS_HOSTNAME}"
  else
    local sgp_body
    sgp_body="$(jq -n \
      --arg gw "${EGRESS_GATEWAY}" \
      --arg net "projects/${PROJECT_ID}/global/networks/${VPC_NETWORK}" \
      --arg sub "projects/${PROJECT_ID}/regions/${REGION}/subnetworks/${VPC_SUBNET}" \
      --arg dns "${DNS_ZONE_NAME}" \
      '{gatewayConfigs: {($gw): {network: $net, subnetwork: $sub, dnsZoneName: $dns}}}')"

    log_info "Provisioning Semantic Governance Policy Engine in ${REGION} (2-5 min)..."
    retry 3 15 api_call PATCH "${sgp_url}?updateMask=gatewayConfigs" "${sgp_body}" >/dev/null
    state_record sgp_engine "semanticGovernancePolicyEngine" "${REGION}" CREATED

    local tries=0 engine_json="" engine_state=""
    while ((tries < 45)); do
      engine_json="$(api_call GET "${sgp_url}" 2>/dev/null || true)"
      engine_state="$(jq -r '.state // empty' <<<"${engine_json}")"
      if [[ "${engine_state}" == "ACTIVE" ]]; then
        break
      fi
      log_info "Waiting for Semantic Governance Policy Engine state=ACTIVE (current='${engine_state:-PROVISIONING}')..."
      sleep 15
      tries=$((tries + 1))
    done
    [[ "${engine_state}" == "ACTIVE" ]] || die "Semantic Governance Policy Engine did not reach ACTIVE state"

    SGP_DNS_HOSTNAME="$(jq -r --arg gw "${EGRESS_GATEWAY}" '.gatewayConfigs[$gw].dnsRecord // empty' <<<"${engine_json}")"
    # Strip trailing DNS dot if returned so authzExtensions authority validation passes.
    SGP_DNS_HOSTNAME="${SGP_DNS_HOSTNAME%.}"
    [[ -n "${SGP_DNS_HOSTNAME}" ]] || die "Could not extract SGP dnsRecord from policy engine response"
    log_info "Semantic Governance Policy Engine ACTIVE at ${SGP_DNS_HOSTNAME}"
  fi
  export SGP_DNS_HOSTNAME
  output_set SGP_DNS_HOSTNAME "${SGP_DNS_HOSTNAME}"
}

#######################################
# Summary: Creates or reuses an Agent Registry service and polls until its
#          projected `registryResource` (`endpoints/...`, `mcpServers/...`, or
#          `agents/...`) materializes.
# Parameters: $1 service id; $2 display name; $3 description; $@ gcloud flags.
# Return Value: Prints full `registryResource` name to stdout.
# Exceptions/Errors: Calls `die` if projection fails.
#######################################
upsert_registry_service() {
  local id="$1" display="$2" desc="$3"
  shift 3
  if gc agent-registry services describe "${id}" --location="${REGION}" --format='value(name)' >/dev/null 2>&1; then
    local update_args=() arg
    for arg in "$@"; do
      case "${arg}" in
        --*-spec-type=*) ;;
        *) update_args+=("${arg}") ;;
      esac
    done
    gc agent-registry services update "${id}" \
      --location="${REGION}" \
      --display-name="${display}" \
      --description="${desc}" \
      "${update_args[@]}" --format=none >&2 || true
    state_record registry_service "${id}" "${REGION}" REUSED
  else
    gc agent-registry services create "${id}" \
      --location="${REGION}" \
      --display-name="${display}" \
      --description="${desc}" \
      "$@" --format=none >&2
    state_record registry_service "${id}" "${REGION}" CREATED
  fi

  local projected="" tries=0
  while [[ -z "${projected}" && ${tries} -lt 15 ]]; do
    projected="$(gc agent-registry services describe "${id}" --location="${REGION}" --format='value(registryResource)' 2>/dev/null || true)"
    [[ -n "${projected}" ]] && break
    sleep 4
    tries=$((tries + 1))
  done
  [[ -n "${projected}" ]] || die "Agent Registry did not project registryResource for ${id}"
  printf '%s' "${projected}"
}

#######################################
# Summary: Writes the exact Google Cloud platform API host list (both standard
#          and `:443` / `.mtls.` variants) required by Agent Runtime behind a
#          default-deny `AGENT_TO_ANYWHERE` gateway.
# Parameters: $1 output JSON file path.
# Return Value: 0.
# Exceptions/Errors: None.
#######################################
write_essential_google_api_interfaces() {
  local out_file="$1"
  local hosts=(
    agentregistry.googleapis.com
    aiplatform.googleapis.com
    aiplatform.mtls.googleapis.com
    "${REGION}-aiplatform.googleapis.com"
    "${REGION}-aiplatform.mtls.googleapis.com"
    "aiplatform.${REGION}.rep.googleapis.com"
    logging.googleapis.com
    logging.mtls.googleapis.com
    telemetry.googleapis.com
    telemetry.mtls.googleapis.com
    cloudtrace.googleapis.com
    cloudtrace.mtls.googleapis.com
    monitoring.googleapis.com
    monitoring.mtls.googleapis.com
    secretmanager.googleapis.com
    secretmanager.mtls.googleapis.com
    cloudresourcemanager.googleapis.com
    cloudresourcemanager.mtls.googleapis.com
    iamcredentials.googleapis.com
    iamcredentials.mtls.googleapis.com
    oauth2.googleapis.com
    sts.googleapis.com
  )
  printf '%s\n' "${hosts[@]}" | jq -R . |
    jq -s 'map(("https://" + .), ("https://" + . + ":443")) | map({protocolBinding: "jsonrpc", url: .})' >"${out_file}"
}

#######################################
# Summary: Calls `tools/list` on the deployed IAM-protected `cymbal-banking-mcp`
#          service to capture the authoritative MCP tool spec (with `readOnlyHint`
#          annotations) for Agent Registry registration. Supports both service
#          accounts (`--audiences`) and `gcloud auth login` user accounts.
# Parameters: $1 mount path (`/mcp`, `/mcp/readonly`, `/mcp/privileged`); $2 out file.
# Return Value: 0 if non-empty tool list written.
# Exceptions/Errors: Returns 1 on HTTP/JSON failure.
#######################################
fetch_mcp_tool_spec() {
  local mount_path="$1" out_file="$2" id_token
  id_token="$(gcloud auth print-identity-token --audiences="${MCP_SERVER_URL}" 2>/dev/null || gcloud auth print-identity-token)"
  curl -fsS --max-time 30 -X POST "${MCP_SERVER_URL}${mount_path}" \
    -H "Authorization: Bearer ${id_token}" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json" \
    -d '{"jsonrpc":"2.0","id":"reg-spec","method":"tools/list","params":{}}' |
    jq -e '.result | select((.tools // []) | length > 0)' >"${out_file}"
}

#######################################
# Summary: Registers Google APIs + Cymbal Banking MCP specs in Agent Registry,
#          imports the Connectivity Template, Ingress & Egress Agent Gateways,
#          and attaches Model Armor, Semantic Governance, and initial IAP
#          (`DRY_RUN`) authorization policies.
# Parameters: None.
# Return Value: 0.
# Exceptions/Errors: Propagates `gcloud` failures.
#######################################
provision_gateways_and_registry() {
  phase_banner "Phase 6 — Agent Registry, Dual Agent Gateways & Content Policies"

  if [[ "${FORCE_GATEWAY:-false}" != "true" ]] &&
    gc network-services agent-gateways describe "${EGRESS_GATEWAY}" --location="${REGION}" --format='value(name)' >/dev/null 2>&1 &&
    gc network-security authz-policies describe "${IAP_AUTHZ_POLICY}" --location="${REGION}" --format='value(name)' >/dev/null 2>&1; then
    local g_raw m_raw ro_raw pr_raw
    g_raw="$(gc agent-registry services describe "${REG_GOOGLE_APIS}" --location="${REGION}" --format='value(registryResource)')"
    m_raw="$(gc agent-registry services describe "${REG_MCP_UNIFIED}" --location="${REGION}" --format='value(registryResource)')"
    ro_raw="$(gc agent-registry services describe "${REG_MCP_READONLY}" --location="${REGION}" --format='value(registryResource)')"
    pr_raw="$(gc agent-registry services describe "${REG_MCP_PRIVILEGED}" --location="${REGION}" --format='value(registryResource)')"
    export REGISTRY_GOOGLE_APIS_NAME="projects/${PROJECT_ID}/locations/${REGION}/endpoints/$(last_segment "${g_raw}")"
    export REGISTRY_MCP_UNIFIED_NAME="projects/${PROJECT_ID}/locations/${REGION}/mcpServers/$(last_segment "${m_raw}")"
    export REGISTRY_MCP_RO_ID="$(last_segment "${ro_raw}")"
    export REGISTRY_MCP_PRIV_ID="$(last_segment "${pr_raw}")"
    export REGISTRY_MCP_RO_NAME="projects/${PROJECT_ID}/locations/${REGION}/mcpServers/${REGISTRY_MCP_RO_ID}"
    export REGISTRY_MCP_PRIV_NAME="projects/${PROJECT_ID}/locations/${REGION}/mcpServers/${REGISTRY_MCP_PRIV_ID}"
    export INGRESS_GATEWAY_RESOURCE="projects/${PROJECT_ID}/locations/${REGION}/agentGateways/${INGRESS_GATEWAY}"
    export EGRESS_GATEWAY_RESOURCE="projects/${PROJECT_ID}/locations/${REGION}/agentGateways/${EGRESS_GATEWAY}"
    output_set REGISTRY_GOOGLE_APIS_NAME "${REGISTRY_GOOGLE_APIS_NAME}"
    output_set REGISTRY_MCP_UNIFIED_NAME "${REGISTRY_MCP_UNIFIED_NAME}"
    output_set REGISTRY_MCP_RO_NAME "${REGISTRY_MCP_RO_NAME}"
    output_set REGISTRY_MCP_PRIV_NAME "${REGISTRY_MCP_PRIV_NAME}"
    output_set INGRESS_GATEWAY_RESOURCE "${INGRESS_GATEWAY_RESOURCE}"
    output_set EGRESS_GATEWAY_RESOURCE "${EGRESS_GATEWAY_RESOURCE}"
    log_info "Reusing provisioned Dual Agent Gateways (${INGRESS_GATEWAY}, ${EGRESS_GATEWAY}) and policies (set FORCE_GATEWAY=true to re-import)."
    return 0
  fi

  local tmp_dir
  tmp_dir="$(mktemp -d)"

  # 1. Register essential Google APIs in Agent Registry.
  write_essential_google_api_interfaces "${tmp_dir}/google-apis.json"
  REGISTRY_GOOGLE_APIS_RAW="$(upsert_registry_service "${REG_GOOGLE_APIS}" \
    "Google Cloud Platform APIs for Agent Runtime" \
    "Essential Gemini, sessions, logging, tracing, and IAM endpoints." \
    --endpoint-spec-type=no-spec \
    --interfaces="${tmp_dir}/google-apis.json")"
  export REGISTRY_GOOGLE_APIS_NAME="projects/${PROJECT_ID}/locations/${REGION}/endpoints/$(last_segment "${REGISTRY_GOOGLE_APIS_RAW}")"
  output_set REGISTRY_GOOGLE_APIS_NAME "${REGISTRY_GOOGLE_APIS_NAME}"

  # 2. Register Cymbal Banking MCP Server (`/mcp`, plus `/mcp/readonly` & `/mcp/privileged`).
  #    Note: `cymbal-rogue-ext` is deliberately NOT registered to prove default-deny!
  retry 4 10 fetch_mcp_tool_spec "/mcp" "${tmp_dir}/mcp-all.json"
  REGISTRY_MCP_UNIFIED_RAW="$(upsert_registry_service "${REG_MCP_UNIFIED}" \
    "Cymbal Banking MCP Server" \
    "Approved Cymbal Bank disputes & treasury MCP server with readOnlyHint annotations." \
    --mcp-server-spec-type=tool-spec \
    --mcp-server-spec-content="$(jq -c . "${tmp_dir}/mcp-all.json")" \
    --interfaces="protocolBinding=jsonrpc,url=${MCP_SERVER_URL}/mcp")"
  export REGISTRY_MCP_UNIFIED_NAME="projects/${PROJECT_ID}/locations/${REGION}/mcpServers/$(last_segment "${REGISTRY_MCP_UNIFIED_RAW}")"
  output_set REGISTRY_MCP_UNIFIED_NAME "${REGISTRY_MCP_UNIFIED_NAME}"

  retry 3 10 fetch_mcp_tool_spec "/mcp/readonly" "${tmp_dir}/mcp-ro.json"
  REGISTRY_MCP_RO_RAW="$(upsert_registry_service "${REG_MCP_READONLY}" \
    "Cymbal Banking MCP Read-Only Mount" \
    "Read-only Cymbal Banking MCP tools for per-SPIFFE least privilege." \
    --mcp-server-spec-type=tool-spec \
    --mcp-server-spec-content="$(jq -c . "${tmp_dir}/mcp-ro.json")" \
    --interfaces="protocolBinding=jsonrpc,url=${MCP_SERVER_URL}/mcp/readonly")"
  export REGISTRY_MCP_RO_ID="$(last_segment "${REGISTRY_MCP_RO_RAW}")"
  export REGISTRY_MCP_RO_NAME="projects/${PROJECT_ID}/locations/${REGION}/mcpServers/${REGISTRY_MCP_RO_ID}"
  output_set REGISTRY_MCP_RO_NAME "${REGISTRY_MCP_RO_NAME}"

  retry 3 10 fetch_mcp_tool_spec "/mcp/privileged" "${tmp_dir}/mcp-priv.json"
  REGISTRY_MCP_PRIV_RAW="$(upsert_registry_service "${REG_MCP_PRIVILEGED}" \
    "Cymbal Banking MCP Privileged Mount" \
    "Privileged Cymbal Banking MCP mutation tools restricted to Risk Specialist SPIFFE ID." \
    --mcp-server-spec-type=tool-spec \
    --mcp-server-spec-content="$(jq -c . "${tmp_dir}/mcp-priv.json")" \
    --interfaces="protocolBinding=jsonrpc,url=${MCP_SERVER_URL}/mcp/privileged")"
  export REGISTRY_MCP_PRIV_ID="$(last_segment "${REGISTRY_MCP_PRIV_RAW}")"
  export REGISTRY_MCP_PRIV_NAME="projects/${PROJECT_ID}/locations/${REGION}/mcpServers/${REGISTRY_MCP_PRIV_ID}"
  output_set REGISTRY_MCP_PRIV_NAME "${REGISTRY_MCP_PRIV_NAME}"

  # 3. Ensure `gcp-sa-agentgateway` holds VPC/DNS peering & reader roles, then import Connectivity Template & Gateways.
  local agw_sa="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-agentgateway.iam.gserviceaccount.com"
  local agw_role
  for agw_role in roles/compute.networkViewer roles/compute.networkUser roles/compute.networkAdmin roles/dns.peer roles/dns.reader; do
    gc projects add-iam-policy-binding "${PROJECT_ID}" \
      --member="${agw_sa}" --role="${agw_role}" --condition=None --format=none >/dev/null 2>&1 || true
  done

  render_template "${ROOT_DIR}/gateway/connectivity-template.yaml.tmpl" \
    "${tmp_dir}/connectivity-template.yaml" \
    PROJECT_NUMBER REGION CONNECTIVITY_TEMPLATE NETWORK_ATTACHMENT SGP_DNS_ZONE_SUFFIX PROJECT_ID VPC_NETWORK
  retry 6 15 gc network-services agent-connectivity-templates import "${CONNECTIVITY_TEMPLATE}" \
    --source="${tmp_dir}/connectivity-template.yaml" \
    --location="${REGION}" --format=none
  state_record connectivity_template "${CONNECTIVITY_TEMPLATE}" "${REGION}" CREATED

  render_template "${ROOT_DIR}/gateway/gateway-ingress.yaml.tmpl" \
    "${tmp_dir}/gateway-ingress.yaml" INGRESS_GATEWAY
  gc network-services agent-gateways import "${INGRESS_GATEWAY}" \
    --source="${tmp_dir}/gateway-ingress.yaml" \
    --location="${REGION}" --format=none
  state_record agent_gateway "${INGRESS_GATEWAY}" "${REGION}" CREATED

  render_template "${ROOT_DIR}/gateway/gateway-egress.yaml.tmpl" \
    "${tmp_dir}/gateway-egress.yaml" \
    EGRESS_GATEWAY PROJECT_NUMBER REGION CONNECTIVITY_TEMPLATE PROJECT_ID
  gc network-services agent-gateways import "${EGRESS_GATEWAY}" \
    --source="${tmp_dir}/gateway-egress.yaml" \
    --location="${REGION}" --format=none
  state_record agent_gateway "${EGRESS_GATEWAY}" "${REGION}" CREATED

  export INGRESS_GATEWAY_RESOURCE="projects/${PROJECT_ID}/locations/${REGION}/agentGateways/${INGRESS_GATEWAY}"
  export EGRESS_GATEWAY_RESOURCE="projects/${PROJECT_ID}/locations/${REGION}/agentGateways/${EGRESS_GATEWAY}"
  output_set INGRESS_GATEWAY_RESOURCE "${INGRESS_GATEWAY_RESOURCE}"
  output_set EGRESS_GATEWAY_RESOURCE "${EGRESS_GATEWAY_RESOURCE}"

  # 4. Attach Model Armor `CONTENT_AUTHZ` to BOTH Ingress and Egress Gateways.
  render_template "${ROOT_DIR}/gateway/ma-authz-extension.yaml.tmpl" \
    "${tmp_dir}/ma-ext.yaml" MA_AUTHZ_EXTENSION REGION PROJECT_ID MODEL_ARMOR_TEMPLATE_ID
  gc beta service-extensions authz-extensions import "${MA_AUTHZ_EXTENSION}" \
    --source="${tmp_dir}/ma-ext.yaml" --location="${REGION}" --format=none
  state_record authz_extension "${MA_AUTHZ_EXTENSION}" "${REGION}" CREATED

  render_template "${ROOT_DIR}/gateway/ma-ingress-policy.yaml.tmpl" \
    "${tmp_dir}/ma-ingress-policy.yaml" MA_INGRESS_POLICY PROJECT_ID REGION INGRESS_GATEWAY MA_AUTHZ_EXTENSION
  gc network-security authz-policies import "${MA_INGRESS_POLICY}" \
    --source="${tmp_dir}/ma-ingress-policy.yaml" --location="${REGION}" --format=none
  state_record authz_policy "${MA_INGRESS_POLICY}" "${REGION}" CREATED

  render_template "${ROOT_DIR}/gateway/ma-egress-policy.yaml.tmpl" \
    "${tmp_dir}/ma-egress-policy.yaml" MA_EGRESS_POLICY PROJECT_ID REGION EGRESS_GATEWAY MA_AUTHZ_EXTENSION
  gc network-security authz-policies import "${MA_EGRESS_POLICY}" \
    --source="${tmp_dir}/ma-egress-policy.yaml" --location="${REGION}" --format=none
  state_record authz_policy "${MA_EGRESS_POLICY}" "${REGION}" CREATED

  # 5. Attach Semantic Governance Policy Engine `CONTENT_AUTHZ` to Egress Gateway.
  render_template "${ROOT_DIR}/gateway/sgp-authz-extension.yaml.tmpl" \
    "${tmp_dir}/sgp-ext.yaml" SGP_AUTHZ_EXTENSION SGP_DNS_HOSTNAME
  gc beta service-extensions authz-extensions import "${SGP_AUTHZ_EXTENSION}" \
    --source="${tmp_dir}/sgp-ext.yaml" --location="${REGION}" --format=none
  state_record authz_extension "${SGP_AUTHZ_EXTENSION}" "${REGION}" CREATED

  render_template "${ROOT_DIR}/gateway/sgp-authz-policy.yaml.tmpl" \
    "${tmp_dir}/sgp-policy.yaml" SGP_AUTHZ_POLICY PROJECT_ID REGION EGRESS_GATEWAY SGP_AUTHZ_EXTENSION
  gc network-security authz-policies import "${SGP_AUTHZ_POLICY}" \
    --source="${tmp_dir}/sgp-policy.yaml" --location="${REGION}" --format=none
  state_record authz_policy "${SGP_AUTHZ_POLICY}" "${REGION}" CREATED

  # 6. Attach IAP `REQUEST_AUTHZ` initially in DRY_RUN while Reasoning Engines boot.
  export IAP_POLICY_VERSION="V2"
  render_template "${ROOT_DIR}/gateway/iap-authz-extension.yaml.tmpl" \
    "${tmp_dir}/iap-ext.yaml" IAP_AUTHZ_EXTENSION IAP_POLICY_VERSION
  printf '  iamEnforcementMode: "DRY_RUN"\n' >>"${tmp_dir}/iap-ext.yaml"
  gc beta service-extensions authz-extensions import "${IAP_AUTHZ_EXTENSION}" \
    --source="${tmp_dir}/iap-ext.yaml" --location="${REGION}" --format=none
  state_record authz_extension "${IAP_AUTHZ_EXTENSION}" "${REGION}" CREATED

  render_template "${ROOT_DIR}/gateway/iap-authz-policy.yaml.tmpl" \
    "${tmp_dir}/iap-policy.yaml" IAP_AUTHZ_POLICY PROJECT_ID REGION EGRESS_GATEWAY IAP_AUTHZ_EXTENSION
  gc network-security authz-policies import "${IAP_AUTHZ_POLICY}" \
    --source="${tmp_dir}/iap-policy.yaml" --location="${REGION}" --format=none
  state_record authz_policy "${IAP_AUTHZ_POLICY}" "${REGION}" CREATED

  rm -rf "${tmp_dir}"
  log_info "Dual Agent Gateways (${INGRESS_GATEWAY}, ${EGRESS_GATEWAY}) and extensions imported."
}
