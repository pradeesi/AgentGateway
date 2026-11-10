"""
Purpose: FastAPI HTTP entrypoint for the Tier-1 Support ADK Agent supporting
         Vertex AI Agent Runtime (`:query` / `/api/reasoning_engine`), Cloud Run,
         and GKE deployments with non-streaming model calls (`StreamingMode.NONE`).
Architecture/Context: Enforces `StreamingMode.NONE` on `RunConfig` so outbound
         Vertex AI calls hit `:generateContent` rather than `:streamGenerateContent`,
         ensuring Google Cloud's Semantic Governance Policy Engine evaluates every
         tool invocation decision on `AGENT_TO_ANYWHERE` Agent Gateway.
Dependencies/Side Effects: Uses `google.adk.runners.InMemoryRunner` and `FastAPI`.
Author: Pradeep Singh
License: Apache-2.0
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.runners import InMemoryRunner
from google.genai import types as genai_types
from pydantic import BaseModel, ConfigDict, Field

from .agent import root_agent


class QueryInput(BaseModel):
    """
    Summary:
        Accepts both Vertex AI Reasoning Engine `:query` (`{"input": {"message": ...}}`)
        and direct REST (`{"message": ..., "user_id": ..., "session_id": ...}`) payloads.

    Parameters:
        input (dict[str, Any] | None): Nested Reasoning Engine input envelope.
        message (str | None): Direct user prompt text.
        user_id (str): Logical operator/caller ID.
        session_id (str | None): Optional conversation session identifier.

    Return Value:
        Validated `QueryInput` payload.

    Exceptions/Errors:
        pydantic.ValidationError: Raised when field types are invalid.
    """

    model_config = ConfigDict(extra="allow")

    input: dict[str, Any] | None = None
    message: str | None = None
    user_id: str = Field(default="cymbal-operator", max_length=128)
    session_id: str | None = None


def create_agent_http_app() -> FastAPI:
    """
    Summary:
        Constructs the FastAPI ASGI service hosting `tier1_support_agent` with
        `/healthz`, `/query`, and `/api/reasoning_engine` endpoints.

    Parameters:
        None.

    Return Value:
        FastAPI: Configured ASGI application.

    Exceptions/Errors:
        None.
    """
    api = FastAPI(
        title="Cymbal Bank Tier-1 Support Agent",
        version="1.0.0",
    )
    runner = InMemoryRunner(agent=root_agent, app_name=root_agent.name)

    @api.get("/")
    @api.get("/health")
    @api.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Container liveness/readiness endpoint."""
        return {"status": "ok", "agent": root_agent.name}

    async def _execute_non_streaming_turn(payload: QueryInput) -> dict[str, Any]:
        """
        Summary:
            Runs one synchronous ADK turn with `StreamingMode.NONE` and extracts
            both the final assistant response and structured tool call telemetry.

        Parameters:
            payload (QueryInput): Parsed request body.

        Return Value:
            dict[str, Any]: Output text, invoked tool trace, and session metadata.

        Exceptions/Errors:
            HTTPException: HTTP 400 if prompt message is empty.
        """
        nested = payload.input or {}
        prompt = (
            payload.message
            or nested.get("message")
            or nested.get("prompt")
            or nested.get("query")
            or ""
        )
        if not isinstance(prompt, str) or not prompt.strip():
            raise HTTPException(status_code=400, detail="Non-empty 'message' is required.")

        user_id = str(nested.get("user_id") or payload.user_id or "cymbal-operator")[:128]
        session_id = str(
            nested.get("session_id") or payload.session_id or f"sess-{uuid.uuid4().hex[:10]}"
        )

        existing = await runner.session_service.get_session(
            app_name=root_agent.name,
            user_id=user_id,
            session_id=session_id,
        )
        if existing is None:
            await runner.session_service.create_session(
                app_name=root_agent.name,
                user_id=user_id,
                session_id=session_id,
            )

        content = genai_types.Content(
            role="user",
            parts=[genai_types.Part.from_text(text=prompt.strip())],
        )
        run_cfg = RunConfig(streaming_mode=StreamingMode.NONE)

        final_texts: list[str] = []
        tool_events: list[dict[str, Any]] = []

        async for event in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=content,
            run_config=run_cfg,
        ):
            ev_content = getattr(event, "content", None)
            parts = getattr(ev_content, "parts", None) or []
            for part in parts:
                fn_call = getattr(part, "function_call", None)
                if fn_call is not None:
                    tool_events.append(
                        {
                            "type": "tool_call",
                            "name": getattr(fn_call, "name", ""),
                            "args": dict(getattr(fn_call, "args", {}) or {}),
                        }
                    )
                fn_resp = getattr(part, "function_response", None)
                if fn_resp is not None:
                    tool_events.append(
                        {
                            "type": "tool_response",
                            "name": getattr(fn_resp, "name", ""),
                            "response": dict(getattr(fn_resp, "response", {}) or {}),
                        }
                    )
                text_val = getattr(part, "text", None)
                if text_val and getattr(event, "author", "") == root_agent.name:
                    final_texts.append(text_val)

        reply_text = "\n".join(t.strip() for t in final_texts if t.strip()).strip()
        return {
            "output": {
                "agent": root_agent.name,
                "session_id": session_id,
                "reply": reply_text or "Request completed with no textual output.",
                "tool_trace": tool_events,
            }
        }

    @api.post("/query")
    async def query_direct(payload: QueryInput) -> dict[str, Any]:
        """Direct synchronous query endpoint for Cloud Run / GKE / local callers."""
        return await _execute_non_streaming_turn(payload)

    @api.post("/api/reasoning_engine")
    async def query_reasoning_engine(payload: QueryInput) -> dict[str, Any]:
        """Vertex AI Agent Runtime `:query` contract endpoint."""
        return await _execute_non_streaming_turn(payload)

    @api.post("/api/stream_reasoning_engine")
    async def stream_reasoning_engine(raw_body: dict[str, Any]) -> StreamingResponse:
        """
        Summary:
            Vertex AI Agent Runtime `:streamQuery` contract endpoint enabling inline
            `CLIENT_TO_AGENT` Model Armor prompt & response screening on `cbgw-ingress-gw`
            while still executing outbound Gemini calls with `StreamingMode.NONE` so
            the Semantic Governance Policy Engine evaluates `:generateContent`.

        Parameters:
            raw_body (dict[str, Any]): Arbitrary Reasoning Engine `:streamQuery` JSON envelope.

        Return Value:
            StreamingResponse: Single-frame NDJSON event containing `output`.

        Exceptions/Errors:
            HTTPException: HTTP 400 if prompt message is empty.
        """
        raw_input = raw_body.get("input")
        if isinstance(raw_input, str):
            try:
                raw_input = json.loads(raw_input)
            except ValueError:
                raw_input = {"message": raw_input}
        if not isinstance(raw_input, dict):
            raw_input = {}
        normalized = QueryInput(
            input=raw_input,
            message=raw_body.get("message") if isinstance(raw_body.get("message"), str) else None,
            user_id=str(raw_body.get("user_id") or raw_input.get("user_id") or "cymbal-operator"),
            session_id=raw_body.get("session_id") or raw_input.get("session_id"),
        )
        result = await _execute_non_streaming_turn(normalized)

        async def _ndjson_frame():
            yield json.dumps(result) + "\n"

        return StreamingResponse(content=_ndjson_frame(), media_type="application/json")

    return api


app = create_agent_http_app()
