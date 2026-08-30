from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from firmquant.application.execution_evidence import BlockerCode
from firmquant.domain.broker_facts import BrokerOrderStatus, Side
from firmquant.domain.values import Price, Shares, Symbol
from firmquant.execution.execution_replay import (
    DailyBar,
    ReplayAccount,
    ReplayOrderResult,
    ReplaySessionResult,
    ReplaySide,
)
from firmquant.execution.planner import ExecutionPlan, PlannedOrder
from firmquant.execution.replay_runner import (
    ExecutionReplayError,
    _broker_execution_facts,
    _replay_orders,
    _tracking,
    _updated_average_costs,
)

_SESSION = date(2026, 8, 28)


def _canonical(symbol: str) -> str:
    return Symbol.parse(symbol).canonical


def _planned_order(*, symbol: str, side: Side, shares: int, price: str) -> PlannedOrder:
    current = shares if side is Side.SELL else 0
    target = 0 if side is Side.SELL else shares
    return PlannedOrder(
        decision_id="decision-1",
        uquant_order_id=f"uquant-{side.value.lower()}-{symbol}",
        symbol=Symbol.parse(symbol),
        side=side,
        target_weight=Decimal("0.5"),
        uquant_authorized_shares=Shares(shares),
        current_shares=Shares(current),
        target_shares=Shares(target),
        trading_unit=Shares(100),
        limit_price=Price(Decimal(price)),
        strategy_session=_SESSION,
        execution_session=_SESSION,
        uquant_source_sha="a" * 40,
        reason_code="TARGET_REBALANCE",
    )


