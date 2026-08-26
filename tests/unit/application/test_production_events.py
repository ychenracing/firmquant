from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from firmquant.application.production_events import ProductionEventJournal
from firmquant.broker.normalization import normalize_broker_event
from firmquant.persistence.database import Database

NOW = datetime(2026, 8, 25, 1, 31, tzinfo=UTC)


def error_event(event_type: str):
    return normalize_broker_event(
        {
            "event_id": f"xtquant-{event_type.lower()}-" + "a" * 64,
            "event_type": event_type,
            "payload": {
                "broker_order_id": "9001",
                "client_order_id": "fq" + "b" * 22 if event_type == "ORDER_ERROR" else None,
                "error_code": 31,
                "message_sha256": "c" * 64,
                "session_date": "2026-08-25",
                "event_time": "2026-08-25T09:31:00+08:00",
            },
        },
        received_at=NOW,
    )


def test_operational_error_is_persisted_and_requests_explicit_halt(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        journal = ProductionEventJournal(database)
        event = error_event("ORDER_ERROR")

        assert journal.append(event) is True
        assert journal.pending_halt_reason == "BROKER_ORDER_ERROR"
        assert database.scalar("SELECT count(*) FROM broker_events") == 1
        row = database.query_one("SELECT code, severity FROM risk_events")
        assert row is not None
        assert tuple(row) == ("BROKER_ORDER_ERROR", "CRITICAL")

        assert journal.append(event) is False
        assert database.scalar("SELECT count(*) FROM broker_events") == 1
        assert database.scalar("SELECT count(*) FROM risk_events") == 1
    finally:
        database.close()


def test_disconnect_is_durable_and_halt_reason_is_monotonic(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        journal = ProductionEventJournal(database)
        order_error = error_event("CANCEL_ERROR")
        disconnected = normalize_broker_event(
            {
                "event_id": "xtquant-disconnected-" + "d" * 64,
                "event_type": "DISCONNECTED",
                "payload": {
                    "session_date": "2026-08-25",
                    "event_time": "2026-08-25T09:32:00+08:00",
                },
            },
            received_at=NOW,
        )

        journal.append(order_error)
        assert journal.pending_halt_reason == "BROKER_CANCEL_ERROR"
        journal.append(disconnected)
        assert journal.pending_halt_reason == "BROKER_CANCEL_ERROR"
        assert database.scalar("SELECT count(*) FROM risk_events") == 2
    finally:
        database.close()
