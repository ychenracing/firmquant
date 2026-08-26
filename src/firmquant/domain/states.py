"""Explicit fail-closed runtime state machine."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from .errors import DomainTransitionError, DomainTypeError, DomainValidationError


class RuntimeState(StrEnum):
    DISARMED = "DISARMED"
    STARTING = "STARTING"
    RECONCILING = "RECONCILING"
    READY = "READY"
    EXECUTING = "EXECUTING"
    DEGRADED = "DEGRADED"
    HALTED = "HALTED"
    STOPPING = "STOPPING"


RUNTIME_TRANSITIONS: Final = MappingProxyType(
    {
        RuntimeState.DISARMED: frozenset({RuntimeState.STARTING}),
        RuntimeState.STARTING: frozenset(
            {RuntimeState.RECONCILING, RuntimeState.HALTED, RuntimeState.STOPPING}
        ),
        RuntimeState.RECONCILING: frozenset(
            {
                RuntimeState.READY,
                RuntimeState.DEGRADED,
                RuntimeState.HALTED,
                RuntimeState.STOPPING,
            }
        ),
        RuntimeState.READY: frozenset(
            {
                RuntimeState.EXECUTING,
                RuntimeState.RECONCILING,
                RuntimeState.DEGRADED,
                RuntimeState.HALTED,
                RuntimeState.STOPPING,
            }
        ),
        RuntimeState.EXECUTING: frozenset(
            {
                RuntimeState.READY,
                RuntimeState.RECONCILING,
                RuntimeState.DEGRADED,
                RuntimeState.HALTED,
                RuntimeState.STOPPING,
            }
        ),
        RuntimeState.DEGRADED: frozenset(
            {RuntimeState.RECONCILING, RuntimeState.HALTED, RuntimeState.STOPPING}
        ),
        RuntimeState.HALTED: frozenset({RuntimeState.RECONCILING, RuntimeState.STOPPING}),
        RuntimeState.STOPPING: frozenset({RuntimeState.DISARMED}),
    }
)


def _canonical_blockers(blockers: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(blockers, tuple):
        raise DomainTypeError("runtime blockers must be a tuple")
    for blocker in blockers:
        if not isinstance(blocker, str) or not blocker or blocker != blocker.strip():
            raise DomainValidationError("runtime blocker must be canonical non-empty text")
    if len(blockers) != len(set(blockers)):
        raise DomainValidationError("runtime blockers must be unique")
    return tuple(sorted(blockers))


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    """Persistable runtime state plus exact blockers and monotonic revision."""

    state: RuntimeState
    revision: int
    reason: str
    blockers: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.state, RuntimeState):
            raise DomainTypeError("runtime state must be RuntimeState")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int):
            raise DomainTypeError("runtime revision must be an integer")
        if self.revision < 0:
            raise DomainValidationError("runtime revision must be nonnegative")
        if not isinstance(self.reason, str) or not self.reason or self.reason != self.reason.strip():
            raise DomainValidationError("runtime reason must be canonical non-empty text")
        object.__setattr__(self, "blockers", _canonical_blockers(self.blockers))

    @classmethod
    def initial(cls) -> RuntimeStatus:
        return cls(
            state=RuntimeState.DISARMED,
            revision=0,
            reason="not started",
            blockers=(),
        )

    def transition(
        self,
        target: RuntimeState,
        *,
        reason: str,
        blockers: tuple[str, ...] = (),
    ) -> RuntimeStatus:
        """Apply one legal transition; HALTED can never jump directly to READY."""

        if not isinstance(target, RuntimeState):
            raise DomainTypeError("runtime target must be RuntimeState")
        canonical_blockers = _canonical_blockers(blockers)
        if target is self.state:
            if reason == self.reason and canonical_blockers == self.blockers:
                return self
        elif target not in RUNTIME_TRANSITIONS[self.state]:
            raise DomainTransitionError(f"illegal runtime transition {self.state.value} -> {target.value}")
        return replace(
            self,
            state=target,
            revision=self.revision + 1,
            reason=reason,
            blockers=canonical_blockers,
        )


__all__ = ("RUNTIME_TRANSITIONS", "RuntimeState", "RuntimeStatus")
