#!/usr/bin/env bash
# ==============================================================================
# Purpose: Starts the Cymbal Banking MCP Server (`:8081`), Rogue Egress Webhook
#          (`:8082`), and Cymbal Bank Operations Portal (`:8080`) locally using
#          the JSON file ledger (`STORAGE_BACKEND=local`) for rapid offline or
#          workstation development.
# Architecture/Context: Traps `SIGINT`/`SIGTERM` to cleanly terminate all three
#          child Uvicorn processes together.
# Dependencies/Side Effects: Writes `.local_data/cymbal_ledger.json`.
# Author: Pradeep Singh
# License: Apache-2.0
# ==============================================================================

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

export STORAGE_BACKEND="${STORAGE_BACKEND:-local}"
export LOCAL_LEDGER_PATH="${LOCAL_LEDGER_PATH:-.local_data/cymbal_ledger.json}"
export MCP_SERVER_URL="${MCP_SERVER_URL:-http://127.0.0.1:8081}"
export ROGUE_WEBHOOK_URL="${ROGUE_WEBHOOK_URL:-http://127.0.0.1:8082/collect}"
export MCP_AUTH_MODE="none"
export AGENT_INVOCATION_MODE="local"
export PORTAL_PORT="${PORTAL_PORT:-8080}"

pids=()
cleanup() {
  for pid in "${pids[@]:-}"; do
    kill "${pid}" 2>/dev/null || true
  done
}
trap cleanup EXIT INT TERM

uv run python scripts/seed_firestore.py >/dev/null

PORT=8081 uv run python -m services.cymbal_banking_mcp.server &
pids+=("$!")

PORT=8082 uv run python -m services.cymbal_rogue_ext.app &
pids+=("$!")

printf '\nCymbal Bank Local Stack Running:\n'
printf '  - Operations Portal : http://127.0.0.1:%s\n' "${PORTAL_PORT}"
printf '  - Banking MCP       : http://127.0.0.1:8081/mcp\n'
printf '  - Rogue Webhook     : http://127.0.0.1:8082/collect\n\n'

PORT="${PORTAL_PORT}" uv run python -m services.cymbal_portal.main &
pids+=("$!")

wait
