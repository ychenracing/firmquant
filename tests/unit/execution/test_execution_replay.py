from __future__ import annotations

from datetime import date
from decimal import Decimal

from firmquant.application.execution_evidence import BlockerCode
from firmquant.execution.execution_replay import (
    DailyBar,
    ReplayAccount,
    ReplayCosts,
    ReplayOrder,
    ReplaySide,
    execute_session,
)


def _bar(
    symbol: str,
    *,
    opened: str = "10",
    high: str = "10.5",
    low: str = "9.5",
    close: str = "10",
    volume: int = 10_000,
    suspended: bool = False,
    limit_up: str = "11",
    limit_down: str = "9",
) -> DailyBar:
    return DailyBar(
        session=date(2026, 8, 27),
        symbol=symbol,
        open=Decimal(opened),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        previous_close=Decimal("10"),
        volume=volume,
        suspended=suspended,
        limit_up=Decimal(limit_up),
        limit_down=Decimal(limit_down),
    )


def _costs() -> ReplayCosts:
    return ReplayCosts(
        commission_rate=Decimal("0.0003"),
        minimum_commission=Decimal("5"),
        sell_stamp_duty_rate=Decimal("0.0005"),
        transfer_fee_rate=Decimal("0.00001"),
        slippage_bps=Decimal("5"),
    )


def test_authorization_is_causal_and_does_not_read_future_high_low() -> None:
    order = ReplayOrder("600000.SH", ReplaySide.BUY, 100, Decimal("10.10"), Decimal("0.10"))
    account = ReplayAccount(cash=Decimal("100000"), positions={}, sellable={})
    first = execute_session(account, (order,), {"600000.SH": _bar("600000.SH", high="10.2", low="9.8")}, _costs())
    second = execute_session(account, (order,), {"600000.SH": _bar("600000.SH", high="20", low="1")}, _costs())
    assert first.orders[0].authorized == second.orders[0].authorized


def test_suspension_limit_and_volume_cap_are_modeled() -> None:
    account = ReplayAccount(cash=Decimal("100000"), positions={}, sellable={})
    suspended = ReplayOrder("600000.SH", ReplaySide.BUY, 100, Decimal("10"), Decimal("0.10"))
    limit_up = ReplayOrder("000001.SZ", ReplaySide.BUY, 100, Decimal("11"), Decimal("0.10"))
    partial = ReplayOrder("300001.SZ", ReplaySide.BUY, 1000, Decimal("10.10"), Decimal("0.10"))
    result = execute_session(
        account,
        (suspended, limit_up, partial),
        {
            "600000.SH": _bar("600000.SH", suspended=True, volume=0),
            "000001.SZ": _bar("000001.SZ", opened="11", high="11", low="11", close="11"),
            "300001.SZ": _bar("300001.SZ", volume=5_000),
        },
        _costs(),
    )
    assert result.orders[0].blocker is BlockerCode.NON_TRADABLE
    assert result.orders[1].blocker is BlockerCode.PRICE_LIMIT
    assert result.orders[2].filled_shares == 500
    assert result.orders[2].blocker is BlockerCode.VOLUME_LIMIT
    assert result.partial_fill_count == 1


def test_t_plus_one_and_sell_before_buy_with_incomplete_sell_dependency() -> None:
    account = ReplayAccount(
        cash=Decimal("0"),
        positions={"600000.SH": 1000},
        sellable={"600000.SH": 1000},
    )
    sell = ReplayOrder("600000.SH", ReplaySide.SELL, 1000, Decimal("9.90"), Decimal("0.05"))
    buy = ReplayOrder(
        "000001.SZ",
        ReplaySide.BUY,
        1000,
        Decimal("10.10"),
        Decimal("0.10"),
        depends_on_sell_proceeds=True,
    )
    result = execute_session(
        account,
        (buy, sell),
        {
            "600000.SH": _bar("600000.SH", volume=10_000),
            "000001.SZ": _bar("000001.SZ", volume=100_000),
        },
        _costs(),
    )
    assert result.orders[0].side is ReplaySide.SELL
    assert result.orders[0].filled_shares == 500
    assert result.orders[1].side is ReplaySide.BUY
    assert result.orders[1].filled_shares == 0
    assert result.orders[1].blocker is BlockerCode.INCOMPLETE_SELL
    assert result.ending_account.positions["600000.SH"] == 500

    bought = ReplayAccount(cash=Decimal("100000"), positions={}, sellable={})
    buy_only = execute_session(
        bought,
        (ReplayOrder("600000.SH", ReplaySide.BUY, 100, Decimal("10.10"), Decimal("0.10")),),
        {"600000.SH": _bar("600000.SH")},
        _costs(),
    )
    same_day_sell = execute_session(
        buy_only.ending_account,
        (ReplayOrder("600000.SH", ReplaySide.SELL, 100, Decimal("9.90"), Decimal("0.10")),),
        {"600000.SH": _bar("600000.SH")},
        _costs(),
    )
    assert same_day_sell.orders[0].filled_shares == 0
    assert same_day_sell.orders[0].blocker is BlockerCode.NON_TRADABLE

    next_day = buy_only.ending_account.roll_session()
    next_day_sell = execute_session(
        next_day,
        (ReplayOrder("600000.SH", ReplaySide.SELL, 100, Decimal("9.90"), Decimal("0.10")),),
        {"600000.SH": _bar("600000.SH")},
        _costs(),
    )
    assert next_day_sell.orders[0].filled_shares == 100


def test_fees_slippage_unfilled_and_determinism_are_stable() -> None:
    order = ReplayOrder("600000.SH", ReplaySide.BUY, 1000, Decimal("10.10"), Decimal("0.05"))
    account = ReplayAccount(cash=Decimal("100000"), positions={}, sellable={})
    bars = {"600000.SH": _bar("600000.SH", volume=10_000)}
    first = execute_session(account, (order,), bars, _costs())
    second = execute_session(account, (order,), bars, _costs())
    assert first.canonical_json() == second.canonical_json()
    assert first.commissions > 0
    assert first.transfer_fees > 0
    assert first.slippage_cost > 0
    assert first.unfilled_notional > 0
