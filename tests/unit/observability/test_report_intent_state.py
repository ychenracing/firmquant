from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from firmquant.observability.reports import DatabaseDailyReportBuilder
from firmquant.persistence.database import Database
from firmquant.persistence.repositories import DecisionSnapshotRepository
from tests.fixtures.session_cases import EXECUTION_SESSION, decision_snapshot, execution_snapshot

NOW = datetime(2026, 8, 25, 2, tzinfo=UTC)


def _persist_cash_snapshot(database: Database) -> None:
    snapshot = execution_snapshot().broker_snapshot
    with database.transaction():
        database.write(
            """
            INSERT INTO broker_snapshots(
                snapshot_id, account_id_hash, account_type, session_date, captured_at,
                broker_event_watermark, raw_payload_sha256, complete
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                snapshot.snapshot_id,
                snapshot.account.account_id_hash,
                snapshot.account.account_type.value,
                EXECUTION_SESSION.isoformat(),
                NOW.isoformat(),
                snapshot.broker_event_watermark,
                snapshot.raw_payload_sha256,
            ),
        )
        database.write(
            "INSERT INTO cash_snapshots(snapshot_id, available_cash, total_assets) VALUES (?, ?, ?)",
            (
                snapshot.snapshot_id,
                snapshot.account.available_cash.canonical,
                snapshot.account.total_assets.canonical,
            ),
        )


def test_report_distinguishes_missing_decision_from_valid_no_intent(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        _persist_cash_snapshot(database)
        builder = DatabaseDailyReportBuilder(
            database,
            clock=lambda: datetime(2026, 8, 25, 3, tzinfo=UTC),
        )

        missing = builder.build(EXECUTION_SESSION)
        assert missing.decision_id is None
        assert missing.intent_state == "MISSING_DECISION"

        no_intent = decision_snapshot(include_sell=False, include_buy=False)
        DecisionSnapshotRepository(database).append(no_intent)
        valid_zero = builder.build(EXECUTION_SESSION)
        assert valid_zero.decision_id == no_intent.decision_id
        assert valid_zero.intent_state == "NO_INTENT"
        assert valid_zero.orders == ()
    finally:
        database.close()
