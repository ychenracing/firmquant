from __future__ import annotations

from dataclasses import replace

import pytest
from uquant.account import economic_state_sha256

from firmquant.strategy.account_sync import StrategySyncError, sync_account
from tests.fixtures.broker_snapshots import (
    cancelled_buy_snapshot,
    completed_buy_snapshot,
    open_buy_account,
)


def test_sync_is_idempotent_and_preserves_broker_sellability() -> None:
    account = open_buy_account()
    snapshot = completed_buy_snapshot()

    first = sync_account(account, snapshot)
    second = sync_account(account, snapshot)

    assert first.fills_imported == 1
    assert second.fills_imported == 0
    assert first.account_after_sha256 == second.account_after_sha256
    assert len(account.fills) == 1
    assert account.cash == 994.9
    assert account.positions["sz300308"].sellable_shares("2026-01-06") == 0


def test_unknown_economic_order_id_rejects_without_mutating_account() -> None:
    account = open_buy_account()
    before = economic_state_sha256(account)
    snapshot = completed_buy_snapshot()
    unknown = replace(snapshot.orders[0], client_order_id="O000000999")

    with pytest.raises(StrategySyncError, match="unknown uquant order"):
        sync_account(account, replace(snapshot, orders=(unknown,)))

    assert economic_state_sha256(account) == before


def test_broker_cancellation_confirmation_closes_uquant_pending_order() -> None:
    account = open_buy_account()

    receipt = sync_account(account, cancelled_buy_snapshot())

    assert receipt.pending_orders == 0
    assert account.pending_orders == []
    assert account.order_ledger[0].status == "CANCELLED"
