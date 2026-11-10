#!/usr/bin/env bash
# ==============================================================================
# Purpose: Authenticated Cloud Run local port-forwarding helper for accessing the
#          IAM-protected Cymbal Bank Operations Portal (`cymbal-portal`) from
#          Google Cloud Shell or a developer workstation.
# Architecture/Context: Mints an explicit OIDC identity token in the foreground
#          and passes `--token="${ID_TOKEN}"` to `gcloud run services proxy` so
#          Google Cloud Shell's `DEVSHELL_CLIENT_PORT` never substitutes an OAuth
#          `ya29...` access token (avoiding HTTP 401 GFE rejections).
# Dependencies/Side Effects: Binds `127.0.0.1:${PORT}` (default `8080`).
# Author: Pradeep Singh
# License: Apache-2.0
# ==============================================================================

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export ROOT_DIR

# shellcheck source=scripts/lib/common.sh
source "${ROOT_DIR}/scripts/lib/common.sh"

#######################################
# Summary: Resolves target GCP project/region, mints an OIDC ID token, and starts
#          `gcloud run services proxy` on `$LOCAL_PORT`.
# Parameters: $@ CLI flags (`--project`, `--region`, `--port`).
# Return Value: Runs in foreground until Ctrl+C.
# Exceptions/Errors: Calls `die` if identity token generation fails.
#######################################
main() {
  local local_port="8080"

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
      --port)
        [[ $# -ge 2 ]] || die "--port requires a value"
        local_port="$2"
        shift 2
        ;;
      -h | --help)
        printf 'Usage: ./scripts/proxy_portal.sh [--project <PROJECT_ID>] [--region <REGION>] [--port <PORT>]\n'
        exit 0
        ;;
      *)
        die "Unknown flag: $1"
        ;;
    esac
  done

  select_gcp_project
  init_environment_context

  local id_token
  id_token="$(gcloud auth print-identity-token 2>/dev/null || true)"
  if [[ -z "${id_token}" ]]; then
    die "Could not mint an OIDC identity token. Run 'gcloud auth login' once in foreground, then retry."
  fi

  if [[ -n "${WEB_HOST:-}" ]]; then
    log_info "Cloud Shell Web Preview URL: https://${local_port}-${WEB_HOST}"
  else
    log_info "Local Browser URL: http://127.0.0.1:${local_port}"
  fi

  exec gcloud run services proxy "${PORTAL_SERVICE_NAME}" \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --port="${local_port}" \
    --token="${id_token}"
}

main "$@"
