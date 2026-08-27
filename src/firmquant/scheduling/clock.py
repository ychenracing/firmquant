"""Fail-closed wall-clock validation plus monotonic runtime time fences."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from firmquant.persistence.repositories import canonical_sha256

_SHANGHAI_NAME = "Asia/Shanghai"
_SHANGHAI = ZoneInfo(_SHANGHAI_NAME)


class ClockValidationError(RuntimeError):
    """Raised when time cannot authorize a production session."""


class RuntimeClockDiscontinuity(ClockValidationError):
    """Raised when wall/monotonic observations prove an unsafe time jump."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ClockReferenceProvider(Protocol):
    """Port for a trusted external or broker-backed reference-time observation."""

    def observe(self, system_time: datetime) -> ClockObservation | None: ...


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


@dataclass(frozen=True, slots=True)
class MonotonicDeadline:
    """Absolute monotonic deadline; never derived from wall-clock differences."""

    value: float

    def __post_init__(self) -> None:
        if isinstance(self.value, bool) or not isinstance(self.value, (int, float)):
            raise ClockValidationError("monotonic deadline must be numeric")
        if self.value < 0:
            raise ClockValidationError("monotonic deadline cannot be negative")

    def remaining(self, monotonic_clock: Callable[[], float]) -> float:
        observed = monotonic_clock()
        if isinstance(observed, bool) or not isinstance(observed, (int, float)):
            raise ClockValidationError("monotonic clock must return a number")
        return max(0.0, float(self.value) - float(observed))

    def expired(self, monotonic_clock: Callable[[], float]) -> bool:
        observed = monotonic_clock()
        if isinstance(observed, bool) or not isinstance(observed, (int, float)):
            raise ClockValidationError("monotonic clock must return a number")
        return float(observed) >= float(self.value)


@dataclass(frozen=True, slots=True)
class RuntimeClockObservation:
    wall_time: datetime
    monotonic_time: float

    def __post_init__(self) -> None:
        _aware(self.wall_time, label="runtime wall time")
        if isinstance(self.monotonic_time, bool) or not isinstance(self.monotonic_time, (int, float)):
            raise ClockValidationError("runtime monotonic time must be numeric")
        if self.monotonic_time < 0:
            raise ClockValidationError("runtime monotonic time cannot be negative")


class RuntimeClockMonitor:
    """Detect process pauses, suspend/resume, wall rollback and clock divergence."""

    def __init__(
        self,
        *,
        max_observation_gap: timedelta = timedelta(seconds=30),
        max_wall_monotonic_divergence: timedelta = timedelta(seconds=2),
    ) -> None:
        if (
            not isinstance(max_observation_gap, timedelta)
            or max_observation_gap <= timedelta(0)
            or not isinstance(max_wall_monotonic_divergence, timedelta)
            or max_wall_monotonic_divergence <= timedelta(0)
        ):
            raise ValueError("runtime clock thresholds must be positive timedeltas")
        self._max_gap = max_observation_gap.total_seconds()
        self._max_divergence = max_wall_monotonic_divergence.total_seconds()
        self._previous: RuntimeClockObservation | None = None

    def observe(self, observation: RuntimeClockObservation) -> None:
        if not isinstance(observation, RuntimeClockObservation):
            raise ClockValidationError("runtime clock observation must be typed")
        previous = self._previous
        self._previous = observation
        if previous is None:
            return
        monotonic_delta = observation.monotonic_time - previous.monotonic_time
        wall_delta = (observation.wall_time - previous.wall_time).total_seconds()
        if monotonic_delta < 0:
            raise RuntimeClockDiscontinuity("MONOTONIC_CLOCK_ROLLBACK")
        if wall_delta < -self._max_divergence:
            raise RuntimeClockDiscontinuity("WALL_CLOCK_ROLLBACK")
        if monotonic_delta > self._max_gap:
            raise RuntimeClockDiscontinuity("RUNTIME_OBSERVATION_GAP")
        if abs(wall_delta - monotonic_delta) > self._max_divergence:
            raise RuntimeClockDiscontinuity("WALL_MONOTONIC_DIVERGENCE")


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
    "ClockReferenceProvider",
    "ClockValidationError",
    "MonotonicDeadline",
    "RuntimeClockDiscontinuity",
    "RuntimeClockMonitor",
    "RuntimeClockObservation",
)
