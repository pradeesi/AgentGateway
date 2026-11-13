"""
Purpose: Implements the three-policy Google Cloud Agent Gateway Token Management
         specification (`TokenAggregationPolicy`, `TokenBudgetPolicy`, and
         `CostPerTokenPolicy`) with two-phase enforcement (`Pre-flight budget check`
         and `Token reconciliation & telemetry`).
Architecture/Context: Mirrors the authoritative `networksecurity.googleapis.com/v1alpha1`
         Token Management architecture (`gateway/token_management/*.yaml.tmpl`),
         evaluating ordered first-match-wins aggregation rules, shared token pools
         with Pacific-Time `DAILY` / UTC `HOURLY` resets, strict HTTP `429 Too Many
         Requests` vs `dryRun` shadow modes, deduplicated `token_management_alert_logs`
         warnings (`STATUS_ALERT_THRESHOLD_REACHED` / `STATUS_FULL_THRESHOLD_REACHED`),
         and per-model USD cost overrides (`costPerToken` and `costDiscount`).
Dependencies/Side Effects: Persists shared pool counters through `common.ledger_store`
         (Cloud Firestore in GCP or JSON locally) and emits structured JSON logs
         matching Cloud Logging `token_management_alert_logs`.
Author: Pradeep Singh
License: Apache-2.0
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from common.ledger_store import LedgerStore
from common.logging_utils import configure_structured_logging, log_structured
from common.models import (
    AuditEvent,
    TokenEvaluationReceipt,
    TokenManagementOverview,
    TokenPoolStatus,
    TokenUsageSummary,
)

logger = configure_structured_logging("cymbal.token_management")

PACIFIC_TZ = ZoneInfo("America/Los_Angeles")

# Baseline public list prices per single token in USD before `CostPerTokenPolicy` overrides.
DEFAULT_MODEL_RATES_USD: dict[str, tuple[float, float, float]] = {
    "gemini-2.5-flash": (0.00000030, 0.00000250, 0.000000075),
    "gemini-2.5-pro": (0.00000125, 0.00001000, 0.0000003125),
}


@dataclass(frozen=True)
class AggregationRuleSpec:
    """
    Summary:
        Immutable runtime representation of a single `TokenAggregationRule` inside
        `TokenAggregationPolicy`.

    Parameters:
        token_group (str): Assigned logical token group when this rule matches.
        header_matches (dict[str, str]): Case-insensitive HTTP header exact matchers.
        agents (tuple[str, ...]): Exact SPIFFE IDs, Agent URNs, or role aliases.
        applications (tuple[str, ...]): Exact App Hub application resource URIs.
        model_endpoints (tuple[str, ...]): Case-insensitive model hostnames.
        description (str): Human-readable summary surfaced in the portal UI.

    Return Value:
        Immutable `AggregationRuleSpec` instance.

    Exceptions/Errors:
        None.
    """

    token_group: str
    header_matches: dict[str, str] = field(default_factory=dict)
    agents: tuple[str, ...] = ()
    applications: tuple[str, ...] = ()
    model_endpoints: tuple[str, ...] = ()
    description: str = ""

    @property
    def is_catch_all(self) -> bool:
        """Returns `True` when no source or destination matchers are specified."""
        return (
            not self.header_matches
            and not self.agents
            and not self.applications
            and not self.model_endpoints
        )


@dataclass(frozen=True)
class TokenPoolSpec:
    """
    Summary:
        Immutable runtime representation of a single `TokenPool` inside
        `TokenBudgetPolicy`.

    Parameters:
        pool_id (str): Unique token pool identifier.
        token_group_memberships (tuple[str, ...]): Token groups sharing this pool.
        max_tokens (int): Maximum raw tokens allowed per reset window.
        reset_period (Literal["DAILY", "HOURLY"]): Window reset schedule.
        alert_threshold (float): Fraction (`0.0`..`1.0`) triggering warning alerts.
        dry_run (bool): When `True`, evaluates without returning HTTP `429`.
        target_models (tuple[str, ...]): Optional model filter (`()` applies to all).

    Return Value:
        Immutable `TokenPoolSpec` instance.

    Exceptions/Errors:
        None.
    """

    pool_id: str
    token_group_memberships: tuple[str, ...]
    max_tokens: int
    reset_period: Literal["DAILY", "HOURLY"] = "DAILY"
    alert_threshold: float = 0.8
    dry_run: bool = False
    target_models: tuple[str, ...] = ()


@dataclass(frozen=True)
class CostOverrideSpec:
    """
    Summary:
        Immutable runtime representation of a per-model pricing override inside
        `CostPerTokenPolicy`.

    Parameters:
        model (str): Model identifier (e.g., `"gemini-2.5-flash"`).
        model_provider_endpoints (tuple[str, ...]): Applicable model hostnames.
        cost_discount (float | None): Fractional discount (`0.0`..`1.0`) off list price.
        input_token_cost (float | None): Custom USD rate per input token.
        output_token_cost (float | None): Custom USD rate per output token.
        cached_token_cost (float | None): Custom USD rate per cached token.

    Return Value:
        Immutable `CostOverrideSpec` instance.

    Exceptions/Errors:
        None.
    """

    model: str
    model_provider_endpoints: tuple[str, ...] = ("aiplatform.googleapis.com",)
    cost_discount: float | None = None
    input_token_cost: float | None = None
    output_token_cost: float | None = None
    cached_token_cost: float | None = None


def _env_int(name: str, default: int) -> int:
    """
    Summary:
        Defensively parses a positive integer environment variable, falling back to
        `default` when unset or invalid.

    Parameters:
        name (str): Environment variable key.
        default (int): Fallback positive integer value.

    Return Value:
        int: Validated positive integer (>= 1).

    Exceptions/Errors:
        None.
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        parsed = int(raw)
        return parsed if parsed >= 1 else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    """
    Summary:
        Defensively parses a non-negative float environment variable, falling back to
        `default` when unset or invalid.

    Parameters:
        name (str): Environment variable key.
        default (float): Fallback non-negative float value.

    Return Value:
        float: Validated non-negative float.

    Exceptions/Errors:
        None.
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        parsed = float(raw)
        return parsed if parsed >= 0.0 and math.isfinite(parsed) else default
    except ValueError:
        return default


def compute_window_key(
    reset_period: Literal["DAILY", "HOURLY"],
    now: datetime | None = None,
) -> str:
    """
    Summary:
        Computes the active quota calendar window identifier matching Google Cloud
        Token Management reset semantics (`DAILY` resets at midnight Pacific Time;
        `HOURLY` resets at the top of every UTC hour).

    Parameters:
        reset_period (Literal["DAILY", "HOURLY"]): Configured pool reset cadence.
        now (datetime | None): Optional timestamp override for deterministic tests.

    Return Value:
        str: Calendar window key (e.g., `"PT-2026-11-13"` or `"UTC-2026-11-13T09"`).

    Exceptions/Errors:
        None.
    """
    ts = now or datetime.now(UTC)
    if reset_period == "HOURLY":
        utc_ts = ts.astimezone(UTC)
        return utc_ts.strftime("UTC-%Y-%m-%dT%H")
    pt_ts = ts.astimezone(PACIFIC_TZ)
    return pt_ts.strftime("PT-%Y-%m-%d")


def estimate_fallback_tokens(prompt_text: str, reply_text: str, tool_events: int = 0) -> TokenUsageSummary:
    """
    Summary:
        Computes a conservative token usage estimate (~4 chars/token plus system
        instruction and tool-call framing) only when an upstream agent container did
        not return explicit `usage_metadata` from Vertex AI Gemini.

    Parameters:
        prompt_text (str): Inbound operator prompt.
        reply_text (str): Assistant response text.
        tool_events (int): Count of tool calls/responses in `tool_trace`.

    Return Value:
        TokenUsageSummary: Estimated input, output, cached, and total token counts.

    Exceptions/Errors:
        None.
    """
    # System prompt + 8 banking tool declarations carry ~320 baseline input tokens per turn.
    input_est = max(1, 320 + math.ceil(len(prompt_text or "") / 4) + (tool_events * 65))
    output_est = max(1, math.ceil(len(reply_text or "") / 4) + (tool_events * 30))
    llm_calls = 2 if tool_events > 0 else 1
    model_name = os.getenv("MODEL_NAME", "gemini-2.5-flash").strip() or "gemini-2.5-flash"
    return TokenUsageSummary(
        input_tokens=input_est,
        output_tokens=output_est,
        cached_tokens=0,
        total_tokens=input_est + output_est,
        llm_calls=llm_calls,
        model=model_name,
        estimated_cost_usd=0.0,
    )


class TokenManagementEngine:
    """
    Summary:
        Evaluates Cymbal Bank's `TokenAggregationPolicy`, `TokenBudgetPolicy`, and
        `CostPerTokenPolicy` across both phases of LLM traffic governance:
          1. Pre-flight budget check (request path -> `HTTP 429` if strict & exhausted)
          2. Post-flight token reconciliation, threshold alerting, and USD cost tracking

    Parameters:
        store (LedgerStore): Backing Firestore or local JSON repository storing shared
            pool token counters across serverless revisions.

    Return Value:
        Configured `TokenManagementEngine` instance.

    Exceptions/Errors:
        None.
    """

    def __init__(self, store: LedgerStore) -> None:
        self._store = store
        prefix = os.getenv("PREFIX", "cbgw").strip() or "cbgw"
        project_id = os.getenv("PROJECT_ID", "local-dev").strip() or "local-dev"
        region = os.getenv("REGION", "us-central1").strip() or "us-central1"
        egress_gw = os.getenv("EGRESS_GATEWAY_NAME", f"{prefix}-egress-gw").strip()
        model_name = os.getenv("MODEL_NAME", "gemini-2.5-flash").strip() or "gemini-2.5-flash"

        self.policy_mode = (
            os.getenv("TOKEN_MANAGEMENT_MODE", "HYBRID_GATEWAY_PREVIEW").strip()
            or "HYBRID_GATEWAY_PREVIEW"
        )
        self.target_gateway = (
            f"//networkservices.googleapis.com/projects/{project_id}"
            f"/locations/{region}/agentGateways/{egress_gw}"
        )
        self.aggregation_policy_id = os.getenv(
            "TOKEN_AGGREGATION_POLICY_ID", f"{prefix}-token-aggregation-policy"
        )
        self.budget_policy_id = os.getenv(
            "TOKEN_BUDGET_POLICY_ID", f"{prefix}-token-budget-policy"
        )
        self.cost_policy_id = os.getenv(
            "COST_PER_TOKEN_POLICY_ID", f"{prefix}-cost-per-token-policy"
        )

        tier1_spiffe = os.getenv(
            "TIER1_SPIFFE_ID",
            "spiffe://agents.global.system.id.goog/.../tier1-support-agent",
        ).strip()
        spec_spiffe = os.getenv(
            "SPECIALIST_SPIFFE_ID",
            "spiffe://agents.global.system.id.goog/.../risk-specialist-agent",
        ).strip()

        # Ordered rules matching `gateway/token_management/token-aggregation-policy.yaml.tmpl`.
        self.rules: tuple[AggregationRuleSpec, ...] = (
            AggregationRuleSpec(
                token_group="Burst-Sandbox",
                header_matches={"x-token-group": "Burst-Sandbox"},
                description="Header X-Token-Group == 'Burst-Sandbox'",
            ),
            AggregationRuleSpec(
                token_group="Treasury-Ops",
                header_matches={"x-token-group": "Treasury-Ops"},
                description="Header X-Token-Group == 'Treasury-Ops'",
            ),
            AggregationRuleSpec(
                token_group="Tier1-Support",
                agents=(tier1_spiffe, "tier1", "tier1-support-agent"),
                model_endpoints=("aiplatform.googleapis.com",),
                description="SPIFFE tier1-support-agent -> aiplatform.googleapis.com",
            ),
            AggregationRuleSpec(
                token_group="Risk-Specialist",
                agents=(spec_spiffe, "specialist", "risk-specialist-agent"),
                model_endpoints=("aiplatform.googleapis.com",),
                description="SPIFFE risk-specialist-agent -> aiplatform.googleapis.com",
            ),
            AggregationRuleSpec(
                token_group="Default",
                description="Catch-all rule (match: {}) -> Default group",
            ),
        )

        # Shared token pools matching `gateway/token_management/token-budget-policy.yaml.tmpl`.
        self.pools: tuple[TokenPoolSpec, ...] = (
            TokenPoolSpec(
                pool_id="cymbal-burst-sandbox-pool",
                token_group_memberships=("Burst-Sandbox",),
                max_tokens=_env_int("TM_SANDBOX_MAX_TOKENS", 6000),
                reset_period="DAILY",
                alert_threshold=0.5,
                dry_run=False,
                target_models=(model_name,),
            ),
            TokenPoolSpec(
                pool_id="cymbal-operations-shared-pool",
                token_group_memberships=("Tier1-Support", "Treasury-Ops"),
                max_tokens=_env_int("TM_SHARED_OPS_MAX_TOKENS", 50000),
                reset_period="DAILY",
                alert_threshold=0.8,
                dry_run=False,
            ),
            TokenPoolSpec(
                pool_id="cymbal-risk-specialist-pool",
                token_group_memberships=("Risk-Specialist",),
                max_tokens=_env_int("TM_SPECIALIST_MAX_TOKENS", 100000),
                reset_period="DAILY",
                alert_threshold=0.8,
                dry_run=False,
            ),
            TokenPoolSpec(
                pool_id="cymbal-default-audit-pool",
                token_group_memberships=("Default",),
                max_tokens=_env_int("TM_DEFAULT_MAX_TOKENS", 25000),
                reset_period="DAILY",
                alert_threshold=0.6,
                dry_run=True,
            ),
        )

        # Enterprise FinOps pricing overrides matching `cost-per-token-policy.yaml.tmpl`.
        self.cost_overrides: tuple[CostOverrideSpec, ...] = (
            CostOverrideSpec(
                model=model_name,
                model_provider_endpoints=("aiplatform.googleapis.com",),
                input_token_cost=_env_float("TM_INPUT_TOKEN_COST_USD", 0.00000015),
                output_token_cost=_env_float("TM_OUTPUT_TOKEN_COST_USD", 0.00000060),
                cached_token_cost=_env_float("TM_CACHED_TOKEN_COST_USD", 0.0000000375),
            ),
            CostOverrideSpec(
                model="gemini-2.5-pro",
                model_provider_endpoints=("aiplatform.googleapis.com",),
                cost_discount=min(1.0, _env_float("TM_PRO_COST_DISCOUNT", 0.20)),
            ),
        )

    def classify_request(
        self,
        agent_id: str,
        headers: dict[str, str] | None = None,
        model_endpoint: str = "aiplatform.googleapis.com",
        application_id: str = "",
    ) -> str:
        """
        Summary:
            Evaluates `TokenAggregationPolicy` rules in strict declaration order
            (first matching rule wins), returning the assigned `tokenGroup` or
            `"unmatched"` if no catch-all rule matches.

        Parameters:
            agent_id (str): Calling agent SPIFFE principal or logical role alias.
            headers (dict[str, str] | None): Outbound/inbound HTTP request headers.
            model_endpoint (str): Target model provider endpoint hostname.
            application_id (str): Optional App Hub application URI.

        Return Value:
            str: Assigned `tokenGroup` name (e.g., `"Tier1-Support"`, `"Burst-Sandbox"`,
            `"Default"`).

        Exceptions/Errors:
            None.
        """
        normalized_headers = {
            str(k).strip().lower(): str(v).strip()
            for k, v in (headers or {}).items()
            if k and v is not None
        }
        # Support explicitly testing catch-all / `Default` dry-run pool from UI or CLI.
        if normalized_headers.get("x-token-group", "").lower() == "default":
            return "Default"

        endpoint_lower = (model_endpoint or "").strip().lower()
        agent_clean = (agent_id or "").strip()
        app_clean = (application_id or "").strip()

        for rule in self.rules:
            if rule.is_catch_all:
                return rule.token_group

            if rule.header_matches:
                headers_ok = all(
                    normalized_headers.get(hk.lower(), "").lower() == hv.lower()
                    for hk, hv in rule.header_matches.items()
                )
                if not headers_ok:
                    continue

            if rule.agents and agent_clean not in rule.agents:
                continue

            if rule.applications and app_clean not in rule.applications:
                continue

            if rule.model_endpoints and endpoint_lower not in {
                ep.lower() for ep in rule.model_endpoints
            }:
                continue

            return rule.token_group

        return "unmatched"

    def resolve_pool(
        self,
        token_group: str,
        model: str | None = None,
    ) -> TokenPoolSpec | None:
        """
        Summary:
            Resolves the `TokenPoolSpec` whose `tokenGroupMemberships` includes
            `token_group`. Per `TokenBudgetPolicy` semantics, if a pool restricts
            `models` and the requested `model` is outside that list, returns `None`
            (recorded as pool `"undefined"` without blocking).

        Parameters:
            token_group (str): Classified `tokenGroup` from `TokenAggregationPolicy`.
            model (str | None): Requested LLM model name.

        Return Value:
            TokenPoolSpec | None: Matching active pool or `None` (`"undefined"`).

        Exceptions/Errors:
            None.
        """
        target_model = (model or os.getenv("MODEL_NAME", "gemini-2.5-flash")).strip()
        for pool in self.pools:
            if token_group in pool.token_group_memberships:
                if pool.target_models and target_model not in pool.target_models:
                    return None
                return pool
        return None

    def calculate_cost_usd(
        self,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int = 0,
        model: str | None = None,
        model_endpoint: str = "aiplatform.googleapis.com",
    ) -> float:
        """
        Summary:
            Computes estimated USD token cost (`gcp.token_management.token.estimated_cost`)
            applying any matching `CostPerTokenPolicy` custom per-token rate card or
            percentage discount override.

        Parameters:
            input_tokens (int): Prompt/input tokens consumed.
            output_tokens (int): Completion/output tokens generated.
            cached_tokens (int): Cached context tokens reused.
            model (str | None): Foundation model identifier.
            model_endpoint (str): Model hosting provider hostname.

        Return Value:
            float: Estimated USD cost rounded to 8 decimal places.

        Exceptions/Errors:
            None.
        """
        resolved_model = (model or os.getenv("MODEL_NAME", "gemini-2.5-flash")).strip()
        endpoint_lower = (model_endpoint or "aiplatform.googleapis.com").strip().lower()
        base_in, base_out, base_cached = DEFAULT_MODEL_RATES_USD.get(
            resolved_model, DEFAULT_MODEL_RATES_USD["gemini-2.5-flash"]
        )

        for override in self.cost_overrides:
            if override.model == resolved_model and endpoint_lower in {
                ep.lower() for ep in override.model_provider_endpoints
            }:
                if (
                    override.input_token_cost is not None
                    or override.output_token_cost is not None
                    or override.cached_token_cost is not None
                ):
                    in_rate = (
                        override.input_token_cost
                        if override.input_token_cost is not None
                        else base_in
                    )
                    out_rate = (
                        override.output_token_cost
                        if override.output_token_cost is not None
                        else base_out
                    )
                    cache_rate = (
                        override.cached_token_cost
                        if override.cached_token_cost is not None
                        else base_cached
                    )
                    cost = (
                        max(0, input_tokens) * in_rate
                        + max(0, output_tokens) * out_rate
                        + max(0, cached_tokens) * cache_rate
                    )
                    return round(cost, 8)
                if override.cost_discount is not None:
                    multiplier = max(0.0, 1.0 - override.cost_discount)
                    cost = (
                        max(0, input_tokens) * base_in
                        + max(0, output_tokens) * base_out
                        + max(0, cached_tokens) * base_cached
                    ) * multiplier
                    return round(cost, 8)

        default_cost = (
            max(0, input_tokens) * base_in
            + max(0, output_tokens) * base_out
            + max(0, cached_tokens) * base_cached
        )
        return round(default_cost, 8)

    def _get_normalized_pool_counter(
        self,
        pool: TokenPoolSpec,
        raw_counters: dict[str, dict[str, Any]],
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """
        Summary:
            Returns the counter dictionary for `pool.pool_id` scoped to the current
            calendar reset window (`PT-YYYY-MM-DD` or `UTC-YYYY-MM-DDTHH`), resetting
            counters automatically if the saved `window_key` is from an older window.

        Parameters:
            pool (TokenPoolSpec): Target token pool definition.
            raw_counters (dict[str, dict[str, Any]]): Persisted counters from `LedgerStore`.
            now (datetime | None): Optional clock override.

        Return Value:
            dict[str, Any]: Active window counter dictionary.

        Exceptions/Errors:
            None.
        """
        current_window = compute_window_key(pool.reset_period, now=now)
        existing = raw_counters.get(pool.pool_id) or {}
        if existing.get("window_key") != current_window:
            return {
                "pool_id": pool.pool_id,
                "window_key": current_window,
                "used_tokens": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cached_tokens": 0,
                "estimated_cost_usd": 0.0,
                "blocked_requests": 0,
                "alert_status": "OK",
            }
        return {
            "pool_id": pool.pool_id,
            "window_key": current_window,
            "used_tokens": int(existing.get("used_tokens", 0) or 0),
            "input_tokens": int(existing.get("input_tokens", 0) or 0),
            "output_tokens": int(existing.get("output_tokens", 0) or 0),
            "cached_tokens": int(existing.get("cached_tokens", 0) or 0),
            "estimated_cost_usd": float(existing.get("estimated_cost_usd", 0.0) or 0.0),
            "blocked_requests": int(existing.get("blocked_requests", 0) or 0),
            "alert_status": str(existing.get("alert_status", "OK") or "OK"),
        }

    def check_preflight_budget(
        self,
        agent_id: str,
        headers: dict[str, str] | None = None,
        model: str | None = None,
        model_endpoint: str = "aiplatform.googleapis.com",
        now: datetime | None = None,
    ) -> TokenEvaluationReceipt:
        """
        Summary:
            Executes **Phase 1 — Pre-flight Budget Check (Request Path)**:
              1. Matches the request against `TokenAggregationPolicy` -> `token_group`.
              2. Resolves the `TokenPool` in `TokenBudgetPolicy`.
              3. Rejects immediately with `http_status=429` (`Too Many Requests`) when
                 `used_tokens >= max_tokens` and `pool.dry_run` is `False`.

        Parameters:
            agent_id (str): Calling SPIFFE principal or logical agent role.
            headers (dict[str, str] | None): Request headers (`X-Token-Group`, etc.).
            model (str | None): Target LLM model name.
            model_endpoint (str): Target model provider endpoint hostname.
            now (datetime | None): Optional timestamp override for tests.

        Return Value:
            TokenEvaluationReceipt: Pre-flight verdict (`pre_flight_allowed=True/False`).

        Exceptions/Errors:
            None.
        """
        token_group = self.classify_request(
            agent_id=agent_id,
            headers=headers,
            model_endpoint=model_endpoint,
        )
        pool = self.resolve_pool(token_group=token_group, model=model)
        if pool is None:
            return TokenEvaluationReceipt(
                policy_mode=self.policy_mode,
                token_group=token_group,
                pool_id="undefined",
                dry_run=True,
                pre_flight_allowed=True,
                http_status=200,
            )

        counters = self._store.get_token_pool_counters()
        cur = self._get_normalized_pool_counter(pool, counters, now=now)
        used = int(cur["used_tokens"])

        if used >= pool.max_tokens and not pool.dry_run:
            cur["blocked_requests"] = int(cur["blocked_requests"]) + 1
            cur["alert_status"] = "STATUS_FULL_THRESHOLD_REACHED"
            self._store.upsert_token_pool_counter(pool.pool_id, cur)

            now_iso = (now or datetime.now(UTC)).isoformat()
            short_agent = agent_id.rsplit("/", 1)[-1] if "/" in agent_id else agent_id
            summary_msg = (
                f"HTTP 429 Too Many Requests: TokenPool '{pool.pool_id}' "
                f"(group '{token_group}') exhausted ({used:,} / {pool.max_tokens:,} tokens "
                f"in window {cur['window_key']})."
            )
            self._store.record_audit_event(
                AuditEvent(
                    timestamp=now_iso,
                    layer="TOKEN_MANAGEMENT",
                    verdict="DENY",
                    principal=short_agent or "agent",
                    target=f"{pool.pool_id} ({token_group})",
                    summary=summary_msg,
                )
            )
            log_structured(
                logger,
                logging.WARNING,
                "token_management_preflight_429_rejected",
                status="STATUS_FULL_THRESHOLD_REACHED",
                budget_policy={
                    "name": self.budget_policy_id,
                    "pool_name": pool.pool_id,
                    "configured_max_token_count": pool.max_tokens,
                    "used_token_count": used,
                },
                token_group=token_group,
            )
            return TokenEvaluationReceipt(
                policy_mode=self.policy_mode,
                token_group=token_group,
                pool_id=pool.pool_id,
                dry_run=pool.dry_run,
                pre_flight_allowed=False,
                http_status=429,
                alert_triggered="STATUS_FULL_THRESHOLD_REACHED",
                pool_used_tokens=used,
                pool_max_tokens=pool.max_tokens,
            )

        return TokenEvaluationReceipt(
            policy_mode=self.policy_mode,
            token_group=token_group,
            pool_id=pool.pool_id,
            dry_run=pool.dry_run,
            pre_flight_allowed=True,
            http_status=200,
            pool_used_tokens=used,
            pool_max_tokens=pool.max_tokens,
        )

    def reconcile_postflight_usage(
        self,
        agent_id: str,
        usage: TokenUsageSummary,
        headers: dict[str, str] | None = None,
        model_endpoint: str = "aiplatform.googleapis.com",
        now: datetime | None = None,
    ) -> TokenEvaluationReceipt:
        """
        Summary:
            Executes **Phase 2 — Token Reconciliation & Telemetry (Response Path)**:
              1. Calculates `estimated_cost_usd` under `CostPerTokenPolicy`.
              2. Credits `input`, `output`, and `cached` tokens against `pool.max_tokens`.
              3. Emits deduplicated `STATUS_ALERT_THRESHOLD_REACHED` or
                 `STATUS_FULL_THRESHOLD_REACHED` warnings (`token_management_alert_logs`
                 schema) when crossing `alert_threshold` or `100%` of `maxTokens`.

        Parameters:
            agent_id (str): Calling SPIFFE principal or logical agent role.
            usage (TokenUsageSummary): Extracted Gemini `usage_metadata` counts.
            headers (dict[str, str] | None): Request headers (`X-Token-Group`, etc.).
            model_endpoint (str): Target model provider endpoint hostname.
            now (datetime | None): Optional timestamp override for tests.

        Return Value:
            TokenEvaluationReceipt: Complete reconciled token & FinOps receipt.

        Exceptions/Errors:
            None.
        """
        token_group = self.classify_request(
            agent_id=agent_id,
            headers=headers,
            model_endpoint=model_endpoint,
        )
        total_raw = max(
            int(usage.total_tokens),
            int(usage.input_tokens) + int(usage.output_tokens) + int(usage.cached_tokens),
        )
        cost_usd = self.calculate_cost_usd(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_tokens=usage.cached_tokens,
            model=usage.model,
            model_endpoint=model_endpoint,
        )
        priced_usage = TokenUsageSummary(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_tokens=usage.cached_tokens,
            total_tokens=total_raw,
            llm_calls=usage.llm_calls,
            model=usage.model,
            estimated_cost_usd=cost_usd,
        )

        pool = self.resolve_pool(token_group=token_group, model=usage.model)
        if pool is None:
            return TokenEvaluationReceipt(
                policy_mode=self.policy_mode,
                token_group=token_group,
                pool_id="undefined",
                dry_run=True,
                pre_flight_allowed=True,
                http_status=200,
                usage=priced_usage,
                pool_used_tokens=0,
                pool_max_tokens=0,
            )

        counters = self._store.get_token_pool_counters()
        cur = self._get_normalized_pool_counter(pool, counters, now=now)
        if total_raw <= 0:
            return TokenEvaluationReceipt(
                policy_mode=self.policy_mode,
                token_group=token_group,
                pool_id=pool.pool_id,
                dry_run=pool.dry_run,
                pre_flight_allowed=True,
                http_status=200,
                usage=priced_usage,
                pool_used_tokens=int(cur["used_tokens"]),
                pool_max_tokens=pool.max_tokens,
            )
        prev_status = str(cur.get("alert_status", "OK"))

        new_used = int(cur["used_tokens"]) + total_raw
        cur["used_tokens"] = new_used
        cur["input_tokens"] = int(cur["input_tokens"]) + int(priced_usage.input_tokens)
        cur["output_tokens"] = int(cur["output_tokens"]) + int(priced_usage.output_tokens)
        cur["cached_tokens"] = int(cur["cached_tokens"]) + int(priced_usage.cached_tokens)
        cur["estimated_cost_usd"] = round(float(cur["estimated_cost_usd"]) + cost_usd, 8)

        ratio = new_used / float(pool.max_tokens)
        triggered_alert: str | None = None
        now_iso = (now or datetime.now(UTC)).isoformat()
        short_agent = agent_id.rsplit("/", 1)[-1] if "/" in agent_id else agent_id

        # Check `alertThreshold` transition first, then `100%` full budget transition,
        # matching `TokenAlertManager::HandleStatusReply` deduplication per refill window.
        if (
            0.0 < pool.alert_threshold < 1.0
            and ratio >= pool.alert_threshold
            and prev_status == "OK"
        ):
            cur["alert_status"] = "STATUS_ALERT_THRESHOLD_REACHED"
            triggered_alert = "STATUS_ALERT_THRESHOLD_REACHED"
            self._emit_threshold_alert(
                now_iso=now_iso,
                short_agent=short_agent,
                pool=pool,
                token_group=token_group,
                used_tokens=new_used,
                status_str="STATUS_ALERT_THRESHOLD_REACHED",
            )
            prev_status = "STATUS_ALERT_THRESHOLD_REACHED"

        if new_used >= pool.max_tokens and prev_status != "STATUS_FULL_THRESHOLD_REACHED":
            cur["alert_status"] = "STATUS_FULL_THRESHOLD_REACHED"
            triggered_alert = "STATUS_FULL_THRESHOLD_REACHED"
            self._emit_threshold_alert(
                now_iso=now_iso,
                short_agent=short_agent,
                pool=pool,
                token_group=token_group,
                used_tokens=new_used,
                status_str="STATUS_FULL_THRESHOLD_REACHED",
            )

        self._store.upsert_token_pool_counter(pool.pool_id, cur)

        log_structured(
            logger,
            logging.INFO,
            "gen_ai_client_token_usage_reconciled",
            metric="gen_ai.client.token.usage",
            cost_metric="gcp.token_management.token.estimated_cost",
            group_name=token_group,
            pool_name=pool.pool_id,
            input_tokens=priced_usage.input_tokens,
            output_tokens=priced_usage.output_tokens,
            cached_tokens=priced_usage.cached_tokens,
            total_tokens=priced_usage.total_tokens,
            estimated_cost_usd=cost_usd,
            pool_used_tokens=new_used,
            pool_max_tokens=pool.max_tokens,
            dry_run=pool.dry_run,
        )

        return TokenEvaluationReceipt(
            policy_mode=self.policy_mode,
            token_group=token_group,
            pool_id=pool.pool_id,
            dry_run=pool.dry_run,
            pre_flight_allowed=True,
            http_status=200,
            alert_triggered=triggered_alert,
            usage=priced_usage,
            pool_used_tokens=new_used,
            pool_max_tokens=pool.max_tokens,
        )

    def _emit_threshold_alert(
        self,
        now_iso: str,
        short_agent: str,
        pool: TokenPoolSpec,
        token_group: str,
        used_tokens: int,
        status_str: Literal[
            "STATUS_ALERT_THRESHOLD_REACHED",
            "STATUS_FULL_THRESHOLD_REACHED",
        ],
    ) -> None:
        """
        Summary:
            Emits a Cloud Logging warning structured identically to Google Cloud's
            `token_management_alert_logs` (`budget_policy.{name, pool_name,
            configured_max_token_count, used_token_count}`) and appends a `WARN`
            entry to the Portal Governance Audit Stream.

        Parameters:
            now_iso (str): ISO-8601 UTC timestamp.
            short_agent (str): Abbreviated agent workload principal.
            pool (TokenPoolSpec): Breached token pool specification.
            token_group (str): Matched `TokenAggregationPolicy` group.
            used_tokens (int): Updated pool token count.
            status_str (Literal[...]): Threshold alert status constant.

        Return Value:
            None.

        Exceptions/Errors:
            None.
        """
        pct = round((used_tokens / float(pool.max_tokens)) * 100.0, 1)
        mode_note = (
            "dryRun=true (audit-only, traffic not blocked)"
            if pool.dry_run
            else (
                "subsequent requests will be rejected with HTTP 429"
                if status_str == "STATUS_FULL_THRESHOLD_REACHED"
                else f"crossed {int(pool.alert_threshold * 100)}% warning threshold"
            )
        )
        log_structured(
            logger,
            logging.WARNING,
            "token_management_alert_logs",
            status=status_str,
            budget_policy={
                "name": self.budget_policy_id,
                "pool_name": pool.pool_id,
                "configured_max_token_count": pool.max_tokens,
                "used_token_count": used_tokens,
            },
            token_group=token_group,
        )
        self._store.record_audit_event(
            AuditEvent(
                timestamp=now_iso,
                layer="TOKEN_MANAGEMENT",
                verdict="WARN",
                principal=short_agent or "agent",
                target=f"{pool.pool_id} ({token_group})",
                summary=(
                    f"{status_str}: Pool '{pool.pool_id}' reached {used_tokens:,} / "
                    f"{pool.max_tokens:,} tokens ({pct}%) — {mode_note}."
                ),
            )
        )

    def get_overview(self, now: datetime | None = None) -> TokenManagementOverview:
        """
        Summary:
            Builds a complete `TokenManagementOverview` snapshot combining all three
            configured policies (`TokenAggregationPolicy`, `TokenBudgetPolicy`, and
            `CostPerTokenPolicy`) with live token consumption and FinOps spend.

        Parameters:
            now (datetime | None): Optional timestamp override for tests.

        Return Value:
            TokenManagementOverview: Complete policy & pool telemetry snapshot.

        Exceptions/Errors:
            None.
        """
        raw_counters = self._store.get_token_pool_counters()
        pool_statuses: list[TokenPoolStatus] = []
        total_tokens = 0
        total_cost = 0.0
        total_blocked = 0

        for pool in self.pools:
            cur = self._get_normalized_pool_counter(pool, raw_counters, now=now)
            used = int(cur["used_tokens"])
            remaining = max(0, pool.max_tokens - used)
            ratio = round(used / float(pool.max_tokens), 4)
            cost = round(float(cur["estimated_cost_usd"]), 6)
            blocked = int(cur["blocked_requests"])
            raw_alert = str(cur.get("alert_status", "OK"))
            alert_val: Literal[
                "OK",
                "STATUS_ALERT_THRESHOLD_REACHED",
                "STATUS_FULL_THRESHOLD_REACHED",
            ] = (
                raw_alert  # type: ignore[assignment]
                if raw_alert
                in (
                    "OK",
                    "STATUS_ALERT_THRESHOLD_REACHED",
                    "STATUS_FULL_THRESHOLD_REACHED",
                )
                else "OK"
            )

            pool_statuses.append(
                TokenPoolStatus(
                    pool_id=pool.pool_id,
                    token_group_memberships=list(pool.token_group_memberships),
                    max_tokens=pool.max_tokens,
                    used_tokens=used,
                    remaining_tokens=remaining,
                    utilization_ratio=ratio,
                    reset_period=pool.reset_period,
                    window_key=str(cur["window_key"]),
                    alert_threshold=pool.alert_threshold,
                    dry_run=pool.dry_run,
                    alert_status=alert_val,
                    estimated_cost_usd=cost,
                    blocked_requests=blocked,
                    target_models=list(pool.target_models),
                )
            )
            total_tokens += used
            total_cost += cost
            total_blocked += blocked

        rules_summary = [
            {
                "order": str(idx + 1),
                "token_group": r.token_group,
                "match_criteria": r.description,
            }
            for idx, r in enumerate(self.rules)
        ]

        overrides_summary: list[dict[str, str]] = []
        for ov in self.cost_overrides:
            if ov.cost_discount is not None:
                pricing_desc = f"{int(ov.cost_discount * 100)}% discount off default rate"
            else:
                in_m = round((ov.input_token_cost or 0.0) * 1_000_000, 4)
                out_m = round((ov.output_token_cost or 0.0) * 1_000_000, 4)
                cache_m = round((ov.cached_token_cost or 0.0) * 1_000_000, 4)
                pricing_desc = (
                    f"${in_m}/1M input · ${out_m}/1M output · ${cache_m}/1M cached"
                )
            overrides_summary.append(
                {
                    "model": ov.model,
                    "endpoints": ", ".join(ov.model_provider_endpoints),
                    "pricing": pricing_desc,
                }
            )

        return TokenManagementOverview(
            policy_mode=self.policy_mode,
            target_gateway=self.target_gateway,
            aggregation_policy_id=self.aggregation_policy_id,
            budget_policy_id=self.budget_policy_id,
            cost_policy_id=self.cost_policy_id,
            aggregation_rules=rules_summary,
            pools=pool_statuses,
            cost_overrides=overrides_summary,
            total_tokens_consumed=total_tokens,
            total_estimated_cost_usd=round(total_cost, 6),
            total_blocked_requests=total_blocked,
        )
