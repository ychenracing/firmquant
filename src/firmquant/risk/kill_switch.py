"""Sticky local emergency stop requiring explicit reconciled reset."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime

from firmquant.domain.errors import DomainTypeError, DomainValidationError


def _reason(value: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 512:
        raise DomainValidationError("kill switch reason must be canonical non-empty text")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise DomainValidationError("kill switch reason contains control characters")


def _aware(value: datetime) -> None:
    if not isinstance(value, datetime):
        raise DomainTypeError("kill switch time must be datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise DomainValidationError("kill switch time must be timezone-aware")


@dataclass(frozen=True, slots=True)
class KillSwitchStatus:
    tripped: bool
    revision: int
    reason: str
    changed_at: datetime | None

    def __post_init__(self) -> None:
        if not isinstance(self.tripped, bool):
            raise DomainTypeError("kill switch tripped flag must be bool")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int):
            raise DomainTypeError("kill switch revision must be integer")
        if self.revision < 0:
            raise DomainValidationError("kill switch revision must be nonnegative")
        _reason(self.reason)
        if self.changed_at is not None:
            _aware(self.changed_at)
        if self.revision == 0 and self.changed_at is not None:
            raise DomainValidationError("initial kill switch status cannot have change time")


class KillSwitch:
    """Thread-safe process state; reset is impossible without operator and reconciliation."""

    __slots__ = ("_lock", "_status")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._status = KillSwitchStatus(
            tripped=False,
            revision=0,
            reason="not tripped",
            changed_at=None,
        )

    def status(self) -> KillSwitchStatus:
        with self._lock:
            return self._status

    def trip(self, *, reason: str, now: datetime) -> KillSwitchStatus:
        _reason(reason)
        _aware(now)
        with self._lock:
            if self._status.tripped and self._status.reason == reason:
                return self._status
            self._status = KillSwitchStatus(
                tripped=True,
                revision=self._status.revision + 1,
                reason=reason,
                changed_at=now,
            )
            return self._status

    def reset(
        self,
        *,
        reason: str,
        now: datetime,
        operator_confirmed: bool,
        reconciliation_passed: bool,
    ) -> KillSwitchStatus:
        _reason(reason)
        _aware(now)
        if not isinstance(operator_confirmed, bool) or not isinstance(reconciliation_passed, bool):
            raise DomainTypeError("kill switch reset gates must be bool")
        if not operator_confirmed:
            raise DomainValidationError("kill switch reset requires operator confirmation")
        if not reconciliation_passed:
            raise DomainValidationError("kill switch reset requires passed reconciliation")
        with self._lock:
            self._status = KillSwitchStatus(
                tripped=False,
                revision=self._status.revision + 1,
                reason=reason,
                changed_at=now,
            )
            return self._status


__all__ = ("KillSwitch", "KillSwitchStatus")
