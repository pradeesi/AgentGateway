"""
Purpose: Repository abstraction persisting Cymbal Bank accounts, transactions,
         disputes, and audit trails in Google Cloud Firestore or a local JSON file.
Architecture/Context: Used by both `services/cymbal_banking_mcp` (for live MCP
         tool reads/mutations) and `services/cymbal_portal` (for real-time Customer
         360 ledger views and 1-command demo resets).
Dependencies/Side Effects: Connects to `google.cloud.firestore` when
         `STORAGE_BACKEND=firestore`; writes to `LOCAL_LEDGER_PATH` when
         `STORAGE_BACKEND=local`. Thread-safe local file locking prevents torn writes.
Author: Pradeep Singh
License: Apache-2.0
"""

from __future__ import annotations

import abc
import json
import logging
import os
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from common.logging_utils import configure_structured_logging, log_structured
from common.models import (
    AccountHoldReceipt,
    AuditEvent,
    CustomerAccount,
    DisputeCase,
    LedgerOverview,
    RefundReceipt,
    TransactionRecord,
    WireReversalReceipt,
)
from common.seed_data import (
    build_seed_accounts,
    build_seed_disputes,
    build_seed_transactions,
)

logger = configure_structured_logging("cymbal.ledger_store")


class LedgerNotFoundError(LookupError):
    """
    Summary:
        Raised when a requested customer, transaction, or dispute identifier does
        not exist in the Cymbal Bank ledger.

    Parameters:
        message (str): Descriptive lookup failure explanation.

    Return Value:
        LedgerNotFoundError exception instance.

    Exceptions/Errors:
        None.
    """


class LedgerValidationError(ValueError):
    """
    Summary:
        Raised when a business state transition or input parameter violates Cymbal
        Bank ledger integrity rules (e.g., refunding an already-refunded dispute).

    Parameters:
        message (str): Descriptive validation failure explanation.

    Return Value:
        LedgerValidationError exception instance.

    Exceptions/Errors:
        None.
    """


class LedgerStore(abc.ABC):
    """
    Summary:
        Abstract repository contract for reading, mutating, and resetting Cymbal
        Bank ledger entities across Firestore and local storage backends.

    Parameters:
        None.

    Return Value:
        Abstract `LedgerStore` base instance.

    Exceptions/Errors:
        NotImplementedError: If concrete subclasses omit abstract methods.
    """

    @property
    @abc.abstractmethod
    def backend_name(self) -> str:
        """
        Summary:
            Returns the identifier of the active persistence engine.

        Parameters:
            None.

        Return Value:
            str: `"firestore"` or `"local"`.

        Exceptions/Errors:
            None.
        """

    @abc.abstractmethod
    def ensure_seeded(self) -> None:
        """
        Summary:
            Populates the backing store with baseline seed accounts, transactions,
            and disputes if no customer records exist yet.

        Parameters:
            None.

        Return Value:
            None.

        Exceptions/Errors:
            RuntimeError: Raised if the underlying storage backend is unreachable.
        """

    @abc.abstractmethod
    def reset_to_seed(self) -> dict[str, int]:
        """
        Summary:
            Atomically overwrites all accounts, transactions, disputes, and ledger
            audit events back to the clean baseline demo dataset.

        Parameters:
            None.

        Return Value:
            dict[str, int]: Counts of seeded `accounts`, `transactions`, `disputes`.

        Exceptions/Errors:
            RuntimeError: Raised if resetting the backing store fails.
        """

    @abc.abstractmethod
    def search_customers(self, query: str) -> list[CustomerAccount]:
        """
        Summary:
            Searches customer accounts by partial name, customer ID, email, or tier.

        Parameters:
            query (str): Case-insensitive search substring (empty string lists all).

        Return Value:
            list[CustomerAccount]: Matching accounts ordered by `customer_id`.

        Exceptions/Errors:
            LedgerValidationError: If `query` exceeds 120 characters.
        """

    @abc.abstractmethod
    def get_account(self, customer_id: str) -> CustomerAccount:
        """
        Summary:
            Retrieves a single `CustomerAccount` by exact `customer_id`.

        Parameters:
            customer_id (str): Target customer identifier (`CUST-...`).

        Return Value:
            CustomerAccount: Matching customer record including raw synthetic PII.

        Exceptions/Errors:
            LedgerNotFoundError: If `customer_id` does not exist.
            LedgerValidationError: If `customer_id` format is invalid.
        """

    @abc.abstractmethod
    def list_transactions(
        self,
        customer_id: str | None = None,
        limit: int = 20,
    ) -> list[TransactionRecord]:
        """
        Summary:
            Lists recent ledger transactions, optionally filtered to one customer.

        Parameters:
            customer_id (str | None): Optional `CUST-...` filter.
            limit (int): Maximum records to return (1..100).

        Return Value:
            list[TransactionRecord]: Ordered transactions newest-first.

        Exceptions/Errors:
            LedgerValidationError: If `limit` is outside `[1, 100]`.
        """

    @abc.abstractmethod
    def get_dispute(self, dispute_id: str) -> DisputeCase:
        """
        Summary:
            Retrieves a single `DisputeCase` by exact `dispute_id`.

        Parameters:
            dispute_id (str): Target dispute identifier (`DSP-...`).

        Return Value:
            DisputeCase: Full dispute case including `merchant_evidence_note`.

        Exceptions/Errors:
            LedgerNotFoundError: If `dispute_id` does not exist.
            LedgerValidationError: If `dispute_id` format is invalid.
        """

    @abc.abstractmethod
    def list_disputes(self, customer_id: str | None = None) -> list[DisputeCase]:
        """
        Summary:
            Lists all dispute cases, optionally filtered by `customer_id`.

        Parameters:
            customer_id (str | None): Optional `CUST-...` filter.

        Return Value:
            list[DisputeCase]: Dispute cases ordered by `dispute_id`.

        Exceptions/Errors:
            None.
        """

    @abc.abstractmethod
    def issue_dispute_refund(
        self,
        dispute_id: str,
        amount_usd: float,
        reason: str,
        supervisor_approval_code: str | None = None,
        actor_principal: str = "mcp-client",
    ) -> RefundReceipt:
        """
        Summary:
            Credits `amount_usd` to the disputing customer's account, records a
            `REFUND_CREDIT` ledger entry, and transitions the dispute to
            `REFUND_ISSUED`.

        Parameters:
            dispute_id (str): Target open dispute (`DSP-...`).
            amount_usd (float): Refund amount in USD (> 0 and <= disputed amount).
            reason (str): Audit justification for the refund.
            supervisor_approval_code (str | None): Optional supervisor override code.
            actor_principal (str): Calling identity recorded in audit trail.

        Return Value:
            RefundReceipt: Confirmation with new customer balance and receipt ID.

        Exceptions/Errors:
            LedgerNotFoundError: If `dispute_id` or linked customer does not exist.
            LedgerValidationError: If the dispute is not `OPEN` or amount is invalid.
        """

    @abc.abstractmethod
    def place_account_hold(
        self,
        customer_id: str,
        reason: str,
        hold_tier: Literal["HOLD_REVIEW", "HOLD_FRAUD"] = "HOLD_REVIEW",
        actor_principal: str = "mcp-client",
    ) -> AccountHoldReceipt:
        """
        Summary:
            Places a compliance or fraud hold on a customer account and records an
            audit event.

        Parameters:
            customer_id (str): Target customer (`CUST-...`).
            reason (str): Non-empty risk/compliance rationale.
            hold_tier (Literal["HOLD_REVIEW", "HOLD_FRAUD"]): Hold severity level.
            actor_principal (str): Calling identity recorded in audit trail.

        Return Value:
            AccountHoldReceipt: Confirmation with previous and updated statuses.

        Exceptions/Errors:
            LedgerNotFoundError: If `customer_id` does not exist.
            LedgerValidationError: If `reason` or `hold_tier` is invalid.
        """

    @abc.abstractmethod
    def initiate_wire_reversal(
        self,
        transaction_id: str,
        reason: str,
        fraud_verified_ticket: str,
        actor_principal: str = "mcp-client",
    ) -> WireReversalReceipt:
        """
        Summary:
            Recalls an outbound wire (`WIRE_OUT`) transaction, restores funds to the
            customer balance, and logs a wire-reversal audit entry.

        Parameters:
            transaction_id (str): Target wire transaction (`TXN-...`).
            reason (str): Non-empty recall explanation.
            fraud_verified_ticket (str): Mandatory Fraud Ops ticket code (`FRD-...`).
            actor_principal (str): Calling identity recorded in audit trail.

        Return Value:
            WireReversalReceipt: Confirmation with restored customer balance.

        Exceptions/Errors:
            LedgerNotFoundError: If `transaction_id` does not exist.
            LedgerValidationError: If the transaction is not `WIRE_OUT`, already
                reversed, or missing a valid `fraud_verified_ticket`.
        """

    @abc.abstractmethod
    def record_audit_event(self, event: AuditEvent) -> None:
        """
        Summary:
            Appends an application-level `AuditEvent` to the ledger audit feed.

        Parameters:
            event (AuditEvent): Validated audit entry.

        Return Value:
            None.

        Exceptions/Errors:
            None.
        """

    @abc.abstractmethod
    def get_overview(self) -> LedgerOverview:
        """
        Summary:
            Returns a complete snapshot of accounts, transactions, disputes, and
            recent ledger audit events for the Operations Portal UI.

        Parameters:
            None.

        Return Value:
            LedgerOverview: Aggregated ledger snapshot.

        Exceptions/Errors:
            None.
        """


