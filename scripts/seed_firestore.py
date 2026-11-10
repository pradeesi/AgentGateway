#!/usr/bin/env python3
"""
Purpose: CLI utility invoked by `./install.sh` and `./reset_demo.sh` to seed or
         reset Cymbal Bank customer accounts, transactions, and dispute cases in
         Google Cloud Firestore (or the local JSON store).
Architecture/Context: Wraps `common.ledger_store.get_ledger_store().reset_to_seed()`
         with structured JSON output and non-zero exit codes on failure.
Dependencies/Side Effects: Reads `STORAGE_BACKEND`, `PROJECT_ID`, and
         `FIRESTORE_DATABASE` from environment variables.
Author: Pradeep Singh
License: Apache-2.0
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Ensure repository root is importable when executed directly as `./scripts/seed_firestore.py`.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.ledger_store import get_ledger_store  # noqa: E402


def main() -> int:
    """
    Summary:
        Executes `reset_to_seed()` against the configured `LedgerStore` and prints
        the seeded document counts to stdout.

    Parameters:
        None.

    Return Value:
        int: Process exit code (`0` on success, `1` on error).

    Exceptions/Errors:
        None; catches exceptions and writes structured diagnostics to stderr.
    """
    try:
        store = get_ledger_store(force_refresh=True)
        counts = store.reset_to_seed()
        print(
            json.dumps(
                {
                    "status": "ok",
                    "backend": store.backend_name,
                    "seeded_counts": counts,
                },
                sort_keys=True,
            )
        )
        return 0
    except Exception as exc:
        print(
            json.dumps({"status": "error", "detail": str(exc)}),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
