#!/usr/bin/env bash
# ==============================================================================
# Purpose: Authenticated Cloud Run local port-forwarding helper for accessing the
#          IAM-protected Cymbal Bank Operations Portal (`cymbal-portal`) from
#          Google Cloud Shell or any Linux/macOS developer workstation.
# Architecture/Context: Uses `gcloud run services proxy` when the `cloud-run-proxy`
#          binary is installed, and automatically falls back to an embedded ASGI
#          (`uvicorn` + `httpx`) OIDC reverse proxy when running on workstations
#          where apt/package-managed Cloud SDK disables the component manager.
#          Refreshes OIDC identity tokens automatically so multi-hour customer
#          demos never fail mid-session with HTTP 401 expiration errors.
# Dependencies/Side Effects: Binds `127.0.0.1:${PORT}` (default `8080`). Requires
#          authenticated `gcloud` CLI and either `cloud-run-proxy` or Python with
#          `fastapi`, `httpx`, and `uvicorn` (installed in `.venv` via `uv sync`).
# Author: Pradeep Singh
# License: Apache-2.0
# ==============================================================================

set -euo pipefail

# Ensure standard system Cloud SDK paths are discoverable in non-login shells
# without overriding existing user binary directories.
if [[ -d "/usr/lib/google-cloud-sdk/bin" && ":${PATH}:" != *":/usr/lib/google-cloud-sdk/bin:"* ]]; then
  export PATH="/usr/lib/google-cloud-sdk/bin:${PATH}"
fi
# Prevent host sitecustomize hooks from breaking bundled Cloud SDK Python.
unset PYTHONPATH

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export ROOT_DIR

# shellcheck source=scripts/lib/common.sh
source "${ROOT_DIR}/scripts/lib/common.sh"

#######################################
# Summary: Checks whether the native `cloud-run-proxy` binary required by
#          `gcloud run services proxy` is installed on the host.
# Parameters: None.
# Return Value: 0 if `cloud-run-proxy` is executable; 1 otherwise.
# Exceptions/Errors: None.
#######################################
has_cloud_run_proxy_binary() {
  if command -v cloud-run-proxy >/dev/null 2>&1; then
    return 0
  fi
  local sdk_root
  sdk_root="$(gcloud info --format='value(installation.sdk_root)' 2>/dev/null || true)"
  if [[ -n "${sdk_root}" && -x "${sdk_root}/bin/cloud-run-proxy" ]]; then
    return 0
  fi
  return 1
}

#######################################
# Summary: Resolves a Python interpreter equipped with `fastapi`, `httpx`, and
#          `uvicorn` for the built-in OIDC reverse-proxy fallback.
# Parameters: None.
# Return Value: Prints absolute/command path to stdout and returns 0.
# Exceptions/Errors: Calls `die` if no suitable Python environment is available.
#######################################
resolve_proxy_python() {
  if [[ -x "${ROOT_DIR}/.venv/bin/python" ]]; then
    printf '%s\n' "${ROOT_DIR}/.venv/bin/python"
    return 0
  fi
  if command -v uv >/dev/null 2>&1; then
    printf '%s\n' "uv run python"
    return 0
  fi
  if command -v python3 >/dev/null 2>&1; then
    printf '%s\n' "python3"
    return 0
  fi
  die "Neither 'cloud-run-proxy' nor a Python 3 runtime (.venv/uv) was found."
}

