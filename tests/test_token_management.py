"""
Purpose: Unit and integration tests validating Google Cloud Agent Gateway Token
         Management policies (`TokenAggregationPolicy`, `TokenBudgetPolicy`, and
         `CostPerTokenPolicy`) across pre-flight HTTP `429` enforcement, post-flight
         token reconciliation, `alertThreshold` deduplication, `dryRun` shadow mode,
         Pacific-Time window resets, and Cymbal Portal HTTP endpoints.
Architecture/Context: Runs hermetically offline against `LocalFileLedgerStore` and
         `create_portal_app` without requiring GCP credentials.
Dependencies/Side Effects: Uses `pytest` `tmp_path` and `monkeypatch` fixtures.
Author: Pradeep Singh
License: Apache-2.0
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from common.ledger_store import LocalFileLedgerStore
from common.models import TokenUsageSummary
from common.token_management import TokenManagementEngine
from services.cymbal_portal import main as portal_main


def test_aggregation_order_shared_pools_and_undefined_model_pool(tmp_path: Path) -> None:
    """
    Summary:
        Verifies `TokenAggregationPolicy` first-match-wins evaluation order, shared
        `TokenPool` group memberships (`Tier1-Support` + `Treasury-Ops`), catch-all
        `Default` fallback, and `"undefined"` pool handling for unconfigured models.

    Parameters:
        tmp_path (Path): Temporary directory for local ledger state.

    Return Value:
        None.

    Exceptions/Errors:
        AssertionError: If rule matching or pool resolution deviates from spec.
    """
    store = LocalFileLedgerStore(tmp_path / "tm_rules.json")
    engine = TokenManagementEngine(store)

    # Header rule #1 (`Burst-Sandbox`) precedes SPIFFE agent rule #3 (`Tier1-Support`).
    assert (
        engine.classify_request(
            agent_id="tier1-support-agent",
            headers={"X-Token-Group": "Burst-Sandbox"},
        )
        == "Burst-Sandbox"
    )

    # Both `Tier1-Support` (SPIFFE) and `Treasury-Ops` (header) share `cymbal-operations-shared-pool`.
    g_t1 = engine.classify_request(agent_id="tier1-support-agent")
    g_ops = engine.classify_request(
        agent_id="tier1-support-agent",
        headers={"X-Token-Group": "Treasury-Ops"},
    )
    assert g_t1 == "Tier1-Support"
    assert g_ops == "Treasury-Ops"
    pool_t1 = engine.resolve_pool(g_t1)
    pool_ops = engine.resolve_pool(g_ops)
    assert pool_t1 is not None and pool_ops is not None
    assert pool_t1.pool_id == "cymbal-operations-shared-pool"
    assert pool_ops.pool_id == "cymbal-operations-shared-pool"

    # Unrecognized external endpoint falls through to catch-all `Default`.
    assert (
        engine.classify_request(
            agent_id="custom-worker",
            model_endpoint="api.anthropic.com",
        )
        == "Default"
    )

    # `cymbal-burst-sandbox-pool` restricts enforcement to `gemini-2.5-flash`; other models -> None (`undefined`).
    assert engine.resolve_pool("Burst-Sandbox", model="claude-3-5-sonnet") is None


def test_two_phase_strict_budget_alerts_429_and_window_reset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Summary:
        Exercises two-phase enforcement on `cymbal-burst-sandbox-pool` (`maxTokens=1200`,
        `alertThreshold=0.5`, `dryRun=False`):
          1. Crossing 50% emits `STATUS_ALERT_THRESHOLD_REACHED` once.
          2. Zero-token turns (e.g., Ingress Model Armor blocks) preserve cumulative `pool_used_tokens`.
          3. Crossing 100% emits `STATUS_FULL_THRESHOLD_REACHED`.
          4. Subsequent pre-flight check rejects with HTTP `429` (`blocked_requests=1`).
          5. Advancing past midnight Pacific Time resets pool quota to `0`.

    Parameters:
        tmp_path (Path): Temporary directory for local ledger state.
        monkeypatch (pytest.MonkeyPatch): Environment variable override fixture.

    Return Value:
        None.

    Exceptions/Errors:
        AssertionError: If alert deduplication, HTTP 429, or PT reset fails.
    """
    monkeypatch.setenv("TM_SANDBOX_MAX_TOKENS", "1200")
    store = LocalFileLedgerStore(tmp_path / "tm_strict.json")
    engine = TokenManagementEngine(store)
    headers = {"X-Token-Group": "Burst-Sandbox"}
    day1 = datetime(2026, 11, 13, 18, 0, tzinfo=UTC)

    # Turn 1: 700 tokens (58.3% >= 50% threshold) -> triggers alert once.
    pf1 = engine.check_preflight_budget("tier1-support-agent", headers=headers, now=day1)
    assert pf1.pre_flight_allowed is True
    assert pf1.http_status == 200
    rec1 = engine.reconcile_postflight_usage(
        "tier1-support-agent",
        TokenUsageSummary(input_tokens=500, output_tokens=200, total_tokens=700),
        headers=headers,
        now=day1,
    )
    assert rec1.alert_triggered == "STATUS_ALERT_THRESHOLD_REACHED"
    assert rec1.pool_used_tokens == 700

    # Turn 2: +200 tokens (900 / 1200) -> alert already emitted in window, so None.
    rec2 = engine.reconcile_postflight_usage(
        "tier1-support-agent",
        TokenUsageSummary(input_tokens=150, output_tokens=50, total_tokens=200),
        headers=headers,
        now=day1,
    )
    assert rec2.alert_triggered is None
    assert rec2.pool_used_tokens == 900

    # Zero-token pre-LLM block preserves existing 900 pool_used_tokens.
    rec_zero = engine.reconcile_postflight_usage(
        "tier1-support-agent",
        TokenUsageSummary(total_tokens=0),
        headers=headers,
        now=day1,
    )
    assert rec_zero.pool_used_tokens == 900

    # Turn 3: +400 tokens (1300 / 1200) -> admitted pre-flight at 900, exhausts pool post-flight.
    pf3 = engine.check_preflight_budget("tier1-support-agent", headers=headers, now=day1)
    assert pf3.pre_flight_allowed is True
    rec3 = engine.reconcile_postflight_usage(
        "tier1-support-agent",
        TokenUsageSummary(input_tokens=300, output_tokens=100, total_tokens=400),
        headers=headers,
        now=day1,
    )
    assert rec3.alert_triggered == "STATUS_FULL_THRESHOLD_REACHED"
    assert rec3.pool_used_tokens == 1300

    # Turn 4: Pre-flight rejects immediately with HTTP 429 Too Many Requests!
    pf4 = engine.check_preflight_budget("tier1-support-agent", headers=headers, now=day1)
    assert pf4.pre_flight_allowed is False
    assert pf4.http_status == 429

    overview = engine.get_overview(now=day1)
    sandbox_pool = next(p for p in overview.pools if p.pool_id == "cymbal-burst-sandbox-pool")
    assert sandbox_pool.blocked_requests == 1
    assert sandbox_pool.alert_status == "STATUS_FULL_THRESHOLD_REACHED"

    # Next Pacific calendar day (2026-11-14 09:00 UTC == 01:00 PST) -> auto-resets to 0!
    day2 = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)
    pf_next_day = engine.check_preflight_budget("tier1-support-agent", headers=headers, now=day2)
    assert pf_next_day.pre_flight_allowed is True
    assert pf_next_day.pool_used_tokens == 0


