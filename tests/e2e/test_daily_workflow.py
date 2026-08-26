from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from firmquant.application.sessions import SessionCoordinator, SessionWorkflowError
from firmquant.application.workflows import (
    DecisionPreparation,
    ExecutionPreparation,
    MarketStatusFact,
    WorkflowOutcome,
)
from firmquant.config import Mode
from firmquant.domain.broker_facts import MarketSessionStatus
from firmquant.domain.states import RuntimeState
from firmquant.execution.planner import ExecutionBrokerSnapshot
from firmquant.market_data.calendar import AuthoritativeTradingCalendar
from firmquant.market_data.validation import (
    Adjustment,
    DataKind,
    DataManifest,
    SeriesSeal,
    StrategyDataValidator,
)
from firmquant.persistence.audit import AuditLedger
from firmquant.persistence.repositories import canonical_sha256
from firmquant.persistence.writer_lease import WriterLease, WriterLeaseBusy
from firmquant.scheduling.clock import ClockGuard, ClockObservation
from firmquant.scheduling.sessions import WorkflowReceiptStore
from firmquant.strategy.identity import StrategyIdentity
from firmquant.strategy.snapshots import DecisionSnapshot
from tests.fixtures.session_cases import NOW, decision_snapshot, execution_snapshot

STRATEGY_SESSION = date(2026, 8, 24)
EXECUTION_SESSION = date(2026, 8, 25)


def _manifest(
    session: date,
    *,
    equity_full: str,
    equity_prefix: str,
    captured_at: datetime,
) -> DataManifest:
    current = session == STRATEGY_SESSION
    row_count = 159 if current else 158
    return DataManifest(
        latest_common_session=session,
        captured_at=captured_at,
        provider="uquant-data-contract",
        series=(
            SeriesSeal(
                series_id="sz300308",
                kind=DataKind.EQUITY,
                adjustment=Adjustment.FORWARD_ADJUSTED,
                first_session=date(2026, 1, 5),
                last_session=session,
                row_count=row_count,
                full_sha256=equity_full,
                verified_prefix_row_count=row_count - 1,
                verified_prefix_sha256=equity_prefix,
            ),
            SeriesSeal(
                series_id="000300.SH",
                kind=DataKind.INDEX,
                adjustment=Adjustment.UNADJUSTED,
                first_session=date(2026, 1, 5),
                last_session=session,
                row_count=row_count,
                full_sha256="c" * 64 if current else "d" * 64,
                verified_prefix_row_count=row_count - 1,
                verified_prefix_sha256="d" * 64 if current else "e" * 64,
            ),
        ),
    )


def _calendar() -> AuthoritativeTradingCalendar:
    return AuthoritativeTradingCalendar(
        source="broker-calendar",
        source_sha256="a" * 64,
        covered_from=date(2026, 8, 21),
        covered_through=date(2026, 8, 26),
        trading_sessions=(date(2026, 8, 21), STRATEGY_SESSION, EXECUTION_SESSION),
    )