def _validate_id(value: str, prefix: str, label: str) -> str:
    """
    Summary:
        Validates and normalizes domain entity IDs (`CUST-`, `TXN-`, `DSP-`) at the
        public repository boundary.

    Parameters:
        value (str): Raw caller-supplied identifier.
        prefix (str): Expected uppercase prefix including hyphen (e.g., `"CUST-"`).
        label (str): Human-readable parameter name for error messages.

    Return Value:
        str: Stripped uppercase identifier.

    Exceptions/Errors:
        LedgerValidationError: Raised when `value` is empty or lacks `prefix`.
    """
    if not isinstance(value, str) or not value.strip():
        raise LedgerValidationError(f"{label} must be a non-empty string.")
    normalized = value.strip().upper()
    if not normalized.startswith(prefix) or len(normalized) > 24:
        raise LedgerValidationError(
            f"{label} '{value}' must start with '{prefix}' and be <= 24 characters."
        )
    return normalized


class LocalFileLedgerStore(LedgerStore):
    """
    Summary:
        Thread-safe JSON-backed `LedgerStore` implementation used for local execution,
        automated unit tests, and offline verification without GCP credentials.

    Parameters:
        file_path (str | Path): Filesystem path for the local JSON ledger state.

    Return Value:
        Initialized `LocalFileLedgerStore` seeded on first access.

    Exceptions/Errors:
        OSError: Raised if parent directory creation or file I/O fails.
    """

    def __init__(self, file_path: str | Path | None = None) -> None:
        default_path = os.getenv("LOCAL_LEDGER_PATH", ".local_data/cymbal_ledger.json")
        self._path = Path(file_path or default_path)
        self._lock = threading.RLock()
        self.ensure_seeded()

    @property
    def backend_name(self) -> str:
        """
        Summary:
            Identifies this store as the `"local"` JSON backend.

        Parameters:
            None.

        Return Value:
            str: `"local"`.

        Exceptions/Errors:
            None.
        """
        return "local"

    def _read_state(self) -> dict[str, list[dict]]:
        """
        Summary:
            Reads and parses the JSON ledger state file under re-entrant lock.

        Parameters:
            None.

        Return Value:
            dict[str, list[dict]]: Parsed collections (`accounts`, `transactions`,
            `disputes`, `events`).

        Exceptions/Errors:
            RuntimeError: Raised if the JSON file is corrupted.
        """
        with self._lock:
            if not self._path.exists():
                self.reset_to_seed()
            try:
                return json.loads(self._path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"Corrupted local ledger file {self._path}: {exc}") from exc

    def _write_state(self, data: dict[str, list[dict]]) -> None:
        """
        Summary:
            Atomically writes the updated ledger state via a temporary sibling file
            to prevent partial reads during concurrent portal/MCP access.

        Parameters:
            data (dict[str, list[dict]]): Complete state dictionary to persist.

        Return Value:
            None.

        Exceptions/Errors:
            OSError: Raised if filesystem write or rename fails.
        """
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._path.with_suffix(".tmp")
            tmp_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
            tmp_path.replace(self._path)

    def ensure_seeded(self) -> None:
        """
        Summary:
            Initializes the local JSON ledger file with baseline Cymbal Bank data if
            it does not yet exist.

        Parameters:
            None.

        Return Value:
            None.

        Exceptions/Errors:
            OSError: Propagated if initial write fails.
        """
        with self._lock:
            if not self._path.exists():
                self.reset_to_seed()

    def reset_to_seed(self) -> dict[str, int]:
        """
        Summary:
            Rebuilds the local JSON ledger from `common/seed_data.py` and logs a
            seed reset audit event.

        Parameters:
            None.

        Return Value:
            dict[str, int]: Entity counts written (`accounts`, `transactions`, `disputes`).

        Exceptions/Errors:
            OSError: Propagated if writing the reset file fails.
        """
        accounts = [a.model_dump() for a in build_seed_accounts()]
        transactions = [t.model_dump() for t in build_seed_transactions()]
        disputes = [d.model_dump() for d in build_seed_disputes()]
        now_iso = datetime.now(UTC).isoformat()
        events = [
            AuditEvent(
                timestamp=now_iso,
                layer="MCP_LEDGER",
                verdict="INFO",
                principal="system:seed-reset",
                target="cymbal_ledger",
                summary=(
                    f"Ledger reset to baseline ({len(accounts)} accounts, "
                    f"{len(transactions)} transactions, {len(disputes)} disputes)."
                ),
            ).model_dump()
        ]
        self._write_state(
            {
                "accounts": accounts,
                "transactions": transactions,
                "disputes": disputes,
                "events": events,
            }
        )
        log_structured(
            logger,
            logging.INFO,
            "ledger_reset_completed",
            backend="local",
            accounts=len(accounts),
            transactions=len(transactions),
            disputes=len(disputes),
        )
        return {
            "accounts": len(accounts),
            "transactions": len(transactions),
            "disputes": len(disputes),
        }

    def search_customers(self, query: str) -> list[CustomerAccount]:
        """
        Summary:
            Performs case-insensitive matching across customer ID, full name, email,
            and banking segment in the local ledger.

        Parameters:
            query (str): Search term up to 120 characters.

        Return Value:
            list[CustomerAccount]: Matching accounts sorted by `customer_id`.

        Exceptions/Errors:
            LedgerValidationError: If `query` exceeds 120 characters.
        """
        if not isinstance(query, str) or len(query) > 120:
            raise LedgerValidationError("Search query must be a string <= 120 characters.")
        needle = query.strip().lower()
        state = self._read_state()
        accounts = [CustomerAccount.model_validate(item) for item in state["accounts"]]
        if not needle:
            return sorted(accounts, key=lambda a: a.customer_id)
        matched = [
            acct
            for acct in accounts
            if needle in acct.customer_id.lower()
            or needle in acct.full_name.lower()
            or needle in acct.email.lower()
            or needle in acct.segment.lower()
        ]
        return sorted(matched, key=lambda a: a.customer_id)

    def get_account(self, customer_id: str) -> CustomerAccount:
        """
        Summary:
            Fetches a customer profile by ID from the local ledger.

        Parameters:
            customer_id (str): Customer identifier (`CUST-...`).

        Return Value:
            CustomerAccount: Validated customer account record.

        Exceptions/Errors:
            LedgerNotFoundError: If no customer matches `customer_id`.
            LedgerValidationError: If `customer_id` format is invalid.
        """
        cid = _validate_id(customer_id, "CUST-", "customer_id")
        state = self._read_state()
        for item in state["accounts"]:
            if item["customer_id"] == cid:
                return CustomerAccount.model_validate(item)
        raise LedgerNotFoundError(f"Customer account '{cid}' was not found.")

    def list_transactions(
        self,
        customer_id: str | None = None,
        limit: int = 20,
    ) -> list[TransactionRecord]:
        """
        Summary:
            Lists recent transactions from the local ledger sorted newest-first.

        Parameters:
            customer_id (str | None): Optional `CUST-...` filter.
            limit (int): Maximum rows to return (1..100).

        Return Value:
            list[TransactionRecord]: Matching transaction records.

        Exceptions/Errors:
            LedgerValidationError: If `limit` or `customer_id` is invalid.
        """
        if not isinstance(limit, int) or limit < 1 or limit > 100:
            raise LedgerValidationError("limit must be an integer between 1 and 100.")
        cid = _validate_id(customer_id, "CUST-", "customer_id") if customer_id else None
        state = self._read_state()
        records = [TransactionRecord.model_validate(item) for item in state["transactions"]]
        if cid:
            records = [txn for txn in records if txn.customer_id == cid]
        records.sort(key=lambda t: t.posted_at, reverse=True)
        return records[:limit]

    def get_dispute(self, dispute_id: str) -> DisputeCase:
        """
        Summary:
            Fetches a single dispute case by `dispute_id` from the local ledger.

        Parameters:
            dispute_id (str): Dispute identifier (`DSP-...`).

        Return Value:
            DisputeCase: Validated dispute case.

        Exceptions/Errors:
            LedgerNotFoundError: If `dispute_id` is absent.
            LedgerValidationError: If `dispute_id` format is invalid.
        """
        did = _validate_id(dispute_id, "DSP-", "dispute_id")
        state = self._read_state()
        for item in state["disputes"]:
            if item["dispute_id"] == did:
                return DisputeCase.model_validate(item)
        raise LedgerNotFoundError(f"Dispute case '{did}' was not found.")

    def list_disputes(self, customer_id: str | None = None) -> list[DisputeCase]:
        """
        Summary:
            Lists all disputes in the local ledger, optionally filtered by customer.

        Parameters:
            customer_id (str | None): Optional `CUST-...` filter.

        Return Value:
            list[DisputeCase]: Disputes sorted by `dispute_id`.

        Exceptions/Errors:
            LedgerValidationError: If `customer_id` format is invalid.
        """
        cid = _validate_id(customer_id, "CUST-", "customer_id") if customer_id else None
        state = self._read_state()
        disputes = [DisputeCase.model_validate(item) for item in state["disputes"]]
        if cid:
            disputes = [d for d in disputes if d.customer_id == cid]
        return sorted(disputes, key=lambda d: d.dispute_id)

    def issue_dispute_refund(
        self,
        dispute_id: str,
        amount_usd: float,
        reason: str,
        supervisor_approval_code: str | None = None,
        actor_principal: str = "mcp-client",
    ) -> RefundReceipt:
        """
        Summary:
            Atomically credits `amount_usd` for an open dispute in the local ledger,
            updates the disputed transaction status, and appends a credit entry.

        Parameters:
            dispute_id (str): Open dispute ID (`DSP-...`).
            amount_usd (float): Positive USD refund amount.
            reason (str): Refund justification (5..400 chars).
            supervisor_approval_code (str | None): Optional supervisor override code.
            actor_principal (str): Calling identity recorded in audit trail.

        Return Value:
            RefundReceipt: Completed refund receipt.

        Exceptions/Errors:
            LedgerNotFoundError: If `dispute_id` or linked customer is missing.
            LedgerValidationError: If dispute is already resolved or amount exceeds
                the disputed total.
        """
        did = _validate_id(dispute_id, "DSP-", "dispute_id")
        if not isinstance(amount_usd, int | float) or amount_usd <= 0:
            raise LedgerValidationError("amount_usd must be a positive number.")
        if not isinstance(reason, str) or len(reason.strip()) < 5:
            raise LedgerValidationError("reason must be at least 5 characters.")

        with self._lock:
            state = self._read_state()
            dispute_raw = next((d for d in state["disputes"] if d["dispute_id"] == did), None)
            if dispute_raw is None:
                raise LedgerNotFoundError(f"Dispute case '{did}' was not found.")
            if dispute_raw["status"] != "OPEN":
                raise LedgerValidationError(
                    f"Dispute '{did}' is already in status '{dispute_raw['status']}'."
                )
            if round(float(amount_usd), 2) > round(float(dispute_raw["disputed_amount_usd"]), 2):
                raise LedgerValidationError(
                    f"Refund ${amount_usd:.2f} exceeds disputed amount "
                    f"${dispute_raw['disputed_amount_usd']:.2f}."
                )

            account_raw = next(
                (a for a in state["accounts"] if a["customer_id"] == dispute_raw["customer_id"]),
                None,
            )
            if account_raw is None:
                raise LedgerNotFoundError(
                    f"Linked customer '{dispute_raw['customer_id']}' was not found."
                )

            rounded_amt = round(float(amount_usd), 2)
            now_iso = datetime.now(UTC).isoformat()
            receipt_id = f"RFD-{uuid.uuid4().hex[:8].upper()}"

            account_raw["available_balance_usd"] = round(
                float(account_raw["available_balance_usd"]) + rounded_amt, 2
            )
            dispute_raw["status"] = "REFUND_ISSUED"
            dispute_raw["refunded_amount_usd"] = rounded_amt
            dispute_raw["resolution_note"] = (
                f"Refunded ${rounded_amt:.2f} ({receipt_id}): {reason.strip()}"
            )

            for txn in state["transactions"]:
                if txn["transaction_id"] == dispute_raw["transaction_id"]:
                    txn["status"] = "REFUNDED"

            credit_txn = TransactionRecord(
                transaction_id=f"TXN-{9500 + len(state['transactions'])}",
                customer_id=account_raw["customer_id"],
                posted_at=now_iso,
                merchant_or_counterparty=f"Cymbal Dispute Credit ({did})",
                channel="REFUND_CREDIT",
                amount_usd=rounded_amt,
                status="POSTED",
                reference_note=f"Receipt {receipt_id}: {reason.strip()[:180]}",
            )
            state["transactions"].append(credit_txn.model_dump())

            audit = AuditEvent(
                timestamp=now_iso,
                layer="MCP_LEDGER",
                verdict="ALLOW",
                principal=actor_principal,
                target=f"issue_dispute_refund ({did})",
                summary=(
                    f"Issued ${rounded_amt:.2f} refund to {account_raw['customer_id']} "
                    f"(new balance ${account_raw['available_balance_usd']:.2f})."
                ),
            )
            state.setdefault("events", []).insert(0, audit.model_dump())
            state["events"] = state["events"][:50]
            self._write_state(state)

            return RefundReceipt(
                receipt_id=receipt_id,
                dispute_id=did,
                customer_id=account_raw["customer_id"],
                refunded_amount_usd=rounded_amt,
                new_available_balance_usd=account_raw["available_balance_usd"],
                dispute_status="REFUND_ISSUED",
                supervisor_approval_code=(
                    supervisor_approval_code.strip() if supervisor_approval_code else None
                ),
                processed_at=now_iso,
            )

    def place_account_hold(
        self,
        customer_id: str,
        reason: str,
        hold_tier: Literal["HOLD_REVIEW", "HOLD_FRAUD"] = "HOLD_REVIEW",
        actor_principal: str = "mcp-client",
    ) -> AccountHoldReceipt:
        """
        Summary:
            Updates a customer account's status to `HOLD_REVIEW` or `HOLD_FRAUD` in
            the local ledger and logs an audit entry.

        Parameters:
            customer_id (str): Customer identifier (`CUST-...`).
            reason (str): Compliance/fraud hold justification.
            hold_tier (Literal["HOLD_REVIEW", "HOLD_FRAUD"]): Target hold state.
            actor_principal (str): Calling identity recorded in audit trail.

        Return Value:
            AccountHoldReceipt: Hold confirmation receipt.

        Exceptions/Errors:
            LedgerNotFoundError: If `customer_id` is missing.
            LedgerValidationError: If `reason` or `hold_tier` is invalid.
        """
        cid = _validate_id(customer_id, "CUST-", "customer_id")
        if hold_tier not in ("HOLD_REVIEW", "HOLD_FRAUD"):
            raise LedgerValidationError("hold_tier must be 'HOLD_REVIEW' or 'HOLD_FRAUD'.")
        if not isinstance(reason, str) or len(reason.strip()) < 5:
            raise LedgerValidationError("reason must be at least 5 characters.")

        with self._lock:
            state = self._read_state()
            account_raw = next((a for a in state["accounts"] if a["customer_id"] == cid), None)
            if account_raw is None:
                raise LedgerNotFoundError(f"Customer account '{cid}' was not found.")

            prev_status = account_raw["account_status"]
            now_iso = datetime.now(UTC).isoformat()
            hold_id = f"HLD-{uuid.uuid4().hex[:8].upper()}"
            account_raw["account_status"] = hold_tier
            account_raw["hold_reason"] = reason.strip()

            audit = AuditEvent(
                timestamp=now_iso,
                layer="MCP_LEDGER",
                verdict="ALLOW",
                principal=actor_principal,
                target=f"place_account_hold ({cid})",
                summary=f"Placed {hold_tier} on {cid}: {reason.strip()}",
            )
            state.setdefault("events", []).insert(0, audit.model_dump())
            state["events"] = state["events"][:50]
            self._write_state(state)

            return AccountHoldReceipt(
                hold_id=hold_id,
                customer_id=cid,
                previous_status=prev_status,
                new_status=hold_tier,
                reason=reason.strip(),
                placed_at=now_iso,
            )

    def initiate_wire_reversal(
        self,
        transaction_id: str,
        reason: str,
        fraud_verified_ticket: str,
        actor_principal: str = "mcp-client",
    ) -> WireReversalReceipt:
        """
        Summary:
            Reverses an outbound wire transaction in the local ledger when a valid
            `FRD-...` fraud ticket is supplied.

        Parameters:
            transaction_id (str): Outbound wire transaction ID (`TXN-...`).
            reason (str): Recall explanation.
            fraud_verified_ticket (str): Mandatory Fraud Ops ticket (`FRD-...`).
            actor_principal (str): Calling identity recorded in audit trail.

        Return Value:
            WireReversalReceipt: Wire recall confirmation receipt.

        Exceptions/Errors:
            LedgerNotFoundError: If `transaction_id` or linked customer is missing.
            LedgerValidationError: If transaction is not `WIRE_OUT`, already reversed,
                or lacks a valid `FRD-...` ticket.
        """
        tid = _validate_id(transaction_id, "TXN-", "transaction_id")
        ticket = _validate_id(fraud_verified_ticket, "FRD-", "fraud_verified_ticket")
        if not isinstance(reason, str) or len(reason.strip()) < 5:
            raise LedgerValidationError("reason must be at least 5 characters.")

        with self._lock:
            state = self._read_state()
            txn_raw = next((t for t in state["transactions"] if t["transaction_id"] == tid), None)
            if txn_raw is None:
                raise LedgerNotFoundError(f"Transaction '{tid}' was not found.")
            if txn_raw["channel"] != "WIRE_OUT":
                raise LedgerValidationError(
                    f"Transaction '{tid}' is channel '{txn_raw['channel']}', not 'WIRE_OUT'."
                )
            if txn_raw["status"] == "REVERSED":
                raise LedgerValidationError(f"Wire transaction '{tid}' is already reversed.")

            account_raw = next(
                (a for a in state["accounts"] if a["customer_id"] == txn_raw["customer_id"]),
                None,
            )
            if account_raw is None:
                raise LedgerNotFoundError(f"Customer '{txn_raw['customer_id']}' was not found.")

            amt = round(float(txn_raw["amount_usd"]), 2)
            now_iso = datetime.now(UTC).isoformat()
            reversal_id = f"WIR-{uuid.uuid4().hex[:8].upper()}"

            txn_raw["status"] = "REVERSED"
            account_raw["available_balance_usd"] = round(
                float(account_raw["available_balance_usd"]) + amt, 2
            )

            for dispute in state["disputes"]:
                if dispute["transaction_id"] == tid and dispute["status"] == "OPEN":
                    dispute["status"] = "REFUND_ISSUED"
                    dispute["refunded_amount_usd"] = amt
                    dispute["resolution_note"] = f"Wire reversed ({reversal_id}, ticket {ticket})"

            audit = AuditEvent(
                timestamp=now_iso,
                layer="MCP_LEDGER",
                verdict="ALLOW",
                principal=actor_principal,
                target=f"initiate_wire_reversal ({tid})",
                summary=(
                    f"Reversed wire {tid} (${amt:.2f}) for {account_raw['customer_id']} "
                    f"under ticket {ticket}."
                ),
            )
            state.setdefault("events", []).insert(0, audit.model_dump())
            state["events"] = state["events"][:50]
            self._write_state(state)

            return WireReversalReceipt(
                reversal_id=reversal_id,
                transaction_id=tid,
                customer_id=account_raw["customer_id"],
                reversed_amount_usd=amt,
                new_available_balance_usd=account_raw["available_balance_usd"],
                fraud_verified_ticket=ticket,
                processed_at=now_iso,
            )

    def record_audit_event(self, event: AuditEvent) -> None:
        """
        Summary:
            Prepends an `AuditEvent` to the local ledger's bounded event ring buffer.

        Parameters:
            event (AuditEvent): Validated audit entry.

        Return Value:
            None.

        Exceptions/Errors:
            OSError: Propagated if persisting state fails.
        """
        with self._lock:
            state = self._read_state()
            state.setdefault("events", []).insert(0, event.model_dump())
            state["events"] = state["events"][:50]
            self._write_state(state)

    def get_overview(self) -> LedgerOverview:
        """
        Summary:
            Loads a complete `LedgerOverview` snapshot from the local JSON store.

        Parameters:
            None.

        Return Value:
            LedgerOverview: Accounts, newest-first transactions, disputes, and events.

        Exceptions/Errors:
            None.
        """
        state = self._read_state()
        accounts = sorted(
            (CustomerAccount.model_validate(a) for a in state["accounts"]),
            key=lambda a: a.customer_id,
        )
        transactions = sorted(
            (TransactionRecord.model_validate(t) for t in state["transactions"]),
            key=lambda t: t.posted_at,
            reverse=True,
        )
        disputes = sorted(
            (DisputeCase.model_validate(d) for d in state["disputes"]),
            key=lambda d: d.dispute_id,
        )
        events = [AuditEvent.model_validate(e) for e in state.get("events", [])]
        return LedgerOverview(
            storage_backend=self.backend_name,
            accounts=accounts,
            transactions=transactions,
            disputes=disputes,
            ledger_events=events,
        )


