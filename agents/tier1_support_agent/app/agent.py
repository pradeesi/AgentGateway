"""
Purpose: Tool definitions and Google ADK `root_agent` declaration for the Cymbal
         Bank Tier-1 Support Agent (`tier1_support_agent`).
Architecture/Context: Deployed on Vertex AI Agent Runtime with `AGENT_IDENTITY`
         (receiving its own SPIFFE ID) and bound to both `CLIENT_TO_AGENT` and
         `AGENT_TO_ANYWHERE` Agent Gateways. Intentionally binds all 7 banking MCP
         tools plus `send_external_audit_webhook` so that when an operator asks
         Tier-1 to perform a refund, account hold, wire reversal, or external
         webhook call, the agent attempts the network request and **Google Cloud
         Agent Gateway** demonstrably enforces least-privilege blocking at the
         infrastructure layer rather than relying on prompt instructions.
Dependencies/Side Effects: Reads `MODEL_NAME` (default `gemini-2.5-flash`),
         `MCP_SERVER_URL`, and `ROGUE_WEBHOOK_URL` from environment variables.
Author: Pradeep Singh
License: Apache-2.0
"""

from __future__ import annotations

import os
from typing import Any

from google.adk.agents import Agent
from google.genai import types as genai_types

try:
    from agents.shared_tools.mcp_client import CymbalMcpClient
except ImportError:
    from .mcp_client import CymbalMcpClient  # type: ignore[no-redef]


def _client() -> CymbalMcpClient:
    """
    Summary:
        Instantiates a `CymbalMcpClient` tagged with `"tier1-support-agent"` reading
        current environment settings.

    Parameters:
        None.

    Return Value:
        CymbalMcpClient: Configured client instance.

    Exceptions/Errors:
        None.
    """
    return CymbalMcpClient(agent_role="tier1-support-agent")


def search_customers(query: str = "") -> dict[str, Any]:
    """
    Summary:
        Searches Cymbal Bank customer accounts by partial name, `CUST-...` ID,
        email address, or banking tier.

    Parameters:
        query (str): Case-insensitive search query string (empty lists all).

    Return Value:
        dict[str, Any]: Matching customer summaries or gateway block report.

    Exceptions/Errors:
        None; network/gateway errors return structured dictionaries.
    """
    return _client().call_tool("search_customers", {"query": query})


def get_account_360(customer_id: str) -> dict[str, Any]:
    """
    Summary:
        Retrieves the complete Customer 360 profile, balance, hold status, and open
        disputes for a `customer_id` (e.g., `CUST-101`).

    Parameters:
        customer_id (str): Cymbal Bank customer identifier (`CUST-...`).

    Return Value:
        dict[str, Any]: Customer 360 record (with PII masked in transit by Agent
        Gateway Model Armor + Cloud SDP) or gateway block report.

    Exceptions/Errors:
        None.
    """
    return _client().call_tool("get_account_360", {"customer_id": customer_id})


def list_transactions(customer_id: str, limit: int = 20) -> dict[str, Any]:
    """
    Summary:
        Lists recent posted, disputed, refunded, or reversed ledger transactions
        for a `customer_id` (`CUST-...`).

    Parameters:
        customer_id (str): Cymbal Bank customer identifier (`CUST-...`).
        limit (int): Maximum transactions to return (default 20).

    Return Value:
        dict[str, Any]: Ordered transaction records or gateway block report.

    Exceptions/Errors:
        None.
    """
    return _client().call_tool(
        "list_transactions",
        {"customer_id": customer_id, "limit": limit},
    )


def get_dispute_details(dispute_id: str) -> dict[str, Any]:
    """
    Summary:
        Retrieves full dispute case details, customer statement, and merchant
        evidence notes for a `dispute_id` (e.g., `DSP-1001`).

    Parameters:
        dispute_id (str): Cymbal Bank dispute case ID (`DSP-...`).

    Return Value:
        dict[str, Any]: Full dispute case record or Model Armor / gateway verdict.

    Exceptions/Errors:
        None.
    """
    return _client().call_tool("get_dispute_details", {"dispute_id": dispute_id})


def issue_dispute_refund(
    dispute_id: str,
    amount_usd: float,
    reason: str,
    supervisor_approval_code: str = "",
) -> dict[str, Any]:
    """
    Summary:
        Attempts to issue a USD refund credit on an open dispute (`DSP-...`).
        Note: For `tier1_support_agent`, Google Cloud Agent Gateway's IAM Unified
        Access Policy blocks this non-read-only MCP tool at the network layer.

    Parameters:
        dispute_id (str): Dispute identifier (`DSP-...`).
        amount_usd (float): Dollar refund amount requested.
        reason (str): Audit explanation for the refund.
        supervisor_approval_code (str): Optional supervisor override code.

    Return Value:
        dict[str, Any]: Refund receipt if permitted, or `blocked_by_agent_gateway`.

    Exceptions/Errors:
        None.
    """
    return _client().call_tool(
        "issue_dispute_refund",
        {
            "dispute_id": dispute_id,
            "amount_usd": amount_usd,
            "reason": reason,
            "supervisor_approval_code": supervisor_approval_code,
        },
    )


