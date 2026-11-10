#!/usr/bin/env bash
# ==============================================================================
# Purpose: Single-command idempotent Google Cloud Shell installer for the Cymbal
#          Bank Google Cloud Agent Gateway Defense-in-Depth Demo.
# Architecture/Context: Orchestrates 8 modular provisioning phases across
#          `scripts/lib/*.sh`, recording state checkpoints in `.state/<PROJECT_ID>.*`
#          so interrupted runs resume cleanly without duplicating resources.
# Dependencies/Side Effects: Provisions Firestore, Cloud Run services, SDP &
#          Model Armor templates, VPC/DNS/PSC, Semantic Governance Policy Engine,
#          Agent Registry, Dual Agent Gateways, and Two ADK Reasoning Engines.
# Author: Pradeep Singh
# License: Apache-2.0
# ==============================================================================

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export ROOT_DIR

# shellcheck source=scripts/lib/common.sh
source "${ROOT_DIR}/scripts/lib/common.sh"
# shellcheck source=scripts/lib/foundation.sh
source "${ROOT_DIR}/scripts/lib/foundation.sh"
# shellcheck source=scripts/lib/services.sh
source "${ROOT_DIR}/scripts/lib/services.sh"
# shellcheck source=scripts/lib/model_armor.sh
source "${ROOT_DIR}/scripts/lib/model_armor.sh"
# shellcheck source=scripts/lib/gateway_sgp.sh
source "${ROOT_DIR}/scripts/lib/gateway_sgp.sh"
# shellcheck source=scripts/lib/agents_runtime.sh
source "${ROOT_DIR}/scripts/lib/agents_runtime.sh"

#######################################
# Summary: Prints CLI usage instructions for `./install.sh`.
# Parameters: None.
# Return Value: 0.
# Exceptions/Errors: None.
#######################################
usage() {
  cat <<'EOF'
Usage: ./install.sh [OPTIONS]

Provisions the complete Cymbal Bank Google Cloud Agent Gateway Demo.

Options:
  --project <PROJECT_ID>   Target GCP project ID (interactive numbered menu if omitted)
  --region <REGION>        Target GCP region (default: us-central1)
  --prefix <PREFIX>        Resource name prefix (default: cbgw)
  --dry-run-policies       Deploy IAP extension in audit-only DRY_RUN mode
  -h, --help               Show this help message
EOF
}

#######################################
# Summary: Parses CLI flags, runs all 8 provisioning phases, and prints the
#          authenticated Cloud Shell proxy launch command.
# Parameters: $@ CLI arguments.
# Return Value: 0.
# Exceptions/Errors: Exits non-zero on invalid flags or provisioning errors.
#######################################
main() {
  export DRY_RUN_POLICIES="false"

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
      --dry-run-policies)
        export DRY_RUN_POLICIES="true"
        shift
        ;;
      -h | --help)
        usage
        exit 0
        ;;
      *)
        die "Unknown argument: $1 (run ./install.sh --help)"
        ;;
    esac
  done

  select_gcp_project
  init_environment_context

  provision_foundation_apis_and_iam
  provision_firestore_and_seed
  provision_backend_cloudrun_services
  provision_model_armor_templates
  provision_vpc_and_semantic_engine
  provision_gateways_and_registry
  provision_agents_and_policies
  provision_portal_cloudrun

  phase_banner "Deployment Complete — Cymbal Bank Agent Gateway Demo"
  cat >&2 <<EOF
  Project ID                 : ${PROJECT_ID} (${PROJECT_NUMBER})
  Region                     : ${REGION}
  Ingress Gateway            : ${INGRESS_GATEWAY_RESOURCE}
  Egress Gateway             : ${EGRESS_GATEWAY_RESOURCE}
  IAP Policy Mode            : ${ACTIVE_IAP_MODE}
  Tier-1 Support Engine      : ${TIER1_ENGINE_RESOURCE}
  Risk Specialist Engine     : ${SPECIALIST_ENGINE_RESOURCE}
  Approved Banking MCP URL   : ${MCP_SERVER_URL}/mcp
  Unregistered Rogue URL     : ${ROGUE_WEBHOOK_URL}
  Operations Portal URL      : ${PORTAL_URL}

  Next Step — Open the IAM-Protected Cymbal Bank Portal via Cloud Shell Proxy:
    ./scripts/proxy_portal.sh --project ${PROJECT_ID} --region ${REGION}
EOF
}

main "$@"
