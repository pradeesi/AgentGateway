"""
Purpose: Unit tests verifying `LocalFileLedgerStore` seed integrity, customer
         search, dispute refunds, account holds, wire reversals, and idempotent
         `reset_to_seed()` restoration.
Architecture/Context: Executed via `uv run pytest` using isolated temporary
         ledger paths (`tmp_path`) so local runtime files remain untouched.
Dependencies/Side Effects: Writes only inside pytest's ephemeral `tmp_path`.
Author: Pradeep Singh
License: Apache-2.0
"""

from __future__ import annotations

from pathlib import Path

import pytest

from common.ledger_store import (
    LedgerNotFoundError,
    LedgerValidationError,
    LocalFileLedgerStore,
)


def test_seed_dataset_and_search(tmp_path: Path) -> None:
    """
    Summary:
        Verifies baseline seed counts, synthetic PII presence for SDP testing,
        and case-insensitive customer search.

    Parameters:
        tmp_path (Path): Pytest temporary directory fixture.

    Return Value:
        None.

    Exceptions/Errors:
        AssertionError: If seed counts or attributes diverge from specification.
    """
    store = LocalFileLedgerStore(tmp_path / "ledger.json")
    overview = store.get_overview()

    assert len(overview.accounts) == 4
    assert len(overview.transactions) == 6
    assert len(overview.disputes) == 4

    elena = store.get_account("CUST-101")
    assert elena.full_name == "Elena Vance"
    assert elena.ssn == "453-29-8174"
    assert elena.card_pan.startswith("4532-")

    # DSP-1004 must carry the untrusted indirect prompt-injection test note.
    poisoned = store.get_dispute("DSP-1004")
    assert "SYSTEM OVERRIDE" in poisoned.merchant_evidence_note

    results = store.search_customers("sterling")
    assert len(results) == 1
    assert results[0].customer_id == "CUST-102"


def test_refund_hold_wire_and_reset(tmp_path: Path) -> None:
    """
    Summary:
        Validates atomic balance updates during refunds and wire reversals,
        rejects duplicate or excessive refunds, and confirms `reset_to_seed()`
        restores exact initial balances.

    Parameters:
        tmp_path (Path): Pytest temporary directory fixture.

    Return Value:
        None.

    Exceptions/Errors:
        AssertionError: If state transitions or validation guards fail.
    """
    store = LocalFileLedgerStore(tmp_path / "ledger.json")
    initial_balance = store.get_account("CUST-101").available_balance_usd

    receipt = store.issue_dispute_refund(
        dispute_id="DSP-1001",
        amount_usd=125.00,
        reason="Duplicate POS swipe confirmed by merchant batch log",
        actor_principal="spiffe://specialist",
    )
    assert receipt.refunded_amount_usd == 125.00
    assert receipt.new_available_balance_usd == round(initial_balance + 125.00, 2)
    assert store.get_dispute("DSP-1001").status == "REFUND_ISSUED"

    # Re-refunding an already resolved dispute must fail validation.
    with pytest.raises(LedgerValidationError):
        store.issue_dispute_refund(
            dispute_id="DSP-1001",
            amount_usd=125.00,
            reason="Duplicate attempt",
        )

    # Exceeding disputed amount must fail validation.
    with pytest.raises(LedgerValidationError):
        store.issue_dispute_refund(
            dispute_id="DSP-1002",
            amount_usd=9999.00,
            reason="Over-limit refund",
        )

    hold = store.place_account_hold(
        customer_id="CUST-104",
        reason="Suspicious digital merchant charge review",
        hold_tier="HOLD_REVIEW",
    )
    assert hold.new_status == "HOLD_REVIEW"
    assert store.get_account("CUST-104").account_status == "HOLD_REVIEW"

    wire = store.initiate_wire_reversal(
        transaction_id="TXN-9004",
        reason="Verified commercial invoice recall",
        fraud_verified_ticket="FRD-99012",
    )
    assert wire.reversed_amount_usd == 8900.00

    with pytest.raises(LedgerNotFoundError):
        store.get_account("CUST-999")

    counts = store.reset_to_seed()
    assert counts == {"accounts": 4, "transactions": 6, "disputes": 4}
    assert store.get_account("CUST-101").available_balance_usd == initial_balance
    assert store.get_dispute("DSP-1001").status == "OPEN"
