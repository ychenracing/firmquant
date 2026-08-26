from __future__ import annotations

from pathlib import Path

from firmquant.persistence.database import Database
from firmquant.persistence.production_recovery import ProductionRecoveryService
from firmquant.persistence.production_repository import MonotonicExecutionLedgerRepository
from tests.fixtures.recovery_cases import NOW


def test_production_recovery_uses_monotonic_execution_repository(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        service = ProductionRecoveryService(
            database=database,
            account_store=None,
            account_path=None,
            gateway=None,
            clock=lambda: NOW,
        )
        assert isinstance(service._orders, MonotonicExecutionLedgerRepository)  # noqa: SLF001
        assert service._orders.database is database  # noqa: SLF001
    finally:
        database.close()
