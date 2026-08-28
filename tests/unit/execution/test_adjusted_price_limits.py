from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd

from firmquant.execution import replay_runner as runner
from firmquant.execution.execution_replay import ReplayAccount

SESSION = date(2024, 8, 7)


def _panel(
    *,
    current_open: str,
    current_high: str,
    current_low: str,
    current_close: str,
    previous_close: str = "21.848",
) -> pd.DataFrame:
    dates = pd.date_range("2024-08-01", periods=7, freq="D")
    return pd.DataFrame(
        {
            "open": ["20", "20.2", "20.4", "20.6", "20.8", previous_close, current_open],
            "high": ["20.5", "20.7", "20.9", "21.1", "21.3", previous_close, current_high],
            "low": ["19.5", "19.7", "19.9", "20.1", "20.3", previous_close, current_low],
            "close": ["20", "20.2", "20.4", "20.6", "20.8", previous_close, current_close],
            "volume": [10_000] * 7,
        },
        index=dates,
    ).astype({column: float for column in ("open", "high", "low", "close")})


def test_forward_adjusted_upper_limit_expands_only_to_observed_traded_envelope() -> None:
    panel = _panel(
        current_open="24.108",
        current_high="24.108",
        current_low="24.108",
        current_close="24.108",
    )

    bar = runner._daily_bar("601869.SH", panel, SESSION)

    assert bar.previous_close == Decimal("21.848")
    assert bar.limit_up == Decimal("24.108")
    assert bar.limit_down == Decimal("19.66")
    assert bar.open == bar.limit_up

    facts, bars = runner._execution_facts(
        ReplayAccount(cash=Decimal("100000"), positions={}, sellable={}),
        {},
        ("601869.SH",),
        {"601869.SH": panel},
        session=SESSION,
    )
    assert bars["601869.SH"].limit_up == Decimal("24.108")
    assert facts.quotes[0].upper_limit.value == Decimal("24.108")
    assert facts.quotes[0].ask_price is not None
    assert facts.quotes[0].ask_price.value == Decimal("24.108")


def test_forward_adjusted_low_or_high_only_expands_outward_and_normal_band_is_unchanged() -> None:
    high_only = runner._daily_bar(
        "601869.SH",
        _panel(
            current_open="22.0",
            current_high="24.05",
            current_low="21.0",
            current_close="23.5",
        ),
        SESSION,
    )
    assert high_only.limit_up == Decimal("24.05")
    assert high_only.limit_down == Decimal("19.66")

    low_only = runner._daily_bar(
        "601869.SH",
        _panel(
            current_open="21.0",
            current_high="22.5",
            current_low="19.60",
            current_close="20.0",
        ),
        SESSION,
    )
    assert low_only.limit_up == Decimal("24.03")
    assert low_only.limit_down == Decimal("19.60")

    normal = runner._daily_bar(
        "601869.SH",
        _panel(
            current_open="22.0",
            current_high="23.0",
            current_low="21.0",
            current_close="22.5",
        ),
        SESSION,
    )
    assert normal.limit_up == Decimal("24.03")
    assert normal.limit_down == Decimal("19.66")
