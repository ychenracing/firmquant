"""Strict governance for an operator-reviewed exchange trading calendar."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from firmquant.market_data.calendar import AuthoritativeTradingCalendar, CalendarValidationError

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FIELDS = {
    "schema_version",
    "source_name",
    "source_sha256",
    "covered_start",
    "covered_end",
    "sessions",
}


@dataclass(frozen=True, slots=True)
class CalendarUpdateReceipt:
    previous_sha256: str
    proposed_sha256: str
    used_through: date
    covered_through: date


def load_trading_calendar_manifest(path: Path) -> AuthoritativeTradingCalendar:
    candidate = Path(path)
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError("trading calendar manifest must be an existing non-symlink file")
    if candidate.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("trading calendar manifest exceeds safety limit")
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("trading calendar manifest is invalid UTF-8 JSON") from error
    if not isinstance(payload, dict) or set(payload) != _FIELDS:
        raise ValueError("trading calendar manifest schema does not match contract")
    if payload["schema_version"] != 1:
        raise ValueError("unsupported trading calendar manifest schema version")
    source_name = payload["source_name"]
    source_sha256 = payload["source_sha256"]
    if not isinstance(source_name, str) or not source_name or source_name != source_name.strip():
        raise ValueError("trading calendar source name is invalid")
    if not isinstance(source_sha256, str) or _SHA256.fullmatch(source_sha256) is None:
        raise ValueError("trading calendar source digest is invalid")
    try:
        covered_start = date.fromisoformat(str(payload["covered_start"]))
        covered_end = date.fromisoformat(str(payload["covered_end"]))
    except ValueError as error:
        raise ValueError("trading calendar coverage must use ISO dates") from error
    raw_sessions = payload["sessions"]
    if not isinstance(raw_sessions, list) or not all(isinstance(item, str) for item in raw_sessions):
        raise ValueError("trading calendar sessions must be an ISO date array")
    try:
        sessions = tuple(date.fromisoformat(item) for item in raw_sessions)
    except ValueError as error:
        raise ValueError("trading calendar session is not an ISO date") from error
    if sessions != tuple(sorted(set(sessions))):
        raise ValueError("trading calendar sessions must be sorted and unique")
    return AuthoritativeTradingCalendar(
        source=source_name,
        source_sha256=source_sha256,
        covered_from=covered_start,
        covered_through=covered_end,
        trading_sessions=sessions,
    )


def validate_calendar_update(
    *,
    current: AuthoritativeTradingCalendar,
    proposed: AuthoritativeTradingCalendar,
    used_through: date,
) -> CalendarUpdateReceipt:
    if type(used_through) is not date:
        raise CalendarValidationError("used-through calendar session must be a date")
    if proposed.covered_from > current.covered_from:
        raise CalendarValidationError("proposed calendar drops prior authoritative coverage")
    if proposed.covered_through < current.covered_through:
        raise CalendarValidationError("proposed calendar shortens authoritative coverage")
    if not proposed.covered_from <= used_through <= proposed.covered_through:
        raise CalendarValidationError("proposed calendar does not cover already used sessions")
    current_used = tuple(item for item in current.trading_sessions if item <= used_through)
    proposed_used = tuple(item for item in proposed.trading_sessions if item <= used_through)
    if current_used != proposed_used:
        raise CalendarValidationError("proposed calendar rewrites an already used session")
    return CalendarUpdateReceipt(
        previous_sha256=current.sha256,
        proposed_sha256=proposed.sha256,
        used_through=used_through,
        covered_through=proposed.covered_through,
    )


__all__ = (
    "CalendarUpdateReceipt",
    "load_trading_calendar_manifest",
    "validate_calendar_update",
)
