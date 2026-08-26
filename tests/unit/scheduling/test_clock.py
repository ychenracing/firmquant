from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from firmquant.market_data.calendar import (
    AuthoritativeTradingCalendar,
    CalendarCoverageError,
)
from firmquant.market_data.validation import (
    Adjustment,
    DataKind,
    DataManifest,
    DataValidationError,
    SeriesSeal,
    StrategyDataValidator,
)
from firmquant.scheduling.clock import ClockGuard, ClockObservation, ClockValidationError

NOW = datetime(2026, 8, 25, 1, 30, tzinfo=UTC)


def _calendar() -> AuthoritativeTradingCalendar:
    return AuthoritativeTradingCalendar(
        source="broker-calendar",
        source_sha256="a" * 64,
        covered_from=date(2026, 8, 24),
        covered_through=date(2026, 8, 28),
        trading_sessions=(
            date(2026, 8, 24),
            date(2026, 8, 25),
            date(2026, 8, 27),
            date(2026, 8, 28),
        ),
    )


def _manifest(*, session: date, full: str, prefix: str, captured_at: datetime = NOW) -> DataManifest:
    return DataManifest(
        latest_common_session=session,
        captured_at=captured_at,
        provider="uquant-data-contract",
        series=(
            SeriesSeal(
                series_id="sz300308",
                kind=DataKind.EQUITY,
                adjustment=Adjustment.FORWARD_ADJUSTED,
                first_session=date(2026, 1, 5),
                last_session=session,
                row_count=160 if session == date(2026, 8, 25) else 159,
                full_sha256=full,
                verified_prefix_row_count=159 if session == date(2026, 8, 25) else 158,
                verified_prefix_sha256=prefix,
            ),
            SeriesSeal(
                series_id="000300.SH",
                kind=DataKind.INDEX,
                adjustment=Adjustment.UNADJUSTED,
                first_session=date(2026, 1, 5),
                last_session=session,
                row_count=160 if session == date(2026, 8, 25) else 159,
                full_sha256="c" * 64 if session == date(2026, 8, 25) else "d" * 64,
                verified_prefix_row_count=159 if session == date(2026, 8, 25) else 158,
                verified_prefix_sha256="d" * 64 if session == date(2026, 8, 25) else "e" * 64,
            ),
        ),
    )


def test_clock_guard_accepts_shanghai_and_binds_observation() -> None:
    receipt = ClockGuard(max_drift=timedelta(seconds=2)).verify(
        ClockObservation(
            system_time=NOW,
            reference_time=NOW + timedelta(milliseconds=750),
            local_timezone="Asia/Shanghai",
        )
    )

    assert receipt.drift_milliseconds == 750
    assert receipt.shanghai_time.isoformat() == "2026-08-25T09:30:00+08:00"
    assert len(receipt.sha256) == 64


@pytest.mark.parametrize(
    ("observation", "message"),
    [
        (
            ClockObservation(
                system_time=NOW,
                reference_time=NOW,
                local_timezone="UTC",
            ),
            "Asia/Shanghai",
        ),
        (
            ClockObservation(
                system_time=NOW,
                reference_time=NOW + timedelta(seconds=3),
                local_timezone="Asia/Shanghai",
            ),
            "drift",
        ),
    ],
)
def test_clock_guard_fails_closed(observation: ClockObservation, message: str) -> None:
    with pytest.raises(ClockValidationError, match=message):
        ClockGuard(max_drift=timedelta(seconds=2)).verify(observation)


def test_clock_observation_rejects_naive_time() -> None:
    with pytest.raises(ClockValidationError, match="timezone-aware"):
        ClockObservation(
            system_time=datetime(2026, 8, 25, 9, 30),
            reference_time=NOW,
            local_timezone="Asia/Shanghai",
        )


def test_authoritative_calendar_does_not_infer_weekdays() -> None:
    calendar = _calendar()

    assert calendar.is_trading_session(date(2026, 8, 25)) is True
    assert calendar.is_trading_session(date(2026, 8, 26)) is False
    assert calendar.next_trading_session(date(2026, 8, 25)) == date(2026, 8, 27)
    assert calendar.previous_trading_session(date(2026, 8, 27)) == date(2026, 8, 25)

    with pytest.raises(CalendarCoverageError, match="coverage"):
        calendar.is_trading_session(date(2026, 8, 31))


def test_strategy_data_validator_accepts_append_only_update() -> None:
    previous = _manifest(session=date(2026, 8, 24), full="b" * 64, prefix="f" * 64)
    current = _manifest(session=date(2026, 8, 25), full="a" * 64, prefix="b" * 64)

    receipt = StrategyDataValidator(max_manifest_age=timedelta(minutes=10)).validate(
        previous=previous,
        current=current,
        target_session=date(2026, 8, 25),
        now=NOW + timedelta(minutes=1),
    )

    assert receipt.current_manifest_sha256 == current.sha256
    assert receipt.previous_manifest_sha256 == previous.sha256
    assert receipt.latest_common_session == date(2026, 8, 25)


def test_strategy_data_validator_rejects_history_prefix_rewrite() -> None:
    previous = _manifest(session=date(2026, 8, 24), full="b" * 64, prefix="f" * 64)
    rewritten = _manifest(session=date(2026, 8, 25), full="a" * 64, prefix="0" * 64)

    with pytest.raises(DataValidationError, match="prefix drift"):
        StrategyDataValidator(max_manifest_age=timedelta(minutes=10)).validate(
            previous=previous,
            current=rewritten,
            target_session=date(2026, 8, 25),
            now=NOW + timedelta(minutes=1),
        )


def test_strategy_data_validator_rejects_stale_common_session_and_manifest() -> None:
    previous = _manifest(session=date(2026, 8, 24), full="b" * 64, prefix="f" * 64)

    with pytest.raises(DataValidationError, match="latest common session"):
        StrategyDataValidator(max_manifest_age=timedelta(minutes=10)).validate(
            previous=previous,
            current=previous,
            target_session=date(2026, 8, 25),
            now=NOW,
        )

    current = _manifest(
        session=date(2026, 8, 25),
        full="a" * 64,
        prefix="b" * 64,
        captured_at=NOW - timedelta(hours=1),
    )
    with pytest.raises(DataValidationError, match="stale"):
        StrategyDataValidator(max_manifest_age=timedelta(minutes=10)).validate(
            previous=previous,
            current=current,
            target_session=date(2026, 8, 25),
            now=NOW,
        )
