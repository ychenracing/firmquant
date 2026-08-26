from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from firmquant.market_data.xtquant_daily import (
    DailyBar,
    SourceEpochResealRequired,
    XtQuantDailyDataUpdater,
)


class Provider:
    def __init__(self, bars: dict[str, tuple[DailyBar, ...]]) -> None:
        self.bars = bars
        self.calls: list[tuple[tuple[str, ...], date]] = []

    def fetch(self, symbols: tuple[str, ...], *, through: date):
        self.calls.append((symbols, through))
        return {symbol: self.bars[symbol] for symbol in symbols}


def bar(day: int, close: str) -> DailyBar:
    value = Decimal(close)
    return DailyBar(
        session=date(2026, 8, day),
        open=value,
        high=value,
        low=value,
        close=value,
        volume=1000,
        amount=value * 1000,
    )


def write_csv(path: Path, rows: list[tuple[str, str]]) -> None:
    body = "date,open,high,low,close,volume,amount\n"
    for day, close in rows:
        body += f"{day},{close},{close},{close},{close},1000,{Decimal(close) * 1000}\n"
    path.write_text(body, encoding="utf-8")


def test_daily_update_appends_only_and_returns_verified_manifest(tmp_path: Path) -> None:
    write_csv(tmp_path / "sh600519.csv", [("2026-08-24", "10")])
    provider = Provider({"sh600519": (bar(24, "10"), bar(25, "11"))})
    updater = XtQuantDailyDataUpdater(root=tmp_path, provider=provider)

    receipt = updater.update(("sh600519",), through=date(2026, 8, 25))

    assert receipt.latest_common_session == date(2026, 8, 25)
    assert receipt.appended_rows == 1
    assert len(receipt.manifest_sha256) == 64
    assert provider.calls == [(("sh600519",), date(2026, 8, 25))]
    text = (tmp_path / "sh600519.csv").read_text(encoding="utf-8")
    assert "2026-08-25,11,11,11,11,1000,11000" in text


def test_daily_update_rejects_qfq_history_rewrite_without_reseal(tmp_path: Path) -> None:
    write_csv(tmp_path / "sh600519.csv", [("2026-08-24", "10")])
    provider = Provider({"sh600519": (bar(24, "9.5"), bar(25, "11"))})
    updater = XtQuantDailyDataUpdater(root=tmp_path, provider=provider)

    with pytest.raises(SourceEpochResealRequired, match="sh600519"):
        updater.update(("sh600519",), through=date(2026, 8, 25))

    assert "9.5" not in (tmp_path / "sh600519.csv").read_text(encoding="utf-8")


def test_daily_update_is_atomic_across_symbols(tmp_path: Path) -> None:
    write_csv(tmp_path / "sh600519.csv", [("2026-08-24", "10")])
    write_csv(tmp_path / "sz300308.csv", [("2026-08-24", "20")])
    provider = Provider(
        {
            "sh600519": (bar(24, "10"), bar(25, "11")),
            "sz300308": (
                DailyBar(
                    session=date(2026, 8, 24),
                    open=Decimal("19"),
                    high=Decimal("19"),
                    low=Decimal("19"),
                    close=Decimal("19"),
                    volume=1000,
                    amount=Decimal("19000"),
                ),
                bar(25, "21"),
            ),
        }
    )
    updater = XtQuantDailyDataUpdater(root=tmp_path, provider=provider)

    with pytest.raises(SourceEpochResealRequired):
        updater.update(("sh600519", "sz300308"), through=date(2026, 8, 25))

    assert "2026-08-25" not in (tmp_path / "sh600519.csv").read_text(encoding="utf-8")
    assert "2026-08-25" not in (tmp_path / "sz300308.csv").read_text(encoding="utf-8")