def test_dry_run_pool_and_cost_per_token_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Summary:
        Verifies `dryRun: true` on `cymbal-default-audit-pool` never blocks requests
        with HTTP `429` even past 100% quota, and validates `CostPerTokenPolicy`
        custom USD rates and percentage discounts.

    Parameters:
        tmp_path (Path): Temporary directory fixture.
        monkeypatch (pytest.MonkeyPatch): Environment override fixture.

    Return Value:
        None.

    Exceptions/Errors:
        AssertionError: If dry-run blocks traffic or cost math is inaccurate.
    """
    monkeypatch.setenv("TM_DEFAULT_MAX_TOKENS", "500")
    store = LocalFileLedgerStore(tmp_path / "tm_dryrun.json")
    engine = TokenManagementEngine(store)

    # Custom per-token rates on gemini-2.5-flash: 1000*0.15e-6 + 500*0.60e-6 + 400*0.0375e-6 = 0.000465
    flash_cost = engine.calculate_cost_usd(
        input_tokens=1000,
        output_tokens=500,
        cached_tokens=400,
        model="gemini-2.5-flash",
    )
    assert flash_cost == pytest.approx(0.000465, rel=1e-6)

    # 20% discount on gemini-2.5-pro: (1000*1.25e-6 + 100*10e-6) * 0.80 = 0.0018
    pro_cost = engine.calculate_cost_usd(
        input_tokens=1000,
        output_tokens=100,
        cached_tokens=0,
        model="gemini-2.5-pro",
    )
    assert pro_cost == pytest.approx(0.0018, rel=1e-6)

    # Exhaust `Default` dry-run pool (600 > 500 maxTokens) and verify next request is STILL allowed.
    headers = {"X-Token-Group": "Default"}
    engine.reconcile_postflight_usage(
        "tier1-support-agent",
        TokenUsageSummary(input_tokens=400, output_tokens=200, total_tokens=600),
        headers=headers,
    )
    pf_after = engine.check_preflight_budget("tier1-support-agent", headers=headers)
    assert pf_after.dry_run is True
    assert pf_after.pre_flight_allowed is True
    assert pf_after.http_status == 200


@pytest.mark.asyncio
async def test_portal_chat_token_telemetry_429_and_reset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Summary:
        Tests `POST /api/agents/tier1/chat` end-to-end with a mocked local ADK turn
        returning `token_usage`, verifying pool exhaustion -> `TOKEN_BUDGET_EXCEEDED_429`
        and recovery after `POST /api/admin/reset`.

    Parameters:
        tmp_path (Path): Temporary ledger file fixture.
        monkeypatch (pytest.MonkeyPatch): Monkeypatch fixture.

    Return Value:
        None.

    Exceptions/Errors:
        AssertionError: If portal 429 enforcement or ledger reset fails.
    """
    monkeypatch.setenv("TM_SANDBOX_MAX_TOKENS", "600")
    monkeypatch.setenv("AGENT_INVOCATION_MODE", "local")

    async def fake_local_agent(
        agent_role: str,
        message: str,
        session_id: str,
    ) -> dict[str, Any]:
        return {
            "reply": f"Handled by {agent_role}: {message}",
            "tool_trace": [],
            "gateway_verdict": "ALLOWED",
            "http_status": 200,
            "session_id": session_id,
            "token_usage": {
                "input_tokens": 450,
                "output_tokens": 200,
                "cached_tokens": 0,
                "total_tokens": 650,
                "llm_calls": 1,
                "model": "gemini-2.5-flash",
            },
        }

    monkeypatch.setattr(portal_main, "_invoke_local_adk_agent", fake_local_agent)
    store = LocalFileLedgerStore(tmp_path / "portal_tm.json")
    app = portal_main.create_portal_app(store_provider=lambda: store)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://portal.test") as client:
        # Request 1 admitted -> consumes 650 / 600 tokens in Burst-Sandbox pool.
        r1 = await client.post(
            "/api/agents/tier1/chat",
            headers={"X-Token-Group": "Burst-Sandbox"},
            json={"message": "Check CUST-101 balance."},
        )
        assert r1.status_code == 200
        b1 = r1.json()
        assert b1["gateway_verdict"] == "ALLOWED"
        assert b1["token_telemetry"]["pool_id"] == "cymbal-burst-sandbox-pool"
        assert b1["token_telemetry"]["pool_used_tokens"] == 650

        # Request 2 rejected pre-flight with HTTP 429 verdict (`TOKEN_BUDGET_EXCEEDED_429`)!
        r2 = await client.post(
            "/api/agents/tier1/chat",
            headers={"X-Token-Group": "Burst-Sandbox"},
            json={"message": "Check CUST-102 balance."},
        )
        assert r2.status_code == 200
        assert r2.headers.get("X-Token-Management-Status") == "429"
        b2 = r2.json()
        assert b2["gateway_verdict"] == "TOKEN_BUDGET_EXCEEDED_429"
        assert b2["http_status"] == 429

        # Meanwhile, normal Tier-1 SPIFFE pool (`cymbal-operations-shared-pool`) is completely unaffected!
        r_normal = await client.post(
            "/api/agents/tier1/chat",
            json={"message": "Check CUST-101 balance on normal SPIFFE pool."},
        )
        assert r_normal.json()["gateway_verdict"] == "ALLOWED"
        assert r_normal.json()["token_telemetry"]["pool_id"] == "cymbal-operations-shared-pool"

        # Resetting demo ledger clears `cymbal-burst-sandbox-pool` back to 0!
        await client.post("/api/admin/reset")
        r3 = await client.post(
            "/api/agents/tier1/chat",
            headers={"X-Token-Group": "Burst-Sandbox"},
            json={"message": "Check CUST-101 after reset."},
        )
        assert r3.json()["gateway_verdict"] == "ALLOWED"