@dataclass(slots=True)
class RecordingWorkflow:
    current_data: DataManifest
    previous_data: DataManifest
    execution_facts: ExecutionBrokerSnapshot
    market_status: MarketSessionStatus = MarketSessionStatus.CLOSED
    premise_matches: bool = True
    reconciliation_healthy: bool = True
    fail_decision_once: bool = False
    fail_execution_once: bool = False
    market_status_session: date = STRATEGY_SESSION
    market_status_observed_at: datetime = NOW
    decide_calls: int = 0
    recover_decision_calls: int = 0
    execute_calls: int = 0
    recover_execution_calls: int = 0
    startup_calls: int = 0
    recover_startup_calls: int = 0
    intraday_calls: int = 0
    eod_calls: int = 0
    recover_eod_calls: int = 0
    frozen_decision: DecisionSnapshot | None = None

    def startup_reconcile(self) -> WorkflowOutcome:
        self.startup_calls += 1
        return WorkflowOutcome(output_sha256="1" * 64, reference_id="startup-ok")

    def recover_startup(self) -> WorkflowOutcome:
        self.recover_startup_calls += 1
        return WorkflowOutcome(output_sha256="1" * 64, reference_id="startup-ok")

    def market_status_fact(self) -> MarketStatusFact:
        return MarketStatusFact(
            session_date=self.market_status_session,
            status=self.market_status,
            observed_at=self.market_status_observed_at,
            source="broker",
            raw_payload_sha256="7" * 64,
        )

    def prepare_decision(self, strategy_session: date) -> DecisionPreparation:
        assert strategy_session == STRATEGY_SESSION
        return DecisionPreparation(
            current_data=self.current_data,
            previous_data=self.previous_data,
            broker_snapshot_sha256="2" * 64,
            account_state_sha256="3" * 64,
            prepared_at=self.current_data.captured_at,
        )

    def decide_after_close(self, preparation: DecisionPreparation) -> DecisionSnapshot:
        self.decide_calls += 1
        snapshot = self._decision(preparation)
        self.frozen_decision = snapshot
        if self.fail_decision_once:
            self.fail_decision_once = False
            raise RuntimeError("injected crash after durable STARTED receipt")
        return snapshot

    @staticmethod
    def _decision(preparation: DecisionPreparation) -> DecisionSnapshot:
        base = decision_snapshot()
        payload: object = json.loads(base.payload_json)
        assert isinstance(payload, dict)
        risk_summary = payload["risk_summary"]
        decision_digest = payload["uquant_decision_digest"]
        assert isinstance(risk_summary, dict)
        assert isinstance(decision_digest, str)
        return DecisionSnapshot.create(
            strategy_session=STRATEGY_SESSION,
            request_fingerprint=base.request_fingerprint,
            input_fingerprint=preparation.input_sha256,
            firmquant_commit=base.firmquant_commit,
            identity=StrategyIdentity.locked(),
            data_manifest_sha256=preparation.current_data.sha256,
            broker_snapshot_sha256=preparation.broker_snapshot_sha256,
            account_before_sha256=preparation.account_state_sha256,
            account_after_sha256="e" * 64,
            uquant_payload=base.uquant_payload,
            uquant_decision_digest=decision_digest,
            risk_summary=risk_summary,
            created_at=preparation.prepared_at,
        )

    def recover_decision(self, strategy_session: date, input_sha256: str) -> DecisionSnapshot:
        assert strategy_session == STRATEGY_SESSION
        assert len(input_sha256) == 64
        self.recover_decision_calls += 1
        if self.frozen_decision is None:
            raise RuntimeError("persisted decision is unavailable")
        return self.frozen_decision

    def load_frozen_decision(self, strategy_session: date) -> DecisionSnapshot:
        assert strategy_session == STRATEGY_SESSION
        if self.frozen_decision is None:
            return self._decision(self.prepare_decision(strategy_session))
        return self.frozen_decision

    def prepare_execution(self, execution_session: date, decision: DecisionSnapshot) -> ExecutionPreparation:
        assert execution_session == EXECUTION_SESSION
        assert decision.strategy_session == STRATEGY_SESSION
        return ExecutionPreparation(
            facts=self.execution_facts,
            premise_matches=self.premise_matches,
            reconciliation_healthy=self.reconciliation_healthy,
            prepared_at=self.execution_facts.broker_snapshot.captured_at,
        )

    def execute_frozen(
        self, decision: DecisionSnapshot, preparation: ExecutionPreparation
    ) -> WorkflowOutcome:
        del preparation
        self.execute_calls += 1
        if self.fail_execution_once:
            self.fail_execution_once = False
            raise RuntimeError("injected crash after execution STARTED receipt")
        return WorkflowOutcome(
            output_sha256=canonical_sha256({"decision_id": decision.decision_id}),
            reference_id="execution-ok",
        )

    def recover_execution(
        self,
        execution_session: date,
        input_sha256: str,
    ) -> WorkflowOutcome:
        assert execution_session == EXECUTION_SESSION
        del input_sha256
        self.recover_execution_calls += 1
        return WorkflowOutcome(output_sha256="4" * 64, reference_id="execution-recovered")

    def process_intraday(self, execution_session: date) -> WorkflowOutcome:
        assert execution_session == EXECUTION_SESSION
        self.intraday_calls += 1
        return WorkflowOutcome(output_sha256="5" * 64, reference_id="intraday-ok")

    def recover_intraday(self, execution_session: date, input_sha256: str) -> WorkflowOutcome:
        del execution_session, input_sha256
        return WorkflowOutcome(output_sha256="5" * 64, reference_id="intraday-ok")

    def reconcile_eod(self, execution_session: date) -> WorkflowOutcome:
        assert execution_session == EXECUTION_SESSION
        self.eod_calls += 1
        return WorkflowOutcome(output_sha256="6" * 64, reference_id="eod-ok")

    def recover_eod(self, execution_session: date, input_sha256: str) -> WorkflowOutcome:
        del execution_session, input_sha256
        self.recover_eod_calls += 1
        return WorkflowOutcome(output_sha256="6" * 64, reference_id="eod-ok")