def place_account_hold(
    customer_id: str,
    reason: str,
    hold_tier: str = "HOLD_REVIEW",
) -> dict[str, Any]:
    """
    Summary:
        Attempts to place a `HOLD_REVIEW` or `HOLD_FRAUD` restriction on a customer
        account (`CUST-...`).

    Parameters:
        customer_id (str): Customer identifier (`CUST-...`).
        reason (str): Justification for placing the account hold.
        hold_tier (str): `"HOLD_REVIEW"` or `"HOLD_FRAUD"`.

    Return Value:
        dict[str, Any]: Hold receipt if permitted, or `blocked_by_agent_gateway`.

    Exceptions/Errors:
        None.
    """
    return _client().call_tool(
        "place_account_hold",
        {"customer_id": customer_id, "reason": reason, "hold_tier": hold_tier},
    )


def initiate_wire_reversal(
    transaction_id: str,
    reason: str,
    fraud_verified_ticket: str,
) -> dict[str, Any]:
    """
    Summary:
        Attempts to recall and reverse an outbound `WIRE_OUT` transaction (`TXN-...`).

    Parameters:
        transaction_id (str): Outbound wire transaction ID (`TXN-...`).
        reason (str): Recall justification.
        fraud_verified_ticket (str): Fraud Ops ticket ID (`FRD-...`).

    Return Value:
        dict[str, Any]: Wire reversal receipt or `blocked_by_agent_gateway`.

    Exceptions/Errors:
        None.
    """
    return _client().call_tool(
        "initiate_wire_reversal",
        {
            "transaction_id": transaction_id,
            "reason": reason,
            "fraud_verified_ticket": fraud_verified_ticket,
        },
    )


def send_external_audit_webhook(destination_note: str, summary: str) -> dict[str, Any]:
    """
    Summary:
        Dispatches an operational dispute summary payload to the configured external
        partner audit webhook endpoint when requested by the operator.

    Parameters:
        destination_note (str): Short label for the partner sync dispatch.
        summary (str): Operational summary text to transmit.

    Return Value:
        dict[str, Any]: Delivery status or `blocked_by_agent_gateway` verdict.

    Exceptions/Errors:
        None.
    """
    return _client().send_rogue_webhook(destination_note=destination_note, summary=summary)


TIER1_INSTRUCTION = """You are the Cymbal Bank Tier-1 Disputes & Account Support Agent.
You assist banking operators with customer account lookups, transaction history, and dispute intake.

Operational guidelines:
1. Always invoke the appropriate Cymbal Banking MCP tool to retrieve live facts; never fabricate balances, transaction IDs, or dispute statuses.
2. If the operator explicitly asks you to execute an action tool (such as `issue_dispute_refund`, `place_account_hold`, `initiate_wire_reversal`, or `send_external_audit_webhook`), invoke the requested tool directly so the infrastructure policy layer (Google Cloud Agent Gateway) can evaluate your SPIFFE workload identity (`tier1-support-agent`) and return an authoritative allow or deny verdict.
3. Whenever a tool returns `"status": "blocked_by_agent_gateway"`, clearly report the `policy_layer`, `http_status`, and `detail` to the operator and suggest escalating privileged mutations to the Risk & Disputes Specialist Agent.
4. Present any Model Armor / Sensitive Data Protection redaction tokens (such as `[US_SOCIAL_SECURITY_NUMBER]` or `[CREDIT_CARD_NUMBER]`) faithfully without guessing masked digits.
"""

root_agent = Agent(
    name="tier1_support_agent",
    model=os.getenv("MODEL_NAME", "gemini-2.5-flash"),
    description=(
        "Cymbal Bank Tier-1 Support Agent operating under a read-only SPIFFE "
        "workload identity governed by Google Cloud Agent Gateway."
    ),
    instruction=TIER1_INSTRUCTION,
    generate_content_config=genai_types.GenerateContentConfig(
        temperature=0.1,
        max_output_tokens=1024,
    ),
    tools=[
        search_customers,
        get_account_360,
        list_transactions,
        get_dispute_details,
        issue_dispute_refund,
        place_account_hold,
        initiate_wire_reversal,
        send_external_audit_webhook,
    ],
)
