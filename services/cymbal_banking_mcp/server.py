"""
Purpose: Cymbal Banking Model Context Protocol (MCP) Server exposing read-only and
         privileged treasury/disputes tools over stateless Streamable HTTP JSON-RPC.
Architecture/Context: Deployed to Google Cloud Run (`cymbal-banking-mcp`, IAM-protected)
         and registered in Google Cloud Agent Registry with full MCP tool specs and
         `readOnlyHint` / `destructiveHint` / `idempotentHint` annotations. Exposes:
           - `POST /mcp`            : All 7 tools (governed via IAM UAP V2 CEL rules).
           - `POST /mcp/readonly`   : 4 read-only tools (IAP V1 org-policy fallback).
           - `POST /mcp/privileged` : 3 write tools (IAP V1 org-policy fallback).
           - `GET  /healthz`        : Container readiness/liveness probe.
Dependencies/Side Effects: Reads/mutates Cymbal Bank state via `common.ledger_store`
         (`STORAGE_BACKEND=firestore|local`). Returns pure `application/json`
         JSON-RPC 2.0 responses so Agent Gateway's MCP parser and Model Armor
         `CONTENT_AUTHZ` inspector evaluate every tool call deterministically.
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
from dataclasses import dataclass
from typing import Any, Literal

import mcp.types as mcp_types
from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import FastMCP

from common.ledger_store import (
    LedgerNotFoundError,
    LedgerStore,
    LedgerValidationError,
    get_ledger_store,
)
from common.logging_utils import configure_structured_logging, log_structured

logger = configure_structured_logging("cymbal.banking_mcp")

MCP_PROTOCOL_VERSION = "2025-03-26"


@dataclass(frozen=True)
class RegisteredMcpTool:
    """
    Summary:
        Metadata and callable wrapper for a single Cymbal Banking MCP tool, pairing
        its `mcp.types.Tool` schema (with MCP `ToolAnnotations`) and tier scope.

    Parameters:
        name (str): Canonical MCP tool identifier.
        tier (Literal["readonly", "privileged"]): Access tier used for sub-mount filtering.
        definition (mcp_types.Tool): Official MCP SDK `Tool` descriptor with annotations.
        handler (Callable[[LedgerStore, dict[str, Any], str], dict[str, Any]]):
            Synchronous execution handler returning a JSON-serializable dictionary.

    Return Value:
        Immutable `RegisteredMcpTool` descriptor.

    Exceptions/Errors:
        None.
    """

    name: str
    tier: Literal["readonly", "privileged"]
    definition: mcp_types.Tool
    handler: Callable[[LedgerStore, dict[str, Any], str], dict[str, Any]]


def _build_tool_registry() -> dict[str, RegisteredMcpTool]:
    """
    Summary:
        Uses `FastMCP` + `mcp.types.ToolAnnotations` to construct canonical MCP
        `Tool` schemas for all 7 Cymbal Banking tools so JSON Schema generation and
        `readOnlyHint` / `destructiveHint` annotations strictly follow the MCP spec.

    Parameters:
        None.

    Return Value:
        dict[str, RegisteredMcpTool]: Map of tool name -> `RegisteredMcpTool`.

    Exceptions/Errors:
        RuntimeError: Raised if FastMCP fails to register any tool definition.
    """
    schema_builder = FastMCP("cymbal-banking-mcp")

    @schema_builder.tool(
        name="search_customers",
        description=(
            "Search Cymbal Bank customer profiles by partial name, customer ID "
            "(CUST-...), email, or banking tier."
        ),
        annotations=mcp_types.ToolAnnotations(
            title="Search Cymbal Bank Customers",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def _sig_search_customers(query: str = "") -> dict[str, Any]:
        """Schema stub used by FastMCP to derive JSON Schema and ToolAnnotations."""
        return {"query": query}

    @schema_builder.tool(
        name="get_account_360",
        description=(
            "Retrieve full Customer 360 profile, available balance, hold status, "
            "and KYC verification details for a Cymbal Bank customer_id (CUST-...)."
        ),
        annotations=mcp_types.ToolAnnotations(
            title="Get Customer Account 360",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def _sig_get_account_360(customer_id: str) -> dict[str, Any]:
        """Schema stub used by FastMCP to derive JSON Schema and ToolAnnotations."""
        return {"customer_id": customer_id}

    @schema_builder.tool(
        name="list_transactions",
        description=(
            "List recent posted, disputed, refunded, or reversed card/ACH/wire "
            "transactions for a customer_id (CUST-...)."
        ),
        annotations=mcp_types.ToolAnnotations(
            title="List Customer Transactions",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def _sig_list_transactions(customer_id: str, limit: int = 20) -> dict[str, Any]:
        """Schema stub used by FastMCP to derive JSON Schema and ToolAnnotations."""
        return {"customer_id": customer_id, "limit": limit}

    @schema_builder.tool(
        name="get_dispute_details",
        description=(
            "Retrieve full dispute case details, customer statement, fraud verification "
            "status, and merchant representment evidence note for a dispute_id (DSP-...)."
        ),
        annotations=mcp_types.ToolAnnotations(
            title="Get Dispute Case Details",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def _sig_get_dispute_details(dispute_id: str) -> dict[str, Any]:
        """Schema stub used by FastMCP to derive JSON Schema and ToolAnnotations."""
        return {"dispute_id": dispute_id}

    @schema_builder.tool(
        name="issue_dispute_refund",
        description=(
            "Privileged action: Issue a dispute refund credit in USD to the customer's "
            "Cymbal Bank account and mark the dispute REFUND_ISSUED. Refunds exceeding "
            "$500.00 require a valid supervisor_approval_code."
        ),
        annotations=mcp_types.ToolAnnotations(
            title="Issue Dispute Refund Credit",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def _sig_issue_dispute_refund(
        dispute_id: str,
        amount_usd: float,
        reason: str,
        supervisor_approval_code: str = "",
    ) -> dict[str, Any]:
        """Schema stub used by FastMCP to derive JSON Schema and ToolAnnotations."""
        return {
            "dispute_id": dispute_id,
            "amount_usd": amount_usd,
            "reason": reason,
            "supervisor_approval_code": supervisor_approval_code,
        }

    @schema_builder.tool(
        name="place_account_hold",
        description=(
            "Privileged destructive action: Place a protective HOLD_REVIEW or HOLD_FRAUD "
            "restriction on a Cymbal Bank customer account."
        ),
        annotations=mcp_types.ToolAnnotations(
            title="Place Customer Account Hold",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def _sig_place_account_hold(
        customer_id: str,
        reason: str,
        hold_tier: str = "HOLD_REVIEW",
    ) -> dict[str, Any]:
        """Schema stub used by FastMCP to derive JSON Schema and ToolAnnotations."""
        return {"customer_id": customer_id, "reason": reason, "hold_tier": hold_tier}

    @schema_builder.tool(
        name="initiate_wire_reversal",
        description=(
            "Privileged destructive action: Recall and reverse an outbound WIRE_OUT "
            "transaction when backed by a verified Fraud Ops ticket (FRD-...)."
        ),
        annotations=mcp_types.ToolAnnotations(
            title="Initiate Outbound Wire Reversal",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    def _sig_initiate_wire_reversal(
        transaction_id: str,
        reason: str,
        fraud_verified_ticket: str,
    ) -> dict[str, Any]:
        """Schema stub used by FastMCP to derive JSON Schema and ToolAnnotations."""
        return {
            "transaction_id": transaction_id,
            "reason": reason,
            "fraud_verified_ticket": fraud_verified_ticket,
        }

    # Extract official MCP Tool descriptors produced by FastMCP's internal tool manager.
    raw_tools: dict[str, mcp_types.Tool] = {}
    for tool_info in schema_builder._tool_manager.list_tools():
        raw_tools[tool_info.name] = mcp_types.Tool(
            name=tool_info.name,
            description=tool_info.description,
            inputSchema=tool_info.parameters,
            annotations=tool_info.annotations,
        )

    handlers: dict[
        str,
        tuple[
            Literal["readonly", "privileged"],
            Callable[[LedgerStore, dict[str, Any], str], dict[str, Any]],
        ],
    ] = {
        "search_customers": ("readonly", _exec_search_customers),
        "get_account_360": ("readonly", _exec_get_account_360),
        "list_transactions": ("readonly", _exec_list_transactions),
        "get_dispute_details": ("readonly", _exec_get_dispute_details),
        "issue_dispute_refund": ("privileged", _exec_issue_dispute_refund),
        "place_account_hold": ("privileged", _exec_place_account_hold),
        "initiate_wire_reversal": ("privileged", _exec_initiate_wire_reversal),
    }

    registry: dict[str, RegisteredMcpTool] = {}
    for name, (tier, handler) in handlers.items():
        if name not in raw_tools:
            raise RuntimeError(f"Missing FastMCP schema definition for '{name}'.")
        registry[name] = RegisteredMcpTool(
            name=name,
            tier=tier,
            definition=raw_tools[name],
            handler=handler,
        )
    return registry


def _exec_search_customers(
    store: LedgerStore,
    args: dict[str, Any],
    actor: str,
) -> dict[str, Any]:
    """
    Summary:
        Executes `search_customers` against the active `LedgerStore`, omitting raw
        SSN/PAN from bulk search summaries so full PII appears specifically in
        `get_account_360` Customer 360 lookups.

    Parameters:
        store (LedgerStore): Active ledger persistence backend.
        args (dict[str, Any]): Tool arguments (`query`).
        actor (str): Caller identity string.

    Return Value:
        dict[str, Any]: Customer summary list and count.

    Exceptions/Errors:
        LedgerValidationError: Propagated on malformed query.
    """
    del actor
    query = str(args.get("query", ""))
    matches = store.search_customers(query)
    summaries = [
        {
            "customer_id": acct.customer_id,
            "full_name": acct.full_name,
            "segment": acct.segment,
            "account_number_masked": acct.account_number_masked,
            "available_balance_usd": acct.available_balance_usd,
            "account_status": acct.account_status,
        }
        for acct in matches
    ]
    return {"status": "success", "count": len(summaries), "customers": summaries}


def _exec_get_account_360(
    store: LedgerStore,
    args: dict[str, Any],
    actor: str,
) -> dict[str, Any]:
    """
    Summary:
        Executes `get_account_360`, returning the complete customer record (including
        raw synthetic `ssn`, `card_pan`, `email`, `phone`, and `routing_number`) plus
        open disputes so Model Armor + Cloud SDP on Agent Gateway visibly de-identifies
        sensitive banking fields in transit.

    Parameters:
        store (LedgerStore): Active ledger persistence backend.
        args (dict[str, Any]): Tool arguments (`customer_id`).
        actor (str): Caller identity string.

    Return Value:
        dict[str, Any]: Full Customer 360 payload and open dispute summaries.

    Exceptions/Errors:
        LedgerNotFoundError: If `customer_id` does not exist.
        LedgerValidationError: If `customer_id` is missing or invalid.
    """
    del actor
    customer_id = str(args.get("customer_id", ""))
    account = store.get_account(customer_id)
    open_disputes = [
        {
            "dispute_id": d.dispute_id,
            "transaction_id": d.transaction_id,
            "disputed_amount_usd": d.disputed_amount_usd,
            "reason_code": d.reason_code,
            "status": d.status,
        }
        for d in store.list_disputes(customer_id=account.customer_id)
    ]
    return {
        "status": "success",
        "account": account.model_dump(),
        "disputes": open_disputes,
    }


def _exec_list_transactions(
    store: LedgerStore,
    args: dict[str, Any],
    actor: str,
) -> dict[str, Any]:
    """
    Summary:
        Executes `list_transactions` for a single customer account.

    Parameters:
        store (LedgerStore): Active ledger persistence backend.
        args (dict[str, Any]): Tool arguments (`customer_id`, optional `limit`).
        actor (str): Caller identity string.

    Return Value:
        dict[str, Any]: Ordered list of customer transactions.

    Exceptions/Errors:
        LedgerValidationError: If `customer_id` or `limit` is invalid.
    """
    del actor
    customer_id = str(args.get("customer_id", ""))
    raw_limit = args.get("limit", 20)
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError) as exc:
        raise LedgerValidationError("limit must be a valid integer.") from exc
    txns = store.list_transactions(customer_id=customer_id, limit=limit)
    return {
        "status": "success",
        "customer_id": customer_id.strip().upper(),
        "count": len(txns),
        "transactions": [t.model_dump() for t in txns],
    }


def _exec_get_dispute_details(
    store: LedgerStore,
    args: dict[str, Any],
    actor: str,
) -> dict[str, Any]:
    """
    Summary:
        Executes `get_dispute_details`, returning the full `DisputeCase` including
        the untrusted `merchant_evidence_note` (which in `DSP-1004` carries an
        indirect prompt-injection payload inspected by Model Armor on Egress Gateway).

    Parameters:
        store (LedgerStore): Active ledger persistence backend.
        args (dict[str, Any]): Tool arguments (`dispute_id`).
        actor (str): Caller identity string.

    Return Value:
        dict[str, Any]: Full dispute record dictionary.

    Exceptions/Errors:
        LedgerNotFoundError: If `dispute_id` does not exist.
        LedgerValidationError: If `dispute_id` is malformed.
    """
    del actor
    dispute_id = str(args.get("dispute_id", ""))
    dispute = store.get_dispute(dispute_id)
    return {"status": "success", "dispute": dispute.model_dump()}


def _exec_issue_dispute_refund(
    store: LedgerStore,
    args: dict[str, Any],
    actor: str,
) -> dict[str, Any]:
    """
    Summary:
        Executes privileged `issue_dispute_refund` against the Cymbal Bank ledger.
        Note: Policy caps ($500 limit without supervisor code) and caller RBAC are
        intentionally enforced by Google Cloud Agent Gateway (IAM UAP + Semantic
        Governance Policy Engine) rather than hardcoded agent prompt rules!

    Parameters:
        store (LedgerStore): Active ledger persistence backend.
        args (dict[str, Any]): Tool arguments (`dispute_id`, `amount_usd`, `reason`,
            `supervisor_approval_code`).
        actor (str): Caller identity recorded in ledger audit trail.

    Return Value:
        dict[str, Any]: Serialized `RefundReceipt`.

    Exceptions/Errors:
        LedgerNotFoundError: If `dispute_id` does not exist.
        LedgerValidationError: If dispute is closed or amount is invalid.
    """
    dispute_id = str(args.get("dispute_id", ""))
    try:
        amount_usd = float(args.get("amount_usd", 0))
    except (TypeError, ValueError) as exc:
        raise LedgerValidationError("amount_usd must be a numeric dollar value.") from exc
    reason = str(args.get("reason", ""))
    sup_code = str(args.get("supervisor_approval_code", "") or "").strip() or None
    receipt = store.issue_dispute_refund(
        dispute_id=dispute_id,
        amount_usd=amount_usd,
        reason=reason,
        supervisor_approval_code=sup_code,
        actor_principal=actor,
    )
    return {"status": "success", "receipt": receipt.model_dump()}


def _exec_place_account_hold(
    store: LedgerStore,
    args: dict[str, Any],
    actor: str,
) -> dict[str, Any]:
    """
    Summary:
        Executes privileged `place_account_hold` on a Cymbal Bank customer account.

    Parameters:
        store (LedgerStore): Active ledger persistence backend.
        args (dict[str, Any]): Tool arguments (`customer_id`, `reason`, `hold_tier`).
        actor (str): Caller identity recorded in ledger audit trail.

    Return Value:
        dict[str, Any]: Serialized `AccountHoldReceipt`.

    Exceptions/Errors:
        LedgerNotFoundError: If `customer_id` does not exist.
        LedgerValidationError: If `hold_tier` or `reason` is invalid.
    """
    customer_id = str(args.get("customer_id", ""))
    reason = str(args.get("reason", ""))
    hold_tier_raw = str(args.get("hold_tier", "HOLD_REVIEW")).strip().upper()
    if hold_tier_raw not in ("HOLD_REVIEW", "HOLD_FRAUD"):
        raise LedgerValidationError("hold_tier must be 'HOLD_REVIEW' or 'HOLD_FRAUD'.")
    hold_tier: Literal["HOLD_REVIEW", "HOLD_FRAUD"] = (
        "HOLD_FRAUD" if hold_tier_raw == "HOLD_FRAUD" else "HOLD_REVIEW"
    )
    receipt = store.place_account_hold(
        customer_id=customer_id,
        reason=reason,
        hold_tier=hold_tier,
        actor_principal=actor,
    )
    return {"status": "success", "receipt": receipt.model_dump()}


def _exec_initiate_wire_reversal(
    store: LedgerStore,
    args: dict[str, Any],
    actor: str,
) -> dict[str, Any]:
    """
    Summary:
        Executes privileged `initiate_wire_reversal` for a disputed `WIRE_OUT`.

    Parameters:
        store (LedgerStore): Active ledger persistence backend.
        args (dict[str, Any]): Tool arguments (`transaction_id`, `reason`,
            `fraud_verified_ticket`).
        actor (str): Caller identity recorded in ledger audit trail.

    Return Value:
        dict[str, Any]: Serialized `WireReversalReceipt`.

    Exceptions/Errors:
        LedgerNotFoundError: If `transaction_id` does not exist.
        LedgerValidationError: If wire eligibility or ticket format fails.
    """
    transaction_id = str(args.get("transaction_id", ""))
    reason = str(args.get("reason", ""))
    ticket = str(args.get("fraud_verified_ticket", ""))
    receipt = store.initiate_wire_reversal(
        transaction_id=transaction_id,
        reason=reason,
        fraud_verified_ticket=ticket,
        actor_principal=actor,
    )
    return {"status": "success", "receipt": receipt.model_dump()}


TOOL_REGISTRY: dict[str, RegisteredMcpTool] = _build_tool_registry()


def _jsonrpc_error(req_id: Any, code: int, message: str) -> JSONResponse:
    """
    Summary:
        Constructs a standard JSON-RPC 2.0 error response with `application/json`.

    Parameters:
        req_id (Any): Request `id` echoed from the client payload.
        code (int): JSON-RPC 2.0 numeric error code.
        message (str): Safe, descriptive error message.

    Return Value:
        JSONResponse: HTTP 200 JSON-RPC error envelope per MCP Streamable HTTP spec.

    Exceptions/Errors:
        None.
    """
    return JSONResponse(
        status_code=200,
        content={
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": code, "message": message},
        },
    )


def create_mcp_http_app(store_provider: Callable[[], LedgerStore] = get_ledger_store) -> FastAPI:
    """
    Summary:
        Builds the FastAPI ASGI application serving the Cymbal Banking MCP Server
        across `/mcp` (all tools), `/mcp/readonly`, and `/mcp/privileged`, plus
        `/healthz`.

    Parameters:
        store_provider (Callable[[], LedgerStore]): Dependency-injectable factory
            returning the active `LedgerStore` (allows isolated test stores).

    Return Value:
        FastAPI: Configured ASGI application ready for Uvicorn or `httpx.ASGITransport`.

    Exceptions/Errors:
        None.
    """
    mcp_app = FastAPI(
        title="Cymbal Banking MCP Server",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
    )

    @mcp_app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """
        Summary:
            Lightweight container health probe verifying storage configuration.

        Parameters:
            None.

        Return Value:
            dict[str, str]: Service status and active storage backend name.

        Exceptions/Errors:
            None.
        """
        store = store_provider()
        return {"status": "ok", "service": "cymbal-banking-mcp", "backend": store.backend_name}

    async def _dispatch_mcp(
        request: Request,
        scope: Literal["all", "readonly", "privileged"],
        mcp_session_id: str | None,
        caller_agent: str | None,
    ) -> Response:
        """
        Summary:
            Processes a single MCP Streamable HTTP JSON-RPC 2.0 request (`initialize`,
            `notifications/*`, `ping`, `tools/list`, `tools/call`) scoped to `all`,
            `readonly`, or `privileged` tools.

        Parameters:
            request (Request): Incoming FastAPI HTTP request.
            scope (Literal["all", "readonly", "privileged"]): Mounted endpoint scope.
            mcp_session_id (str | None): Optional `Mcp-Session-Id` header from client.
            caller_agent (str | None): Optional `X-Agent-Role` telemetry header.

        Return Value:
            Response: HTTP 202 for notifications or `application/json` JSON-RPC 2.0 reply.

        Exceptions/Errors:
            None; all malformed payloads and domain exceptions map to JSON-RPC errors.
        """
        started = time.perf_counter()
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return _jsonrpc_error(None, -32700, "Invalid JSON payload.")

        if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
            return _jsonrpc_error(
                body.get("id") if isinstance(body, dict) else None,
                -32600,
                "Invalid Request: expected JSON-RPC 2.0 object.",
            )

        req_id = body.get("id")
        method = body.get("method")
        params = body.get("params") or {}
        if not isinstance(method, str) or not isinstance(params, dict):
            return _jsonrpc_error(req_id, -32600, "Invalid JSON-RPC method or params.")

        # MCP notifications carry no `id` and expect HTTP 202 Accepted with empty body.
        if method.startswith("notifications/") or req_id is None:
            return Response(status_code=202)

        visible_tools = {
            name: reg
            for name, reg in TOOL_REGISTRY.items()
            if scope == "all" or reg.tier == scope
        }

        session_header = mcp_session_id or f"cymbal-mcp-{uuid.uuid4().hex[:12]}"
        resp_headers = {"Mcp-Session-Id": session_header}

        if method == "initialize":
            return JSONResponse(
                headers=resp_headers,
                content={
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": MCP_PROTOCOL_VERSION,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {
                            "name": f"cymbal-banking-mcp ({scope})",
                            "version": "1.0.0",
                        },
                    },
                },
            )

        if method == "ping":
            return JSONResponse(
                headers=resp_headers,
                content={"jsonrpc": "2.0", "id": req_id, "result": {}},
            )

        if method == "tools/list":
            serialized_tools = [
                reg.definition.model_dump(mode="json", exclude_none=True)
                for reg in visible_tools.values()
            ]
            log_structured(
                logger,
                logging.INFO,
                "mcp_tools_listed",
                scope=scope,
                tool_count=len(serialized_tools),
                caller=caller_agent or "unknown",
            )
            return JSONResponse(
                headers=resp_headers,
                content={
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"tools": serialized_tools},
                },
            )

        if method == "tools/call":
            tool_name = params.get("name")
            arguments = params.get("arguments") or {}
            if not isinstance(tool_name, str) or not isinstance(arguments, dict):
                return _jsonrpc_error(
                    req_id,
                    -32602,
                    "Invalid params: 'name' (str) and 'arguments' (object) required.",
                )
            if tool_name not in visible_tools:
                log_structured(
                    logger,
                    logging.WARNING,
                    "mcp_tool_not_in_scope",
                    tool_name=tool_name,
                    scope=scope,
                    caller=caller_agent or "unknown",
                )
                return _jsonrpc_error(
                    req_id,
                    -32601,
                    f"Tool '{tool_name}' is not available on endpoint scope '{scope}'.",
                )

            store = store_provider()
            actor = (caller_agent or "mcp-agent").strip()[:120]
            try:
                output = visible_tools[tool_name].handler(store, arguments, actor)
                duration_ms = round((time.perf_counter() - started) * 1000, 2)
                log_structured(
                    logger,
                    logging.INFO,
                    "mcp_tool_executed",
                    tool_name=tool_name,
                    scope=scope,
                    caller=actor,
                    duration_ms=duration_ms,
                )
                return JSONResponse(
                    headers=resp_headers,
                    content={
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "content": [
                                {"type": "text", "text": json.dumps(output, sort_keys=True)}
                            ],
                            "structuredContent": output,
                            "isError": False,
                        },
                    },
                )
            except (LedgerNotFoundError, LedgerValidationError) as domain_err:
                duration_ms = round((time.perf_counter() - started) * 1000, 2)
                err_payload = {
                    "status": "domain_error",
                    "error_type": type(domain_err).__name__,
                    "message": str(domain_err),
                }
                log_structured(
                    logger,
                    logging.WARNING,
                    "mcp_tool_domain_error",
                    tool_name=tool_name,
                    caller=actor,
                    error=str(domain_err),
                    duration_ms=duration_ms,
                )
                return JSONResponse(
                    headers=resp_headers,
                    content={
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "content": [
                                {"type": "text", "text": json.dumps(err_payload, sort_keys=True)}
                            ],
                            "structuredContent": err_payload,
                            "isError": True,
                        },
                    },
                )
            except Exception as exc:
                log_structured(
                    logger,
                    logging.ERROR,
                    "mcp_tool_internal_error",
                    tool_name=tool_name,
                    caller=actor,
                    error=str(exc),
                )
                return _jsonrpc_error(req_id, -32603, "Internal ledger execution error.")

        return _jsonrpc_error(req_id, -32601, f"Unsupported MCP method '{method}'.")

    @mcp_app.post("/mcp")
    async def mcp_all(
        request: Request,
        mcp_session_id: str | None = Header(default=None, alias="Mcp-Session-Id"),
        x_agent_role: str | None = Header(default=None, alias="X-Agent-Role"),
    ) -> Response:
        """Unified MCP endpoint exposing all 7 Cymbal Banking tools."""
        return await _dispatch_mcp(request, "all", mcp_session_id, x_agent_role)

    @mcp_app.post("/mcp/readonly")
    async def mcp_readonly(
        request: Request,
        mcp_session_id: str | None = Header(default=None, alias="Mcp-Session-Id"),
        x_agent_role: str | None = Header(default=None, alias="X-Agent-Role"),
    ) -> Response:
        """Read-only MCP sub-mount exposing the 4 `readOnlyHint=true` tools."""
        return await _dispatch_mcp(request, "readonly", mcp_session_id, x_agent_role)

    @mcp_app.post("/mcp/privileged")
    async def mcp_privileged(
        request: Request,
        mcp_session_id: str | None = Header(default=None, alias="Mcp-Session-Id"),
        x_agent_role: str | None = Header(default=None, alias="X-Agent-Role"),
    ) -> Response:
        """Privileged MCP sub-mount exposing the 3 financial mutation tools."""
        return await _dispatch_mcp(request, "privileged", mcp_session_id, x_agent_role)

    return mcp_app


app = create_mcp_http_app()


def main() -> None:
    """
    Summary:
        CLI entry point starting Uvicorn on `$PORT` (default `8081`).

    Parameters:
        None.

    Return Value:
        None.

    Exceptions/Errors:
        ValueError: If `PORT` environment variable is not a valid integer.
    """
    import uvicorn

    port = int(os.getenv("PORT", "8081"))
    uvicorn.run(
        "services.cymbal_banking_mcp.server:app",
        host="0.0.0.0",
        port=port,
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
    )


if __name__ == "__main__":
    main()
