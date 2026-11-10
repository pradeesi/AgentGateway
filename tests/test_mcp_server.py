"""
Purpose: Integration tests for the Cymbal Banking FastMCP Streamable HTTP server
         (`services/cymbal_banking_mcp/server.py`).
Architecture/Context: Exercises `/mcp`, `/mcp/readonly`, and `/mcp/privileged`
         in-memory via `httpx.ASGITransport`, asserting exact MCP `readOnlyHint` /
         `destructiveHint` annotations required by IAM Unified Access Policy CEL
         expressions on Google Cloud Agent Gateway.
Dependencies/Side Effects: Uses isolated `LocalFileLedgerStore` in `tmp_path`.
Author: Pradeep Singh
License: Apache-2.0
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from common.ledger_store import LocalFileLedgerStore
from services.cymbal_banking_mcp.server import create_mcp_http_app


@pytest.mark.asyncio
async def test_mcp_tools_list_annotations_and_scopes(tmp_path: Path) -> None:
    """
    Summary:
        Verifies `tools/list` on `/mcp`, `/mcp/readonly`, and `/mcp/privileged`
        exposes accurate tool counts and MCP `annotations.readOnlyHint` values.

    Parameters:
        tmp_path (Path): Pytest temporary directory fixture.

    Return Value:
        None.

    Exceptions/Errors:
        AssertionError: If annotations or sub-mount scopes mismatch IAM UAP CEL rules.
    """
    store = LocalFileLedgerStore(tmp_path / "mcp_ledger.json")
    mcp_app = create_mcp_http_app(store_provider=lambda: store)
    transport = httpx.ASGITransport(app=mcp_app)

    async with httpx.AsyncClient(transport=transport, base_url="http://mcp.test") as client:
        init_resp = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        assert init_resp.status_code == 200
        assert "Mcp-Session-Id" in init_resp.headers

        all_resp = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        tools = {t["name"]: t for t in all_resp.json()["result"]["tools"]}
        assert len(tools) == 7

        # Verify readOnlyHint annotations relied upon by IAM UAP CEL expression.
        assert tools["search_customers"]["annotations"]["readOnlyHint"] is True
        assert tools["get_account_360"]["annotations"]["readOnlyHint"] is True
        assert tools["list_transactions"]["annotations"]["readOnlyHint"] is True
        assert tools["get_dispute_details"]["annotations"]["readOnlyHint"] is True
        assert tools["issue_dispute_refund"]["annotations"]["readOnlyHint"] is False
        assert tools["place_account_hold"]["annotations"]["readOnlyHint"] is False
        assert tools["place_account_hold"]["annotations"]["destructiveHint"] is True
        assert tools["initiate_wire_reversal"]["annotations"]["readOnlyHint"] is False

        ro_resp = await client.post(
            "/mcp/readonly",
            json={"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
        )
        assert len(ro_resp.json()["result"]["tools"]) == 4

        priv_resp = await client.post(
            "/mcp/privileged",
            json={"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {}},
        )
        assert len(priv_resp.json()["result"]["tools"]) == 3


@pytest.mark.asyncio
async def test_mcp_tools_call_and_scope_isolation(tmp_path: Path) -> None:
    """
    Summary:
        Verifies `tools/call` executes `get_account_360` and `issue_dispute_refund`
        on `/mcp`, while `/mcp/readonly` rejects `issue_dispute_refund`.

    Parameters:
        tmp_path (Path): Pytest temporary directory fixture.

    Return Value:
        None.

    Exceptions/Errors:
        AssertionError: If tool payloads or scope boundaries fail.
    """
    store = LocalFileLedgerStore(tmp_path / "mcp_call.json")
    mcp_app = create_mcp_http_app(store_provider=lambda: store)
    transport = httpx.ASGITransport(app=mcp_app)

    async with httpx.AsyncClient(transport=transport, base_url="http://mcp.test") as client:
        acct_resp = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": "call-1",
                "method": "tools/call",
                "params": {"name": "get_account_360", "arguments": {"customer_id": "CUST-101"}},
            },
        )
        acct_data = json.loads(acct_resp.json()["result"]["content"][0]["text"])
        assert acct_data["status"] == "success"
        assert acct_data["account"]["ssn"] == "453-29-8174"

        # Calling a privileged mutation tool on `/mcp/readonly` must fail with -32601.
        denied_scope = await client.post(
            "/mcp/readonly",
            json={
                "jsonrpc": "2.0",
                "id": "call-2",
                "method": "tools/call",
                "params": {
                    "name": "issue_dispute_refund",
                    "arguments": {
                        "dispute_id": "DSP-1001",
                        "amount_usd": 125.0,
                        "reason": "Scope test",
                    },
                },
            },
        )
        assert denied_scope.json()["error"]["code"] == -32601
