"""Strict local manifest loader for an operator-reviewed exchange trading calendar."""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from firmquant.market_data.calendar import AuthoritativeTradingCalendar

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FIELDS = {
    "schema_version",
    "source_name",
    "source_sha256",
    "covered_start",
    "covered_end",
    "sessions",
}


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


__all__ = ("load_trading_calendar_manifest",)
