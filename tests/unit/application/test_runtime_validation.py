from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime

import pytest

from firmquant.application.runtime import (
    ModeWriteBlocked,
    ReadOnlyBrokerSession,
    Runtime,
    RuntimeSessionError,
    ShadowReconciliation,
    ShadowReportReceipt,
    ShadowSessionDraft,
)
from firmquant.config import Mode
from firmquant.domain.errors import DomainTypeError, DomainValidationError
from firmquant.domain.states import RuntimeState
from firmquant.execution.planner import ExecutionPlanner
from tests.e2e.test_shadow_session import RecordingShadowWorkflow, real_like_read_broker
from tests.fixtures.session_cases import NOW, decision_snapshot, execution_snapshot


def _draft() -> ShadowSessionDraft:
    facts = execution_snapshot()
    decision = decision_snapshot()
    plan = ExecutionPlanner().plan(decision, facts)
    return ShadowSessionDraft(
        mode=Mode.SHADOW,
        broker_snapshot=facts.broker_snapshot,
        reconciliation=ShadowReconciliation(
            receipt_id="recon-1",
            passed=True,
            blockers=(),
            broker_snapshot_sha256=facts.broker_snapshot.raw_payload_sha256,
        ),
        decision=decision,
        plan=plan,
        started_at=NOW,
        completed_at=NOW,
    )


@pytest.mark.parametrize(
    ("factory", "exception"),
    [
        (
            lambda: ShadowReconciliation("", True, (), "a" * 64),
            DomainValidationError,
        ),
        (
            lambda: ShadowReconciliation("recon", 1, (), "a" * 64),
            DomainTypeError,
        ),
        (
            lambda: ShadowReconciliation("recon", True, [], "a" * 64),
            DomainTypeError,
        ),
        (
            lambda: ShadowReconciliation("recon", False, ("Z", "A"), "a" * 64),
            DomainValidationError,
        ),
        (
            lambda: ShadowReconciliation("recon", False, (" bad",), "a" * 64),
            DomainValidationError,
        ),
        (
            lambda: ShadowReconciliation("recon", True, ("BLOCKED",), "a" * 64),
            DomainValidationError,
        ),
        (
            lambda: ShadowReconciliation("recon", True, (), "bad"),
            DomainValidationError,
        ),
        (lambda: ShadowReportReceipt("", "a" * 64), DomainValidationError),
        (lambda: ShadowReportReceipt("report", "bad"), DomainValidationError),
    ],
)
def test_shadow_evidence_models_reject_invalid_values(
    factory: Callable[[], object], exception: type[Exception]
) -> None:
    with pytest.raises(exception):
        factory()


@pytest.mark.parametrize(
    ("change", "exception"),
    [
        ({"mode": Mode.PAPER}, DomainValidationError),
        ({"broker_snapshot": object()}, DomainTypeError),
        ({"reconciliation": object()}, DomainTypeError),
        ({"decision": object()}, DomainTypeError),
        ({"plan": object()}, DomainTypeError),
        ({"started_at": datetime(2026, 8, 25)}, DomainValidationError),
        ({"completed_at": datetime(2026, 8, 25)}, DomainValidationError),
        ({"completed_at": NOW.replace(year=2025)}, DomainValidationError),
        ({"plan": replace(_draft().plan, decision_id="other")}, DomainValidationError),
        (
            {
                "reconciliation": replace(
                    _draft().reconciliation,
                    broker_snapshot_sha256="c" * 64,
                )
            },
            DomainValidationError,
        ),
    ],
)
def test_shadow_session_draft_rejects_crossed_evidence(
    change: dict[str, object], exception: type[Exception]
) -> None:
    with pytest.raises(exception):
        replace(_draft(), **change)


def test_read_only_session_rejects_invalid_dependencies_and_exposes_no_write_port() -> None:
    with pytest.raises(DomainTypeError):
        ReadOnlyBrokerSession(gateway=object(), clock=lambda: NOW)  # type: ignore[arg-type]
    with pytest.raises(DomainTypeError):
        ReadOnlyBrokerSession(gateway=real_like_read_broker(), clock=object())  # type: ignore[arg-type]

    session = ReadOnlyBrokerSession(gateway=real_like_read_broker(), clock=lambda: NOW)
    assert repr(session) == "<ReadOnlyBrokerSession>"
    assert not hasattr(session, "submit_order")
    assert not hasattr(session, "cancel_order")


def test_snapshot_capture_rejects_naive_clock() -> None:
    gateway = real_like_read_broker()
    gateway.connect()
    session = ReadOnlyBrokerSession(gateway=gateway, clock=lambda: datetime(2026, 8, 25))
    with pytest.raises(DomainValidationError, match="timezone-aware"):
        session.capture_snapshot()


def test_runtime_rejects_invalid_composition_and_illegal_calls() -> None:
    gateway = real_like_read_broker()
    workflow = RecordingShadowWorkflow()
    with pytest.raises(DomainTypeError):
        Runtime(gateway=object(), workflow=workflow, clock=lambda: NOW)  # type: ignore[arg-type]
    with pytest.raises(DomainTypeError):
        Runtime(gateway=gateway, workflow=object(), clock=lambda: NOW)  # type: ignore[arg-type]
    with pytest.raises(DomainTypeError):
        Runtime(gateway=gateway, workflow=workflow, clock=object())  # type: ignore[arg-type]

    runtime = Runtime(gateway=gateway, workflow=workflow, clock=lambda: NOW)
    with pytest.raises(DomainTypeError):
        runtime.start("SHADOW")  # type: ignore[arg-type]
    with pytest.raises(RuntimeSessionError, match="not READY"):
        runtime.run_one_session()
    with pytest.raises(ModeWriteBlocked, match="UNSTARTED"):
        runtime.cancel_system_orders()
    runtime.stop()
    assert runtime.status.state is RuntimeState.DISARMED
    assert repr(runtime) == "<Runtime gateway=redacted>"


def test_runtime_rejects_second_start_and_missing_startup_evidence() -> None:
    gateway = real_like_read_broker()
    runtime = Runtime(gateway=gateway, workflow=RecordingShadowWorkflow(), clock=lambda: NOW)
    runtime.start(Mode.SHADOW)
    with pytest.raises(RuntimeSessionError, match="already started"):
        runtime.start(Mode.SHADOW)

    runtime._snapshot = None  # type: ignore[attr-defined]
    with pytest.raises(RuntimeSessionError, match="evidence is unavailable"):
        runtime.run_one_session()


def test_disconnect_failure_is_recorded_without_masking_safe_stop() -> None:
    gateway = real_like_read_broker()
    runtime = Runtime(gateway=gateway, workflow=RecordingShadowWorkflow(), clock=lambda: NOW)
    runtime.start(Mode.SHADOW)

    def fail_disconnect() -> None:
        raise RuntimeError("injected disconnect failure")

    gateway.disconnect = fail_disconnect  # type: ignore[method-assign]
    runtime.stop()
    assert runtime.status.state is RuntimeState.DISARMED
    assert runtime.status.blockers == ("BROKER_DISCONNECT_FAILED",)
