from __future__ import annotations

from pathlib import Path

from firmquant.domain.events import CancelNotAccepted
from firmquant.domain.orders import OrderState
from firmquant.persistence.database import Database
from tests.fixtures.recovery_cases import NOW, acknowledge_locally, broker_order, create_submitting_case


def test_definite_cancel_rejection_restores_pre_cancel_open_state(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        case = create_submitting_case(database)
        acknowledged = acknowledge_locally(case, broker_order(case.command))
        with database.transaction():
            cancelling, _ = case.repository.begin_cancel(acknowledged, started_at=NOW)

        restored = cancelling.apply(
            CancelNotAccepted(
                event_id="cancel-not-accepted",
                evidence_sha256="e" * 64,
            )
        )

        assert cancelling.state is OrderState.CANCEL_REQUESTED
        assert restored.state is OrderState.ACKNOWLEDGED
    finally:
        database.close()