#######################################
# Summary: Launches an in-process `127.0.0.1` ASGI reverse proxy that attaches
#          and auto-refreshes `gcloud auth print-identity-token` OIDC headers
#          on every request forwarded to the IAM-protected Cloud Run Portal URL.
# Parameters:
#   $1 (string) - Target HTTPS Cloud Run URL (`PORTAL_URL`).
#   $2 (int)    - Local TCP port to bind on `127.0.0.1`.
# Return Value: Runs in foreground until SIGINT/SIGTERM.
# Exceptions/Errors: Exits non-zero if port binding or token minting fails.
#######################################
run_python_oidc_proxy() {
  local target_url="$1"
  local local_port="$2"
  local py_cmd
  py_cmd="$(resolve_proxy_python)"

  log_info "Starting built-in auto-refreshing OIDC reverse proxy -> ${target_url}"
  export PROXY_TARGET_URL="${target_url}"
  export PROXY_LOCAL_PORT="${local_port}"

  # shellcheck disable=SC2086
  exec ${py_cmd} - <<'PY'
"""
Purpose: Loopback-only OIDC reverse proxy for IAM-protected Cloud Run services.
Architecture/Context: Invoked by `scripts/proxy_portal.sh` when `cloud-run-proxy`
         is unavailable on apt-managed Linux workstations.
Dependencies/Side Effects: Binds `127.0.0.1:$PROXY_LOCAL_PORT` and shells out to
         `gcloud auth print-identity-token` every 45 minutes (or on HTTP 401).
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)-5s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
logger = logging.getLogger("cymbal.oidc_proxy")

TARGET_URL = os.environ["PROXY_TARGET_URL"].rstrip("/")
LOCAL_PORT = int(os.environ["PROXY_LOCAL_PORT"])
# Refresh 15 minutes before standard 3600s Google OIDC token expiration.
TOKEN_TTL_SECONDS = 2700.0

_HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "authorization",
        "content-length",
    }
)


class OidcTokenCache:
    """
    Summary:
        Thread-safe cache that mints and refreshes Google OIDC identity tokens
        via `gcloud auth print-identity-token`.

    Parameters:
        ttl_seconds (float): Maximum lifetime in seconds before proactive refresh.

    Return Value:
        Initialized `OidcTokenCache` instance.

    Exceptions/Errors:
        RuntimeError: Raised by `get_token` when `gcloud` cannot mint an ID token.
    """

    def __init__(self, ttl_seconds: float = TOKEN_TTL_SECONDS) -> None:
        self._ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._token: str = ""
        self._expires_at: float = 0.0

    def get_token(self, *, force_refresh: bool = False) -> str:
        """
        Summary:
            Returns a cached valid OIDC identity token or mints a fresh one.

        Parameters:
            force_refresh (bool): Forces immediate re-minting (used on HTTP 401).

        Return Value:
            str: Signed OIDC bearer token string.

        Exceptions/Errors:
            RuntimeError: If `gcloud auth print-identity-token` exits non-zero.
        """
        now = time.monotonic()
        with self._lock:
            if not force_refresh and self._token and now < self._expires_at:
                return self._token

            env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
            proc = subprocess.run(
                ["gcloud", "auth", "print-identity-token"],
                capture_output=True,
                text=True,
                timeout=15,
                env=env,
                check=False,
            )
            token = proc.stdout.strip()
            if proc.returncode != 0 or not token:
                detail = proc.stderr.strip() or "empty identity token"
                raise RuntimeError(f"Failed to mint OIDC token via gcloud: {detail}")

            self._token = token
            self._expires_at = now + self._ttl_seconds
            logger.info("Minted fresh OIDC identity token (proactive refresh in 45m).")
            return self._token


_token_cache = OidcTokenCache()
_http_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    """
    Summary:
        Manages the shared upstream `httpx.AsyncClient` connection pool.

    Parameters:
        _ (FastAPI): FastAPI application instance.

    Return Value:
        AsyncIterator[None]: Lifespan context manager.

    Exceptions/Errors:
        RuntimeError: Propagated if initial OIDC token validation fails.
    """
    global _http_client
    _token_cache.get_token(force_refresh=True)
    # 120s timeout accommodates synchronous Agent Runtime + Semantic Governance turns.
    _http_client = httpx.AsyncClient(
        base_url=TARGET_URL,
        timeout=httpx.Timeout(120.0, connect=15.0),
        follow_redirects=False,
    )
    logger.info("Cymbal Portal OIDC Proxy ready at http://127.0.0.1:%d -> %s", LOCAL_PORT, TARGET_URL)
    try:
        yield
    finally:
        await _http_client.aclose()


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan)


@app.api_route(
    "/{full_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
async def proxy_request(full_path: str, request: Request) -> Response:
    """
    Summary:
        Forwards any incoming loopback HTTP request to the Cloud Run Portal with
        a fresh `Authorization: Bearer <OIDC_TOKEN>` header.

    Parameters:
        full_path (str): Request URL path segment.
        request (Request): Incoming Starlette/FastAPI HTTP request.

    Return Value:
        Response: Upstream Cloud Run HTTP status, headers, and payload bytes.

    Exceptions/Errors:
        HTTPException: HTTP 502 if upstream Cloud Run or token refresh fails.
    """
    if _http_client is None:
        raise HTTPException(status_code=503, detail="Upstream HTTP client not initialized.")

    body = await request.body()
    upstream_path = f"/{full_path}"
    if request.url.query:
        upstream_path = f"{upstream_path}?{request.url.query}"

    outbound_headers = {
        k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP_HEADERS
    }

    try:
        token = _token_cache.get_token()
        outbound_headers["Authorization"] = f"Bearer {token}"
        upstream_resp = await _http_client.request(
            method=request.method,
            url=upstream_path,
            content=body,
            headers=outbound_headers,
        )
        # Re-mint once transparently if Google Front End rejected a stale token.
        if upstream_resp.status_code == 401:
            token = _token_cache.get_token(force_refresh=True)
            outbound_headers["Authorization"] = f"Bearer {token}"
            upstream_resp = await _http_client.request(
                method=request.method,
                url=upstream_path,
                content=body,
                headers=outbound_headers,
            )
    except Exception as exc:
        logger.error("Proxy upstream error for %s %s: %s", request.method, upstream_path, exc)
        raise HTTPException(status_code=502, detail=f"Upstream proxy error: {exc}") from exc

    response_headers = {
        k: v
        for k, v in upstream_resp.headers.items()
        if k.lower() not in _HOP_BY_HOP_HEADERS and k.lower() != "content-encoding"
    }
    return Response(
        content=upstream_resp.content,
        status_code=upstream_resp.status_code,
        headers=response_headers,
    )


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=LOCAL_PORT, log_level="warning")
PY
}

#######################################
# Summary: Resolves target GCP project/region, mints an OIDC ID token, and starts
#          `gcloud run services proxy` (or the built-in Python OIDC proxy fallback)
#          on `127.0.0.1:$LOCAL_PORT`.
# Parameters: $@ CLI flags (`--project`, `--region`, `--port`).
# Return Value: Runs in foreground until Ctrl+C.
# Exceptions/Errors: Calls `die` on invalid arguments, missing Portal deployment,
#                    or OIDC identity token generation failure.
#######################################
main() {
  local local_port="8080"

  while (($# > 0)); do
    case "$1" in
      --project)
        [[ $# -ge 2 && -n "$2" ]] || die "--project requires a non-empty value"
        export PROJECT_ID="$2"
        shift 2
        ;;
      --region)
        [[ $# -ge 2 && -n "$2" ]] || die "--region requires a non-empty value"
        export REGION="$2"
        shift 2
        ;;
      --port)
        [[ $# -ge 2 && "$2" =~ ^[0-9]+$ ]] || die "--port requires an integer (1..65535)"
        (( "$2" >= 1 && "$2" <= 65535 )) || die "--port must be between 1 and 65535"
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

  local portal_url
  portal_url="$(output_get PORTAL_URL)"
  if [[ -z "${portal_url}" ]]; then
    portal_url="$(gc run services describe "${PORTAL_SERVICE_NAME}" \
      --region="${REGION}" --format='value(status.url)' 2>/dev/null || true)"
  fi
  [[ -n "${portal_url}" ]] || die "Cloud Run service '${PORTAL_SERVICE_NAME}' not found in ${PROJECT_ID}/${REGION}."

  if [[ -n "${WEB_HOST:-}" ]]; then
    log_info "Cloud Shell Web Preview URL: https://${local_port}-${WEB_HOST}"
  else
    log_info "Local Browser URL: http://127.0.0.1:${local_port}"
  fi

  if has_cloud_run_proxy_binary; then
    exec gcloud run services proxy "${PORTAL_SERVICE_NAME}" \
      --project="${PROJECT_ID}" \
      --region="${REGION}" \
      --port="${local_port}" \
      --token="${id_token}"
  else
    log_warn "'cloud-run-proxy' binary not installed; using built-in Python OIDC reverse proxy."
    run_python_oidc_proxy "${portal_url}" "${local_port}"
  fi
}

main "$@"
