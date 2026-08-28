from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from firmquant.application.execution_evidence import BlockerCode
from firmquant.execution import execution_replay as replay

SESSION = date(2026, 8, 27)


def _bar(
    symbol: str = "600000.SH",
    *,
    session: date = SESSION,
    opened: str = "10",
    high: str = "10.5",
    low: str = "9.5",
    close: str = "10",
    volume: int = 10_000,
    suspended: bool = False,
    limit_up: str = "11",
    limit_down: str = "9",
) -> replay.DailyBar:
    return replay.DailyBar(
        session=session,
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


def _costs(*, max_deviation: str = "300") -> replay.ReplayCosts:
    return replay.ReplayCosts(
        commission_rate=Decimal("0.0003"),
        minimum_commission=Decimal("5"),
        sell_stamp_duty_rate=Decimal("0.0005"),
        transfer_fee_rate=Decimal("0.00001"),
        slippage_bps=Decimal("5"),
        max_price_deviation_bps=Decimal(max_deviation),
    )


def test_daily_bar_contract_rejects_noncanonical_inputs() -> None:
    valid = {
        "session": SESSION,
        "symbol": "600000.SH",
        "open": Decimal("10"),
        "high": Decimal("10.5"),
        "low": Decimal("9.5"),
        "close": Decimal("10"),
        "previous_close": Decimal("10"),
        "volume": 1000,
        "suspended": False,
        "limit_up": Decimal("11"),
        "limit_down": Decimal("9"),
    }
    cases = (
        ({"session": datetime(2026, 8, 27)}, TypeError),
        ({"symbol": ""}, ValueError),
        ({"open": Decimal("NaN")}, ValueError),
        ({"open": Decimal("0")}, ValueError),
        ({"low": Decimal("11")}, ValueError),
        ({"open": Decimal("11")}, ValueError),
        ({"close": Decimal("11")}, ValueError),
        ({"previous_close": Decimal("12")}, ValueError),
        ({"volume": -1}, ValueError),
        ({"suspended": 1}, TypeError),
        ({"suspended": True, "volume": 1}, ValueError),
    )
    for override, error_type in cases:
        with pytest.raises(error_type):
            replay.DailyBar(**(valid | override))  # type: ignore[arg-type]


def test_cost_order_and_account_contracts_fail_closed() -> None:
    with pytest.raises(ValueError):
        replay.ReplayCosts(
            Decimal("NaN"),
            Decimal("5"),
            Decimal("0"),
            Decimal("0"),
            Decimal("0"),
        )
    with pytest.raises(ValueError):
        replay.ReplayCosts(
            Decimal("1.1"),
            Decimal("5"),
            Decimal("0"),
            Decimal("0"),
            Decimal("0"),
        )
    with pytest.raises(ValueError):
        replay.ReplayCosts(
            Decimal("0"),
            Decimal("-1"),
            Decimal("0"),
            Decimal("0"),
            Decimal("0"),
        )
    with pytest.raises(ValueError):
        replay.ReplayCosts(
            Decimal("0"),
            Decimal("0"),
            Decimal("0"),
            Decimal("0"),
            Decimal("10001"),
        )

    order_cases = (
        ("", replay.ReplaySide.BUY, 100, Decimal("10"), Decimal("1"), False),
        ("600000.SH", "BUY", 100, Decimal("10"), Decimal("1"), False),
        ("600000.SH", replay.ReplaySide.BUY, True, Decimal("10"), Decimal("1"), False),
        ("600000.SH", replay.ReplaySide.BUY, 0, Decimal("10"), Decimal("1"), False),
        ("600000.SH", replay.ReplaySide.BUY, 100, Decimal("0"), Decimal("1"), False),
        ("600000.SH", replay.ReplaySide.BUY, 100, Decimal("10"), Decimal("0"), False),
        ("600000.SH", replay.ReplaySide.BUY, 100, Decimal("10"), Decimal("1.1"), False),
        ("600000.SH", replay.ReplaySide.BUY, 100, Decimal("10"), Decimal("1"), 1),
        ("600000.SH", replay.ReplaySide.SELL, 100, Decimal("10"), Decimal("1"), True),
    )
    for values in order_cases:
        with pytest.raises((TypeError, ValueError)):
            replay.ReplayOrder(*values)  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        replay.ReplayAccount(Decimal("NaN"), {}, {})
    with pytest.raises(TypeError):
        replay.ReplayAccount(Decimal("1"), [], {})  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        replay.ReplayAccount(Decimal("1"), {"": 1}, {})
    with pytest.raises(ValueError):
        replay.ReplayAccount(Decimal("1"), {"600000.SH": -1}, {})
    with pytest.raises(ValueError):
        replay.ReplayAccount(Decimal("1"), {"600000.SH": 1}, {"600000.SH": -1})
    with pytest.raises(ValueError):
        replay.ReplayAccount(Decimal("1"), {"600000.SH": 1}, {"600000.SH": 2})
    with pytest.raises(ValueError):
        replay.ReplayAccount(Decimal("1"), {"600000.SH": 0}, {})


def test_execute_session_validates_all_input_boundaries() -> None:
    account = replay.ReplayAccount(Decimal("1000"), {}, {})
    order = replay.ReplayOrder(
        "600000.SH",
        replay.ReplaySide.BUY,
        100,
        Decimal("10"),
        Decimal("1"),
    )
    costs = _costs()
    with pytest.raises(TypeError):
        replay.execute_session(object(), (order,), {"600000.SH": _bar()}, costs)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        replay.execute_session(account, [order], {"600000.SH": _bar()}, costs)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        replay.execute_session(account, (object(),), {"600000.SH": _bar()}, costs)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        replay.execute_session(account, (order,), [], costs)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        replay.execute_session(account, (order,), {"600000.SH": object()}, costs)  # type: ignore[dict-item]
    with pytest.raises(TypeError):
        replay.execute_session(account, (order,), {"600000.SH": _bar()}, object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="at least one order"):
        replay.execute_session(account, (), {}, costs)
    with pytest.raises(ValueError, match="missing daily bars"):
        replay.execute_session(account, (order,), {}, costs)

    second = replay.ReplayOrder(
        "000001.SZ",
        replay.ReplaySide.BUY,
        100,
        Decimal("10"),
        Decimal("1"),
    )
    with pytest.raises(ValueError, match="one session"):
        replay.execute_session(
            account,
            (order, second),
            {
                "600000.SH": _bar(),
                "000001.SZ": _bar("000001.SZ", session=date(2026, 8, 28)),
            },
            costs,
        )


def test_authorization_and_post_authorization_blockers_cover_edge_paths() -> None:
    buy_account = replay.ReplayAccount(Decimal("100000"), {}, {})
    stale = replay.execute_session(
        buy_account,
        (
            replay.ReplayOrder(
                "600000.SH",
                replay.ReplaySide.BUY,
                100,
                Decimal("11"),
                Decimal("1"),
            ),
        ),
        {"600000.SH": _bar()},
        _costs(max_deviation="100"),
    )
    assert stale.orders[0].blocker is BlockerCode.STALE_QUOTE
    assert stale.orders[0].authorized is False

    sell_account = replay.ReplayAccount(
        Decimal("0"),
        {"600000.SH": 100},
        {"600000.SH": 100},
    )
    lower_limit = replay.execute_session(
        sell_account,
        (
            replay.ReplayOrder(
                "600000.SH",
                replay.ReplaySide.SELL,
                100,
                Decimal("9"),
                Decimal("1"),
            ),
        ),
        {
            "600000.SH": _bar(
                opened="9",
                high="9",
                low="9",
                close="9",
                limit_down="9",
            )
        },
        _costs(max_deviation="1000"),
    )
    assert lower_limit.orders[0].blocker is BlockerCode.PRICE_LIMIT

    buy_not_reached = replay.execute_session(
        buy_account,
        (
            replay.ReplayOrder(
                "600000.SH",
                replay.ReplaySide.BUY,
                100,
                Decimal("9.8"),
                Decimal("1"),
            ),
        ),
        {"600000.SH": _bar(low="9.9")},
        _costs(max_deviation="1000"),
    )
    assert buy_not_reached.orders[0].authorized is True
    assert buy_not_reached.orders[0].blocker is BlockerCode.PRICE_LIMIT

    sell_not_reached = replay.execute_session(
        sell_account,
        (
            replay.ReplayOrder(
                "600000.SH",
                replay.ReplaySide.SELL,
                100,
                Decimal("10.2"),
                Decimal("1"),
            ),
        ),
        {"600000.SH": _bar(high="10.1")},
        _costs(max_deviation="1000"),
    )
    assert sell_not_reached.orders[0].authorized is True
    assert sell_not_reached.orders[0].blocker is BlockerCode.PRICE_LIMIT


def test_cash_volume_and_sellable_edges_preserve_explicit_blockers() -> None:
    zero_cap = replay.execute_session(
        replay.ReplayAccount(Decimal("100000"), {}, {}),
        (
            replay.ReplayOrder(
                "600000.SH",
                replay.ReplaySide.BUY,
                100,
                Decimal("10.1"),
                Decimal("0.1"),
            ),
        ),
        {"600000.SH": _bar(volume=50)},
        _costs(),
    )
    assert zero_cap.orders[0].blocker is BlockerCode.VOLUME_LIMIT
    assert zero_cap.orders[0].authorized is True

    no_cash = replay.execute_session(
        replay.ReplayAccount(Decimal("0"), {}, {}),
        (
            replay.ReplayOrder(
                "600000.SH",
                replay.ReplaySide.BUY,
                100,
                Decimal("10.1"),
                Decimal("1"),
            ),
        ),
        {"600000.SH": _bar()},
        _costs(),
    )
    assert no_cash.orders[0].blocker is BlockerCode.INSUFFICIENT_CASH
    assert no_cash.orders[0].filled_shares == 0

    partial_cash = replay.execute_session(
        replay.ReplayAccount(Decimal("1200"), {}, {}),
        (
            replay.ReplayOrder(
                "600000.SH",
                replay.ReplaySide.BUY,
                200,
                Decimal("10.1"),
                Decimal("1"),
            ),
        ),
        {"600000.SH": _bar()},
        _costs(),
    )
    assert partial_cash.orders[0].filled_shares == 100
    assert partial_cash.orders[0].blocker is BlockerCode.INSUFFICIENT_CASH

    partial_sell = replay.execute_session(
        replay.ReplayAccount(
            Decimal("0"),
            {"600000.SH": 100},
            {"600000.SH": 50},
        ),
        (
            replay.ReplayOrder(
                "600000.SH",
                replay.ReplaySide.SELL,
                100,
                Decimal("9.9"),
                Decimal("1"),
            ),
        ),
        {"600000.SH": _bar()},
        _costs(),
    )
    assert partial_sell.orders[0].filled_shares == 50
    assert partial_sell.orders[0].blocker is BlockerCode.VOLUME_LIMIT
    assert partial_sell.ending_account.positions["600000.SH"] == 50

    full_sell = replay.execute_session(
        replay.ReplayAccount(
            Decimal("0"),
            {"600000.SH": 100},
            {"600000.SH": 100},
        ),
        (
            replay.ReplayOrder(
                "600000.SH",
                replay.ReplaySide.SELL,
                100,
                Decimal("9.9"),
                Decimal("1"),
            ),
        ),
        {"600000.SH": _bar(close="9")},
        _costs(),
    )
    assert full_sell.ending_account.positions == {}
    assert full_sell.stamp_duty > 0


def test_private_execution_math_edges_are_deterministic() -> None:
    bar = _bar(volume=105, close="11")
    assert replay._volume_cap(bar, Decimal("1"), side=replay.ReplaySide.SELL) == 105
    assert replay._fees(Decimal("0"), replay.ReplaySide.BUY, _costs()) == (
        Decimal("0"),
        Decimal("0"),
        Decimal("0"),
    )
    assert replay._unfilled_loss(
        replay.ReplayOrder(
            "600000.SH",
            replay.ReplaySide.BUY,
            100,
            Decimal("10"),
            Decimal("1"),
        ),
        bar,
        0,
    ) == Decimal("0")
    assert replay._unfilled_loss(
        replay.ReplayOrder(
            "600000.SH",
            replay.ReplaySide.BUY,
            100,
            Decimal("10"),
            Decimal("1"),
        ),
        bar,
        100,
    ) > 0
    assert replay._unfilled_loss(
        replay.ReplayOrder(
            "600000.SH",
            replay.ReplaySide.SELL,
            100,
            Decimal("10"),
            Decimal("1"),
        ),
        _bar(close="9"),
        100,
    ) > 0

    blocked = replay._blocked_result(
        replay.ReplayOrder(
            "600000.SH",
            replay.ReplaySide.BUY,
            100,
            Decimal("10"),
            Decimal("1"),
        ),
        bar,
        blocker=BlockerCode.PRICE_LIMIT,
        authorized=False,
    )
    payload = blocked.payload()
    assert payload["fill_price"] is None
    assert payload["blocker"] == BlockerCode.PRICE_LIMIT.value
