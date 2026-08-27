from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from firmquant.market_data.validation import (
    Adjustment,
    DataKind,
    DataManifest,
    SeriesSeal,
    StrategyDataValidator,
)


PREVIOUS = date(2026, 8, 24)
TARGET = date(2026, 8, 25)
NOW = datetime(2026, 8, 25, 7, 10, tzinfo=UTC)


def seal(*, suspended: bool, last: date, rows: int, digest: str, prefix_rows: int, prefix: str) -> SeriesSeal:
    return SeriesSeal(
        series_id="sz300308",
        kind=DataKind.EQUITY,
        adjustment=Adjustment.FORWARD_ADJUSTED,
        first_session=date(2026, 1, 5),
        last_session=last,
        row_count=rows,
        full_sha256=digest,
        verified_prefix_row_count=prefix_rows,
        verified_prefix_sha256=prefix,
        suspension_session=TARGET if suspended else None,
        suspension_evidence_sha256="f" * 64 if suspended else None,
    )


def test_validator_accepts_unchanged_suspended_equity_and_requires_resume_append() -> None:
    previous = DataManifest(
        latest_common_session=PREVIOUS,
        captured_at=NOW - timedelta(days=1),
        provider="xtquant",
        series=(seal(suspended=False, last=PREVIOUS, rows=10, digest="a" * 64, prefix_rows=9, prefix="0" * 64),),
    )
    suspended = DataManifest(
        latest_common_session=TARGET,
        captured_at=NOW,
        provider="xtquant",
        series=(seal(suspended=True, last=PREVIOUS, rows=10, digest="a" * 64, prefix_rows=9, prefix="0" * 64),),
    )

    receipt = StrategyDataValidator(max_manifest_age=timedelta(minutes=5)).validate(
        previous=previous,
        current=suspended,
        target_session=TARGET,
        now=NOW + timedelta(minutes=1),
    )

    assert receipt.latest_common_session == TARGET
