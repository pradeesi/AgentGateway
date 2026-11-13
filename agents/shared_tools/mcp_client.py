"""
Purpose: Defensive HTTP/JSON-RPC 2.0 MCP client and Agent Gateway verdict classifier
         used by Cymbal Bank ADK agents to invoke `cymbal-banking-mcp` and test
         default-deny egress against `cymbal-rogue-ext`.
Architecture/Context: Outbound calls from Agent Runtime pass through Google-managed
         `AGENT_TO_ANYWHERE` Agent Gateway:
           1. When `MCP_AUTH_MODE=cloud_run_iam`, fetches an unbound OIDC identity
              token from the GCP metadata server (`.../identity?audience=<origin>`)
              so Cloud Run's `roles/run.invoker` IAM check authenticates the agent's
              SPIFFE workload identity cleanly across TLS inspection.
           2. Classifies HTTP `403` / `498` / `400` / `451` gateway rejections into
              structured security telemetry (`blocked_by_agent_gateway`) distinguishing
              IAP/IAM UAP denials, Agent Registry unregistered-egress blocks, and
              Model Armor content security blocks.
Dependencies/Side Effects: Uses `httpx` with bounded timeouts and never logs bearer
         tokens.
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

logger = logging.getLogger("cymbal.agent.mcp_client")

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
        Extracts `<scheme>://<netloc>` from a target URL for Cloud Run OIDC audience
        binding.

    Parameters:
        url (str): Full HTTP/HTTPS endpoint URL.

    Return Value:
        str: Scheme and host origin (e.g., `"https://cymbal-banking-mcp-xyz.a.run.app"`).

    Exceptions/Errors:
        ValueError: If `url` lacks an `http` or `https` scheme or hostname.
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
        Translates non-2xx HTTP responses emitted by Google Cloud Agent Gateway
        (IAP / IAM UAP `REQUEST_AUTHZ`, Agent Registry default-deny, or Model Armor
        `CONTENT_AUTHZ`) into a deterministic dictionary for the LLM and UI.

    Parameters:
        http_status (int): HTTP status code returned by the gateway proxy.
        response_body (str): Raw text snippet of the rejection response.
        response_headers (dict[str, str]): Lowercased response headers.
        target_description (str): Tool name or destination URL attempted.
        is_unregistered_target (bool): True when calling an endpoint intentionally
            omitted from Agent Registry to test default-deny egress.

    Return Value:
        dict[str, Any]: Structured block report containing `status`, `http_status`,
        `policy_layer`, and `detail`.

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
    elif http_status == 429:
        layer = "EGRESS_TOKEN_MANAGEMENT_POLICY"
        reason = (
            f"Agent Gateway TokenBudgetPolicy rejected '{target_description}' "
            f"(HTTP 429 Too Many Requests): shared token pool maxTokens quota is exhausted."
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
        Stateless HTTP JSON-RPC 2.0 client invoking `cymbal-banking-mcp` tools and
        the unregistered `cymbal-rogue-ext` webhook through Google Cloud Agent Gateway.

    Parameters:
        agent_role (str): Logical caller name (`"tier1-support-agent"` or
            `"risk-specialist-agent"`).
        mcp_base_url (str | None): Base URL of `cymbal-banking-mcp`.
        rogue_webhook_url (str | None): Full URL of `cymbal-rogue-ext` `/collect`.
        auth_mode (str | None): `"none"` (local) or `"cloud_run_iam"` (GCP).
        routing_mode (str | None): `"unified"` (`/mcp`, default for IAM UAP V2) or
            `"split"` (`/mcp/readonly` + `/mcp/privileged`, used when `install.sh`
            activates the IAP V1 org-policy fallback).
        timeout_seconds (float | None): Per-request timeout in seconds.
        transport (httpx.BaseTransport | None): Optional mock/ASGI transport for tests.

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
        """
        Summary:
            Selects `/mcp` in unified IAM UAP V2 mode, or `/mcp/readonly` vs
            `/mcp/privileged` when running in IAP V1 split-mount fallback mode.

        Parameters:
            tool_name (str): Target MCP tool name.

        Return Value:
            str: Full HTTP POST URL for the JSON-RPC `tools/call` request.

        Exceptions/Errors:
            None.
        """
        if self.routing_mode == "split":
            subpath = "/mcp/readonly" if tool_name in READONLY_TOOLS else "/mcp/privileged"
            return f"{self.mcp_base_url}{subpath}"
        unified_path = os.getenv("MCP_UNIFIED_PATH", "/mcp").strip() or "/mcp"
        if not unified_path.startswith("/"):
            unified_path = f"/{unified_path}"
        return f"{self.mcp_base_url}{unified_path}"

    def _fetch_cloud_run_id_token(self, target_url: str) -> str | None:
        """
        Summary:
            Mints a short-lived unbound OIDC identity token from the GCP metadata
            server scoped to `target_url`'s origin, caching for 4 minutes.

        Parameters:
            target_url (str): Destination Cloud Run HTTPS URL.

        Return Value:
            str | None: Raw JWT string when running in GCP `cloud_run_iam` mode,
            or `None` when `MCP_AUTH_MODE=none`.

        Exceptions/Errors:
            RuntimeError: Raised if `cloud_run_iam` is enabled and metadata token
            retrieval fails.
        """
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
        """
        Summary:
            Invokes `tools/call` on `cymbal-banking-mcp` and returns either the
            parsed tool dictionary (including any Model Armor SDP redactions applied
            in transit by Agent Gateway) or a structured `blocked_by_agent_gateway`
            verdict dictionary.

        Parameters:
            tool_name (str): Registered Cymbal Banking MCP tool name.
            arguments (dict[str, Any]): JSON-serializable tool input parameters.

        Return Value:
            dict[str, Any]: Parsed MCP result or structured gateway block report.

        Exceptions/Errors:
            ValueError: If `tool_name` is empty.
        """
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
            logger.error("OIDC token acquisition failed for %s: %s", tool_name, auth_err)
            return {
                "status": "auth_error",
                "tool": tool_name,
                "detail": str(auth_err),
            }

        rpc_body = {
            "jsonrpc": "2.0",
            "id": f"{self.agent_role}-{uuid.uuid4().hex[:10]}",
            "method": "tools/call",
            "params": {"name": tool_name.strip(), "arguments": arguments},
        }

        try:
            with httpx.Client(
                timeout=self.timeout_seconds,
                transport=self._transport,
            ) as client:
                response = client.post(endpoint, headers=headers, json=rpc_body)
        except httpx.HTTPError as net_err:
            logger.warning(
                "Network/gateway transport rejection calling %s at %s: %s",
                tool_name,
                endpoint,
                net_err,
            )
            return {
                "status": "blocked_by_agent_gateway",
                "policy_layer": "AGENT_GATEWAY_TRANSPORT",
                "target": f"mcp:{tool_name}",
                "detail": (
                    f"Connection to '{tool_name}' terminated by Agent Gateway or network "
                    f"perimeter: {type(net_err).__name__}: {str(net_err)[:220]}"
                ),
            }

        if response.status_code >= 400:
            verdict = classify_gateway_block(
                http_status=response.status_code,
                response_body=response.text,
                response_headers=dict(response.headers),
                target_description=f"mcp:{tool_name}",
            )
            logger.warning(
                "Agent Gateway blocked MCP tool %s for %s: %s",
                tool_name,
                self.agent_role,
                verdict,
            )
            return verdict

        try:
            envelope = response.json()
        except ValueError:
            # If Model Armor replaced the body with a plain-text block notice on HTTP 200.
            return classify_gateway_block(
                http_status=response.status_code,
                response_body=response.text,
                response_headers=dict(response.headers),
                target_description=f"mcp:{tool_name}",
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
                # Preserves de-identified text even if SDP token replacement altered quotes.
                return {"status": "success", "sanitized_text": raw_text}

        if "structuredContent" in result and isinstance(result["structuredContent"], dict):
            return result["structuredContent"]

        return {"status": "success", "result": result}

    def send_rogue_webhook(self, destination_note: str, summary: str) -> dict[str, Any]:
        """
        Summary:
            Attempts an outbound `POST` to `ROGUE_WEBHOOK_URL` (`cymbal-rogue-ext`),
            which is intentionally unregistered in Agent Registry so `AGENT_TO_ANYWHERE`
            Agent Gateway blocks the request with default-deny enforcement.

        Parameters:
            destination_note (str): Short label describing the external webhook target.
            summary (str): Payload string the caller attempted to transmit.

        Return Value:
            dict[str, Any]: `blocked_by_agent_gateway` verdict when governed by Agent
            Gateway, or `received_ungoverned` warning in unproxied local mode.

        Exceptions/Errors:
            ValueError: If `destination_note` or `summary` is empty.
        """
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
            # Even without an ID token, Agent Gateway blocks the unregistered host
            # before forwarding; continue so the gateway block is exercised cleanly.
            pass

        body = {
            "destination_note": destination_note.strip()[:240],
            "summary": summary.strip()[:2000],
            "agent_role": self.agent_role,
        }

        try:
            with httpx.Client(
                timeout=self.timeout_seconds,
                transport=self._transport,
            ) as client:
                response = client.post(self.rogue_webhook_url, headers=headers, json=body)
        except httpx.HTTPError as net_err:
            return {
                "status": "blocked_by_agent_gateway",
                "policy_layer": "AGENT_REGISTRY_DEFAULT_DENY",
                "target": self.rogue_webhook_url,
                "detail": (
                    f"Agent Gateway blocked outbound request to unregistered endpoint "
                    f"'{self.rogue_webhook_url}': {type(net_err).__name__}: {str(net_err)[:200]}"
                ),
            }

        if response.status_code >= 400:
            return classify_gateway_block(
                http_status=response.status_code,
                response_body=response.text,
                response_headers=dict(response.headers),
                target_description=self.rogue_webhook_url,
                is_unregistered_target=True,
            )

        try:
            return response.json()
        except ValueError:
            return {"status": "received_ungoverned", "raw": response.text[:240]}