class FirestoreLedgerStore(LedgerStore):
    """
    Summary:
        Production Google Cloud Firestore Native `LedgerStore` implementation sharing
        real-time banking state across serverless `cymbal-banking-mcp` and
        `cymbal-portal` Cloud Run revisions.

    Parameters:
        project_id (str | None): GCP Project ID hosting Firestore.
        database (str | None): Firestore database ID (defaults to `"(default)"`).
        collection_prefix (str | None): Prefix isolating Cymbal Bank collections.

    Return Value:
        Initialized `FirestoreLedgerStore` instance.

    Exceptions/Errors:
        RuntimeError: Raised if Firestore client initialization or seeding fails.
    """

    def __init__(
        self,
        project_id: str | None = None,
        database: str | None = None,
        collection_prefix: str | None = None,
    ) -> None:
        from google.cloud import firestore

        resolved_project = (
            project_id
            or os.getenv("PROJECT_ID")
            or os.getenv("GOOGLE_CLOUD_PROJECT")
            or None
        )
        resolved_db = database or os.getenv("FIRESTORE_DATABASE", "(default)")
        prefix = (
            collection_prefix or os.getenv("FIRESTORE_COLLECTION_PREFIX", "cymbal_agw")
        ).strip()

        oauth_token = os.getenv("GOOGLE_OAUTH_ACCESS_TOKEN", "").strip()
        if oauth_token:
            from google.oauth2.credentials import Credentials

            explicit_creds = Credentials(
                token=oauth_token,
                quota_project_id=resolved_project,
            )
            self._client = firestore.Client(
                project=resolved_project,
                database=resolved_db,
                credentials=explicit_creds,
            )
        else:
            self._client = firestore.Client(project=resolved_project, database=resolved_db)
        self._accounts_col = f"{prefix}_accounts"
        self._txns_col = f"{prefix}_transactions"
        self._disputes_col = f"{prefix}_disputes"
        self._events_col = f"{prefix}_events"
        self.ensure_seeded()

    @property
    def backend_name(self) -> str:
        """
        Summary:
            Identifies this store as the `"firestore"` backend.

        Parameters:
            None.

        Return Value:
            str: `"firestore"`.

        Exceptions/Errors:
            None.
        """
        return "firestore"

    def ensure_seeded(self) -> None:
        """
        Summary:
            Checks whether Firestore already holds `CUST-101`; if absent, runs
            `reset_to_seed()` to populate baseline Cymbal Bank collections.

        Parameters:
            None.

        Return Value:
            None.

        Exceptions/Errors:
            RuntimeError: Propagated if Firestore is unreachable.
        """
        doc = self._client.collection(self._accounts_col).document("CUST-101").get()
        if not doc.exists:
            self.reset_to_seed()

    def reset_to_seed(self) -> dict[str, int]:
        """
        Summary:
            Purges existing documents in the four prefixed Cymbal collections and
            writes the deterministic seed dataset in a Firestore batch.

        Parameters:
            None.

        Return Value:
            dict[str, int]: Counts of seeded `accounts`, `transactions`, `disputes`.

        Exceptions/Errors:
            RuntimeError: Propagated if batch commit fails.
        """
        for col_name in (
            self._accounts_col,
            self._txns_col,
            self._disputes_col,
            self._events_col,
        ):
            docs = list(self._client.collection(col_name).list_documents())
            if docs:
                purge_batch = self._client.batch()
                for ref in docs:
                    purge_batch.delete(ref)
                purge_batch.commit()

        accounts = build_seed_accounts()
        transactions = build_seed_transactions()
        disputes = build_seed_disputes()
        now_iso = datetime.now(UTC).isoformat()

        seed_batch = self._client.batch()
        for acct in accounts:
            ref = self._client.collection(self._accounts_col).document(acct.customer_id)
            seed_batch.set(ref, acct.model_dump())
        for txn in transactions:
            ref = self._client.collection(self._txns_col).document(txn.transaction_id)
            seed_batch.set(ref, txn.model_dump())
        for dsp in disputes:
            ref = self._client.collection(self._disputes_col).document(dsp.dispute_id)
            seed_batch.set(ref, dsp.model_dump())

        init_event = AuditEvent(
            timestamp=now_iso,
            layer="MCP_LEDGER",
            verdict="INFO",
            principal="system:seed-reset",
            target="firestore_ledger",
            summary=(
                f"Firestore ledger reset to baseline ({len(accounts)} accounts, "
                f"{len(transactions)} transactions, {len(disputes)} disputes)."
            ),
        )
        event_ref = self._client.collection(self._events_col).document(uuid.uuid4().hex)
        seed_batch.set(event_ref, init_event.model_dump())
        seed_batch.commit()

        log_structured(
            logger,
            logging.INFO,
            "ledger_reset_completed",
            backend="firestore",
            accounts=len(accounts),
            transactions=len(transactions),
            disputes=len(disputes),
        )
        return {
            "accounts": len(accounts),
            "transactions": len(transactions),
            "disputes": len(disputes),
        }

    def search_customers(self, query: str) -> list[CustomerAccount]:
        """
        Summary:
            Streams all customer profiles from Firestore (bounded demo collection)
            and filters case-insensitively by ID, name, email, or segment.

        Parameters:
            query (str): Search substring up to 120 characters.

        Return Value:
            list[CustomerAccount]: Matching accounts ordered by `customer_id`.

        Exceptions/Errors:
            LedgerValidationError: If `query` exceeds 120 characters.
        """
        if not isinstance(query, str) or len(query) > 120:
            raise LedgerValidationError("Search query must be a string <= 120 characters.")
        needle = query.strip().lower()
        docs = self._client.collection(self._accounts_col).stream()
        accounts = [CustomerAccount.model_validate(d.to_dict()) for d in docs]
        if not needle:
            return sorted(accounts, key=lambda a: a.customer_id)
        matched = [
            acct
            for acct in accounts
            if needle in acct.customer_id.lower()
            or needle in acct.full_name.lower()
            or needle in acct.email.lower()
            or needle in acct.segment.lower()
        ]
        return sorted(matched, key=lambda a: a.customer_id)

    def get_account(self, customer_id: str) -> CustomerAccount:
        """
        Summary:
            Retrieves a `CustomerAccount` document from Firestore by `customer_id`.

        Parameters:
            customer_id (str): Customer identifier (`CUST-...`).

        Return Value:
            CustomerAccount: Validated account model.

        Exceptions/Errors:
            LedgerNotFoundError: If document does not exist.
            LedgerValidationError: If `customer_id` format is invalid.
        """
        cid = _validate_id(customer_id, "CUST-", "customer_id")
        snap = self._client.collection(self._accounts_col).document(cid).get()
        if not snap.exists:
            raise LedgerNotFoundError(f"Customer account '{cid}' was not found.")
        return CustomerAccount.model_validate(snap.to_dict())

    def list_transactions(
        self,
        customer_id: str | None = None,
        limit: int = 20,
    ) -> list[TransactionRecord]:
        """
        Summary:
            Lists recent transactions from Firestore ordered newest-first.

        Parameters:
            customer_id (str | None): Optional `CUST-...` filter.
            limit (int): Maximum records to return (1..100).

        Return Value:
            list[TransactionRecord]: Matching transaction models.

        Exceptions/Errors:
            LedgerValidationError: If `limit` or `customer_id` is invalid.
        """
        if not isinstance(limit, int) or limit < 1 or limit > 100:
            raise LedgerValidationError("limit must be an integer between 1 and 100.")
        cid = _validate_id(customer_id, "CUST-", "customer_id") if customer_id else None
        col = self._client.collection(self._txns_col)
        docs = col.where("customer_id", "==", cid).stream() if cid else col.stream()
        records = [TransactionRecord.model_validate(d.to_dict()) for d in docs]
        records.sort(key=lambda t: t.posted_at, reverse=True)
        return records[:limit]

    def get_dispute(self, dispute_id: str) -> DisputeCase:
        """
        Summary:
            Retrieves a `DisputeCase` document from Firestore by `dispute_id`.

        Parameters:
            dispute_id (str): Dispute identifier (`DSP-...`).

        Return Value:
            DisputeCase: Validated dispute case.

        Exceptions/Errors:
            LedgerNotFoundError: If document does not exist.
            LedgerValidationError: If `dispute_id` format is invalid.
        """
        did = _validate_id(dispute_id, "DSP-", "dispute_id")
        snap = self._client.collection(self._disputes_col).document(did).get()
        if not snap.exists:
            raise LedgerNotFoundError(f"Dispute case '{did}' was not found.")
        return DisputeCase.model_validate(snap.to_dict())

    def list_disputes(self, customer_id: str | None = None) -> list[DisputeCase]:
        """
        Summary:
            Lists disputes from Firestore, optionally filtered by `customer_id`.

        Parameters:
            customer_id (str | None): Optional `CUST-...` filter.

        Return Value:
            list[DisputeCase]: Sorted dispute models.

        Exceptions/Errors:
            LedgerValidationError: If `customer_id` format is invalid.
        """
        cid = _validate_id(customer_id, "CUST-", "customer_id") if customer_id else None
        col = self._client.collection(self._disputes_col)
        docs = col.where("customer_id", "==", cid).stream() if cid else col.stream()
        disputes = [DisputeCase.model_validate(d.to_dict()) for d in docs]
        return sorted(disputes, key=lambda d: d.dispute_id)

    def issue_dispute_refund(
        self,
        dispute_id: str,
        amount_usd: float,
        reason: str,
        supervisor_approval_code: str | None = None,
        actor_principal: str = "mcp-client",
    ) -> RefundReceipt:
        """
        Summary:
            Executes an atomic Firestore batch updating dispute status, customer
            balance, original transaction state, and credit ledger entry.

        Parameters:
            dispute_id (str): Open dispute ID (`DSP-...`).
            amount_usd (float): Positive USD refund amount.
            reason (str): Refund justification (5..400 chars).
            supervisor_approval_code (str | None): Optional supervisor override code.
            actor_principal (str): Calling identity recorded in audit trail.

        Return Value:
            RefundReceipt: Completed refund confirmation.

        Exceptions/Errors:
            LedgerNotFoundError: If dispute or customer is missing.
            LedgerValidationError: If dispute is closed or amount exceeds disputed sum.
        """
        did = _validate_id(dispute_id, "DSP-", "dispute_id")
        if not isinstance(amount_usd, int | float) or amount_usd <= 0:
            raise LedgerValidationError("amount_usd must be a positive number.")
        if not isinstance(reason, str) or len(reason.strip()) < 5:
            raise LedgerValidationError("reason must be at least 5 characters.")

        dispute = self.get_dispute(did)
        if dispute.status != "OPEN":
            raise LedgerValidationError(
                f"Dispute '{did}' is already in status '{dispute.status}'."
            )
        rounded_amt = round(float(amount_usd), 2)
        if rounded_amt > round(dispute.disputed_amount_usd, 2):
            raise LedgerValidationError(
                f"Refund ${rounded_amt:.2f} exceeds disputed amount "
                f"${dispute.disputed_amount_usd:.2f}."
            )

        account = self.get_account(dispute.customer_id)
        new_balance = round(account.available_balance_usd + rounded_amt, 2)
        now_iso = datetime.now(UTC).isoformat()
        receipt_id = f"RFD-{uuid.uuid4().hex[:8].upper()}"
        credit_txn_id = f"TXN-{int(datetime.now(UTC).timestamp()) % 900000 + 100000}"

        batch = self._client.batch()
        acct_ref = self._client.collection(self._accounts_col).document(account.customer_id)
        batch.update(acct_ref, {"available_balance_usd": new_balance})

        dsp_ref = self._client.collection(self._disputes_col).document(did)
        batch.update(
            dsp_ref,
            {
                "status": "REFUND_ISSUED",
                "refunded_amount_usd": rounded_amt,
                "resolution_note": f"Refunded ${rounded_amt:.2f} ({receipt_id}): {reason.strip()}",
            },
        )

        orig_txn_ref = self._client.collection(self._txns_col).document(dispute.transaction_id)
        if orig_txn_ref.get().exists:
            batch.update(orig_txn_ref, {"status": "REFUNDED"})

        credit_txn = TransactionRecord(
            transaction_id=credit_txn_id,
            customer_id=account.customer_id,
            posted_at=now_iso,
            merchant_or_counterparty=f"Cymbal Dispute Credit ({did})",
            channel="REFUND_CREDIT",
            amount_usd=rounded_amt,
            status="POSTED",
            reference_note=f"Receipt {receipt_id}: {reason.strip()[:180]}",
        )
        batch.set(
            self._client.collection(self._txns_col).document(credit_txn_id),
            credit_txn.model_dump(),
        )
        batch.commit()

        self.record_audit_event(
            AuditEvent(
                timestamp=now_iso,
                layer="MCP_LEDGER",
                verdict="ALLOW",
                principal=actor_principal,
                target=f"issue_dispute_refund ({did})",
                summary=(
                    f"Issued ${rounded_amt:.2f} refund to {account.customer_id} "
                    f"(new balance ${new_balance:.2f})."
                ),
            )
        )
        return RefundReceipt(
            receipt_id=receipt_id,
            dispute_id=did,
            customer_id=account.customer_id,
            refunded_amount_usd=rounded_amt,
            new_available_balance_usd=new_balance,
            dispute_status="REFUND_ISSUED",
            supervisor_approval_code=(
                supervisor_approval_code.strip() if supervisor_approval_code else None
            ),
            processed_at=now_iso,
        )

    def place_account_hold(
        self,
        customer_id: str,
        reason: str,
        hold_tier: Literal["HOLD_REVIEW", "HOLD_FRAUD"] = "HOLD_REVIEW",
        actor_principal: str = "mcp-client",
    ) -> AccountHoldReceipt:
        """
        Summary:
            Places a compliance or fraud hold on a customer profile in Firestore.

        Parameters:
            customer_id (str): Customer identifier (`CUST-...`).
            reason (str): Compliance/fraud justification.
            hold_tier (Literal["HOLD_REVIEW", "HOLD_FRAUD"]): Hold level.
            actor_principal (str): Calling identity recorded in audit trail.

        Return Value:
            AccountHoldReceipt: Hold confirmation receipt.

        Exceptions/Errors:
            LedgerNotFoundError: If customer does not exist.
            LedgerValidationError: If `reason` or `hold_tier` is invalid.
        """
        cid = _validate_id(customer_id, "CUST-", "customer_id")
        if hold_tier not in ("HOLD_REVIEW", "HOLD_FRAUD"):
            raise LedgerValidationError("hold_tier must be 'HOLD_REVIEW' or 'HOLD_FRAUD'.")
        if not isinstance(reason, str) or len(reason.strip()) < 5:
            raise LedgerValidationError("reason must be at least 5 characters.")

        account = self.get_account(cid)
        now_iso = datetime.now(UTC).isoformat()
        hold_id = f"HLD-{uuid.uuid4().hex[:8].upper()}"
        self._client.collection(self._accounts_col).document(cid).update(
            {"account_status": hold_tier, "hold_reason": reason.strip()}
        )
        self.record_audit_event(
            AuditEvent(
                timestamp=now_iso,
                layer="MCP_LEDGER",
                verdict="ALLOW",
                principal=actor_principal,
                target=f"place_account_hold ({cid})",
                summary=f"Placed {hold_tier} on {cid}: {reason.strip()}",
            )
        )
        return AccountHoldReceipt(
            hold_id=hold_id,
            customer_id=cid,
            previous_status=account.account_status,
            new_status=hold_tier,
            reason=reason.strip(),
            placed_at=now_iso,
        )

    def initiate_wire_reversal(
        self,
        transaction_id: str,
        reason: str,
        fraud_verified_ticket: str,
        actor_principal: str = "mcp-client",
    ) -> WireReversalReceipt:
        """
        Summary:
            Reverses an outbound `WIRE_OUT` transaction in Firestore and restores the
            customer's available balance.

        Parameters:
            transaction_id (str): Target wire transaction (`TXN-...`).
            reason (str): Recall explanation.
            fraud_verified_ticket (str): Mandatory Fraud Ops ticket (`FRD-...`).
            actor_principal (str): Calling identity recorded in audit trail.

        Return Value:
            WireReversalReceipt: Wire recall confirmation receipt.

        Exceptions/Errors:
            LedgerNotFoundError: If transaction or customer is missing.
            LedgerValidationError: If transaction is not an eligible `WIRE_OUT` or
                `fraud_verified_ticket` is malformed.
        """
        tid = _validate_id(transaction_id, "TXN-", "transaction_id")
        ticket = _validate_id(fraud_verified_ticket, "FRD-", "fraud_verified_ticket")
        if not isinstance(reason, str) or len(reason.strip()) < 5:
            raise LedgerValidationError("reason must be at least 5 characters.")

        txn_ref = self._client.collection(self._txns_col).document(tid)
        snap = txn_ref.get()
        if not snap.exists:
            raise LedgerNotFoundError(f"Transaction '{tid}' was not found.")
        txn = TransactionRecord.model_validate(snap.to_dict())
        if txn.channel != "WIRE_OUT":
            raise LedgerValidationError(
                f"Transaction '{tid}' is channel '{txn.channel}', not 'WIRE_OUT'."
            )
        if txn.status == "REVERSED":
            raise LedgerValidationError(f"Wire transaction '{tid}' is already reversed.")

        account = self.get_account(txn.customer_id)
        new_balance = round(account.available_balance_usd + txn.amount_usd, 2)
        now_iso = datetime.now(UTC).isoformat()
        reversal_id = f"WIR-{uuid.uuid4().hex[:8].upper()}"

        batch = self._client.batch()
        batch.update(txn_ref, {"status": "REVERSED"})
        batch.update(
            self._client.collection(self._accounts_col).document(account.customer_id),
            {"available_balance_usd": new_balance},
        )
        for dsp in self.list_disputes(customer_id=account.customer_id):
            if dsp.transaction_id == tid and dsp.status == "OPEN":
                batch.update(
                    self._client.collection(self._disputes_col).document(dsp.dispute_id),
                    {
                        "status": "REFUND_ISSUED",
                        "refunded_amount_usd": txn.amount_usd,
                        "resolution_note": f"Wire reversed ({reversal_id}, ticket {ticket})",
                    },
                )
        batch.commit()

        self.record_audit_event(
            AuditEvent(
                timestamp=now_iso,
                layer="MCP_LEDGER",
                verdict="ALLOW",
                principal=actor_principal,
                target=f"initiate_wire_reversal ({tid})",
                summary=(
                    f"Reversed wire {tid} (${txn.amount_usd:.2f}) for {account.customer_id} "
                    f"under ticket {ticket}."
                ),
            )
        )
        return WireReversalReceipt(
            reversal_id=reversal_id,
            transaction_id=tid,
            customer_id=account.customer_id,
            reversed_amount_usd=txn.amount_usd,
            new_available_balance_usd=new_balance,
            fraud_verified_ticket=ticket,
            processed_at=now_iso,
        )

    def record_audit_event(self, event: AuditEvent) -> None:
        """
        Summary:
            Writes an `AuditEvent` document to the Firestore audit collection.

        Parameters:
            event (AuditEvent): Validated audit entry.

        Return Value:
            None.

        Exceptions/Errors:
            None.
        """
        doc_id = f"{event.timestamp}_{uuid.uuid4().hex[:6]}"
        self._client.collection(self._events_col).document(doc_id).set(event.model_dump())

    def get_overview(self) -> LedgerOverview:
        """
        Summary:
            Fetches accounts, newest-first transactions, disputes, and up to 30
            recent ledger audit events from Firestore.

        Parameters:
            None.

        Return Value:
            LedgerOverview: Aggregated ledger snapshot.

        Exceptions/Errors:
            None.
        """
        accounts = self.search_customers("")
        transactions = self.list_transactions(limit=50)
        disputes = self.list_disputes()
        event_docs = list(self._client.collection(self._events_col).stream())
        events = sorted(
            (AuditEvent.model_validate(d.to_dict()) for d in event_docs),
            key=lambda e: e.timestamp,
            reverse=True,
        )[:30]
        return LedgerOverview(
            storage_backend=self.backend_name,
            accounts=accounts,
            transactions=transactions,
            disputes=disputes,
            ledger_events=events,
        )


_STORE_LOCK = threading.Lock()
_CACHED_STORE: LedgerStore | None = None


def get_ledger_store(force_refresh: bool = False) -> LedgerStore:
    """
    Summary:
        Factory returning a singleton `LedgerStore` configured from `STORAGE_BACKEND`
        (`firestore` or `local`, defaulting to `local` when unset).

    Parameters:
        force_refresh (bool): When `True`, discards any cached store instance.

    Return Value:
        LedgerStore: Configured `FirestoreLedgerStore` or `LocalFileLedgerStore`.

    Exceptions/Errors:
        ValueError: If `STORAGE_BACKEND` specifies an unsupported backend name.
    """
    global _CACHED_STORE
    with _STORE_LOCK:
        if _CACHED_STORE is not None and not force_refresh:
            return _CACHED_STORE

        backend = os.getenv("STORAGE_BACKEND", "local").strip().lower()
        if backend == "firestore":
            _CACHED_STORE = FirestoreLedgerStore()
        elif backend == "local":
            _CACHED_STORE = LocalFileLedgerStore()
        else:
            raise ValueError(
                f"Unsupported STORAGE_BACKEND='{backend}'. Expected 'firestore' or 'local'."
            )
        return _CACHED_STORE
