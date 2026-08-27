from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from firmquant.domain.values import Money, Shares
from firmquant.persistence.account_authority import AccountBinding
from firmquant.reconciliation.account_preflight import evaluate_account_preflight
from firmquant.strategy.account_sync import sync_account
from tests.fixtures.broker_snapshots import completed_buy_snapshot, open_buy_account
from tests.fixtures.reconciliation_cases import NOW, healthy_reconciliation_facts


def _binding() -> AccountBinding:
    snapshot = completed_buy_snapshot()
    return AccountBinding.create(
        account_id_hash=snapshot.account.account_id_hash,
        account_type=snapshot.account.account_type,
        broker_snapshot_sha256="a" * 64,
        account_state_sha256="b" * 64,
        uquant_commit="1" * 40,
        uquant_code_fingerprint="c" * 64,
        data_hash="d" * 64,
        data_as_of="2026-01-05",
        data_symbols=("sz300308",),
        created_at=NOW,
    )


def test_known_system_fill_explains_pre_sync_cash_and_position_delta() -> None:
    account = open_buy_account()
    facts = healthy_reconciliation_facts()

    result = evaluate_account_preflight(
        snapshot=facts.broker_snapshot,
        account=account,
        operational_ledger=facts.operational_ledger,
        binding=_binding(),
        cash_tolerance=Money(Decimal("0.01")),
    )

    assert result.passed is True
    assert result.blockers == ()
    assert result.explained_fill_ids == ("broker-fill-1",)


def test_unexplained_cash_change_is_blocked_before_account_sync() -> None:
    account = open_buy_account()
    facts = healthy_reconciliation_facts()
    broker = facts.broker_snapshot
    changed = replace(
        broker,
        account=replace(
            broker.account,
            available_cash=Money(broker.account.available_cash.value - Decimal("10")),
            total_assets=Money(broker.account.total_assets.value - Decimal("10")),
        ),
        raw_payload_sha256="8" * 64,
    )

    result = evaluate_account_preflight(
        snapshot=changed,
        account=account,
        operational_ledger=facts.operational_ledger,
        binding=_binding(),
        cash_tolerance=Money(Decimal("0.01")),
    )

    assert result.passed is False
    assert "UNEXPLAINED_CASH_CHANGE" in result.blockers


def test_manual_position_change_is_not_adopted_as_strategy_lifecycle() -> None:
    account = open_buy_account()
    sync_account(account, completed_buy_snapshot())
    facts = healthy_reconciliation_facts()
    broker = facts.broker_snapshot
    changed = replace(
        broker,
        account=replace(
            broker.account,
            available_cash=Money(Decimal("1994.9")),
            total_assets=Money(Decimal("1994.9")),
        ),
        positions=(),
        orders=(),
        fills=(),
        raw_payload_sha256="7" * 64,
    )

    result = evaluate_account_preflight(
        snapshot=changed,
        account=account,
        operational_ledger=replace(
            facts.operational_ledger,
            orders=(),
            known_broker_fill_ids=frozenset(),
        ),
        binding=_binding(),
        cash_tolerance=Money(Decimal("0.01")),
    )

    assert "UNEXPLAINED_POSITION_CHANGE" in result.blockers
    assert "CORPORATE_ACTION_SUSPECTED" in result.blockers
    assert "UNEXPLAINED_CASH_CHANGE" in result.blockers


def test_external_order_blocks_preflight_even_when_account_economics_match() -> None:
    account = open_buy_account()
    facts = healthy_reconciliation_facts()
    order = facts.broker_snapshot.orders[0]
    external = replace(
        order,
        broker_order_id="manual-order",
        client_order_id="MANUAL-ORDER",
        filled_shares=Shares(0),
        raw_payload_sha256="6" * 64,
    )
    changed = replace(
        facts.broker_snapshot,
        orders=(*facts.broker_snapshot.orders, external),
        raw_payload_sha256="5" * 64,
    )

    result = evaluate_account_preflight(
        snapshot=changed,
        account=account,
        operational_ledger=facts.operational_ledger,
        binding=_binding(),
        cash_tolerance=Money(Decimal("0.01")),
    )

    assert "EXTERNAL_BROKER_ORDER" in result.blockers
