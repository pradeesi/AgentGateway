#!/usr/bin/env bash
# ==============================================================================
# Purpose: Shared Bash foundation library providing structured logging, retry
#          wrappers, template rendering, interactive GCP project selection, REST
#          API callers, and `.state/<PROJECT_ID>.*` checkpoint management.
# Architecture/Context: Sourced by `install.sh`, `reset_demo.sh`, `uninstall.sh`,
#          and all `scripts/lib/*.sh` modules.
# Dependencies/Side Effects: Requires `bash>=4`, `gcloud`, `curl`, `jq`, and
#          `envsubst` (gettext-base, pre-installed in Google Cloud Shell).
# Author: Pradeep Singh
# License: Apache-2.0
# ==============================================================================

set -euo pipefail
export CLOUDSDK_CORE_DISABLE_PROMPTS=1

#######################################
# Summary: Emits a timestamped structured log message to stderr.
# Parameters: $1 severity (INFO|WARN|ERROR); $@ message text.
# Return Value: 0.
# Exceptions/Errors: None.
#######################################
_log() {
  local level="$1"
  shift
  printf '[%s] [%-5s] %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "${level}" "$*" >&2
}

log_info() { _log INFO "$@"; }
log_warn() { _log WARN "$@"; }
log_error() { _log ERROR "$@"; }

#######################################
# Summary: Logs a fatal error message and terminates execution with exit code 1.
# Parameters: $@ fatal diagnostic description.
# Return Value: Never returns (exits 1).
# Exceptions/Errors: Exits 1.
#######################################
die() {
  log_error "$@"
  exit 1
}

#######################################
# Summary: Prints a prominent phase banner to orient the operator during install.
# Parameters: $1 phase title.
# Return Value: 0.
# Exceptions/Errors: None.
#######################################
phase_banner() {
  printf '\n==============================================================================\n' >&2
  printf '  %s\n' "$1" >&2
  printf '==============================================================================\n' >&2
}

#######################################
# Summary: Retries a command with fixed backoff when transient cloud propagation
#          errors occur.
# Parameters: $1 max attempts; $2 sleep seconds; $@ command and arguments.
# Return Value: 0 if any attempt succeeds; last exit code otherwise.
# Exceptions/Errors: Returns non-zero after exhausting `$1` attempts.
#######################################
retry() {
  local max_attempts="$1" delay_secs="$2"
  shift 2
  local attempt=1 rc=0
  while true; do
    if "$@"; then
      return 0
    else
      rc=$?
    fi
    if ((attempt >= max_attempts)); then
      log_warn "Command failed after ${max_attempts} attempts (exit ${rc}): $*"
      return "${rc}"
    fi
    log_warn "Attempt ${attempt}/${max_attempts} failed (exit ${rc}); retrying in ${delay_secs}s..."
    sleep "${delay_secs}"
    attempt=$((attempt + 1))
  done
}

#######################################
# Summary: Wrapper around `gcloud` pinning `--project=${PROJECT_ID}` and `--quiet`.
# Parameters: $@ gcloud subcommand arguments.
# Return Value: Propagates `gcloud` exit status.
# Exceptions/Errors: Propagates `gcloud` errors.
#######################################
gc() {
  gcloud --project="${PROJECT_ID}" --quiet "$@"
}

#######################################
# Summary: Renders a `.tmpl` file substituting ONLY explicitly named environment
#          variables so CEL expressions and JSON `$` literals stay intact.
# Parameters: $1 source template path; $2 destination path; $@ variable names.
# Return Value: 0.
# Exceptions/Errors: Calls `die` if template is missing or `envsubst` fails.
#######################################
render_template() {
  local src="$1" dst="$2"
  shift 2
  [[ -f "${src}" ]] || die "Template file not found: ${src}"
  local var_spec="" var_name
  for var_name in "$@"; do
    var_spec+="\${${var_name}} "
  done
  mkdir -p "$(dirname "${dst}")"
  envsubst "${var_spec}" <"${src}" >"${dst}" || die "Failed rendering template ${src}"
}

#######################################
# Summary: Performs an authenticated Google Cloud REST call with `curl`, returning
#          the response body on HTTP 2xx or failing with diagnostics otherwise.
# Parameters: $1 HTTP method; $2 full URL; $3 optional JSON body or `@filepath`.
# Return Value: Prints response JSON to stdout.
# Exceptions/Errors: Returns 1 on HTTP >= 400 or transport error.
#######################################
api_call() {
  local method="$1" url="$2" body="${3:-}"
  local token resp_file status
  token="$(gcloud auth print-access-token)"
  resp_file="$(mktemp)"
  local curl_args=(
    -sS -o "${resp_file}" -w '%{http_code}' --max-time 90
    -X "${method}"
    -H "Authorization: Bearer ${token}"
    -H "X-Goog-User-Project: ${PROJECT_ID}"
    -H "Content-Type: application/json"
  )
  if [[ -n "${body}" ]]; then
    curl_args+=(-d "${body}")
  fi
  status="$(curl "${curl_args[@]}" "${url}" || printf '000')"
  if [[ "${status}" =~ ^2[0-9][0-9]$ ]]; then
    cat "${resp_file}"
    rm -f "${resp_file}"
    return 0
  fi
  log_warn "REST ${method} ${url} returned HTTP ${status}: $(head -c 400 "${resp_file}")"
  rm -f "${resp_file}"
  return 1
}

