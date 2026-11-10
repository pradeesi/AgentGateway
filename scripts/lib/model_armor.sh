#!/usr/bin/env bash
# ==============================================================================
# Purpose: Phase 4 provisioning module—upserts Sensitive Data Protection (Cloud
#          DLP) inspect/de-identify templates and the regional Model Armor
#          template (auto-merging any org floorSetting), then runs a live SSN
#          redaction self-test.
# Architecture/Context: Sourced by `install.sh`. The resulting Model Armor
#          template is bound to both `CLIENT_TO_AGENT` and `AGENT_TO_ANYWHERE`
#          Agent Gateways via `CONTENT_AUTHZ` service extensions.
# Dependencies/Side Effects: Calls `dlp.googleapis.com` and `modelarmor.*.rep.googleapis.com`.
# Author: Pradeep Singh
# License: Apache-2.0
# ==============================================================================

#######################################
# Summary: Renders a JSON template in `gateway/model_armor/` and strips `_comment`.
# Parameters: $1 filename; $2 output JSON file path.
# Return Value: 0.
# Exceptions/Errors: Calls `die` if rendered output is invalid JSON.
#######################################
render_armor_json() {
  local name="$1" dst="$2"
  render_template "${ROOT_DIR}/gateway/model_armor/${name}" "${dst}.raw" \
    PROJECT_ID REGION SDP_INSPECT_TEMPLATE_ID SDP_DEIDENTIFY_TEMPLATE_ID \
    SDP_MIN_LIKELIHOOD MODEL_ARMOR_PI_CONFIDENCE
  jq 'del(._comment)' "${dst}.raw" >"${dst}" || die "Invalid JSON produced for ${name}"
  rm -f "${dst}.raw"
}

#######################################
# Summary: Creates or patches a Cloud SDP (`dlp.googleapis.com/v2`) template.
# Parameters: $1 collection (inspectTemplates|deidentifyTemplates); $2 id; $3 file.
# Return Value: 0.
# Exceptions/Errors: Propagates REST errors.
#######################################
upsert_sdp_template() {
  local kind="$1" id="$2" file="$3"
  local base="https://dlp.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/${kind}"
  local field state_tag
  if [[ "${kind}" == "inspectTemplates" ]]; then
    field="inspectTemplate"
    state_tag="sdp_inspect_template"
  else
    field="deidentifyTemplate"
    state_tag="sdp_deidentify_template"
  fi

  if [[ "$(api_status "${base}/${id}")" == "200" ]]; then
    jq --arg f "${field}" '{($f): .[$f]}' "${file}" >"${file}.patch"
    api_call PATCH "${base}/${id}" "@${file}.patch" >/dev/null
    rm -f "${file}.patch"
    state_record "${state_tag}" "${id}" "${REGION}" REUSED
  else
    api_call POST "${base}" "@${file}" >/dev/null
    state_record "${state_tag}" "${id}" "${REGION}" CREATED
  fi
}

