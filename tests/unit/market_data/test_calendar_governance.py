from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from firmquant.market_data.calendar import CalendarCoverageState, CalendarValidationError
from firmquant.market_data.calendar_manifest import (
    load_trading_calendar_manifest,
    validate_calendar_update,
)


def manifest(path: Path, *, end: str, sessions: list[str]) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_name": "reviewed-calendar",
                "source_sha256": "a" * 64,
                "covered_start": "2026-08-20",
                "covered_end": end,
                "sessions": sessions,
            },
            separators=(",", ":"),
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return path


def test_calendar_coverage_warns_before_expiry_and_blocks_after_expiry(tmp_path: Path) -> None:
    path = manifest(
        tmp_path / "calendar.json",
        end="2026-08-28",
        sessions=["2026-08-21", "2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28"],
    )
    calendar = load_trading_calendar_manifest(path)

    assert calendar.coverage_status(date(2026, 8, 25), warning_days=5).state is CalendarCoverageState.WARNING
    assert calendar.coverage_status(date(2026, 8, 29), warning_days=5).state is CalendarCoverageState.EXPIRED
    with pytest.raises(CalendarValidationError, match="outside authoritative calendar coverage"):
        calendar.is_trading_session(date(2026, 8, 29))


def test_calendar_update_cannot_rewrite_any_already_used_session(tmp_path: Path) -> None:
    current = load_trading_calendar_manifest(
        manifest(
            tmp_path / "current.json",
            end="2026-08-28",
            sessions=["2026-08-21", "2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28"],
        )
    )
    proposed = load_trading_calendar_manifest(
        manifest(
            tmp_path / "proposed.json",
            end="2026-09-04",
            sessions=["2026-08-21", "2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28", "2026-08-31", "2026-09-01"],
        )
    )

    with pytest.raises(CalendarValidationError, match="used session"):
        validate_calendar_update(current=current, proposed=proposed, used_through=date(2026, 8, 25))


def test_calendar_update_may_extend_future_coverage_without_changing_past(tmp_path: Path) -> None:
    sessions = ["2026-08-21", "2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28"]
    current = load_trading_calendar_manifest(
        manifest(tmp_path / "current.json", end="2026-08-28", sessions=sessions)
    )
    proposed = load_trading_calendar_manifest(
        manifest(
            tmp_path / "proposed.json",
            end="2026-09-04",
            sessions=[*sessions, "2026-08-31", "2026-09-01"],
        )
    )

    receipt = validate_calendar_update(
        current=current,
        proposed=proposed,
        used_through=date(2026, 8, 25),
    )

    assert receipt.previous_sha256 == current.sha256
    assert receipt.proposed_sha256 == proposed.sha256
    assert receipt.covered_through == date(2026, 9, 4)
