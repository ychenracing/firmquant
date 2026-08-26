"""Explicit authoritative trading calendar without weekday inference."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from firmquant.persistence.repositories import canonical_sha256

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class CalendarValidationError(ValueError):
    """Raised when a calendar manifest or query is not authoritatively valid."""


class CalendarCoverageError(CalendarValidationError):
    """Raised rather than guessing outside verified calendar coverage."""


def _calendar_date(value: date, *, label: str) -> None:
    if type(value) is not date:
        raise CalendarValidationError(f"{label} must be a date")


@dataclass(frozen=True, slots=True)
class AuthoritativeTradingCalendar:
    """A finite, sealed set of sessions from a trusted current provider."""

    source: str
    source_sha256: str
    covered_from: date
    covered_through: date
    trading_sessions: tuple[date, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not self.source or self.source != self.source.strip():
            raise CalendarValidationError("calendar source must be canonical text")
        if not isinstance(self.source_sha256, str) or _SHA256.fullmatch(self.source_sha256) is None:
            raise CalendarValidationError("calendar source digest must be lowercase SHA-256")
        _calendar_date(self.covered_from, label="calendar coverage start")
        _calendar_date(self.covered_through, label="calendar coverage end")
        if self.covered_through < self.covered_from:
            raise CalendarValidationError("calendar coverage end precedes start")
        if not isinstance(self.trading_sessions, tuple):
            raise CalendarValidationError("trading sessions must be a tuple")
        for session in self.trading_sessions:
            _calendar_date(session, label="trading session")
            if not self.covered_from <= session <= self.covered_through:
                raise CalendarValidationError("trading session is outside declared coverage")
        if self.trading_sessions != tuple(sorted(set(self.trading_sessions))):
            raise CalendarValidationError("trading sessions must be sorted and unique")

    @property
    def sha256(self) -> str:
        return canonical_sha256(
            {
                "schema": "firmquant.authoritative-trading-calendar.v1",
                "source": self.source,
                "source_sha256": self.source_sha256,
                "covered_from": self.covered_from,
                "covered_through": self.covered_through,
                "trading_sessions": self.trading_sessions,
            }
        )

    def _require_coverage(self, session: date) -> None:
        _calendar_date(session, label="calendar query")
        if not self.covered_from <= session <= self.covered_through:
            raise CalendarCoverageError("date is outside authoritative calendar coverage")

    def is_trading_session(self, session: date) -> bool:
        self._require_coverage(session)
        return session in self.trading_sessions

    def next_trading_session(self, session: date) -> date:
        self._require_coverage(session)
        for candidate in self.trading_sessions:
            if candidate > session:
                return candidate
        raise CalendarCoverageError("next session is outside authoritative calendar coverage")

    def previous_trading_session(self, session: date) -> date:
        self._require_coverage(session)
        for candidate in reversed(self.trading_sessions):
            if candidate < session:
                return candidate
        raise CalendarCoverageError("previous session is outside authoritative calendar coverage")


__all__ = (
    "AuthoritativeTradingCalendar",
    "CalendarCoverageError",
    "CalendarValidationError",
)