#######################################
# Summary: Merges any enforced org/project Model Armor floorSetting so template
#          creation never fails with HTTP 400 in floor-enforced organizations.
# Parameters: $1 rendered `ma-template.json` file (updated in place).
# Return Value: 0.
# Exceptions/Errors: Calls `die` if jq merge fails.
#######################################
merge_model_armor_floor() {
  local file="$1" floor_tmp
  floor_tmp="$(mktemp)"
  local url="https://modelarmor.googleapis.com/v1/projects/${PROJECT_ID}/locations/global/floorSetting"
  if [[ "$(api_status "${url}")" == "200" ]]; then
    api_call GET "${url}" >"${floor_tmp}" || true
    if [[ "$(jq -r '.enableFloorSettingEnforcement // false' "${floor_tmp}")" == "true" ]]; then
      jq --slurpfile floor "${floor_tmp}" '
        def rank: {"LOW_AND_ABOVE": 0, "MEDIUM_AND_ABOVE": 1, "HIGH": 2}[.] // 3;
        def stricter($a; $b): if ($a | rank) <= ($b | rank) then $a else $b end;
        ($floor[0].filterConfig // {}) as $f
        | .filterConfig as $t
        | .filterConfig.raiSettings.raiFilters = (
            [($t.raiSettings.raiFilters // [])[], ($f.raiSettings.raiFilters // [])[]]
            | group_by(.filterType)
            | map(reduce .[] as $x ({}; .filterType = $x.filterType
                | .confidenceLevel = (if .confidenceLevel then stricter(.confidenceLevel; $x.confidenceLevel) else $x.confidenceLevel end))))
        | if (.filterConfig.raiSettings.raiFilters | length) == 0 then del(.filterConfig.raiSettings) else . end
      ' "${file}" >"${file}.merged" || die "Failed merging Model Armor floor settings"
      mv "${file}.merged" "${file}"
    fi
  fi
  rm -f "${floor_tmp}"
}

#######################################
# Summary: Provisions SDP + Model Armor templates and validates live SSN masking.
# Parameters: None.
# Return Value: 0.
# Exceptions/Errors: Calls `die` if self-test does not mask synthetic SSN.
#######################################
provision_model_armor_templates() {
  phase_banner "Phase 4 — Cloud SDP & Model Armor Guardrail Templates"

  local tmp_dir
  tmp_dir="$(mktemp -d)"

  render_armor_json "sdp-inspect.json.tmpl" "${tmp_dir}/sdp-inspect.json"
  render_armor_json "sdp-deidentify.json.tmpl" "${tmp_dir}/sdp-deidentify.json"
  render_armor_json "ma-template.json.tmpl" "${tmp_dir}/ma-template.json"
  merge_model_armor_floor "${tmp_dir}/ma-template.json"

  upsert_sdp_template "inspectTemplates" "${SDP_INSPECT_TEMPLATE_ID}" "${tmp_dir}/sdp-inspect.json"
  upsert_sdp_template "deidentifyTemplates" "${SDP_DEIDENTIFY_TEMPLATE_ID}" "${tmp_dir}/sdp-deidentify.json"

  local ma_base="https://modelarmor.${REGION}.rep.googleapis.com/v1/projects/${PROJECT_ID}/locations/${REGION}/templates"
  if [[ "$(api_status "${ma_base}/${MODEL_ARMOR_TEMPLATE_ID}")" == "200" ]]; then
    api_call PATCH "${ma_base}/${MODEL_ARMOR_TEMPLATE_ID}?updateMask=filterConfig,templateMetadata,labels" \
      "@${tmp_dir}/ma-template.json" >/dev/null
    state_record model_armor_template "${MODEL_ARMOR_TEMPLATE_ID}" "${REGION}" REUSED
  else
    retry 4 15 api_call POST "${ma_base}?templateId=${MODEL_ARMOR_TEMPLATE_ID}" \
      "@${tmp_dir}/ma-template.json" >/dev/null
    state_record model_armor_template "${MODEL_ARMOR_TEMPLATE_ID}" "${REGION}" CREATED
  fi
  rm -rf "${tmp_dir}"

  # Verify SDP template + IAM propagation by sanitizing a synthetic SSN record.
  local test_url="${ma_base}/${MODEL_ARMOR_TEMPLATE_ID}:sanitizeModelResponse"
  local probe='{"modelResponseData":{"text":"{\"customer\":\"Elena Vance\",\"ssn\":\"453-29-8174\"}"}}'
  local attempt=1 result="" masked=""
  while ((attempt <= 6)); do
    result="$(api_call POST "${test_url}" "${probe}" || true)"
    masked="$(jq -r '
      .sanitizationResult.filterResults.sdp.sdpFilterResult.deidentifyResult.data.text
      // ([.sanitizationResult.filterResults[]?.sdpFilterResult.deidentifyResult.data.text // empty] | first)
      // ""
    ' <<<"${result}")"
    if [[ "${masked}" == *"[US_SOCIAL_SECURITY_NUMBER]"* && "${masked}" != *"453-29-8174"* ]]; then
      log_info "Model Armor + SDP self-test passed: ${masked}"
      break
    fi
    if ((attempt == 6)); then
      die "Model Armor SDP self-test failed to mask SSN after 6 attempts: ${result}"
    fi
    log_info "Waiting 10s for Model Armor + SDP template propagation (attempt ${attempt}/6)..."
    sleep 10
    attempt=$((attempt + 1))
  done
  output_set MODEL_ARMOR_TEMPLATE "projects/${PROJECT_ID}/locations/${REGION}/templates/${MODEL_ARMOR_TEMPLATE_ID}"
}
