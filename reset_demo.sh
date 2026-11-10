#!/usr/bin/env bash
# ==============================================================================
# Purpose: Resets Cymbal Bank customer balances, transactions, and dispute cases
#          back to baseline seed state in <3 seconds between customer demos.
# Architecture/Context: Supports both GCP Firestore (`STORAGE_BACKEND=firestore`)
#          and local JSON mode (`--local` or `STORAGE_BACKEND=local`).
# Dependencies/Side Effects: Overwrites Cymbal Bank demo collections/JSON file.
# Author: Pradeep Singh
# License: Apache-2.0
# ==============================================================================

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export ROOT_DIR

# shellcheck source=scripts/lib/common.sh
source "${ROOT_DIR}/scripts/lib/common.sh"

#######################################
# Summary: Parses `--project` / `--local` flags and invokes `seed_firestore.py`.
# Parameters: $@ CLI arguments.
# Return Value: 0.
# Exceptions/Errors: Propagates Python seed reset failures.
#######################################
main() {
  local mode="firestore"

  while (($# > 0)); do
    case "$1" in
      --project)
        [[ $# -ge 2 ]] || die "--project requires a value"
        export PROJECT_ID="$2"
        shift 2
        ;;
      --local)
        mode="local"
        shift
        ;;
      -h | --help)
        printf 'Usage: ./reset_demo.sh [--project <PROJECT_ID>] [--local]\n'
        exit 0
        ;;
      *)
        die "Unknown flag: $1"
        ;;
    esac
  done

  if [[ "${mode}" == "local" ]]; then
    log_info "Resetting local Cymbal Bank JSON ledger..."
    STORAGE_BACKEND=local uv run python "${ROOT_DIR}/scripts/seed_firestore.py"
    log_info "Local Cymbal Bank ledger restored to baseline."
    return 0
  fi

  select_gcp_project
  init_environment_context
  log_info "Resetting Cymbal Bank Firestore ledger in project '${PROJECT_ID}'..."
  GOOGLE_OAUTH_ACCESS_TOKEN="$(gcloud auth print-access-token)" \
    STORAGE_BACKEND=firestore \
    PROJECT_ID="${PROJECT_ID}" \
    FIRESTORE_DATABASE="${FIRESTORE_DATABASE}" \
    FIRESTORE_COLLECTION_PREFIX="${FIRESTORE_COLLECTION_PREFIX}" \
    uv run python "${ROOT_DIR}/scripts/seed_firestore.py"
  log_info "Firestore ledger restored to clean baseline."
}

main "$@"
