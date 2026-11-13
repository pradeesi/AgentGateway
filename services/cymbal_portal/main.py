"""
Purpose: FastAPI backend for the Cymbal Bank Disputes & Treasury Operations Portal,
         serving the institutional banking UI, live Firestore/local ledger views,
         synchronous Agent Runtime `:query` chat routing (via `CLIENT_TO_AGENT`
         Ingress Agent Gateway), two-phase LLM Token Management policy telemetry
         (`TokenAggregationPolicy`, `TokenBudgetPolicy`, `CostPerTokenPolicy`), and
         live Cloud Logging governance verdicts.
Architecture/Context: Deployed as an IAM-protected Cloud Run service (`cymbal-portal`)
         accessed via `./scripts/proxy_portal.sh`. Supports two agent invocation modes:
           - `AGENT_INVOCATION_MODE=agent_runtime`: Calls deployed Vertex AI
             Reasoning Engines (`TIER1_REASONING_ENGINE`, `SPECIALIST_REASONING_ENGINE`)
             over HTTPS `:streamQuery` / `:query` (governed inbound by `CLIENT_TO_AGENT`
             Model Armor and outbound by `AGENT_TO_ANYWHERE` IAP/UAP, Model Armor,
             Semantic Governance Policy Engine, and Token Management policies).
           - `AGENT_INVOCATION_MODE=local`: Uses ADK `InMemoryRunner` with
             `StreamingMode.NONE` for local smoke testing.
Dependencies/Side Effects: Reads `common.ledger_store`, `common.token_management`,
         `google-auth`, and optional `google-cloud-logging` for real-time gateway
         audit log retrieval.
Author: Pradeep Singh
License: Apache-2.0
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import google.auth
import google.auth.transport.requests
import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from common.ledger_store import LedgerStore, get_ledger_store
from common.logging_utils import configure_structured_logging, log_structured
from common.models import AuditEvent, TokenUsageSummary
from common.token_management import TokenManagementEngine, estimate_fallback_tokens

logger = configure_structured_logging("cymbal.portal")

STATIC_DIR = Path(__file__).resolve().parent / "static"


class ChatRequest(BaseModel):
    """
    Summary:
        Inbound operator chat turn submitted from the Cymbal Bank Operations Portal.

    Parameters:
        message (str): Operator prompt sent to the selected ADK agent (1..4000 chars).
        session_id (str | None): Optional conversation session token.
        token_group (str | None): Optional FinOps attribution tag (`"Burst-Sandbox"`,
            `"Treasury-Ops"`, or `"Default"`) mapped to `X-Token-Group` for
            `TokenAggregationPolicy` evaluation.

    Return Value:
        Validated `ChatRequest` model.

    Exceptions/Errors:
        pydantic.ValidationError: If `message` is empty or exceeds 4,000 characters.
    """

    model_config = ConfigDict(extra="forbid")

    message: str = Field(..., min_length=1, max_length=4000)
    session_id: str | None = Field(default=None, max_length=120)
    token_group: str | None = Field(default=None, max_length=64)


def _mask_portal_pii(account_dict: dict[str, Any]) -> dict[str, Any]:
    """
    Summary:
        Masks SSN and card PAN fields to last-4 format for standard display inside
        the Operations Portal table while leaving the underlying Firestore/local
        record intact so `cymbal-banking-mcp` still sends raw fields through Agent
        Gateway's Model Armor + SDP de-identification pipeline.

    Parameters:
        account_dict (dict[str, Any]): Serialized `CustomerAccount` dictionary.

    Return Value:
        dict[str, Any]: Copy with `ssn` and `card_pan` masked to last-4 digits.

    Exceptions/Errors:
        None.
    """
    masked = dict(account_dict)
    raw_ssn = str(masked.get("ssn", ""))
    raw_pan = str(masked.get("card_pan", ""))
    if len(raw_ssn) >= 4:
        masked["ssn"] = f"***-**-{raw_ssn[-4:]}"
    if len(raw_pan) >= 4:
        masked["card_pan"] = f"****-****-****-{raw_pan[-4:]}"
    return masked


def _mint_gcp_access_token() -> str:
    """
    Summary:
        Obtains a short-lived OAuth 2.0 Bearer access token via Application Default
        Credentials to call the Vertex AI Reasoning Engine REST API.

    Parameters:
        None.

    Return Value:
        str: Valid OAuth 2.0 access token.

    Exceptions/Errors:
        RuntimeError: If ADC credentials cannot be resolved or refreshed.
    """
    try:
        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        creds.refresh(google.auth.transport.requests.Request())
        if not creds.token:
            raise RuntimeError("ADC refresh produced an empty token.")
        return str(creds.token)
    except Exception as exc:
        raise RuntimeError(f"Unable to mint Google Cloud access token: {exc}") from exc


def _parse_reasoning_engine_payload(raw_text: str) -> dict[str, Any]:
    """
    Summary:
        Parses either a standard JSON dictionary or an SSE / NDJSON stream returned
        by Vertex AI `reasoningEngines:streamQuery?alt=sse`.

    Parameters:
        raw_text (str): Raw HTTP response body string.

    Return Value:
        dict[str, Any]: Parsed JSON dictionary containing `output` or merged text.

    Exceptions/Errors:
        ValueError: If no JSON object can be decoded from `raw_text`.
    """
    stripped = raw_text.strip()
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed
    except ValueError:
        pass

    frames: list[dict[str, Any]] = []
    for raw_line in stripped.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line or line == "[DONE]":
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                frames.append(obj)
        except ValueError:
            continue

    if not frames:
        raise ValueError("No valid JSON frames found in Reasoning Engine response.")
    for frame in reversed(frames):
        if "output" in frame:
            return frame
    return frames[-1]


async def _invoke_remote_reasoning_engine(
    engine_resource: str,
    region: str,
    message: str,
    session_id: str,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """
    Summary:
        Calls a deployed Vertex AI Agent Runtime (`ReasoningEngine`) via
        `:streamQuery?alt=sse` (routing inbound through `CLIENT_TO_AGENT` Agent
        Gateway + Model Armor, with automatic fallback to `/api/api/reasoning_engine`),
        while the agent's internal ADK runner uses `StreamingMode.NONE` so outbound
        Gemini calls hit `:generateContent` for Semantic Governance and Token
        Management evaluation.

    Parameters:
        engine_resource (str): Full resource name
            (`projects/<PROJECT>/locations/<REGION>/reasoningEngines/<ID>`).
        region (str): GCP region hosting the Reasoning Engine and Agent Gateway.
        message (str): Operator prompt text.
        session_id (str): Conversation session identifier.
        extra_headers (dict[str, str] | None): Optional attribution headers such as
            `X-Token-Group` forwarded with the request.

    Return Value:
        dict[str, Any]: Normalized response containing `reply`, `tool_trace`,
        `gateway_verdict`, `http_status`, `session_id`, and optional `token_usage`.

    Exceptions/Errors:
        RuntimeError: If token acquisition fails.
    """
    token = _mint_gcp_access_token()
    stream_url = (
        f"https://{region}-aiplatform.googleapis.com/v1/{engine_resource}:streamQuery?alt=sse"
    )
    passthrough_url = (
        f"https://{region}-aiplatform.googleapis.com/reasoningEngines/v1/"
        f"{engine_resource}/api/api/reasoning_engine"
    )

    headers: dict[str, str] = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    for hk, hv in (extra_headers or {}).items():
        if hk and hv:
            headers[hk] = hv

    stream_body = {
        "class_method": "async_stream_query",
        "input": {
            "message": message,
            "user_id": "cymbal-portal-operator",
            "session_id": session_id,
        },
    }
    sync_body = {
        "input": {
            "message": message,
            "user_id": "cymbal-portal-operator",
            "session_id": session_id,
        }
    }

    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.post(stream_url, headers=headers, json=stream_body)
        # Custom-container Reasoning Engines screen inbound prompts via `:streamQuery`
        # (enforcing `CLIENT_TO_AGENT` Model Armor HTTP 403 on violations) and execute
        # non-streaming turns via `/reasoningEngines/v1/.../api/api/reasoning_engine`.
        if resp.status_code in (400, 404, 405, 422, 501) and (
            "model armor" not in resp.text.lower() and "modelarmor" not in resp.text.lower()
        ):
            resp = await client.post(passthrough_url, headers=headers, json=sync_body)

    if resp.status_code >= 400:
        raw_err = resp.text.strip()[:500]
        lowered = raw_err.lower()
        if "model armor" in lowered or "modelarmor" in lowered:
            return {
                "reply": (
                    "Blocked by Client-to-Agent Ingress Gateway (Model Armor CONTENT_AUTHZ) "
                    "before reaching Vertex AI Agent Runtime.\n\n"
                    f"HTTP {resp.status_code} Verdict: {raw_err}"
                ),
                "tool_trace": [],
                "gateway_verdict": "BLOCKED_AT_INGRESS_MODEL_ARMOR",
                "http_status": resp.status_code,
                "session_id": session_id,
            }
        if resp.status_code == 429 or "too many requests" in lowered:
            return {
                "reply": (
                    "Rejected by Google Cloud Agent Gateway TokenBudgetPolicy "
                    "(HTTP 429 Too Many Requests): shared token pool quota is exhausted.\n\n"
                    f"HTTP 429 Gateway Verdict: {raw_err}"
                ),
                "tool_trace": [],
                "gateway_verdict": "TOKEN_BUDGET_EXCEEDED_429",
                "http_status": 429,
                "session_id": session_id,
            }
        if resp.status_code in (400, 403):
            return {
                "reply": (
                    "Request blocked by Google Cloud Agent Gateway before agent completion.\n\n"
                    f"HTTP {resp.status_code} Gateway Verdict: {raw_err}"
                ),
                "tool_trace": [],
                "gateway_verdict": "BLOCKED_AT_INGRESS_OR_POLICY",
                "http_status": resp.status_code,
                "session_id": session_id,
            }
        return {
            "reply": f"Agent Runtime returned HTTP {resp.status_code}: {raw_err}",
            "tool_trace": [],
            "gateway_verdict": "ERROR",
            "http_status": resp.status_code,
            "session_id": session_id,
        }

    data = _parse_reasoning_engine_payload(resp.text)
    output = data.get("output", data)
    raw_token_usage: dict[str, Any] | None = None
    if isinstance(output, dict):
        reply_text = (
            output.get("reply")
            or output.get("text")
            or output.get("response")
            or str(output)
        )
        tool_trace = output.get("tool_trace") or []
        if isinstance(output.get("token_usage"), dict):
            raw_token_usage = output["token_usage"]
    else:
        reply_text = str(output)
        tool_trace = []

    has_gateway_block = any(
        isinstance(ev.get("response"), dict)
        and ev["response"].get("status") == "blocked_by_agent_gateway"
        for ev in tool_trace
        if isinstance(ev, dict)
    )
    reply_lower = str(reply_text).lower()
    is_sgp_denial = (
        "denied due to a business policy" in reply_lower
        or "compliance policy denial" in reply_lower
    )
    if is_sgp_denial:
        verdict = "SEMANTIC_GOVERNANCE_DENIED"
    elif has_gateway_block:
        verdict = "EGRESS_POLICY_ENFORCED"
    else:
        verdict = "ALLOWED"

    result: dict[str, Any] = {
        "reply": str(reply_text),
        "tool_trace": tool_trace,
        "gateway_verdict": verdict,
        "http_status": 200,
        "session_id": session_id,
    }
    if raw_token_usage is not None:
        result["token_usage"] = raw_token_usage
    return result


async def _invoke_local_adk_agent(
    agent_role: Literal["tier1", "specialist"],
    message: str,
    session_id: str,
) -> dict[str, Any]:
    """
    Summary:
        Executes `tier1_support_agent` or `risk_specialist_agent` in-process via
        ADK `InMemoryRunner` (`StreamingMode.NONE`) when running locally, capturing
        any `usage_metadata` token counts emitted by Gemini.

    Parameters:
        agent_role (Literal["tier1", "specialist"]): Selected agent persona.
        message (str): Operator prompt text.
        session_id (str): Conversation session identifier.

    Return Value:
        dict[str, Any]: Normalized response dictionary.

    Exceptions/Errors:
        RuntimeError: Propagated if local Vertex AI / Gemini credentials are missing.
    """
    from google.adk.agents.run_config import RunConfig, StreamingMode
    from google.adk.runners import InMemoryRunner
    from google.genai import types as genai_types

    if agent_role == "tier1":
        from agents.tier1_support_agent.app.agent import root_agent as target_agent
    else:
        from agents.risk_specialist_agent.app.agent import root_agent as target_agent

    runner = InMemoryRunner(agent=target_agent, app_name=target_agent.name)
    await runner.session_service.create_session(
        app_name=target_agent.name,
        user_id="local-operator",
        session_id=session_id,
    )
    content = genai_types.Content(
        role="user",
        parts=[genai_types.Part.from_text(text=message)],
    )
    final_texts: list[str] = []
    tool_trace: list[dict[str, Any]] = []
    input_tokens = 0
    output_tokens = 0
    cached_tokens = 0
    total_tokens = 0
    llm_calls = 0

    async for event in runner.run_async(
        user_id="local-operator",
        session_id=session_id,
        new_message=content,
        run_config=RunConfig(streaming_mode=StreamingMode.NONE),
    ):
        usage_meta = getattr(event, "usage_metadata", None)
        if usage_meta is not None:
            p_tok = int(getattr(usage_meta, "prompt_token_count", 0) or 0)
            c_tok = int(getattr(usage_meta, "candidates_token_count", 0) or 0)
            cache_tok = int(getattr(usage_meta, "cached_content_token_count", 0) or 0)
            tot_tok = int(getattr(usage_meta, "total_token_count", 0) or 0)
            if p_tok > 0 or c_tok > 0 or tot_tok > 0:
                input_tokens += p_tok
                output_tokens += c_tok
                cached_tokens += cache_tok
                total_tokens += max(tot_tok, p_tok + c_tok + cache_tok)
                llm_calls += 1

        ev_content = getattr(event, "content", None)
        for part in getattr(ev_content, "parts", None) or []:
            fn_call = getattr(part, "function_call", None)
            if fn_call is not None:
                tool_trace.append(
                    {
                        "type": "tool_call",
                        "name": getattr(fn_call, "name", ""),
                        "args": dict(getattr(fn_call, "args", {}) or {}),
                    }
                )
            fn_resp = getattr(part, "function_response", None)
            if fn_resp is not None:
                tool_trace.append(
                    {
                        "type": "tool_response",
                        "name": getattr(fn_resp, "name", ""),
                        "response": dict(getattr(fn_resp, "response", {}) or {}),
                    }
                )
            txt = getattr(part, "text", None)
            if txt and getattr(event, "author", "") == target_agent.name:
                final_texts.append(txt)

    response_payload: dict[str, Any] = {
        "reply": "\n".join(t.strip() for t in final_texts if t.strip())
        or "Completed local agent turn.",
        "tool_trace": tool_trace,
        "gateway_verdict": "LOCAL_DEV_MODE",
        "http_status": 200,
        "session_id": session_id,
    }
    if llm_calls > 0:
        response_payload["token_usage"] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": cached_tokens,
            "total_tokens": total_tokens,
            "llm_calls": llm_calls,
            "model": str(getattr(target_agent, "model", "gemini-2.5-flash")),
        }
    return response_payload


def _fetch_cloud_logging_governance_events(project_id: str, limit: int = 35) -> list[AuditEvent]:
    """
    Summary:
        Queries Google Cloud Logging for recent governance verdicts across:
          - Semantic Governance Policy Engine (`logs/semantic-governance-policy`)
          - Model Armor + Cloud SDP (`logs/modelarmor.googleapis.com%2Fsanitize_operations`)
          - Token Management Alerts (`logs/token_management_alert_logs`)
          - Agent Gateway + IAP/IAM UAP (`resource.type="networkservices.googleapis.com/Gateway"`)

    Parameters:
        project_id (str): GCP Project ID hosting Agent Gateway.
        limit (int): Maximum entries to retrieve.

    Return Value:
        list[AuditEvent]: Normalized governance events ordered newest-first; returns
        an empty list gracefully if Cloud Logging is unreachable or lacks permissions.

    Exceptions/Errors:
        None; errors are logged at `WARNING` and return `[]`.
    """
    if not project_id or project_id == "your-gcp-project-id":
        return []

    try:
        from google.cloud import logging as cloud_logging

        client = cloud_logging.Client(project=project_id)
        cutoff = (datetime.now(UTC) - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
        log_filter = (
            f'timestamp >= "{cutoff}" AND ('
            f'logName="projects/{project_id}/logs/semantic-governance-policy" OR '
            f'logName="projects/{project_id}/logs/modelarmor.googleapis.com%2Fsanitize_operations" OR '
            f'logName="projects/{project_id}/logs/token_management_alert_logs" OR '
            f'resource.type="networkservices.googleapis.com/Gateway"'
            f")"
        )
        entries = client.list_entries(
            filter_=log_filter,
            order_by=cloud_logging.DESCENDING,
            max_results=limit,
        )
        results: list[AuditEvent] = []
        for entry in entries:
            ts = (
                entry.timestamp.isoformat()
                if getattr(entry, "timestamp", None)
                else datetime.now(UTC).isoformat()
            )
            payload = entry.payload if isinstance(entry.payload, dict) else {}
            log_name = str(getattr(entry, "log_name", ""))
            resource = getattr(entry, "resource", None)
            res_labels = getattr(resource, "labels", {}) or {}

            if "token_management_alert_logs" in log_name:
                alert_status = str(payload.get("status") or "STATUS_ALERT_THRESHOLD_REACHED")
                bp = payload.get("budget_policy") or {}
                pool_name = str(bp.get("pool_name") or "token-pool")
                pol_name = str(bp.get("name") or "token-budget-policy")
                max_tok = int(bp.get("configured_max_token_count") or 0)
                used_tok = int(bp.get("used_token_count") or 0)
                verdict_str: Literal["WARN", "DENY"] = (
                    "DENY" if alert_status == "STATUS_FULL_THRESHOLD_REACHED" else "WARN"
                )
                results.append(
                    AuditEvent(
                        timestamp=ts,
                        layer="TOKEN_MANAGEMENT",
                        verdict=verdict_str,
                        principal=pol_name.rsplit("/", 1)[-1],
                        target=pool_name,
                        summary=(
                            f"{alert_status}: Pool '{pool_name}' consumed "
                            f"{used_tok:,} / {max_tok:,} tokens."
                        ),
                    )
                )
            elif "semantic-governance-policy" in log_name:
                # Skip periodic engine pod heartbeat records that carry no evaluation verdict
                if "verdict" not in payload:
                    continue
                raw_verdict = str(payload.get("verdict", "INFO")).upper()
                agent_id = str(payload.get("agentIdentity") or "risk-specialist-agent")
                short_principal = agent_id.rsplit("/", 1)[-1] if "/" in agent_id else agent_id
                evals = payload.get("evaluations") or []
                first_eval = evals[0] if evals and isinstance(evals[0], dict) else {}
                tool_name = str(first_eval.get("toolName") or "model_tool_call")
                rationale = str(
                    first_eval.get("rationale")
                    or payload.get("userFacingDenialMessage")
                    or "Semantic Governance evaluation logged."
                )
                results.append(
                    AuditEvent(
                        timestamp=ts,
                        layer="SEMANTIC_POLICY",
                        verdict="ALLOW" if raw_verdict == "ALLOW" else "DENY",
                        principal=short_principal,
                        target=tool_name,
                        summary=rationale,
                    )
                )
            elif "modelarmor.googleapis.com" in log_name:
                op_type = str(payload.get("operationType") or "SanitizeModelResponse")
                san_res = payload.get("sanitizationResult") or {}
                filter_res = san_res.get("filterResults") or {}
                pi_state = (
                    filter_res.get("pi_and_jailbreak", {})
                    .get("piAndJailbreakFilterResult", {})
                    .get("matchState", "")
                )
                uri_state = (
                    filter_res.get("malicious_uris", {})
                    .get("maliciousUriFilterResult", {})
                    .get("matchState", "")
                )
                deid_res = (
                    filter_res.get("sdp", {}).get("sdpFilterResult", {}).get("deidentifyResult", {})
                )
                deid_state = str(deid_res.get("matchState") or "")
                san_verdict = str(san_res.get("sanitizationVerdict") or "")

                if (
                    pi_state == "MATCH_FOUND"
                    or uri_state == "MATCH_FOUND"
                    or san_verdict == "BLOCK"
                ):
                    reason = str(
                        san_res.get("sanitizationVerdictReason")
                        or "Prompt injection / content safety violation blocked."
                    )
                    results.append(
                        AuditEvent(
                            timestamp=ts,
                            layer="MODEL_ARMOR_GUARD",
                            verdict="DENY",
                            principal="model-armor-gateway",
                            target=op_type,
                            summary=f"Model Armor blocked {op_type}: {reason}",
                        )
                    )
                elif deid_state == "MATCH_FOUND":
                    info_types = deid_res.get("infoTypes") or []
                    transformed_bytes = deid_res.get("transformedBytes", 0)
                    joined_types = ", ".join(str(t) for t in info_types) or "SENSITIVE_DATA"
                    results.append(
                        AuditEvent(
                            timestamp=ts,
                            layer="MODEL_ARMOR_SDP",
                            verdict="MASKED",
                            principal="cloud-sdp-deidentify",
                            target=op_type,
                            summary=(
                                f"Cloud SDP de-identified {transformed_bytes} bytes across "
                                f"[{joined_types}]."
                            ),
                        )
                    )
                else:
                    results.append(
                        AuditEvent(
                            timestamp=ts,
                            layer="MODEL_ARMOR_SDP",
                            verdict="ALLOW",
                            principal="model-armor-gateway",
                            target=op_type,
                            summary=f"Model Armor {op_type} passed with zero policy violations.",
                        )
                    )
            else:
                http_req = getattr(entry, "http_request", None) or {}
                status_code = int(http_req.get("status", 200) or 200)
                req_url = str(http_req.get("requestUrl") or "gateway-route")
                gw_name = str(res_labels.get("gateway_name") or "agent-gateway")
                authz_info = payload.get("authzPolicyInfo") or {}
                policies = authz_info.get("policies") or []
                policy_names = ", ".join(
                    str(p.get("name", "")).rsplit("/", 1)[-1]
                    for p in policies
                    if isinstance(p, dict) and p.get("name")
                )
                if status_code == 429:
                    layer_name = "TOKEN_MANAGEMENT"
                elif "iap-policy" in policy_names:
                    layer_name = "EGRESS_IAP_UAP"
                elif "ingress" in gw_name:
                    layer_name = "INGRESS_GATEWAY"
                else:
                    layer_name = "AGENT_GATEWAY"
                verdict_val: Literal["ALLOW", "DENY"] = "ALLOW" if status_code < 400 else "DENY"
                policy_suffix = f" via [{policy_names}]" if policy_names else ""
                results.append(
                    AuditEvent(
                        timestamp=ts,
                        layer=layer_name,
                        verdict=verdict_val,
                        principal=gw_name,
                        target=req_url,
                        summary=f"Gateway proxy HTTP {status_code}{policy_suffix}.",
                    )
                )
        return results
    except Exception as exc:
        log_structured(
            logger,
            logging.WARNING,
            "cloud_logging_query_skipped",
            project_id=project_id,
            reason=str(exc),
        )
        return []


def create_portal_app(store_provider: Callable[[], LedgerStore] = get_ledger_store) -> FastAPI:
    """
    Summary:
        Creates the FastAPI application serving the Cymbal Bank Operations Portal
        UI (`/`), static assets (`/static`), ledger endpoints, Token Management
        telemetry (`/api/token-management/overview`), agent chat proxy, and Cloud
        Logging governance feed.

    Parameters:
        store_provider (Callable[[], LedgerStore]): Dependency-injectable ledger
            factory.

    Return Value:
        FastAPI: Configured portal ASGI application.

    Exceptions/Errors:
        None.
    """
    portal = FastAPI(
        title="Cymbal Bank Disputes & Treasury Operations Portal",
        version="1.1.0",
        docs_url=None,
        redoc_url=None,
    )
    if STATIC_DIR.exists():
        portal.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @portal.get("/healthz")
    @portal.get("/api/health")
    async def healthz() -> dict[str, str]:
        """Container readiness/liveness endpoint (`/api/health` bypasses Cloud Run GFE reservation)."""
        store = store_provider()
        return {
            "status": "ok",
            "service": "cymbal-portal",
            "storage_backend": store.backend_name,
            "agent_mode": os.getenv("AGENT_INVOCATION_MODE", "local"),
        }

    @portal.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        """Serves the institutional Cymbal Bank Operations Portal HTML page."""
        return FileResponse(STATIC_DIR / "index.html")

    @portal.get("/api/metadata")
    async def metadata() -> dict[str, Any]:
        """Returns active environment, gateway resource names, Token Management policies, and SPIFFE labels."""
        store = store_provider()
        tm_engine = TokenManagementEngine(store)
        return {
            "project_id": os.getenv("PROJECT_ID", "local-dev"),
            "region": os.getenv("REGION", "us-central1"),
            "storage_backend": store.backend_name,
            "agent_invocation_mode": os.getenv("AGENT_INVOCATION_MODE", "local"),
            "ingress_gateway": os.getenv("INGRESS_GATEWAY_NAME", "cbgw-ingress-gw"),
            "egress_gateway": os.getenv("EGRESS_GATEWAY_NAME", "cbgw-egress-gw"),
            "iap_policy_mode": os.getenv("IAP_POLICY_MODE", "IAM_UAP_V2"),
            "token_management": {
                "policy_mode": tm_engine.policy_mode,
                "aggregation_policy_id": tm_engine.aggregation_policy_id,
                "budget_policy_id": tm_engine.budget_policy_id,
                "cost_policy_id": tm_engine.cost_policy_id,
            },
            "agents": {
                "tier1": {
                    "display_name": "Tier-1 Support Agent",
                    "tier_badge": "Read-Only MCP Tier (SPIFFE Identity #1)",
                    "spiffe_id": os.getenv(
                        "TIER1_SPIFFE_ID",
                        "spiffe://agents.global.system.id.goog/.../tier1-support-agent",
                    ),
                    "reasoning_engine": os.getenv("TIER1_REASONING_ENGINE", "local"),
                },
                "specialist": {
                    "display_name": "Risk & Disputes Specialist Agent",
                    "tier_badge": "Privileged Tier + Semantic $500 Guardrail (SPIFFE Identity #2)",
                    "spiffe_id": os.getenv(
                        "SPECIALIST_SPIFFE_ID",
                        "spiffe://agents.global.system.id.goog/.../risk-specialist-agent",
                    ),
                    "reasoning_engine": os.getenv("SPECIALIST_REASONING_ENGINE", "local"),
                },
            },
        }

    @portal.get("/api/ledger/overview")
    async def ledger_overview() -> dict[str, Any]:
        """Returns masked Customer 360 accounts, transactions, and disputes."""
        store = store_provider()
        overview = store.get_overview()
        raw = overview.model_dump()
        raw["accounts"] = [_mask_portal_pii(a) for a in raw["accounts"]]
        return raw

    @portal.get("/api/token-management/overview")
    async def token_management_overview() -> dict[str, Any]:
        """Returns active TokenAggregationPolicy, TokenBudgetPolicy pools, and CostPerTokenPolicy FinOps state."""
        store = store_provider()
        tm_engine = TokenManagementEngine(store)
        return tm_engine.get_overview().model_dump()

    @portal.post("/api/admin/reset")
    async def admin_reset() -> dict[str, Any]:
        """Resets Cymbal Bank accounts, transactions, disputes, and token budget pools to baseline."""
        store = store_provider()
        counts = store.reset_to_seed()
        return {"status": "reset_complete", "counts": counts}

    @portal.get("/api/governance/events")
    async def governance_events() -> dict[str, Any]:
        """Combines Cloud Logging Agent Gateway verdicts with ledger and Token Management audit events."""
        store = store_provider()
        overview = store.get_overview()
        project_id = os.getenv("PROJECT_ID", "")
        cloud_events = _fetch_cloud_logging_governance_events(project_id=project_id)
        merged = [e.model_dump() for e in cloud_events] + [
            e.model_dump() for e in overview.ledger_events
        ]
        merged.sort(key=lambda item: str(item.get("timestamp", "")), reverse=True)
        return {"count": len(merged[:40]), "events": merged[:40]}

    @portal.post("/api/agents/{agent_role}/chat")
    async def chat_with_agent(
        agent_role: Literal["tier1", "specialist"],
        request: ChatRequest,
        http_request: Request,
        http_response: Response,
    ) -> dict[str, Any]:
        """
        Summary:
            Evaluates Phase 1 (`TokenAggregationPolicy` + `TokenBudgetPolicy` pre-flight
            quota check), routes admitted prompts to `tier1` or `specialist` via Agent
            Runtime (through `CLIENT_TO_AGENT` Ingress Gateway) or local ADK Runner,
            and executes Phase 2 (`usage_metadata` token reconciliation, `alertThreshold`
            warnings, and `CostPerTokenPolicy` USD attribution).

        Parameters:
            agent_role (Literal["tier1", "specialist"]): Selected agent persona.
            request (ChatRequest): Validated operator message body.
            http_request (Request): Inbound HTTP request (inspected for `X-Token-Group`).
            http_response (Response): Outbound HTTP response (carries telemetry headers).

        Return Value:
            dict[str, Any]: Reply text, tool execution trace, `token_telemetry`,
            latency, and gateway verdict.

        Exceptions/Errors:
            HTTPException: HTTP 500/502 if agent invocation fails unexpectedly.
        """
        started = time.perf_counter()
        session_id = request.session_id or f"portal-{agent_role}-{uuid.uuid4().hex[:8]}"
        mode = os.getenv("AGENT_INVOCATION_MODE", "local").strip().lower()
        region = os.getenv("REGION", "us-central1").strip()

        store = store_provider()
        tm_engine = TokenManagementEngine(store)

        agent_spiffe = (
            os.getenv("TIER1_SPIFFE_ID", "tier1-support-agent").strip()
            if agent_role == "tier1"
            else os.getenv("SPECIALIST_SPIFFE_ID", "risk-specialist-agent").strip()
        ) or (
            "tier1-support-agent" if agent_role == "tier1" else "risk-specialist-agent"
        )

        # Honor explicit HTTP header `X-Token-Group` first, then optional JSON `token_group`.
        header_group = (
            http_request.headers.get("x-token-group")
            or request.token_group
            or ""
        ).strip()
        eval_headers: dict[str, str] = {}
        if header_group:
            eval_headers["X-Token-Group"] = header_group

        # Phase 1: Pre-flight TokenBudgetPolicy quota check on request path.
        preflight = tm_engine.check_preflight_budget(
            agent_id=agent_spiffe,
            headers=eval_headers,
        )
        http_response.headers["X-Token-Group"] = preflight.token_group
        http_response.headers["X-Token-Pool"] = preflight.pool_id

        if not preflight.pre_flight_allowed:
            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            http_response.headers["X-Token-Management-Status"] = "429"
            return {
                "reply": (
                    "Rejected by Agent Gateway Token Management Policy "
                    "(HTTP 429 Too Many Requests).\n\n"
                    f"Token Group '{preflight.token_group}' mapped to strict pool "
                    f"'{preflight.pool_id}', which has exhausted its configured budget "
                    f"({preflight.pool_used_tokens:,} / {preflight.pool_max_tokens:,} tokens). "
                    "Reset the demo ledger or wait for the next DAILY Pacific Time reset window."
                ),
                "tool_trace": [],
                "gateway_verdict": "TOKEN_BUDGET_EXCEEDED_429",
                "http_status": 429,
                "session_id": session_id,
                "agent_role": agent_role,
                "latency_ms": elapsed_ms,
                "token_telemetry": preflight.model_dump(),
            }

        try:
            if mode == "agent_runtime":
                engine_env = (
                    "TIER1_REASONING_ENGINE"
                    if agent_role == "tier1"
                    else "SPECIALIST_REASONING_ENGINE"
                )
                engine_res = os.getenv(engine_env, "").strip()
                if not engine_res:
                    raise HTTPException(
                        status_code=500,
                        detail=f"{engine_env} is not configured on cymbal-portal.",
                    )
                result = await _invoke_remote_reasoning_engine(
                    engine_resource=engine_res,
                    region=region,
                    message=request.message.strip(),
                    session_id=session_id,
                    extra_headers=eval_headers,
                )
            else:
                result = await _invoke_local_adk_agent(
                    agent_role=agent_role,
                    message=request.message.strip(),
                    session_id=session_id,
                )
        except HTTPException:
            raise
        except Exception as exc:
            log_structured(
                logger,
                logging.ERROR,
                "portal_agent_chat_failed",
                agent_role=agent_role,
                mode=mode,
                error=str(exc),
            )
            raise HTTPException(
                status_code=502,
                detail=f"Failed to invoke '{agent_role}' agent ({mode}): {exc}",
            ) from exc

        # Phase 2: Post-flight token reconciliation & CostPerTokenPolicy attribution.
        # Note: Requests rejected pre-LLM at Ingress Model Armor (`http_status >= 400`)
        # never reach Gemini and therefore consume 0 model tokens.
        raw_usage = result.pop("token_usage", None)
        if int(result.get("http_status", 200)) >= 400:
            usage_summary = TokenUsageSummary(
                input_tokens=0,
                output_tokens=0,
                cached_tokens=0,
                total_tokens=0,
                llm_calls=0,
            )
        elif isinstance(raw_usage, dict):
            usage_summary = TokenUsageSummary(
                input_tokens=int(raw_usage.get("input_tokens", 0) or 0),
                output_tokens=int(raw_usage.get("output_tokens", 0) or 0),
                cached_tokens=int(raw_usage.get("cached_tokens", 0) or 0),
                total_tokens=int(raw_usage.get("total_tokens", 0) or 0),
                llm_calls=int(raw_usage.get("llm_calls", 1) or 1),
                model=str(
                    raw_usage.get("model")
                    or os.getenv("MODEL_NAME", "gemini-2.5-flash")
                ),
            )
        else:
            usage_summary = estimate_fallback_tokens(
                prompt_text=request.message,
                reply_text=str(result.get("reply", "")),
                tool_events=len(result.get("tool_trace") or []),
            )

        reconciled = tm_engine.reconcile_postflight_usage(
            agent_id=agent_spiffe,
            usage=usage_summary,
            headers=eval_headers,
        )
        http_response.headers["X-Token-Management-Status"] = (
            reconciled.alert_triggered or "OK"
        )

        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        result["agent_role"] = agent_role
        result["latency_ms"] = elapsed_ms
        result["token_telemetry"] = reconciled.model_dump()
        return result

    return portal


app = create_portal_app()


def main() -> None:
    """
    Summary:
        CLI entry point starting the Cymbal Bank Operations Portal on `$PORT`
        (default `8080`).

    Parameters:
        None.

    Return Value:
        None.

    Exceptions/Errors:
        ValueError: If `PORT` is not a valid integer.
    """
    import uvicorn

    port = int(os.getenv("PORT", os.getenv("PORTAL_PORT", "8080")))
    uvicorn.run(
        "services.cymbal_portal.main:app",
        host="0.0.0.0",
        port=port,
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
    )


if __name__ == "__main__":
    main()
