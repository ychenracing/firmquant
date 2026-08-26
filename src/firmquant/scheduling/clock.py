"""Fail-closed wall-clock and timezone validation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from firmquant.persistence.repositories import canonical_sha256

_SHANGHAI_NAME = "Asia/Shanghai"
_SHANGHAI = ZoneInfo(_SHANGHAI_NAME)


class ClockValidationError(RuntimeError):
    """Raised when time cannot authorize a production session."""


def _aware(value: datetime, *, label: str) -> None:
    if not isinstance(value, datetime):
        raise ClockValidationError(f"{label} must be datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ClockValidationError(f"{label} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ClockObservation:
    """One system/reference observation supplied by an operator-approved source."""

    system_time: datetime
    reference_time: datetime
    local_timezone: str

    def __post_init__(self) -> None:
        _aware(self.system_time, label="system time")
        _aware(self.reference_time, label="reference time")
        if (
            not isinstance(self.local_timezone, str)
            or not self.local_timezone
            or self.local_timezone != self.local_timezone.strip()
        ):
            raise ClockValidationError("local timezone must be canonical text")


@dataclass(frozen=True, slots=True)
class ClockReceipt:
    system_time: datetime
    reference_time: datetime
    shanghai_time: datetime
    drift_milliseconds: int
    sha256: str


class ClockGuard:
    """Require Asia/Shanghai configuration and a bounded trusted-clock drift."""

    def __init__(self, *, max_drift: timedelta) -> None:
        if not isinstance(max_drift, timedelta):
            raise TypeError("maximum clock drift must be timedelta")
        if max_drift <= timedelta(0):
            raise ValueError("maximum clock drift must be positive")
        self._max_drift = max_drift

    @property
    def max_drift(self) -> timedelta:
        return self._max_drift

    def verify(self, observation: ClockObservation) -> ClockReceipt:
        if not isinstance(observation, ClockObservation):
            raise ClockValidationError("clock observation must be typed")
        if observation.local_timezone != _SHANGHAI_NAME:
            raise ClockValidationError("local timezone must be Asia/Shanghai")
        drift = abs(observation.system_time - observation.reference_time)
        if drift > self._max_drift:
            raise ClockValidationError("system clock drift exceeds configured maximum")
        drift_milliseconds = round(drift.total_seconds() * 1_000)
        shanghai_time = observation.system_time.astimezone(_SHANGHAI)
        digest = canonical_sha256(
            {
                "schema": "firmquant.clock-receipt.v1",
                "system_time": observation.system_time,
                "reference_time": observation.reference_time,
                "shanghai_time": shanghai_time,
                "local_timezone": observation.local_timezone,
                "drift_milliseconds": drift_milliseconds,
            }
        )
        return ClockReceipt(
            system_time=observation.system_time,
            reference_time=observation.reference_time,
            shanghai_time=shanghai_time,
            drift_milliseconds=drift_milliseconds,
            sha256=digest,
        )


__all__ = (
    "ClockGuard",
    "ClockObservation",
    "ClockReceipt",
    "ClockValidationError",
)
