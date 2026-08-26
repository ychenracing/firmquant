from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from firmquant.observability.notifiers import (
    Alert,
    AlertSeverity,
    AlertStore,
    NotifierFanout,
)
from firmquant.persistence.database import Database

NOW = datetime(2026, 8, 26, 8, tzinfo=UTC)


class RecordingNotifier:
    def __init__(self) -> None:
        self.alert_ids: list[str] = []

    def notify(self, alert: Alert) -> None:
        self.alert_ids.append(alert.alert_id)


class FailingWebhookNotifier:
    def notify(self, alert: Alert) -> None:
        del alert
        raise RuntimeError("https://user:secret@example.invalid/account-123")


def test_notifier_failure_does_not_rollback_trading_facts_and_creates_safe_alert(
    tmp_path: Path,
) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    successful = RecordingNotifier()
    try:
        with database.transaction():
            database.write(
                """
                INSERT INTO risk_events(
                    risk_event_id, severity, code, execution_id, symbol,
                    payload_json, payload_sha256, created_at
                ) VALUES ('risk-1', 'CRITICAL', 'BROKER_DISCONNECT_TIMEOUT', NULL,
                          NULL, '{}', ?, ?)
                """,
                ("0" * 64, NOW.isoformat()),
            )

        receipt = NotifierFanout(
            notifiers=(successful, FailingWebhookNotifier()),
            alert_store=AlertStore(database),
        ).publish(
            Alert.create(
                severity=AlertSeverity.CRITICAL,
                code="BROKER_DISCONNECT_TIMEOUT",
                payload={"diagnostic_code": "DISCONNECTED"},
                created_at=NOW,
            )
        )

        assert receipt.delivered_count == 1
        assert receipt.failed_count == 1
        assert successful.alert_ids == [receipt.alert_id]
        assert database.scalar("SELECT count(*) FROM risk_events WHERE risk_event_id = 'risk-1'") == 1
        row = database.query_one(
            "SELECT code, payload_json FROM alerts WHERE code = 'NOTIFIER_DELIVERY_FAILED'"
        )
        assert row is not None
        assert row["code"] == "NOTIFIER_DELIVERY_FAILED"
        assert "secret" not in str(row["payload_json"])
        assert "account-123" not in str(row["payload_json"])
    finally:
        database.close()
