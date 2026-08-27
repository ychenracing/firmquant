# ruff: noqa: I001
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from firmquant.market_data.xtquant_daily import (
    DailyBar,
    DailyDataDeadlineExceeded,
    DailyDataRetriesExhausted,
    DailyDataUpdateError,
    DailyFetchPolicy,
    InstrumentSessionState,
    InstrumentSessionStatus,
    XtQuantDailyDataUpdater,
)


TARGET = date(2026, 8, 25)
NOW = datetime(2026, 8, 25, 7, 10, tzinfo=UTC)


def bar(day: int, close: str = "10") -> DailyBar:
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


def status(
    symbol: str,
    state: InstrumentSessionState,
    *,
    observed_at: datetime = NOW,
) -> InstrumentSessionStatus:
    return InstrumentSessionStatus(
        symbol=symbol,
        session=TARGET,
        state=state,
        observed_at=observed_at,
        source="reviewed-xtquant-status",
        raw_payload_sha256="a" * 64,
    )


class Provider:
    def __init__(
        self,
        bars: dict[str, tuple[DailyBar, ...]],
        statuses: dict[str, InstrumentSessionStatus],
        *,
        failures: int = 0,
    ) -> None:
        self.bars = bars
        self.statuses = statuses
        self.failures = failures
        self.fetch_calls = 0

    def fetch(self, symbols: tuple[str, ...], *, through: date):
        assert through == TARGET
        self.fetch_calls += 1
        if self.fetch_calls <= self.failures:
            raise RuntimeError("temporary history failure")
        return {symbol: self.bars[symbol] for symbol in symbols}

    def fetch_status(self, symbols: tuple[str, ...], *, session: date):
        assert session == TARGET
        return {symbol: self.statuses[symbol] for symbol in symbols}


def _retry_cause(error: DailyDataRetriesExhausted, message: str) -> None:
    assert isinstance(error.__cause__, DailyDataUpdateError)
    assert message in str(error.__cause__)


def test_suspended_security_may_keep_last_real_bar_without_fabrication(tmp_path: Path) -> None:
    symbol = "sz300308"
    provider = Provider(
        {symbol: (bar(24),)},
        {symbol: status(symbol, InstrumentSessionState.SUSPENDED)},
    )
    updater = XtQuantDailyDataUpdater(root=tmp_path, provider=provider, clock=lambda: NOW)

    receipt = updater.update((symbol,), through=TARGET)

    observation = receipt.observations[0]
    assert observation.symbol == symbol
    assert observation.latest_observed_session == date(2026, 8, 24)
    assert observation.suspension_evidence_sha256 is not None
    assert "2026-08-25" not in (tmp_path / f"{symbol}.csv").read_text(encoding="utf-8")


def test_normal_security_missing_target_bar_fails_closed_after_bounded_retries(tmp_path: Path) -> None:
    symbol = "sz300308"
    provider = Provider(
        {symbol: (bar(24),)},
        {symbol: status(symbol, InstrumentSessionState.TRADING)},
    )
    updater = XtQuantDailyDataUpdater(root=tmp_path, provider=provider, clock=lambda: NOW)

    with pytest.raises(DailyDataRetriesExhausted) as captured:
        updater.update((symbol,), through=TARGET)
    _retry_cause(captured.value, "while security is trading")
    assert provider.fetch_calls == 3


def test_stale_suspension_fact_fails_closed_after_bounded_retries(tmp_path: Path) -> None:
    symbol = "sz300308"
    stale = datetime(2026, 8, 25, 6, 30, tzinfo=UTC)
    provider = Provider(
        {symbol: (bar(24),)},
        {symbol: status(symbol, InstrumentSessionState.SUSPENDED, observed_at=stale)},
    )
    updater = XtQuantDailyDataUpdater(root=tmp_path, provider=provider, clock=lambda: NOW)

    with pytest.raises(DailyDataRetriesExhausted) as captured:
        updater.update((symbol,), through=TARGET)
    _retry_cause(captured.value, "status is stale")
    assert provider.fetch_calls == 3


def test_reference_index_missing_target_bar_is_never_excused(tmp_path: Path) -> None:
    symbol = "sh000300"
    provider = Provider(
        {symbol: (bar(24),)},
        {symbol: status(symbol, InstrumentSessionState.SUSPENDED)},
    )
    updater = XtQuantDailyDataUpdater(
        root=tmp_path,
        provider=provider,
        clock=lambda: NOW,
        required_complete_symbols=frozenset({symbol}),
    )

    with pytest.raises(DailyDataRetriesExhausted) as captured:
        updater.update((symbol,), through=TARGET)
    _retry_cause(captured.value, "required complete")
    assert provider.fetch_calls == 3


def test_bounded_retry_succeeds_and_persists_attempt_receipts(tmp_path: Path) -> None:
    symbol = "sz300308"
    provider = Provider(
        {symbol: (bar(24), bar(25, "11"))},
        {symbol: status(symbol, InstrumentSessionState.TRADING)},
        failures=2,
    )
    monotonic = [0.0]

    def sleep(seconds: float) -> None:
        monotonic[0] += seconds

    updater = XtQuantDailyDataUpdater(
        root=tmp_path / "data",
        state_root=tmp_path / "state",
        provider=provider,
        clock=lambda: NOW,
        monotonic=lambda: monotonic[0],
        sleep=sleep,
        fetch_policy=DailyFetchPolicy(
            max_attempts=3,
            retry_interval_seconds=1,
            total_deadline_seconds=5,
        ),
    )

    receipt = updater.update((symbol,), through=TARGET)

    assert receipt.fetch_attempts == 3
    attempts = sorted((tmp_path / "state" / "attempts" / TARGET.isoformat()).glob("*.json"))
    assert len(attempts) == 3
    assert "temporary history failure" not in attempts[0].read_text(encoding="utf-8")


def test_bounded_retry_exhaustion_and_deadline_do_not_loop_forever(tmp_path: Path) -> None:
    symbol = "sz300308"
    provider = Provider(
        {symbol: (bar(24),)},
        {symbol: status(symbol, InstrumentSessionState.TRADING)},
        failures=99,
    )
    monotonic = [0.0]

    def sleep(seconds: float) -> None:
        monotonic[0] += seconds

    updater = XtQuantDailyDataUpdater(
        root=tmp_path / "data",
        state_root=tmp_path / "state",
        provider=provider,
        clock=lambda: NOW,
        monotonic=lambda: monotonic[0],
        sleep=sleep,
        fetch_policy=DailyFetchPolicy(
            max_attempts=10,
            retry_interval_seconds=2,
            total_deadline_seconds=3,
        ),
    )

    with pytest.raises(DailyDataDeadlineExceeded):
        updater.update((symbol,), through=TARGET)
    assert provider.fetch_calls == 2