@pytest.fixture
def writer_lease(tmp_path: Path) -> Iterator[WriterLease]:
    value = WriterLease.acquire(
        tmp_path / "workflow.sqlite3",
        owner="daily-workflow-test",
        clock=lambda: NOW,
    )
    try:
        yield value
    finally:
        value.release()


def _workflow(*, now: datetime = NOW) -> RecordingWorkflow:
    previous = _manifest(
        date(2026, 8, 21),
        equity_full="b" * 64,
        equity_prefix="f" * 64,
        captured_at=now - timedelta(days=3),
    )
    current = _manifest(
        STRATEGY_SESSION,
        equity_full="a" * 64,
        equity_prefix="b" * 64,
        captured_at=now,
    )
    return RecordingWorkflow(
        current_data=current,
        previous_data=previous,
        execution_facts=execution_snapshot(),
    )


def _coordinator(
    writer_lease: WriterLease,
    workflow: RecordingWorkflow,
    *,
    now: datetime = NOW,
) -> SessionCoordinator:
    return SessionCoordinator(
        services=workflow,
        mode=Mode.PAPER,
        calendar=_calendar(),
        clock_guard=ClockGuard(max_drift=timedelta(seconds=2)),
        clock_observer=lambda: ClockObservation(
            system_time=now,
            reference_time=now + timedelta(milliseconds=100),
            local_timezone="Asia/Shanghai",
        ),
        data_validator=StrategyDataValidator(max_manifest_age=timedelta(minutes=10)),
        receipts=WorkflowReceiptStore(writer_lease=writer_lease),
        max_quote_age=timedelta(seconds=30),
    )


def test_daily_workflow_freezes_decision_and_never_decides_next_day(
    writer_lease: WriterLease,
) -> None:
    workflow = _workflow()
    coordinator = _coordinator(writer_lease, workflow)

    assert coordinator.startup().reference_id == "startup-ok"
    decision = coordinator.post_close_decision(STRATEGY_SESSION)
    assert decision.strategy_session == STRATEGY_SESSION
    assert workflow.decide_calls == 1

    workflow.market_status = MarketSessionStatus.OPEN
    workflow.market_status_session = EXECUTION_SESSION
    result = coordinator.next_day_execute(EXECUTION_SESSION)

    assert result.reference_id == "execution-ok"
    assert workflow.decide_calls == 1
    assert workflow.execute_calls == 1
    assert coordinator.status.state is RuntimeState.READY


def test_next_day_execution_requires_completed_decision_receipt(
    writer_lease: WriterLease,
) -> None:
    workflow = _workflow()
    coordinator = _coordinator(writer_lease, workflow)
    coordinator.startup()
    workflow.market_status = MarketSessionStatus.OPEN
    workflow.market_status_session = EXECUTION_SESSION

    with pytest.raises(SessionWorkflowError, match="decision receipt"):
        coordinator.next_day_execute(EXECUTION_SESSION)

    assert workflow.decide_calls == 0
    assert workflow.execute_calls == 0