def _plan(*orders: PlannedOrder) -> ExecutionPlan:
    return ExecutionPlan(
        plan_id="plan-1",
        decision_id="decision-1",
        strategy_session=_SESSION,
        execution_session=_SESSION,
        broker_snapshot_sha256="b" * 64,
        orders=orders,
        blockers=(),
        created_at=datetime(2026, 8, 28, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
    )


def _bar(symbol: str, price: str) -> DailyBar:
    value = Decimal(price)
    return DailyBar(
        session=_SESSION,
        symbol=_canonical(symbol),
        open=value,
        high=value,
        low=value,
        close=value,
        previous_close=value,
        volume=100_000,
        suspended=False,
        limit_up=value * Decimal("1.1"),
        limit_down=value * Decimal("0.9"),
    )


def _order_result(
    *,
    symbol: str,
    side: ReplaySide,
    requested: int,
    filled: int,
    price: Decimal | None,
    commission: Decimal = Decimal("0"),
    transfer_fee: Decimal = Decimal("0"),
) -> ReplayOrderResult:
    return ReplayOrderResult(
        symbol=_canonical(symbol),
        side=side,
        requested_shares=requested,
        authorized=True,
        filled_shares=filled,
        fill_price=price,
        blocker=None if filled == requested else BlockerCode.VOLUME_LIMIT,
        commission=commission,
        stamp_duty=Decimal("0"),
        transfer_fee=transfer_fee,
        slippage_cost=Decimal("0"),
        unfilled_notional=Decimal("0"),
        unfilled_loss=Decimal("0"),
    )


def _session_result(orders: tuple[ReplayOrderResult, ...], after: ReplayAccount) -> ReplaySessionResult:
    return ReplaySessionResult(
        session=_SESSION,
        orders=orders,
        ending_account=after,
        commissions=sum((item.commission for item in orders), start=Decimal(0)),
        stamp_duty=sum((item.stamp_duty for item in orders), start=Decimal(0)),
        transfer_fees=sum((item.transfer_fee for item in orders), start=Decimal(0)),
        slippage_cost=Decimal("0"),
        unfilled_notional=Decimal("0"),
        unfilled_loss=Decimal("0"),
        turnover_notional=Decimal("0"),
        partial_fill_count=0,
        price_limit_blocks=0,
        suspension_blocks=0,
        incomplete_sell_blocked_buys=0,
    )


@pytest.mark.parametrize(
    ("cash", "buy_depends_on_sell"),
    [(Decimal("999"), True), (Decimal("1000"), False)],
)
def test_replay_orders_preserve_plan_economics_and_mark_cash_dependency(
    cash: Decimal, buy_depends_on_sell: bool
) -> None:
    plan = _plan(
        _planned_order(symbol="600000.SH", side=Side.SELL, shares=100, price="8"),
        _planned_order(symbol="000001.SZ", side=Side.BUY, shares=100, price="10"),
    )

    orders = _replay_orders(
        plan,
        ReplayAccount(
            cash=cash,
            positions={_canonical("600000.SH"): 100},
            sellable={_canonical("600000.SH"): 100},
        ),
        Decimal("0.25"),
    )

    assert [(item.symbol, item.side, item.shares, item.limit_price) for item in orders] == [
        (_canonical("600000.SH"), ReplaySide.SELL, 100, Decimal("8")),
        (_canonical("000001.SZ"), ReplaySide.BUY, 100, Decimal("10")),
    ]
    assert orders[0].depends_on_sell_proceeds is False
    assert orders[1].depends_on_sell_proceeds is buy_depends_on_sell
    assert all(item.max_volume_participation == Decimal("0.25") for item in orders)


def test_tracking_measures_target_and_account_only_symbols() -> None:
    decision = SimpleNamespace(uquant_payload={"targets": [{"symbol": "000001.SZ", "weight": "0.5"}]})
    account = ReplayAccount(
        cash=Decimal("400"),
        positions={_canonical("000001.SZ"): 40, _canonical("600000.SH"): 10},
        sellable={_canonical("000001.SZ"): 40, _canonical("600000.SH"): 10},
    )

    errors, weighted, notional = _tracking(
        decision,
        account,
        {
            _canonical("000001.SZ"): _bar("000001.SZ", "10"),
            _canonical("600000.SH"): _bar("600000.SH", "20"),
        },
        target_equity=Decimal("1000"),
    )

    assert errors == [Decimal("0.2"), Decimal("0.1")]
    assert weighted == Decimal("80.0")
    assert notional == Decimal("600")


@pytest.mark.parametrize(
    ("targets", "message"),
    [
        (None, "targets are unavailable"),
        (["not-an-object"], "payload is malformed"),
        ([{"symbol": "000001.SZ", "weight": True}], "weight is malformed"),
        ([{"symbol": "000001.SZ", "weight": "NaN"}], "outside bounds"),
        ([{"symbol": "000001.SZ", "weight": "1.01"}], "outside bounds"),
    ],
)
def test_tracking_rejects_noncanonical_targets(targets: object, message: str) -> None:
    with pytest.raises(ExecutionReplayError, match=message):
        _tracking(
            SimpleNamespace(uquant_payload={"targets": targets}),
            ReplayAccount(cash=Decimal("1000"), positions={}, sellable={}),
            {},
            target_equity=Decimal("1000"),
        )


def test_tracking_rejects_nonpositive_equity_and_missing_bar() -> None:
    decision = SimpleNamespace(uquant_payload={"targets": [{"symbol": "000001.SZ", "weight": 1}]})
    account = ReplayAccount(cash=Decimal("1000"), positions={}, sellable={})

    with pytest.raises(ExecutionReplayError, match="target equity must be positive"):
        _tracking(decision, account, {}, target_equity=Decimal("0"))
    with pytest.raises(ExecutionReplayError, match="bar is unavailable"):
        _tracking(decision, account, {}, target_equity=Decimal("1000"))


def test_updated_average_costs_apply_buy_fees_and_remove_fully_sold_position() -> None:
    before = ReplayAccount(
        cash=Decimal("1000"),
        positions={_canonical("000001.SZ"): 100, _canonical("600000.SH"): 100},
        sellable={_canonical("000001.SZ"): 100, _canonical("600000.SH"): 100},
    )
    after = ReplayAccount(
        cash=Decimal("0"),
        positions={_canonical("000001.SZ"): 200},
        sellable={_canonical("000001.SZ"): 100},
    )
    result = _session_result(
        (
            _order_result(
                symbol="000001.SZ",
                side=ReplaySide.BUY,
                requested=100,
                filled=100,
                price=Decimal("10"),
                commission=Decimal("1"),
                transfer_fee=Decimal("1"),
            ),
            _order_result(
                symbol="600000.SH",
                side=ReplaySide.SELL,
                requested=100,
                filled=100,
                price=Decimal("8"),
            ),
            _order_result(
                symbol="300001.SZ",
                side=ReplaySide.BUY,
                requested=100,
                filled=0,
                price=None,
            ),
        ),
        after,
    )

    assert _updated_average_costs(
        before,
        after,
        {_canonical("000001.SZ"): Decimal("5"), _canonical("600000.SH"): Decimal("7")},
        result,
    ) == {_canonical("000001.SZ"): Decimal("7.51000000")}


def test_updated_average_costs_preserve_cost_on_partial_sell() -> None:
    before = ReplayAccount(
        cash=Decimal("0"),
        positions={_canonical("600000.SH"): 200},
        sellable={_canonical("600000.SH"): 200},
    )
    after = ReplayAccount(
        cash=Decimal("800"),
        positions={_canonical("600000.SH"): 100},
        sellable={_canonical("600000.SH"): 100},
    )
    result = _session_result(
        (
            _order_result(
                symbol="600000.SH",
                side=ReplaySide.SELL,
                requested=100,
                filled=100,
                price=Decimal("8"),
            ),
        ),
        after,
    )

    assert _updated_average_costs(before, after, {_canonical("600000.SH"): Decimal("7.25")}, result) == {
        _canonical("600000.SH"): Decimal("7.25")
    }


@pytest.mark.parametrize(
    ("item", "message"),
    [
        (SimpleNamespace(symbol=None, filled_shares=1), "result is malformed"),
        (
            SimpleNamespace(symbol="000001.SZ", filled_shares=True, side=ReplaySide.BUY),
            "result is malformed",
        ),
        (
            SimpleNamespace(
                symbol="000001.SZ",
                filled_shares=100,
                side=ReplaySide.BUY,
                fill_price=None,
            ),
            "fill economics are malformed",
        ),
        (
            SimpleNamespace(
                symbol="000001.SZ",
                filled_shares=100,
                side=ReplaySide.BUY,
                fill_price=Decimal("10"),
                commission=None,
                transfer_fee=Decimal("0"),
            ),
            "fill fees are malformed",
        ),
    ],
)
def test_updated_average_costs_rejects_malformed_order_results(item: SimpleNamespace, message: str) -> None:
    account = ReplayAccount(cash=Decimal("1000"), positions={}, sellable={})
    with pytest.raises(ExecutionReplayError, match=message):
        _updated_average_costs(account, account, {}, SimpleNamespace(orders=(item,)))


def test_updated_average_costs_requires_tuple_orders_and_exact_position_keys() -> None:
    empty = ReplayAccount(cash=Decimal("1000"), positions={}, sellable={})
    with pytest.raises(ExecutionReplayError, match="orders are unavailable"):
        _updated_average_costs(empty, empty, {}, SimpleNamespace(orders=[]))
    with pytest.raises(ExecutionReplayError, match="differs from replay positions"):
        _updated_average_costs(
            empty,
            ReplayAccount(cash=Decimal("1000"), positions={_canonical("000001.SZ"): 100}, sellable={}),
            {},
            _session_result((), empty),
        )


def test_broker_execution_facts_bind_filled_and_unfilled_results_to_plan() -> None:
    buy = _planned_order(symbol="000001.SZ", side=Side.BUY, shares=100, price="10")
    sell = _planned_order(symbol="600000.SH", side=Side.SELL, shares=100, price="8")
    after = ReplayAccount(cash=Decimal("0"), positions={}, sellable={})
    result = _session_result(
        (
            _order_result(
                symbol="000001.SZ",
                side=ReplaySide.BUY,
                requested=100,
                filled=100,
                price=Decimal("10.01"),
                commission=Decimal("5"),
                transfer_fee=Decimal("0.02"),
            ),
            _order_result(
                symbol="600000.SH",
                side=ReplaySide.SELL,
                requested=100,
                filled=0,
                price=None,
            ),
        ),
        after,
    )

    orders, fills = _broker_execution_facts(_plan(buy, sell), result, session=_SESSION)

    assert [item.status for item in orders] == [
        BrokerOrderStatus.FILLED,
        BrokerOrderStatus.CANCELLED,
    ]
    assert [item.event_sequence for item in orders] == [1, 3]
    assert len(fills) == 1
    assert fills[0].broker_order_id == orders[0].broker_order_id
    assert fills[0].shares == Shares(100)
    assert fills[0].price == Price(Decimal("10.01"))
    assert fills[0].event_sequence == 2
    assert len(orders[0].raw_payload_sha256) == 64
    assert len(fills[0].raw_payload_sha256) == 64


def test_broker_execution_facts_rejects_absent_or_malformed_results() -> None:
    planned = _planned_order(symbol="000001.SZ", side=Side.BUY, shares=100, price="10")
    plan = _plan(planned)

    with pytest.raises(ExecutionReplayError, match="planned execution result is missing"):
        _broker_execution_facts(plan, None, session=_SESSION)
    with pytest.raises(ExecutionReplayError, match="orders are unavailable"):
        _broker_execution_facts(plan, SimpleNamespace(orders=[]), session=_SESSION)


def test_broker_execution_facts_rejects_filled_order_without_price() -> None:
    planned = _planned_order(symbol="000001.SZ", side=Side.BUY, shares=100, price="10")
    malformed = SimpleNamespace(
        symbol=_canonical("000001.SZ"),
        side=ReplaySide.BUY,
        filled_shares=100,
        fill_price=None,
    )

    with pytest.raises(ExecutionReplayError, match="has no price"):
        _broker_execution_facts(_plan(planned), SimpleNamespace(orders=(malformed,)), session=_SESSION)
