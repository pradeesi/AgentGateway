"""
Purpose: Unit tests validating `CymbalMcpClient` gateway verdict classification
         (IAP UAP `403`, Agent Registry `498` default-deny, Model Armor blocks,
         and SDP PII-masked JSON responses) plus ADK `root_agent` initialization.
Architecture/Context: Uses `httpx.MockTransport` to deterministically simulate
         Google Cloud Agent Gateway HTTP enforcement responses offline.
Dependencies/Side Effects: Pure in-memory mock transport tests.
Author: Pradeep Singh
License: Apache-2.0
"""

from __future__ import annotations

import json

import httpx

from agents.risk_specialist_agent.app.agent import root_agent as specialist_agent
from agents.shared_tools.mcp_client import CymbalMcpClient
from agents.tier1_support_agent.app.agent import root_agent as tier1_agent


def test_adk_agents_initialize_with_all_tools() -> None:
    """
    Summary:
        Confirms both `tier1_support_agent` and `risk_specialist_agent` instantiate
        cleanly with all 8 tools bound so policy enforcement occurs at Agent Gateway.

    Parameters:
        None.

    Return Value:
        None.

    Exceptions/Errors:
        AssertionError: If agent names or tool counts are misconfigured.
    """
    assert tier1_agent.name == "tier1_support_agent"
    assert specialist_agent.name == "risk_specialist_agent"
    assert len(tier1_agent.tools) == 8
    assert len(specialist_agent.tools) == 8


def test_mcp_client_gateway_verdicts_and_sdp_masking() -> None:
    """
    Summary:
        Simulates IAP UAP `403`, Agent Registry `498`, Model Armor `400`, and SDP
        de-identified `200` responses through `CymbalMcpClient`.

    Parameters:
        None.

    Return Value:
        None.

    Exceptions/Errors:
        AssertionError: If policy layers or masked fields are misclassified.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "rogue.example" in url:
            return httpx.Response(498, text="Destination host is unregistered in Agent Registry")

        payload = json.loads(request.content.decode("utf-8"))
        tool = payload["params"]["name"]
        if tool == "issue_dispute_refund":
            return httpx.Response(403, text="IAP Unified Access Policy denied principal")
        if tool == "get_dispute_details":
            return httpx.Response(
                400,
                headers={"x-model-armor-blocked": "true"},
                text="Blocked by Model Armor piAndJailbreakFilterSettings",
            )
        # Simulate Model Armor + Cloud SDP masking SSN & card PAN in transit.
        masked_doc = {
            "status": "success",
            "account": {
                "customer_id": "CUST-101",
                "ssn": "[US_SOCIAL_SECURITY_NUMBER]",
                "card_pan": "[CREDIT_CARD_NUMBER]",
            },
        }
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {
                    "content": [{"type": "text", "text": json.dumps(masked_doc)}],
                    "isError": False,
                },
            },
        )

    client = CymbalMcpClient(
        agent_role="tier1-support-agent",
        mcp_base_url="https://mcp.example",
        rogue_webhook_url="https://rogue.example/collect",
        auth_mode="none",
        transport=httpx.MockTransport(handler),
    )

    masked_res = client.call_tool("get_account_360", {"customer_id": "CUST-101"})
    assert masked_res["account"]["ssn"] == "[US_SOCIAL_SECURITY_NUMBER]"
    assert masked_res["account"]["card_pan"] == "[CREDIT_CARD_NUMBER]"

    iap_block = client.call_tool(
        "issue_dispute_refund",
        {"dispute_id": "DSP-1001", "amount_usd": 125.0, "reason": "Test"},
    )
    assert iap_block["status"] == "blocked_by_agent_gateway"
    assert iap_block["policy_layer"] == "EGRESS_IAP_IAM_UAP"

    ma_block = client.call_tool("get_dispute_details", {"dispute_id": "DSP-1004"})
    assert ma_block["status"] == "blocked_by_agent_gateway"
    assert ma_block["policy_layer"] == "EGRESS_MODEL_ARMOR"

    rogue_block = client.send_rogue_webhook("audit-drop", "customer dump")
    assert rogue_block["status"] == "blocked_by_agent_gateway"
    assert rogue_block["policy_layer"] == "AGENT_REGISTRY_DEFAULT_DENY"


def test_mcp_client_classifies_http_429_token_budget_policy() -> None:
    """
    Summary:
        Verifies `classify_gateway_block` maps HTTP `429 Too Many Requests`
        rejections to `EGRESS_TOKEN_MANAGEMENT_POLICY`.

    Parameters:
        None.

    Return Value:
        None.

    Exceptions/Errors:
        AssertionError: If HTTP 429 is not attributed to Token Management policy.
    """
    from agents.shared_tools.mcp_client import classify_gateway_block

    verdict = classify_gateway_block(
        http_status=429,
        response_body="Too Many Requests: TokenPool maxTokens quota exhausted",
        response_headers={},
        target_description="aiplatform:generateContent",
    )
    assert verdict["status"] == "blocked_by_agent_gateway"
    assert verdict["http_status"] == 429
    assert verdict["policy_layer"] == "EGRESS_TOKEN_MANAGEMENT_POLICY"
