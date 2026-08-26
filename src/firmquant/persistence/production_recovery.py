"""Production recovery service using monotonic broker-order persistence."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from firmquant.broker.gateway import BrokerGateway

from .database import Database
from .production_repository import MonotonicExecutionLedgerRepository
from .recovery import AccountStateStore, RecoveryService


class ProductionRecoveryService(RecoveryService):
    """Recovery semantics identical to RecoveryService with stricter live order merging."""

    def __init__(
        self,
        *,
        database: Database,
        account_store: AccountStateStore | None,
        account_path: Path | None,
        gateway: BrokerGateway | None,
        clock: Callable[[], datetime],
    ) -> None:
        super().__init__(
            database=database,
            account_store=account_store,
            account_path=account_path,
            gateway=gateway,
            clock=clock,
        )
        self._orders = MonotonicExecutionLedgerRepository(database)


__all__ = ("ProductionRecoveryService",)
