from __future__ import annotations

import pytest

from firmquant.domain.errors import DomainTransitionError
from firmquant.domain.states import RuntimeState, RuntimeStatus


def test_runtime_uses_explicit_operational_states() -> None:
    assert {state.value for state in RuntimeState} == {
        "DISARMED",
        "STARTING",
        "RECONCILING",
        "READY",
        "EXECUTING",
        "DEGRADED",
        "HALTED",
        "STOPPING",
    }


def test_startup_must_reconcile_before_ready() -> None:
    status = RuntimeStatus.initial()

    status = status.transition(RuntimeState.STARTING, reason="operator start")
    with pytest.raises(DomainTransitionError, match=r"STARTING.*READY"):
        status.transition(RuntimeState.READY, reason="skip reconciliation")
    status = status.transition(RuntimeState.RECONCILING, reason="startup snapshot")
    status = status.transition(RuntimeState.READY, reason="reconciliation passed")

    assert status.state is RuntimeState.READY
    assert status.revision == 3


def test_halted_runtime_can_only_stop_or_reconcile() -> None:
    halted = RuntimeStatus(
        state=RuntimeState.HALTED,
        revision=7,
        reason="kill switch",
        blockers=("KILL_SWITCH",),
    )

    with pytest.raises(DomainTransitionError, match=r"HALTED.*READY"):
        halted.transition(RuntimeState.READY, reason="unsafe resume")
    reconciling = halted.transition(
        RuntimeState.RECONCILING,
        reason="explicit resume requires reconciliation",
        blockers=(),
    )

    assert reconciling.state is RuntimeState.RECONCILING


def test_same_runtime_state_is_idempotent_only_for_same_facts() -> None:
    status = RuntimeStatus.initial()

    assert status.transition(RuntimeState.DISARMED, reason="not started") is status
    changed = status.transition(
        RuntimeState.DISARMED,
        reason="operator disarmed",
        blockers=("ARM_LEASE_MISSING",),
    )

    assert changed.revision == 1
    assert changed.blockers == ("ARM_LEASE_MISSING",)
