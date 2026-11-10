"""
Purpose: Strongly-typed Pydantic v2 domain models for Cymbal Bank accounts,
         transactions, card/wire disputes, operational receipts, and audit events.
Architecture/Context: Shared data contract across Firestore/local persistence,
         FastMCP tool responses, and the Cymbal Bank Operations Portal REST API.
Dependencies/Side Effects: Requires `pydantic>=2.9.0`. Performs strict boundary
         validation on identifiers, monetary amounts, and enum states.
Author: Pradeep Singh
License: Apache-2.0
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CustomerAccount(BaseModel):
    """
    Summary:
        Represents a Cymbal Bank customer profile and deposit/card account summary,
        including synthetic PII fields used to demonstrate Model Armor + Cloud SDP
        in-transit redaction on Agent Gateway.

    Parameters:
        customer_id (str): Unique customer identifier (e.g., `"CUST-101"`).
        full_name (str): Account holder legal name.
        segment (Literal["Premier", "Commercial", "Retail"]): Banking tier.
        email (str): Primary contact email address.
        phone (str): Primary phone number.
        ssn (str): Synthetic US Social Security Number stored in raw ledger.
        card_pan (str): Synthetic 16-digit payment card number stored in raw ledger.
        routing_number (str): Synthetic ABA bank routing number.
        account_number_masked (str): Last-4 masked checking account identifier.
        available_balance_usd (float): Current available account balance in USD.
        account_status (Literal["ACTIVE", "HOLD_REVIEW", "HOLD_FRAUD"]): Operational state.
        hold_reason (str | None): Active hold explanation if placed under hold.
        kyc_verified (bool): Whether customer KYC verification is complete.

    Return Value:
        Validated `CustomerAccount` model instance.

    Exceptions/Errors:
        pydantic.ValidationError: Raised when required fields fail format constraints.
    """

    model_config = ConfigDict(extra="forbid")

    customer_id: str = Field(..., pattern=r"^CUST-\d{3,6}$")
    full_name: str = Field(..., min_length=2, max_length=120)
    segment: Literal["Premier", "Commercial", "Retail"]
    email: str = Field(..., min_length=5, max_length=160)
    phone: str = Field(..., min_length=7, max_length=32)
    ssn: str = Field(..., min_length=9, max_length=16)
    card_pan: str = Field(..., min_length=13, max_length=24)
    routing_number: str = Field(..., min_length=9, max_length=9)
    account_number_masked: str = Field(..., min_length=4, max_length=24)
    available_balance_usd: float = Field(...)
    account_status: Literal["ACTIVE", "HOLD_REVIEW", "HOLD_FRAUD"] = "ACTIVE"
    hold_reason: str | None = None
    kyc_verified: bool = True


class TransactionRecord(BaseModel):
    """
    Summary:
        Represents an immutable or reversible card/wire ledger entry associated with
        a Cymbal Bank customer account.

    Parameters:
        transaction_id (str): Unique transaction identifier (e.g., `"TXN-9001"`).
        customer_id (str): Owning customer ID (`CUST-...`).
        posted_at (str): ISO-8601 UTC timestamp string.
        merchant_or_counterparty (str): Merchant descriptor or wire beneficiary.
        channel (Literal["CARD_POS", "CARD_ECOM", "WIRE_OUT", "ACH_DEBIT", "REFUND_CREDIT"]):
            Payment rail channel.
        amount_usd (float): Positive USD transaction value.
        status (Literal["POSTED", "DISPUTED", "REFUNDED", "REVERSED"]): Settlement status.
        reference_note (str): Short settlement memo.

    Return Value:
        Validated `TransactionRecord` model instance.

    Exceptions/Errors:
        pydantic.ValidationError: Raised when identifiers or amounts are invalid.
    """

    model_config = ConfigDict(extra="forbid")

    transaction_id: str = Field(..., pattern=r"^TXN-\d{4,8}$")
    customer_id: str = Field(..., pattern=r"^CUST-\d{3,6}$")
    posted_at: str = Field(..., min_length=10, max_length=40)
    merchant_or_counterparty: str = Field(..., min_length=2, max_length=140)
    channel: Literal["CARD_POS", "CARD_ECOM", "WIRE_OUT", "ACH_DEBIT", "REFUND_CREDIT"]
    amount_usd: float = Field(..., gt=0.0)
    status: Literal["POSTED", "DISPUTED", "REFUNDED", "REVERSED"]
    reference_note: str = Field(default="", max_length=500)


class DisputeCase(BaseModel):
    """
    Summary:
        Represents an open or resolved customer transaction dispute evaluated by
        Tier-1 Support and Risk & Disputes Specialist agents.

    Parameters:
        dispute_id (str): Unique dispute case ID (e.g., `"DSP-1001"`).
        customer_id (str): Customer who filed the dispute.
        transaction_id (str): Disputed transaction ID.
        opened_at (str): ISO-8601 UTC timestamp when opened.
        disputed_amount_usd (float): Disputed dollar value (> 0).
        reason_code (str): Standardized banking dispute category.
        status (Literal["OPEN", "REFUND_ISSUED", "ESCALATED", "REJECTED"]): Case status.
        fraud_verified (bool): Whether Cymbal Fraud Ops confirmed unauthorized activity.
        fraud_ticket_id (str | None): Verified fraud investigation ticket ID if confirmed.
        customer_statement (str): Narrative submitted by the account holder.
        merchant_evidence_note (str): Raw merchant representment text (may contain
            untrusted external input screened by Model Armor on Agent Gateway).
        refunded_amount_usd (float): Cumulative USD refunded on this case.
        resolution_note (str | None): Audit trail of action taken.

    Return Value:
        Validated `DisputeCase` model instance.

    Exceptions/Errors:
        pydantic.ValidationError: Raised when dispute fields violate constraints.
    """

    model_config = ConfigDict(extra="forbid")

    dispute_id: str = Field(..., pattern=r"^DSP-\d{4,8}$")
    customer_id: str = Field(..., pattern=r"^CUST-\d{3,6}$")
    transaction_id: str = Field(..., pattern=r"^TXN-\d{4,8}$")
    opened_at: str = Field(..., min_length=10, max_length=40)
    disputed_amount_usd: float = Field(..., gt=0.0)
    reason_code: str = Field(..., min_length=3, max_length=64)
    status: Literal["OPEN", "REFUND_ISSUED", "ESCALATED", "REJECTED"] = "OPEN"
    fraud_verified: bool = False
    fraud_ticket_id: str | None = None
    customer_statement: str = Field(..., min_length=5, max_length=1000)
    merchant_evidence_note: str = Field(..., min_length=3, max_length=2000)
    refunded_amount_usd: float = Field(default=0.0, ge=0.0)
    resolution_note: str | None = None


class RefundReceipt(BaseModel):
    """
    Summary:
        Structured confirmation returned after issuing a provisional or final
        dispute credit to a customer's account.

    Parameters:
        receipt_id (str): Generated refund receipt identifier.
        dispute_id (str): Target dispute case ID.
        customer_id (str): Credited customer ID.
        refunded_amount_usd (float): Credited USD amount.
        new_available_balance_usd (float): Updated customer balance after credit.
        dispute_status (str): Updated dispute status (`REFUND_ISSUED`).
        supervisor_approval_code (str | None): Optional supervisor override code.
        processed_at (str): ISO-8601 UTC timestamp.

    Return Value:
        Validated `RefundReceipt` instance.

    Exceptions/Errors:
        pydantic.ValidationError: Raised if amounts or identifiers are invalid.
    """

    model_config = ConfigDict(extra="forbid")

    receipt_id: str
    dispute_id: str
    customer_id: str
    refunded_amount_usd: float = Field(..., gt=0.0)
    new_available_balance_usd: float
    dispute_status: str
    supervisor_approval_code: str | None = None
    processed_at: str


class AccountHoldReceipt(BaseModel):
    """
    Summary:
        Structured confirmation returned when placing a protective compliance or
        fraud hold on a customer account.

    Parameters:
        hold_id (str): Unique hold action identifier.
        customer_id (str): Target customer account ID.
        previous_status (str): Prior account status before hold.
        new_status (Literal["HOLD_REVIEW", "HOLD_FRAUD"]): Applied hold tier.
        reason (str): Recorded compliance/risk justification.
        placed_at (str): ISO-8601 UTC timestamp.

    Return Value:
        Validated `AccountHoldReceipt` instance.

    Exceptions/Errors:
        pydantic.ValidationError: Raised if hold tier or reason is invalid.
    """

    model_config = ConfigDict(extra="forbid")

    hold_id: str
    customer_id: str
    previous_status: str
    new_status: Literal["HOLD_REVIEW", "HOLD_FRAUD"]
    reason: str = Field(..., min_length=5, max_length=500)
    placed_at: str


class WireReversalReceipt(BaseModel):
    """
    Summary:
        Structured confirmation returned when recalling/reversing an outbound wire
        transaction linked to a verified fraud ticket.

    Parameters:
        reversal_id (str): Unique wire recall reference ID.
        transaction_id (str): Reversed wire transaction ID.
        customer_id (str): Credited customer ID.
        reversed_amount_usd (float): Reversed USD principal.
        new_available_balance_usd (float): Updated customer balance.
        fraud_verified_ticket (str): Verified fraud ops ticket ID.
        processed_at (str): ISO-8601 UTC timestamp.

    Return Value:
        Validated `WireReversalReceipt` instance.

    Exceptions/Errors:
        pydantic.ValidationError: Raised if parameters violate field rules.
    """

    model_config = ConfigDict(extra="forbid")

    reversal_id: str
    transaction_id: str
    customer_id: str
    reversed_amount_usd: float = Field(..., gt=0.0)
    new_available_balance_usd: float
    fraud_verified_ticket: str = Field(..., min_length=4, max_length=64)
    processed_at: str


class AuditEvent(BaseModel):
    """
    Summary:
        Normalized governance or application audit entry surfaced in the Cymbal Bank
        Operations Portal telemetry drawer.

    Parameters:
        timestamp (str): ISO-8601 UTC timestamp.
        layer (str): Enforcement layer (`INGRESS_MA`, `EGRESS_IAP_UAP`, `EGRESS_MA_SDP`,
            `SEMANTIC_POLICY`, `MCP_LEDGER`).
        verdict (Literal["ALLOW", "DENY", "MASKED", "INFO"]): Policy decision outcome.
        principal (str): Calling SPIFFE ID, service principal, or operator role.
        target (str): Target MCP tool, Reasoning Engine, or URL.
        summary (str): Human-readable verdict or rationale summary.

    Return Value:
        Validated `AuditEvent` instance.

    Exceptions/Errors:
        pydantic.ValidationError: Raised if required fields are missing.
    """

    model_config = ConfigDict(extra="forbid")

    timestamp: str
    layer: str
    verdict: Literal["ALLOW", "DENY", "MASKED", "INFO"]
    principal: str
    target: str
    summary: str

    @field_validator("summary")
    @classmethod
    def _trim_summary(cls, value: str) -> str:
        """
        Summary:
            Bounds audit summary text length so oversized upstream payloads cannot
            bloat portal responses.

        Parameters:
            value (str): Raw summary text.

        Return Value:
            str: Trimmed summary capped at 600 characters.

        Exceptions/Errors:
            None.
        """
        cleaned = value.strip()
        return cleaned[:600] if len(cleaned) > 600 else cleaned


class LedgerOverview(BaseModel):
    """
    Summary:
        Aggregated snapshot of Cymbal Bank accounts, transactions, disputes, and
        recent ledger audit entries rendered by the Operations Portal.

    Parameters:
        storage_backend (str): Active persistence engine (`firestore` or `local`).
        accounts (list[CustomerAccount]): Ordered customer accounts.
        transactions (list[TransactionRecord]): Recent ledger transactions.
        disputes (list[DisputeCase]): Active and resolved dispute cases.
        ledger_events (list[AuditEvent]): Recent MCP/ledger mutation audit events.

    Return Value:
        Validated `LedgerOverview` instance.

    Exceptions/Errors:
        pydantic.ValidationError: Raised if nested models fail validation.
    """

    model_config = ConfigDict(extra="forbid")

    storage_backend: str
    accounts: list[CustomerAccount]
    transactions: list[TransactionRecord]
    disputes: list[DisputeCase]
    ledger_events: list[AuditEvent] = Field(default_factory=list)
