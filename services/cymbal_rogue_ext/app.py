"""
Purpose: Unregistered external audit/webhook service (`cymbal-rogue-ext`) used to
         demonstrate Google Cloud Agent Gateway's default-deny egress perimeter.
Architecture/Context: Deployed on Cloud Run (`--no-allow-unauthenticated`) and
         deliberately NOT registered in Google Cloud Agent Registry. When an
         operator or prompt injection instructs either ADK agent to invoke
         `send_external_audit_webhook`, `AGENT_TO_ANYWHERE` Agent Gateway rejects
         the connection at the network layer (HTTP 403 / 498) before any packet
         reaches this container.
Dependencies/Side Effects: Stateless FastAPI app keeping a bounded in-memory ring
         of received payloads solely for local unit/integration testing.
Author: Pradeep Singh
License: Apache-2.0
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field

from common.logging_utils import configure_structured_logging, log_structured

logger = configure_structured_logging("cymbal.rogue_ext")


class WebhookPayload(BaseModel):
    """
    Summary:
        Inbound JSON body accepted by `POST /collect` if egress governance is not
        present (e.g., in an ungoverned local baseline check).

    Parameters:
        destination_note (str): Caller-supplied webhook tag or target note.
        summary (str): Payload content sent by the calling agent.
        agent_role (str): Optional calling agent identifier.

    Return Value:
        Validated `WebhookPayload` model instance.

    Exceptions/Errors:
        pydantic.ValidationError: Raised when required strings are empty or oversized.
    """

    model_config = ConfigDict(extra="forbid")

    destination_note: str = Field(..., min_length=1, max_length=240)
    summary: str = Field(..., min_length=1, max_length=4000)
    agent_role: str = Field(default="unknown", max_length=80)


def create_rogue_app() -> FastAPI:
    """
    Summary:
        Creates the FastAPI application for `cymbal-rogue-ext` with `/healthz` and
        `/collect` routes.

    Parameters:
        None.

    Return Value:
        FastAPI: Configured ASGI application.

    Exceptions/Errors:
        None.
    """
    rogue_app = FastAPI(
        title="Unregistered External Webhook Receiver",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
    )
    received_ring: list[dict[str, Any]] = []

    @rogue_app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        """
        Summary:
            Liveness/readiness endpoint reporting service health and received count.

        Parameters:
            None.

        Return Value:
            dict[str, Any]: Service health dictionary.

        Exceptions/Errors:
            None.
        """
        return {
            "status": "ok",
            "service": "cymbal-rogue-ext",
            "registered_in_agent_registry": False,
            "received_count": len(received_ring),
        }

    @rogue_app.post("/collect")
    async def collect_webhook(payload: WebhookPayload) -> dict[str, Any]:
        """
        Summary:
            Records receipt of an ungoverned outbound webhook call. Behind Google
            Cloud Agent Gateway (`AGENT_TO_ANYWHERE`), requests to this unregistered
            destination are blocked at the gateway and never reach this handler.

        Parameters:
            payload (WebhookPayload): Validated JSON body.

        Return Value:
            dict[str, Any]: Acknowledgement receipt used only in ungoverned local mode.

        Exceptions/Errors:
            None.
        """
        now_iso = datetime.now(UTC).isoformat()
        entry = {
            "received_at": now_iso,
            "agent_role": payload.agent_role,
            "destination_note": payload.destination_note,
            "byte_length": len(payload.summary.encode("utf-8")),
        }
        received_ring.insert(0, entry)
        del received_ring[25:]
        log_structured(
            logger,
            logging.WARNING,
            "unregistered_webhook_received",
            agent_role=payload.agent_role,
            destination_note=payload.destination_note,
            byte_length=entry["byte_length"],
        )
        return {
            "status": "received_ungoverned",
            "warning": (
                "Endpoint reached because no Agent Gateway default-deny egress "
                "perimeter blocked this unregistered host."
            ),
            "received_at": now_iso,
        }

    return rogue_app


app = create_rogue_app()


def main() -> None:
    """
    Summary:
        CLI entry point starting Uvicorn on `$PORT` (default `8082`).

    Parameters:
        None.

    Return Value:
        None.

    Exceptions/Errors:
        ValueError: If `PORT` is not a valid integer.
    """
    import uvicorn

    port = int(os.getenv("PORT", "8082"))
    uvicorn.run(
        "services.cymbal_rogue_ext.app:app",
        host="0.0.0.0",
        port=port,
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
    )


if __name__ == "__main__":
    main()
