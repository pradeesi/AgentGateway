"""
Purpose: Structured semantic JSON logging helpers aligned with Google Cloud Logging.
Architecture/Context: Shared across FastMCP server, Rogue endpoint, Cymbal Bank
         Operations Portal, and ADK agent tools so every log line parses cleanly
         into `jsonPayload` with `severity` in Cloud Logging.
Dependencies/Side Effects: Configures Python's standard `logging` module streams;
         reads `LOG_LEVEL` from environment variables (defaults to `INFO`).
Author: Pradeep Singh
License: Apache-2.0
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from typing import Any

_SENSITIVE_KEY_FRAGMENTS = (
    "secret",
    "token",
    "password",
    "authorization",
    "apikey",
    "api_key",
    "ssn",
    "card_pan",
)


class CloudJsonFormatter(logging.Formatter):
    """
    Summary:
        Formats Python `LogRecord` instances as single-line JSON objects compatible
        with Google Cloud Logging structured ingestion (`severity`, `message`, `time`).

    Parameters:
        None.

    Return Value:
        CloudJsonFormatter instance.

    Exceptions/Errors:
        None.
    """

    def format(self, record: logging.LogRecord) -> str:
        """
        Summary:
            Serializes a `LogRecord` and any attached `structured_fields` dictionary
            into a single JSON line while scrubbing sensitive keys.

        Parameters:
            record (logging.LogRecord): Standard Python log record emitted by a logger.

        Return Value:
            str: Single-line JSON representation suitable for stdout/Cloud Logging.

        Exceptions/Errors:
            None; non-serializable values fall back to `str()`.
        """
        payload: dict[str, Any] = {
            "severity": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "timestamp": datetime.now(UTC).isoformat(),
        }
        extra_fields = getattr(record, "structured_fields", None)
        if isinstance(extra_fields, dict):
            payload.update(_redact_mapping(extra_fields))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, sort_keys=True)


def _redact_mapping(fields: dict[str, Any]) -> dict[str, Any]:
    """
    Summary:
        Recursively masks dictionary entries whose keys match sensitive credential or
        raw PII identifiers so secrets never leak into application logs.

    Parameters:
        fields (dict[str, Any]): Arbitrary structured event dictionary.

    Return Value:
        dict[str, Any]: Sanitized copy with sensitive values replaced by `"[REDACTED]"`.

    Exceptions/Errors:
        None.
    """
    sanitized: dict[str, Any] = {}
    for key, value in fields.items():
        lowered = str(key).lower()
        if any(fragment in lowered for fragment in _SENSITIVE_KEY_FRAGMENTS):
            sanitized[key] = "[REDACTED]"
        elif isinstance(value, dict):
            sanitized[key] = _redact_mapping(value)
        else:
            sanitized[key] = value
    return sanitized


def configure_structured_logging(service_name: str) -> logging.Logger:
    """
    Summary:
        Configures root/service logging with `CloudJsonFormatter` and returns a named
        service logger honoring the `LOG_LEVEL` environment variable.

    Parameters:
        service_name (str): Logical service or component identifier (non-empty).

    Return Value:
        logging.Logger: Configured logger writing structured JSON lines to stdout.

    Exceptions/Errors:
        ValueError: Raised if `service_name` is empty or whitespace-only.
    """
    if not service_name or not service_name.strip():
        raise ValueError("service_name must be a non-empty string.")

    level_name = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)

    root_logger = logging.getLogger()
    if not any(isinstance(h.formatter, CloudJsonFormatter) for h in root_logger.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(CloudJsonFormatter())
        root_logger.handlers = [handler]
    root_logger.setLevel(level)

    logger = logging.getLogger(service_name.strip())
    logger.setLevel(level)
    return logger


def log_structured(
    logger: logging.Logger,
    level: int,
    event: str,
    **fields: Any,
) -> None:
    """
    Summary:
        Emits a structured semantic log entry carrying an `event` identifier and
        key-value metadata fields.

    Parameters:
        logger (logging.Logger): Target logger instance.
        level (int): Logging level constant (e.g., `logging.INFO`, `logging.WARNING`).
        event (str): Machine-parsable event slug (e.g., `"mcp_tool_invoked"`).
        **fields (Any): Arbitrary contextual fields to attach to the JSON payload.

    Return Value:
        None.

    Exceptions/Errors:
        None.
    """
    structured = {"event": event, **fields}
    logger.log(level, event, extra={"structured_fields": structured})
