from __future__ import annotations

import io
import json
import logging
from datetime import date
from pathlib import Path

from firmquant.observability.logging import EventContext, configure_logging, log_event


def test_json_and_console_logs_are_context_complete_and_recursively_redacted() -> None:
    json_stream = io.StringIO()
    console_stream = io.StringIO()
    logger = configure_logging(
        logger_name="firmquant.test.logging",
        json_stream=json_stream,
        console_stream=console_stream,
        level=logging.INFO,
    )

    log_event(
        logger,
        level=logging.WARNING,
        event="BROKER_DISCONNECT_TIMEOUT",
        message="券商连接超时",
        context=EventContext(
            session=date(2026, 8, 26),
            correlation_id="correlation-1",
            decision_id="decision-safe",
            execution_id="execution-safe",
            uquant_order_id="O-SAFE-1",
            broker_order_id="broker-safe-1",
            symbol="300308.SZ",
        ),
        payload={
            "password": "broker-password",
            "nested": {"session_token": "session-secret"},
            "report_path": Path("C:/private/firmquant/report.json"),
            "reason_code": "BROKER_DISCONNECT_TIMEOUT",
        },
    )

    payload = json.loads(json_stream.getvalue())
    assert payload.keys() >= {
        "timestamp",
        "session",
        "correlation_id",
        "decision_id",
        "execution_id",
        "uquant_order_id",
        "broker_order_id",
        "symbol",
        "severity",
        "event",
        "message",
        "payload",
    }
    assert payload["severity"] == "WARNING"
    assert payload["payload"] == {
        "nested": {"session_token": "<redacted>"},
        "password": "<redacted>",
        "reason_code": "BROKER_DISCONNECT_TIMEOUT",
        "report_path": "<redacted>",
    }
    rendered = json_stream.getvalue() + console_stream.getvalue()
    assert "broker-password" not in rendered
    assert "session-secret" not in rendered
    assert "C:/private" not in rendered
    assert "WARNING BROKER_DISCONNECT_TIMEOUT" in console_stream.getvalue()


def test_missing_context_fields_are_emitted_as_null_not_omitted() -> None:
    json_stream = io.StringIO()
    logger = configure_logging(
        logger_name="firmquant.test.logging.empty-context",
        json_stream=json_stream,
        console_stream=None,
    )

    log_event(
        logger,
        level=logging.INFO,
        event="RUNTIME_READY",
        message="运行就绪",
    )

    payload = json.loads(json_stream.getvalue())
    for field in (
        "session",
        "correlation_id",
        "decision_id",
        "execution_id",
        "uquant_order_id",
        "broker_order_id",
        "symbol",
    ):
        assert field in payload
        assert payload[field] is None


def test_account_identifiers_are_redacted_by_key() -> None:
    json_stream = io.StringIO()
    logger = configure_logging(
        logger_name="firmquant.test.logging.account-redaction",
        json_stream=json_stream,
        console_stream=None,
    )

    account_number = "".join(("1234", "5678", "9012"))
    account_no = "".join(("9988", "7766", "5544"))
    log_event(
        logger,
        level=logging.INFO,
        event="BROKER_SNAPSHOT_OBSERVED",
        message="券商快照已读取",
        payload={
            "account_number": account_number,
            "nested": {"account_no": account_no, "account_hash": "f" * 64},
        },
    )

    rendered = json_stream.getvalue()
    assert account_number not in rendered
    assert account_no not in rendered
    assert "f" * 64 not in rendered
    assert json.loads(rendered)["payload"] == {
        "account_number": "<redacted>",
        "nested": {"account_no": "<redacted>", "account_hash": "<redacted>"},
    }
