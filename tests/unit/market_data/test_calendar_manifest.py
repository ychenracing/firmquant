from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from firmquant.market_data.calendar import CalendarValidationError
from firmquant.market_data.calendar_manifest import load_trading_calendar_manifest


def test_calendar_manifest_loads_explicit_exchange_sessions(tmp_path: Path) -> None:
    path = tmp_path / "trading-calendar.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_name": "xtquant-reviewed-calendar",
                "source_sha256": "a" * 64,
                "covered_start": "2026-08-24",
                "covered_end": "2026-08-28",
                "sessions": ["2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28"],
            }
        ),
        encoding="utf-8",
    )

    calendar = load_trading_calendar_manifest(path)

    assert calendar.is_trading_session(date(2026, 8, 25))
    assert calendar.previous_trading_session(date(2026, 8, 26)) == date(2026, 8, 25)
    assert len(calendar.sha256) == 64


def test_calendar_manifest_rejects_missing_coverage_and_weekday_inference(tmp_path: Path) -> None:
    path = tmp_path / "trading-calendar.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_name": "xtquant-reviewed-calendar",
                "source_sha256": "a" * 64,
                "covered_start": "2026-08-25",
                "covered_end": "2026-08-25",
                "sessions": [],
            }
        ),
        encoding="utf-8",
    )

    calendar = load_trading_calendar_manifest(path)
    assert calendar.is_trading_session(date(2026, 8, 25)) is False
    with pytest.raises(CalendarValidationError, match="outside authoritative calendar coverage"):
        calendar.is_trading_session(date(2026, 8, 24))


def test_calendar_manifest_rejects_extra_fields(tmp_path: Path) -> None:
    path = tmp_path / "trading-calendar.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_name": "xtquant-reviewed-calendar",
                "source_sha256": "a" * 64,
                "covered_start": "2026-08-25",
                "covered_end": "2026-08-25",
                "sessions": ["2026-08-25"],
                "weekday_fallback": True,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="schema"):
        load_trading_calendar_manifest(path)