#######################################
# Summary: Returns the HTTP status code of an authenticated `GET` request.
# Parameters: $1 full URL.
# Return Value: Prints 3-digit HTTP status (e.g., `200`, `404`).
# Exceptions/Errors: Prints `000` on connection error.
#######################################
api_status() {
  local url="$1" token
  token="$(gcloud auth print-access-token)"
  curl -sS -o /dev/null -w '%{http_code}' --max-time 30 \
    -H "Authorization: Bearer ${token}" \
    -H "X-Goog-User-Project: ${PROJECT_ID}" \
    "${url}" || printf '000'
}

#######################################
# Summary: Interactive numbered GCP project selector used when `--project` is not
#          supplied on the CLI.
# Parameters: None; sets and exports `PROJECT_ID`.
# Return Value: 0.
# Exceptions/Errors: Calls `die` if no accessible active GCP projects are found.
#######################################
select_gcp_project() {
  if [[ -n "${PROJECT_ID:-}" ]]; then
    export PROJECT_ID
    return 0
  fi

  log_info "Discovering accessible Google Cloud projects..."
  local projects=()
  mapfile -t projects < <(
    gcloud projects list --filter="lifecycleState:ACTIVE" \
      --format="value(projectId)" --sort-by=projectId 2>/dev/null
  )
  ((${#projects[@]} > 0)) || die "No active GCP projects found. Run 'gcloud auth login' or pass --project <ID>."

  printf '\nAvailable Google Cloud Projects:\n' >&2
  local idx
  for idx in "${!projects[@]}"; do
    printf '  [%2d] %s\n' "$((idx + 1))" "${projects[$idx]}" >&2
  done
  printf '\n' >&2

  local choice=""
  while true; do
    read -r -p "Select a GCP project number [1-${#projects[@]}]: " choice </dev/tty
    if [[ "${choice}" =~ ^[0-9]+$ ]] && ((choice >= 1 && choice <= ${#projects[@]})); then
      PROJECT_ID="${projects[$((choice - 1))]}"
      export PROJECT_ID
      log_info "Selected GCP project: ${PROJECT_ID}"
      return 0
    fi
    printf 'Invalid selection "%s". Enter a number between 1 and %d.\n' "${choice}" "${#projects[@]}" >&2
  done
}

#######################################
# Summary: Initializes resource naming variables, project/org metadata, and
#          `.state/<PROJECT_ID>.{state,outputs}` checkpoint files.
# Parameters: None.
# Return Value: 0.
# Exceptions/Errors: Calls `die` if `gcloud projects describe` fails.
#######################################
init_environment_context() {
  export REGION="${REGION:-us-central1}"
  export PREFIX="${PREFIX:-cbgw}"
  export MODEL_NAME="${MODEL_NAME:-gemini-2.5-flash}"
  export MODEL_ARMOR_PI_CONFIDENCE="${MODEL_ARMOR_PI_CONFIDENCE:-LOW_AND_ABOVE}"
  export SDP_MIN_LIKELIHOOD="${SDP_MIN_LIKELIHOOD:-POSSIBLE}"
  export SGP_MAX_REFUND_USD="${SGP_MAX_REFUND_USD:-500}"
  export SGP_SUPERVISOR_CODE="${SGP_SUPERVISOR_CODE:-SUP-APR-8841}"
  export SGP_DNS_ZONE_SUFFIX="${SGP_DNS_ZONE_SUFFIX:-sgp.cymbal.internal.}"
  export FIRESTORE_DATABASE="${FIRESTORE_DATABASE:-(default)}"
  export FIRESTORE_COLLECTION_PREFIX="${FIRESTORE_COLLECTION_PREFIX:-cymbal_agw}"

  PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
  [[ -n "${PROJECT_NUMBER}" ]] || die "Could not resolve projectNumber for ${PROJECT_ID}"
  export PROJECT_NUMBER

  ORG_ID="$(gcloud projects get-ancestors "${PROJECT_ID}" --format=json 2>/dev/null |
    jq -r '.[] | select(.type=="organization") | .id' | head -n1 || true)"
  export ORG_ID
  if [[ -n "${ORG_ID}" ]]; then
    export AGENT_IDENTITY_TRUST_DOMAIN="agents.global.org-${ORG_ID}.system.id.goog"
  else
    export AGENT_IDENTITY_TRUST_DOMAIN="agents.global.proj-${PROJECT_NUMBER}.system.id.goog"
  fi

  # Deterministic resource names derived from $PREFIX.
  export AR_REPO="${PREFIX}-containers"
  export MCP_SERVICE_NAME="${PREFIX}-banking-mcp"
  export ROGUE_SERVICE_NAME="${PREFIX}-rogue-ext"
  export PORTAL_SERVICE_NAME="${PREFIX}-portal"
  export SDP_INSPECT_TEMPLATE_ID="${PREFIX}_sdp_inspect"
  export SDP_DEIDENTIFY_TEMPLATE_ID="${PREFIX}_sdp_deidentify"
  export MODEL_ARMOR_TEMPLATE_ID="${PREFIX}-gateway-armor"
  export VPC_NETWORK="${PREFIX}-vpc"
  export VPC_SUBNET="${PREFIX}-subnet"
  export VPC_PROXY_SUBNET="${PREFIX}-proxy-subnet"
  export DNS_ZONE_NAME="${PREFIX}-sgp-zone"
  export NETWORK_ATTACHMENT="${PREFIX}-psc-attachment"
  export CONNECTIVITY_TEMPLATE="${PREFIX}-conn-tmpl"
  export INGRESS_GATEWAY="${PREFIX}-ingress-gw"
  export EGRESS_GATEWAY="${PREFIX}-egress-gw"
  export IAP_AUTHZ_EXTENSION="${PREFIX}-iap-ext"
  export IAP_AUTHZ_POLICY="${PREFIX}-iap-policy"
  export MA_AUTHZ_EXTENSION="${PREFIX}-ma-ext"
  export MA_INGRESS_POLICY="${PREFIX}-ma-ingress-policy"
  export MA_EGRESS_POLICY="${PREFIX}-ma-egress-policy"
  export SGP_AUTHZ_EXTENSION="${PREFIX}-sgp-ext"
  export SGP_AUTHZ_POLICY="${PREFIX}-sgp-policy"
  export SGP_REFUND_POLICY_ID="${PREFIX}-refund-cap-policy"
  export UAP_POLICY_ID="${PREFIX}-uap-least-privilege"
  export UAP_BINDING_ID="${PREFIX}-uap-project-binding"
  export REG_GOOGLE_APIS="${PREFIX}-google-apis"
  export REG_MCP_UNIFIED="${PREFIX}-banking-mcp"
  export REG_MCP_READONLY="${PREFIX}-banking-mcp-ro"
  export REG_MCP_PRIVILEGED="${PREFIX}-banking-mcp-priv"
  export REG_TIER1_AGENT="${PREFIX}-tier1-agent"
  export REG_SPECIALIST_AGENT="${PREFIX}-specialist-agent"
  export TIER1_DISPLAY_NAME="${PREFIX}-tier1-support-agent"
  export SPECIALIST_DISPLAY_NAME="${PREFIX}-risk-specialist-agent"

  export STATE_DIR="${ROOT_DIR}/.state"
  mkdir -p "${STATE_DIR}"
  export STATE_FILE="${STATE_DIR}/${PROJECT_ID}.state"
  export OUTPUTS_FILE="${STATE_DIR}/${PROJECT_ID}.outputs"
  touch "${STATE_FILE}" "${OUTPUTS_FILE}"
}

#######################################
# Summary: Records a provisioned resource line in `.state/<PROJECT_ID>.state`.
# Parameters: $1 type; $2 name/id; $3 location; $4 action (CREATED|REUSED).
# Return Value: 0.
# Exceptions/Errors: None.
#######################################
state_record() {
  local kind="$1" name="$2" loc="$3" mode="${4:-CREATED}"
  if ! grep -Fq "${kind}|${name}|${loc}|" "${STATE_FILE}" 2>/dev/null; then
    printf '%s|%s|%s|%s\n' "${kind}" "${name}" "${loc}" "${mode}" >>"${STATE_FILE}"
  fi
}

#######################################
# Summary: Stores or updates a `KEY=VALUE` entry in `.state/<PROJECT_ID>.outputs`.
# Parameters: $1 key; $2 value.
# Return Value: 0.
# Exceptions/Errors: None.
#######################################
output_set() {
  local key="$1" val="$2"
  local tmp="${OUTPUTS_FILE}.tmp"
  grep -v "^${key}=" "${OUTPUTS_FILE}" >"${tmp}" 2>/dev/null || true
  printf '%s=%s\n' "${key}" "${val}" >>"${tmp}"
  mv "${tmp}" "${OUTPUTS_FILE}"
}

#######################################
# Summary: Reads a `KEY` value from `.state/<PROJECT_ID>.outputs`.
# Parameters: $1 key.
# Return Value: Prints value to stdout (empty if absent).
# Exceptions/Errors: None.
#######################################
output_get() {
  local key="$1"
  grep "^${key}=" "${OUTPUTS_FILE}" 2>/dev/null | head -n1 | cut -d= -f2- || true
}
