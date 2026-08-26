from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from firmquant.broker.production_snapshot import (
    ProductionSnapshotCollector,
    ProductionSnapshotUnstable,
)
from firmquant.domain.values import Money, Shares
from tests.fixtures.session_cases import execution_snapshot

NOW = datetime(2026, 8, 25, 1, 31, tzinfo=UTC)


class ScriptedReads:
    def __init__(self, snapshots: list[object]) -> None:
        self.snapshots = snapshots
        self.calls = 0

    def _current(self):
        index = min(self.calls // 4, len(self.snapshots) - 1)
        return self.snapshots[index]

    def query_account(self):
        value = self._current().account
        self.calls += 1
        return value

    def query_positions(self):
        value = self._current().positions
        self.calls += 1
        return value

    def query_orders(self):
        value = self._current().orders
        self.calls += 1
        return value

    def query_fills(self):
        value = self._current().fills
        self.calls += 1
        return value


def test_snapshot_allows_mark_to_market_changes_when_economic_quantities_are_stable() -> None:
    base = execution_snapshot().broker_snapshot
    first = replace(
        base,
        account=replace(base.account, total_assets=Money(Decimal("1000"))),
        positions=tuple(
            replace(position, market_value=Money(position.market_value.value + Decimal("1")))
            for position in base.positions
        ),
    )
    second = replace(
        base,
        account=replace(base.account, total_assets=Money(Decimal("1001"))),
        positions=tuple(
            replace(position, market_value=Money(position.market_value.value + Decimal("2")))
            for position in base.positions
        ),
    )
    broker = ScriptedReads([first, second])

    snapshot = ProductionSnapshotCollector(
        broker=broker,
        clock=lambda: NOW,
        max_attempts=2,
    ).capture()

    assert snapshot.account.total_assets == Money(Decimal("1001"))
    assert snapshot.positions == second.positions
    assert snapshot.complete is True


def test_snapshot_retries_then_fails_when_position_quantities_keep_changing() -> None:
    base = execution_snapshot().broker_snapshot
    position = base.positions[0]
    low = replace(
        base,
        positions=(
            replace(position, total_shares=Shares(100), sellable_shares=Shares(100)),
        ),
    )
    high = replace(
        base,
        positions=(
            replace(position, total_shares=Shares(200), sellable_shares=Shares(200)),
        ),
    )
    broker = ScriptedReads([low, high, low, high])

    with pytest.raises(ProductionSnapshotUnstable, match="bounded"):
        ProductionSnapshotCollector(
            broker=broker,
            clock=lambda: NOW,
            max_attempts=2,
        ).capture()
    assert broker.calls == 16


def test_snapshot_fails_closed_on_account_identity_change() -> None:
    base = execution_snapshot().broker_snapshot
    changed = replace(
        base,
        account=replace(base.account, account_id_hash="f" * 64),
    )
    broker = ScriptedReads([base, changed])

    with pytest.raises(ProductionSnapshotUnstable, match="account identity"):
        ProductionSnapshotCollector(
            broker=broker,
            clock=lambda: NOW,
            max_attempts=3,
        ).capture()
