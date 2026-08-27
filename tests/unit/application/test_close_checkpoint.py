from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from firmquant.application.close_checkpoint import (
    CloseCheckpointError,
    CloseCheckpointStore,
    CloseStep,
)
from firmquant.persistence.database import Database

SESSION = date(2026, 8, 25)
NOW = datetime(2026, 8, 25, 9, tzinfo=UTC)
STEPS = tuple(CloseStep)


def _evidence(step: CloseStep) -> dict[str, object]:
    return {"step": step.value, "identity": f"receipt-{step.value.lower()}"}


@pytest.mark.parametrize("crash_after", range(1, len(STEPS)))
def test_restart_resumes_from_each_durable_close_boundary(
    tmp_path: Path,
    crash_after: int,
) -> None:
    database_path = tmp_path / "firmquant.sqlite3"
    database = Database.open(database_path)
    try:
        store = CloseCheckpointStore(database)
        for step in STEPS[:crash_after]:
            store.append(SESSION, step, evidence=_evidence(step), created_at=NOW)
        assert store.latest_incomplete_session() == SESSION
        assert store.completed(SESSION) is None
    finally:
        database.close()

    recovered = Database.open(database_path)
    try:
        store = CloseCheckpointStore(recovered)
        for step in STEPS[:crash_after]:
            checkpoint = store.load(SESSION, step)
            assert checkpoint is not None
            assert checkpoint.evidence == _evidence(step)
        for step in STEPS[crash_after:]:
            assert store.load(SESSION, step) is None
            store.append(SESSION, step, evidence=_evidence(step), created_at=NOW)
        completed = store.completed(SESSION)
        assert completed is not None
        assert completed.step is CloseStep.COMPLETED
        assert store.latest_incomplete_session() is None
        assert store.latest_completed_session() == SESSION
    finally:
        recovered.close()


def test_checkpoint_order_idempotency_and_conflict_are_fail_closed(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        store = CloseCheckpointStore(database)
        with pytest.raises(CloseCheckpointError, match="predecessor"):
            store.append(
                SESSION,
                CloseStep.DATA_VALIDATED,
                evidence=_evidence(CloseStep.DATA_VALIDATED),
                created_at=NOW,
            )

        first = store.append(
            SESSION,
            CloseStep.EOD_RECONCILED,
            evidence=_evidence(CloseStep.EOD_RECONCILED),
            created_at=NOW,
        )
        duplicate = store.append(
            SESSION,
            CloseStep.EOD_RECONCILED,
            evidence=_evidence(CloseStep.EOD_RECONCILED),
            created_at=NOW,
        )
        assert duplicate == first
        assert database.scalar("SELECT count(*) FROM audit_events WHERE category = 'CLOSE_SESSION'") == 1

        with pytest.raises(CloseCheckpointError, match="conflicts"):
            store.append(
                SESSION,
                CloseStep.EOD_RECONCILED,
                evidence={"step": "changed"},
                created_at=NOW,
            )
        with pytest.raises(ValueError, match="timezone-aware"):
            store.append(
                SESSION,
                CloseStep.EOD_RECONCILED,
                evidence=_evidence(CloseStep.EOD_RECONCILED),
                created_at=datetime(2026, 8, 25, 9),
            )
        with pytest.raises(TypeError, match="identity"):
            store.load(datetime(2026, 8, 25, tzinfo=UTC), CloseStep.EOD_RECONCILED)  # type: ignore[arg-type]
    finally:
        database.close()


def test_latest_receipts_distinguish_completed_from_newer_incomplete_session(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        store = CloseCheckpointStore(database)
        for step in STEPS:
            store.append(SESSION, step, evidence=_evidence(step), created_at=NOW)
        next_session = date(2026, 8, 26)
        store.append(
            next_session,
            CloseStep.EOD_RECONCILED,
            evidence=_evidence(CloseStep.EOD_RECONCILED),
            created_at=datetime(2026, 8, 26, 9, tzinfo=UTC),
        )
        assert store.latest_completed_session() == SESSION
        assert store.latest_incomplete_session() == next_session
    finally:
        database.close()
