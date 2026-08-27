"""Explicit authoritative trading calendar without weekday inference."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from firmquant.persistence.repositories import canonical_sha256

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class CalendarValidationError(ValueError):
    """Raised when a calendar manifest or query is not authoritatively valid."""


class CalendarCoverageError(CalendarValidationError):
    """Raised rather than guessing outside verified calendar coverage."""


class CalendarCoverageState(StrEnum):
    HEALTHY = "HEALTHY"
    WARNING = "WARNING"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True, slots=True)
class CalendarCoverageStatus:
    state: CalendarCoverageState
    as_of: date
    covered_through: date
    remaining_days: int


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

    def coverage_status(self, as_of: date, *, warning_days: int) -> CalendarCoverageStatus:
        _calendar_date(as_of, label="calendar coverage query")
        if isinstance(warning_days, bool) or not isinstance(warning_days, int) or warning_days < 0:
            raise CalendarValidationError("calendar warning days must be a nonnegative integer")
        remaining = (self.covered_through - as_of).days
        if as_of < self.covered_from or remaining < 0:
            state = CalendarCoverageState.EXPIRED
        elif remaining <= warning_days:
            state = CalendarCoverageState.WARNING
        else:
            state = CalendarCoverageState.HEALTHY
        return CalendarCoverageStatus(
            state=state,
            as_of=as_of,
            covered_through=self.covered_through,
            remaining_days=remaining,
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
    "CalendarCoverageState",
    "CalendarCoverageStatus",
    "CalendarValidationError",
)