@pytest.mark.parametrize("market_status", [MarketSessionStatus.AUCTION, MarketSessionStatus.UNKNOWN])
def test_default_execution_requires_broker_reported_continuous_market(
    writer_lease: WriterLease, market_status: MarketSessionStatus
) -> None:
    workflow = _workflow()
    coordinator = _coordinator(writer_lease, workflow)
    coordinator.startup()
    coordinator.post_close_decision(STRATEGY_SESSION)
    workflow.market_status = market_status
    workflow.market_status_session = EXECUTION_SESSION

    with pytest.raises(SessionWorkflowError, match="market status"):
        coordinator.next_day_execute(EXECUTION_SESSION)

    assert coordinator.status.state is RuntimeState.HALTED
    assert workflow.execute_calls == 0


def test_holiday_is_blocked_even_when_it_is_a_weekday(writer_lease: WriterLease) -> None:
    workflow = _workflow()
    coordinator = _coordinator(writer_lease, workflow)
    coordinator.startup()

    with pytest.raises(SessionWorkflowError, match="authoritative trading session"):
        coordinator.post_close_decision(date(2026, 8, 26))

    assert workflow.decide_calls == 0
    assert coordinator.status.state is RuntimeState.HALTED


def test_history_prefix_drift_halts_before_strategy_decision(
    writer_lease: WriterLease,
) -> None:
    workflow = _workflow()
    workflow.current_data = replace(
        workflow.current_data,
        series=(
            replace(workflow.current_data.series[0], verified_prefix_sha256="0" * 64),
            workflow.current_data.series[1],
        ),
    )
    coordinator = _coordinator(writer_lease, workflow)
    coordinator.startup()

    with pytest.raises(SessionWorkflowError, match="strategy data"):
        coordinator.post_close_decision(STRATEGY_SESSION)

    assert workflow.decide_calls == 0
    assert coordinator.status.state is RuntimeState.HALTED


def test_stale_quote_halts_before_execution(writer_lease: WriterLease) -> None:
    workflow = _workflow()
    facts = workflow.execution_facts
    stale_time = NOW - timedelta(minutes=5)
    workflow.execution_facts = replace(
        facts,
        quotes=tuple(replace(quote, event_time=stale_time, received_at=stale_time) for quote in facts.quotes),
    )
    coordinator = _coordinator(writer_lease, workflow)
    coordinator.startup()
    coordinator.post_close_decision(STRATEGY_SESSION)
    workflow.market_status = MarketSessionStatus.OPEN
    workflow.market_status_session = EXECUTION_SESSION

    with pytest.raises(SessionWorkflowError, match="execution facts"):
        coordinator.next_day_execute(EXECUTION_SESSION)

    assert workflow.execute_calls == 0
    assert coordinator.status.state is RuntimeState.HALTED


def test_restart_recovers_decision_without_calling_production_engine_twice(
    writer_lease: WriterLease,
) -> None:
    crashed = _workflow()
    crashed.fail_decision_once = True
    first = _coordinator(writer_lease, crashed)
    first.startup()

    with pytest.raises(SessionWorkflowError, match="post-close decision"):
        first.post_close_decision(STRATEGY_SESSION)

    restarted = _workflow()
    restarted.frozen_decision = crashed.frozen_decision
    restarted.current_data = replace(
        restarted.current_data,
        captured_at=NOW + timedelta(seconds=5),
    )
    second = _coordinator(writer_lease, restarted, now=NOW + timedelta(seconds=5))
    second.startup()
    recovered = second.post_close_decision(STRATEGY_SESSION)

    assert recovered.decision_id == restarted.load_frozen_decision(STRATEGY_SESSION).decision_id
    assert restarted.decide_calls == 0
    assert restarted.recover_decision_calls >= 1
    assert second.status.state is RuntimeState.READY


def test_intraday_and_eod_never_create_a_strategy_decision(
    writer_lease: WriterLease,
) -> None:
    workflow = _workflow()
    coordinator = _coordinator(writer_lease, workflow)
    coordinator.startup()
    workflow.market_status = MarketSessionStatus.OPEN
    coordinator.intraday(EXECUTION_SESSION)
    workflow.market_status = MarketSessionStatus.CLOSED
    workflow.market_status_session = EXECUTION_SESSION
    coordinator.eod(EXECUTION_SESSION)

    assert workflow.decide_calls == 0
    assert workflow.intraday_calls == 1
    assert workflow.eod_calls == 1


