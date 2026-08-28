from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from firmquant.domain.values import Symbol
from firmquant.execution import replay_runner as runner
from firmquant.execution.execution_replay import ReplayAccount

SESSION = date(2026, 8, 7)


def _panel(*, rows: int = 7) -> pd.DataFrame:
    dates = pd.date_range("2026-08-01", periods=rows, freq="D")
    return pd.DataFrame(
        {
            "open": [10 + index / 10 for index in range(rows)],
            "high": [10.5 + index / 10 for index in range(rows)],
            "low": [9.5 + index / 10 for index in range(rows)],
            "close": [10 + index / 10 for index in range(rows)],
            "volume": [10_000 + index for index in range(rows)],
        },
        index=dates,
    )


def _write_panel(path: Path, *, complete: bool = True) -> None:
    frame = _panel().reset_index(names="date")
    if not complete:
        frame = frame.drop(columns=["volume"])
    frame.to_csv(path, index=False)


def test_file_and_symbol_identity_helpers_fail_closed(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    with pytest.raises(runner.ExecutionReplayError, match="not a regular file"):
        runner._sha256_file(missing)

    payload = tmp_path / "payload.json"
    payload.write_bytes(b"firmquant")
    assert runner._sha256_file(payload) == hashlib.sha256(b"firmquant").hexdigest()

    assert runner._canonical_symbol("sh600000") == "600000.SH"
    assert runner._canonical_symbol("SZ300001") == "300001.SZ"
    assert runner._canonical_symbol("bj430001") == "430001.BJ"
    for raw in ("600000", "xx600000", "sh60000x", "sh6000000"):
        with pytest.raises(runner.ExecutionReplayError, match="A-share symbol"):
            runner._canonical_symbol(raw)


def test_load_panels_requires_reference_indices_and_complete_schema(tmp_path: Path) -> None:
    _write_panel(tmp_path / "sh000300.csv")
    with pytest.raises(runner.ExecutionReplayError, match="reference-index"):
        runner._load_panels(tmp_path, ())

    _write_panel(tmp_path / "sh000682.csv")
    loaded = runner._load_panels(tmp_path, ())
    assert set(loaded) == {"000300.SH", "000682.SH"}
    assert loaded["000300.SH"].index.is_monotonic_increasing

    broken = tmp_path / "broken"
    broken.mkdir()
    _write_panel(broken / "sh000300.csv", complete=False)
    _write_panel(broken / "sh000682.csv")
    with pytest.raises(runner.ExecutionReplayError, match="schema is incomplete"):
        runner._load_panels(broken, ())


def test_session_and_bar_lookup_helpers_cover_missing_and_duplicate_data() -> None:
    panel = _panel()
    panels = {"000300.SH": panel, "000682.SH": panel}
    sessions = runner._sessions(panels, date(2026, 8, 2), date(2026, 8, 4))
    assert sessions == (date(2026, 8, 2), date(2026, 8, 3), date(2026, 8, 4))
    with pytest.raises(runner.ExecutionReplayError, match="at least two"):
        runner._sessions(panels, date(2026, 8, 2), date(2026, 8, 2))

    assert runner._bar_row(panel, date(2026, 9, 1)) is None
    first = runner._bar_row(panel, date(2026, 8, 1))
    assert first is not None
    assert Decimal(str(first["close"])) == Decimal("10.0")

    duplicate = pd.concat([panel.iloc[[0]], panel.iloc[[0]]])
    with pytest.raises(runner.ExecutionReplayError, match="duplicate sessions"):
        runner._bar_row(duplicate, date(2026, 8, 1))

    assert runner._previous_close(panel, date(2026, 8, 1)) is None
    assert runner._previous_close(panel, date(2026, 8, 2)) == Decimal("10.0")
    assert runner._listing_session_number(panel, date(2026, 8, 3)) == 3


def test_limit_fraction_and_daily_bar_model_listing_and_suspension() -> None:
    short = _panel(rows=4)
    assert runner._limit_fraction(Symbol.parse("600000.SH"), short, date(2026, 8, 4)) is None

    panel = _panel()
    assert runner._limit_fraction(Symbol.parse("430001.BJ"), panel, SESSION) == Decimal("0.30")
    assert runner._limit_fraction(Symbol.parse("300001.SZ"), panel, SESSION) == Decimal("0.20")
    assert runner._limit_fraction(Symbol.parse("688001.SH"), panel, SESSION) == Decimal("0.20")
    assert runner._limit_fraction(Symbol.parse("600000.SH"), panel, SESSION) == Decimal("0.10")
    assert runner._tick_price(Decimal("10.005")) == Decimal("10.01")

    with pytest.raises(runner.ExecutionReplayError, match="previous close"):
        runner._daily_bar("600000.SH", panel, date(2026, 8, 1))

    first_five = runner._daily_bar("600000.SH", short, date(2026, 8, 2))
    assert first_five.limit_up == Decimal("100.00")
    assert first_five.limit_down == Decimal("1.00")

    normal = runner._daily_bar("600000.SH", panel, SESSION)
    assert normal.suspended is False
    assert normal.limit_up == Decimal("11.55")
    assert normal.limit_down == Decimal("9.45")

    suspended = runner._daily_bar("600000.SH", panel, date(2026, 8, 8))
    assert suspended.suspended is True
    assert suspended.volume == 0
    assert suspended.open == Decimal("10.6")


def test_market_value_snapshot_and_execution_facts_preserve_account_truth() -> None:
    bar = runner._daily_bar("600000.SH", _panel(), SESSION)
    account = ReplayAccount(
        cash=Decimal("100"),
        positions={"600000.SH": 100},
        sellable={"600000.SH": 100},
    )
    assert runner._market_value(account, {"600000.SH": bar}, field="close") > Decimal("100")
    with pytest.raises(runner.ExecutionReplayError, match="marking bar"):
        runner._market_value(account, {}, field="close")

    with pytest.raises(runner.ExecutionReplayError, match="average cost"):
        runner._snapshot(
            account,
            {"600000.SH": bar},
            average_costs={},
            session=SESSION,
            captured_at=runner._timestamp(SESSION, time(15, 5)),
            field="close",
        )

    snapshot = runner._snapshot(
        account,
        {"600000.SH": bar},
        average_costs={"600000.SH": Decimal("9.5")},
        session=SESSION,
        captured_at=runner._timestamp(SESSION, time(15, 5)),
        field="close",
    )
    assert snapshot.positions[0].average_cost.value == Decimal("9.5")
    assert snapshot.account.total_assets.value > Decimal("100")

    with pytest.raises(runner.ExecutionReplayError, match="panel is unavailable"):
        runner._execution_facts(
            ReplayAccount(Decimal("1000"), {}, {}),
            {},
            ("600000.SH",),
            {},
            session=SESSION,
        )

    facts, bars = runner._execution_facts(
        ReplayAccount(Decimal("1000"), {}, {}),
        {},
        ("600000.SH",),
        {"600000.SH": _panel()},
        session=SESSION,
    )
    assert bars["600000.SH"].suspended is False
    assert facts.instruments[0].symbol.canonical == "sh600000"
    assert facts.quotes[0].bid_price is not None

    suspended_facts, suspended_bars = runner._execution_facts(
        ReplayAccount(Decimal("1000"), {}, {}),
        {},
        ("600000.SH",),
        {"600000.SH": _panel()},
        session=date(2026, 8, 8),
    )
    assert suspended_bars["600000.SH"].suspended is True
    assert suspended_facts.quotes[0].bid_price is None
    assert suspended_facts.quotes[0].ask_price is None


def test_decision_plan_and_cost_helpers_reject_malformed_evidence() -> None:
    malformed = SimpleNamespace(payload_json=json.dumps([]))
    with pytest.raises(runner.ExecutionReplayError, match="payload is unavailable"):
        runner._plan_symbols(malformed)  # type: ignore[arg-type]

    missing = SimpleNamespace(payload_json=json.dumps({"pending_orders": [], "targets": None}))
    with pytest.raises(runner.ExecutionReplayError, match="targets are unavailable"):
        runner._plan_symbols(missing)  # type: ignore[arg-type]

    valid = SimpleNamespace(
        payload_json=json.dumps(
            {
                "pending_orders": [{"symbol": "600000.SH"}, {"ignored": True}],
                "targets": [{"symbol": "300001.SZ"}, {"symbol": 1}],
            }
        )
    )
    assert runner._plan_symbols(valid) == ("sh600000", "sz300001")  # type: ignore[arg-type]

    config = SimpleNamespace(
        slippage=0.0005,
        commission_rate=0.0003,
        min_commission=5,
        stamp_duty=0.0005,
        transfer_fee=0.00001,
    )
    costs = runner._replay_costs(config, max_price_deviation_bps=Decimal("123"))
    assert costs.slippage_bps == Decimal("5.0000")
    assert costs.max_price_deviation_bps == Decimal("123")


def test_timestamp_and_execution_replay_range_validation_are_strict(tmp_path: Path) -> None:
    observed = runner._timestamp(SESSION, time(9, 30))
    assert isinstance(observed, datetime)
    assert observed.utcoffset() is not None

    with pytest.raises(ValueError, match="date range"):
        runner.run_execution_replay(
            source_checkout=tmp_path,
            data_root=tmp_path,
            start=SESSION,
            end=SESSION,
            max_price_deviation_bps=Decimal("100"),
        )
    with pytest.raises(ValueError, match="date range"):
        runner.run_execution_replay(
            source_checkout=tmp_path,
            data_root=tmp_path,
            start=datetime(2026, 8, 1),  # type: ignore[arg-type]
            end=SESSION,
            max_price_deviation_bps=Decimal("100"),
        )
