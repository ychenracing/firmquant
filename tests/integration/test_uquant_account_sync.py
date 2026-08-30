from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from uquant.account import economic_state_sha256
from uquant.types import AccountState

from firmquant.strategy.account_sync import StrategySyncError, sync_account
from firmquant.strategy.runtime_account import _load_account
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


def test_old_schema_five_account_requires_reviewed_rebaseline(tmp_path: Path) -> None:
    account = AccountState.empty(1_000.0)
    account.code_hash = "1" * 64
    account.data_hash = "2" * 64
    payload = account.to_dict()
    payload["schema_version"] = 5
    path = tmp_path / "schema-five-account.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match="cannot be loaded") as captured:
        _load_account(path)

    assert captured.value.__cause__ is not None
    assert "schema 5" in str(captured.value.__cause__)