def test_execution_restart_recovers_with_new_quotes_without_resubmission(
    writer_lease: WriterLease,
) -> None:
    crashed = _workflow()
    first = _coordinator(writer_lease, crashed)
    first.startup()
    first.post_close_decision(STRATEGY_SESSION)
    crashed.market_status = MarketSessionStatus.OPEN
    crashed.market_status_session = EXECUTION_SESSION
    crashed.fail_execution_once = True

    with pytest.raises(SessionWorkflowError, match="next-day execution"):
        first.next_day_execute(EXECUTION_SESSION)

    restarted = _workflow(now=NOW + timedelta(seconds=5))
    restarted.frozen_decision = crashed.frozen_decision
    restarted.market_status = MarketSessionStatus.OPEN
    restarted.market_status_session = EXECUTION_SESSION
    restarted.execution_facts = replace(
        restarted.execution_facts,
        quotes=tuple(
            replace(
                quote,
                sequence=quote.sequence + 1,
                event_time=NOW + timedelta(seconds=5),
                received_at=NOW + timedelta(seconds=5),
            )
            for quote in restarted.execution_facts.quotes
        ),
        broker_snapshot=replace(
            restarted.execution_facts.broker_snapshot,
            captured_at=NOW + timedelta(seconds=5),
        ),
        instruments=tuple(
            replace(item, observed_at=NOW + timedelta(seconds=5))
            for item in restarted.execution_facts.instruments
        ),
    )
    second = _coordinator(writer_lease, restarted, now=NOW + timedelta(seconds=5))

    second.startup()

    assert second.status.state is RuntimeState.READY
    assert restarted.recover_execution_calls == 1
    assert restarted.execute_calls == 0
    assert restarted.decide_calls == 0


def test_market_status_must_be_fresh_and_bound_to_requested_session(
    writer_lease: WriterLease,
) -> None:
    workflow = _workflow()
    workflow.market_status_session = date(2026, 8, 21)
    coordinator = _coordinator(writer_lease, workflow)
    coordinator.startup()

    with pytest.raises(SessionWorkflowError, match="market status fact"):
        coordinator.post_close_decision(STRATEGY_SESSION)

    assert workflow.decide_calls == 0
    assert coordinator.status.state is RuntimeState.HALTED


def test_second_writer_cannot_construct_parallel_session_owner(tmp_path: Path) -> None:
    path = tmp_path / "single-owner.sqlite3"
    first = WriterLease.acquire(path, owner="first", clock=lambda: NOW)
    try:
        with pytest.raises(WriterLeaseBusy):
            WriterLease.acquire(path, owner="second", clock=lambda: NOW)
    finally:
        first.release()


def test_runtime_halt_is_durable(writer_lease: WriterLease) -> None:
    workflow = _workflow()
    workflow.current_data = replace(
        workflow.current_data,
        series=(
            replace(workflow.current_data.series[0], verified_prefix_sha256="0" * 64),
            workflow.current_data.series[1],
        ),
    )
    first = _coordinator(writer_lease, workflow)
    first.startup()
    with pytest.raises(SessionWorkflowError):
        first.post_close_decision(STRATEGY_SESSION)

    second = _coordinator(writer_lease, _workflow())

    assert second.status.state is RuntimeState.HALTED
    assert second.status.blockers == ("STRATEGY_DATA_INVALID",)
    assert AuditLedger(writer_lease.database).verify().count >= 1


def test_repeated_eod_uses_durable_receipt_despite_new_status_observation(
    writer_lease: WriterLease,
) -> None:
    workflow = _workflow()
    coordinator = _coordinator(writer_lease, workflow)
    coordinator.startup()
    workflow.market_status = MarketSessionStatus.CLOSED
    workflow.market_status_session = EXECUTION_SESSION

    first = coordinator.eod(EXECUTION_SESSION)
    workflow.market_status_observed_at = NOW + timedelta(seconds=10)
    second = coordinator.eod(EXECUTION_SESSION)

    assert first == second
    assert workflow.eod_calls == 1
    assert workflow.recover_eod_calls == 1
    assert coordinator.status.state is RuntimeState.READY
