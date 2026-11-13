"""
Purpose: Integration tests for the Cymbal Bank Operations Portal FastAPI routes,
         clean UI policy (verifying zero exploit buttons in static assets), and
         validation of all declarative `gateway/*.tmpl` JSON/YAML templates.
Architecture/Context: Runs offline against `create_portal_app` and checks template
         variable substitution integrity.
Dependencies/Side Effects: Uses `tmp_path` for ledger state.
Author: Pradeep Singh
License: Apache-2.0
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest

from common.ledger_store import LocalFileLedgerStore
from services.cymbal_portal.main import create_portal_app

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_portal_endpoints_and_clean_ui_convention(tmp_path: Path) -> None:
    """
    Summary:
        Exercises `/healthz`, `/`, `/api/metadata`, `/api/ledger/overview` (verifying
        portal-side last-4 masking), `/api/governance/events`, and `/api/admin/reset`,
        and asserts static HTML/JS contain no exploit cheat-sheet buttons.

    Parameters:
        tmp_path (Path): Pytest temporary directory fixture.

    Return Value:
        None.

    Exceptions/Errors:
        AssertionError: If portal endpoints or UI cleanliness checks fail.
    """
    store = LocalFileLedgerStore(tmp_path / "portal_ledger.json")
    portal_app = create_portal_app(store_provider=lambda: store)
    transport = httpx.ASGITransport(app=portal_app)

    async with httpx.AsyncClient(transport=transport, base_url="http://portal.test") as client:
        health = await client.get("/healthz")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"

        html_resp = await client.get("/")
        assert html_resp.status_code == 200
        assert "Cymbal Bank" in html_resp.text
        assert "SYSTEM OVERRIDE" not in html_resp.text

        overview = (await client.get("/api/ledger/overview")).json()
        assert len(overview["accounts"]) == 4
        # Portal overview masks SSN to last-4 while leaving raw SSN in ledger for MCP SDP demo.
        assert overview["accounts"][0]["ssn"] == "***-**-8174"
        assert store.get_account("CUST-101").ssn == "453-29-8174"

        store.place_account_hold("CUST-101", "Temporary test hold", "HOLD_REVIEW")
        reset_resp = await client.post("/api/admin/reset")
        assert reset_resp.status_code == 200
        assert store.get_account("CUST-101").account_status == "ACTIVE"

        events_resp = await client.get("/api/governance/events")
        assert events_resp.status_code == 200
        assert events_resp.json()["count"] >= 1

        tm_resp = await client.get("/api/token-management/overview")
        assert tm_resp.status_code == 200
        tm_json = tm_resp.json()
        assert len(tm_json["pools"]) == 4
        assert tm_json["total_tokens_consumed"] == 0


def test_gateway_json_and_yaml_templates_substitute_cleanly() -> None:
    """
    Summary:
        Substitutes sample environment variables across every `.tmpl` file under
        `gateway/` and asserts zero unexpanded `${...}` placeholders remain and all
        `.json.tmpl` files parse as valid JSON.

    Parameters:
        None.

    Return Value:
        None.

    Exceptions/Errors:
        AssertionError: If any template has missing variables or malformed JSON.
    """
    sample_vars = {
        "PROJECT_ID": "demo-proj",
        "PROJECT_NUMBER": "1234567890",
        "REGION": "us-central1",
        "MODEL_NAME": "gemini-2.5-flash",
        "CONNECTIVITY_TEMPLATE": "cbgw-conn-tmpl",
        "NETWORK_ATTACHMENT": "cbgw-psc-attachment",
        "SGP_DNS_ZONE_SUFFIX": "sgp.cymbal.internal.",
        "VPC_NETWORK": "cbgw-vpc",
        "INGRESS_GATEWAY": "cbgw-ingress-gw",
        "EGRESS_GATEWAY": "cbgw-egress-gw",
        "IAP_AUTHZ_EXTENSION": "cbgw-iap-ext",
        "IAP_POLICY_VERSION": "V2",
        "IAP_AUTHZ_POLICY": "cbgw-iap-policy",
        "MA_AUTHZ_EXTENSION": "cbgw-ma-ext",
        "MODEL_ARMOR_TEMPLATE_ID": "cbgw-gateway-armor",
        "MA_INGRESS_POLICY": "cbgw-ma-ingress-policy",
        "MA_EGRESS_POLICY": "cbgw-ma-egress-policy",
        "SGP_AUTHZ_EXTENSION": "cbgw-sgp-ext",
        "SGP_DNS_HOSTNAME": "us-central1.sgp.cymbal.internal",
        "SGP_AUTHZ_POLICY": "cbgw-sgp-policy",
        "AGENT_PRINCIPAL_SET": "principalSet://agents.global.org-1.system.id.goog/*",
        "REGISTRY_GOOGLE_APIS_NAME": "projects/demo-proj/locations/us-central1/endpoints/ep1",
        "TIER1_PRINCIPAL": "principal://agents.global.org-1.system.id.goog/tier1",
        "SPECIALIST_PRINCIPAL": "principal://agents.global.org-1.system.id.goog/spec",
        "REGISTRY_MCP_UNIFIED_NAME": "projects/demo-proj/locations/us-central1/mcpServers/mcp1",
        "REGISTRY_MCP_RO_NAME": "projects/demo-proj/locations/us-central1/mcpServers/mcp-ro",
        "REGISTRY_MCP_PRIV_NAME": "projects/demo-proj/locations/us-central1/mcpServers/mcp-priv",
        "SDP_INSPECT_TEMPLATE_ID": "cbgw_sdp_inspect",
        "SDP_DEIDENTIFY_TEMPLATE_ID": "cbgw_sdp_deidentify",
        "SDP_MIN_LIKELIHOOD": "LIKELY",
        "MODEL_ARMOR_PI_CONFIDENCE": "LOW_AND_ABOVE",
        "TM_SANDBOX_MAX_TOKENS": "6000",
        "TM_SHARED_OPS_MAX_TOKENS": "50000",
        "TM_SPECIALIST_MAX_TOKENS": "100000",
        "TM_DEFAULT_MAX_TOKENS": "25000",
        "TM_INPUT_TOKEN_COST_USD": "0.00000015",
        "TM_OUTPUT_TOKEN_COST_USD": "0.00000060",
        "TM_CACHED_TOKEN_COST_USD": "0.0000000375",
        "TM_PRO_COST_DISCOUNT": "0.20",
    }

    tmpl_files = list((REPO_ROOT / "gateway").rglob("*.tmpl"))
    assert len(tmpl_files) >= 15

    placeholder_re = re.compile(r"\$\{[A-Z0-9_]+\}")
    for path in tmpl_files:
        rendered = path.read_text(encoding="utf-8")
        for key, val in sample_vars.items():
            rendered = rendered.replace(f"${{{key}}}", val)
        leftover = placeholder_re.findall(rendered)
        assert not leftover, f"Unexpanded placeholders in {path.name}: {leftover}"
        if path.name.endswith(".json.tmpl"):
            parsed = json.loads(rendered)
            assert isinstance(parsed, dict)
