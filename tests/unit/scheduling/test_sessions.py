from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from firmquant.application.workflows import WorkflowOutcome
from firmquant.persistence.audit import AuditLedger
from firmquant.persistence.writer_lease import WriterLease
from firmquant.scheduling.sessions import (
    WorkflowAlreadyClaimed,
    WorkflowConflict,
    WorkflowReceiptStore,
    WorkflowRecoveryRequired,
    WorkflowRunner,
    WorkflowStep,
)

NOW = datetime(2026, 8, 25, 1, 30, tzinfo=UTC)
SESSION = date(2026, 8, 25)
OUTCOME = WorkflowOutcome(output_sha256="a" * 64, reference_id="receipt-1")


@pytest.fixture
def writer_lease(tmp_path: Path) -> Iterator[WriterLease]:
    value = WriterLease.acquire(
        tmp_path / "workflow-receipts.sqlite3",
        owner="workflow-receipt-test",
        clock=lambda: NOW,
    )
    try:
        yield value
    finally:
        value.release()


def _runner(writer_lease: WriterLease) -> WorkflowRunner:
    return WorkflowRunner(receipts=WorkflowReceiptStore(writer_lease=writer_lease))


def test_completed_step_recovers_without_repeating_action(writer_lease: WriterLease) -> None:
    runner = _runner(writer_lease)
    action_calls = 0
    recovery_calls = 0

    def action() -> WorkflowOutcome:
        nonlocal action_calls
        action_calls += 1
        return OUTCOME

    def recover(_: str) -> WorkflowOutcome:
        nonlocal recovery_calls
        recovery_calls += 1
        return OUTCOME

    first = runner.run(
        step=WorkflowStep.EOD,
        session=SESSION,
        input_sha256="1" * 64,
        now=lambda: NOW,
        action=action,
        recover=recover,
        seal=lambda value: value,
    )
    second = runner.run(
        step=WorkflowStep.EOD,
        session=SESSION,
        input_sha256="1" * 64,
        now=lambda: NOW,
        action=action,
        recover=recover,
        seal=lambda value: value,
    )

    assert first.recovered is False
    assert second.recovered is True
    assert action_calls == 1
    assert recovery_calls == 1
    assert AuditLedger(writer_lease.database).verify().count == 2


def test_failed_step_enters_recovery_instead_of_blind_retry(
    writer_lease: WriterLease,
) -> None:
    runner = _runner(writer_lease)
    action_calls = 0

    def crash() -> WorkflowOutcome:
        nonlocal action_calls
        action_calls += 1
        raise RuntimeError("injected failure")

    with pytest.raises(RuntimeError, match="injected failure"):
        runner.run(
            step=WorkflowStep.NEXT_DAY_EXECUTION,
            session=SESSION,
            input_sha256="2" * 64,
            now=lambda: NOW,
            action=crash,
            recover=lambda _: OUTCOME,
            seal=lambda value: value,
        )

    with pytest.raises(WorkflowRecoveryRequired, match="startup recovery"):
        runner.run(
            step=WorkflowStep.NEXT_DAY_EXECUTION,
            session=SESSION,
            input_sha256="2" * 64,
            now=lambda: NOW,
            action=crash,
            recover=lambda _: OUTCOME,
            seal=lambda value: value,
        )

    recovered = runner.run(
        step=WorkflowStep.NEXT_DAY_EXECUTION,
        session=SESSION,
        input_sha256="2" * 64,
        now=lambda: NOW,
        action=crash,
        recover=lambda _: OUTCOME,
        seal=lambda value: value,
        allow_incomplete_recovery=True,
    )

    assert recovered.recovered is True
    assert recovered.value == OUTCOME
    assert action_calls == 1
    assert AuditLedger(writer_lease.database).verify().count == 3


def test_atomic_claim_loser_never_runs_side_effect(writer_lease: WriterLease) -> None:
    runner = _runner(writer_lease)
    calls = 0

    def action() -> WorkflowOutcome:
        nonlocal calls
        calls += 1
        return OUTCOME

    runner.run_new(
        step=WorkflowStep.EOD,
        session=SESSION,
        input_sha256="6" * 64,
        evidence={},
        now=lambda: NOW,
        action=action,
        seal=lambda value: value,
    )
    with pytest.raises(WorkflowAlreadyClaimed, match="atomically claimed"):
        runner.run_new(
            step=WorkflowStep.EOD,
            session=SESSION,
            input_sha256="6" * 64,
            evidence={},
            now=lambda: NOW,
            action=action,
            seal=lambda value: value,
        )

    assert calls == 1


def test_changed_input_for_same_session_requires_explicit_resolution(
    writer_lease: WriterLease,
) -> None:
    runner = _runner(writer_lease)
    runner.run(
        step=WorkflowStep.POST_CLOSE_DECISION,
        session=SESSION,
        input_sha256="3" * 64,
        now=lambda: NOW,
        action=lambda: OUTCOME,
        recover=lambda _: OUTCOME,
        seal=lambda value: value,
    )

    with pytest.raises(WorkflowConflict, match="different sealed inputs"):
        runner.run(
            step=WorkflowStep.POST_CLOSE_DECISION,
            session=SESSION,
            input_sha256="4" * 64,
            now=lambda: NOW,
            action=lambda: OUTCOME,
            recover=lambda _: OUTCOME,
            seal=lambda value: value,
        )

    assert AuditLedger(writer_lease.database).verify().count == 2


def test_completed_outcome_cannot_be_silently_rewritten(writer_lease: WriterLease) -> None:
    runner = _runner(writer_lease)
    runner.run(
        step=WorkflowStep.STARTUP,
        session=SESSION,
        input_sha256="5" * 64,
        now=lambda: NOW,
        action=lambda: OUTCOME,
        recover=lambda _: OUTCOME,
        seal=lambda value: value,
    )
    changed = WorkflowOutcome(output_sha256="b" * 64, reference_id="receipt-2")

    with pytest.raises(WorkflowConflict, match="differs from durable completion"):
        runner.run(
            step=WorkflowStep.STARTUP,
            session=SESSION,
            input_sha256="5" * 64,
            now=lambda: NOW,
            action=lambda: changed,
            recover=lambda _: changed,
            seal=lambda value: value,
        )

    assert AuditLedger(writer_lease.database).verify().count == 2
