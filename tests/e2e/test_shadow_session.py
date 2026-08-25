from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

import pytest

from firmquant.application.runtime import (
    ModeCompositionError,
    ModeWriteBlocked,
    ReadOnlyBrokerSession,
    Runtime,
    RuntimeSessionError,
    ShadowReconciliation,
    ShadowReportReceipt,
    ShadowSessionDraft,
    StartupReconciliationFailed,
)
from firmquant.broker.fake import FakeBroker
from firmquant.config import Mode
from firmquant.domain.broker_facts import BrokerSnapshot
from firmquant.domain.states import RuntimeState
from firmquant.domain.values import Money, Symbol
from firmquant.execution.planner import (
    ExecutionBrokerSnapshot,
    ExecutionPlan,
    ExecutionPlanner,
)
from firmquant.strategy.snapshots import DecisionSnapshot
from tests.fixtures.session_cases import (
    BUY_SYMBOL,
    NOW,
    SELL_SYMBOL,
    decision_snapshot,
    execution_snapshot,
)


def real_like_read_broker() -> FakeBroker:
    facts = execution_snapshot()
    broker_snapshot = facts.broker_snapshot
    return FakeBroker(
        account=broker_snapshot.account,
        positions=broker_snapshot.positions,
        orders=broker_snapshot.orders,
        fills=broker_snapshot.fills,
        instruments=facts.instruments,
        quotes=facts.quotes,
        market_status=facts.market_status,
        clock=lambda: NOW,
    )


@dataclass(slots=True)
class RecordingShadowWorkflow:
    reconciliation_passed: bool = True
    reconcile_calls: int = 0
    decide_calls: int = 0
    plan_calls: int = 0
    report_calls: int = 0
    read_session_had_write_port: bool | None = None

    def reconcile_startup(self, snapshot: BrokerSnapshot) -> ShadowReconciliation:
        self.reconcile_calls += 1
        return ShadowReconciliation(
            receipt_id="shadow-reconciliation-1",
            passed=self.reconciliation_passed,
            blockers=() if self.reconciliation_passed else ("EXTERNAL_ACTIVE_ORDER",),
            broker_snapshot_sha256=snapshot.raw_payload_sha256,
        )

    def decide_after_close(self, snapshot: BrokerSnapshot) -> DecisionSnapshot:
        del snapshot
        self.decide_calls += 1
        return decision_snapshot(include_sell=True, include_buy=True)

    def plan_next_session(
        self,
        decision: DecisionSnapshot,
        session: ReadOnlyBrokerSession,
        snapshot: BrokerSnapshot,
    ) -> ExecutionPlan:
        self.plan_calls += 1
        self.read_session_had_write_port = hasattr(session, "submit_order") or hasattr(
            session, "cancel_order"
        )
        symbols: tuple[Symbol, ...] = (SELL_SYMBOL, BUY_SYMBOL)
        execution_facts = ExecutionBrokerSnapshot(
            broker_snapshot=snapshot,
            instruments=tuple(session.query_instrument(symbol) for symbol in symbols),
            quotes=tuple(session.query_quote(symbol) for symbol in symbols),
            market_status=session.query_market_status(),
        )
        return ExecutionPlanner().plan(decision, execution_facts)

    def write_shadow_report(self, draft: ShadowSessionDraft) -> ShadowReportReceipt:
        self.report_calls += 1
        return ShadowReportReceipt(
            report_id="shadow-report-1",
            payload_sha256=draft.payload_sha256,
        )


def test_shadow_has_no_write_port_and_creates_hypothetical_plan() -> None:
    gateway = real_like_read_broker()
    workflow = RecordingShadowWorkflow()
    runtime = Runtime(gateway=gateway, workflow=workflow, clock=lambda: NOW)

    startup = runtime.start(Mode.SHADOW)
    result = runtime.run_one_session()

    assert startup.reconciliation.passed is True
    assert result.plan_created is True
    assert result.report_created is True
    assert result.planned_order_count == 2
    assert result.mode is Mode.SHADOW
    assert result.runtime_state is RuntimeState.READY
    assert workflow.read_session_had_write_port is False
    assert workflow.reconcile_calls == 1
    assert workflow.decide_calls == 1
    assert workflow.plan_calls == 1
    assert workflow.report_calls == 1
    assert gateway.submitted_commands == ()
    assert gateway.cancelled_order_ids == ()

    runtime.stop()
    assert runtime.status.state is RuntimeState.DISARMED


def test_shadow_reconciliation_failure_halts_before_decision() -> None:
    gateway = real_like_read_broker()
    workflow = RecordingShadowWorkflow(reconciliation_passed=False)
    runtime = Runtime(gateway=gateway, workflow=workflow, clock=lambda: NOW)

    with pytest.raises(StartupReconciliationFailed, match="EXTERNAL_ACTIVE_ORDER"):
        runtime.start(Mode.SHADOW)

    assert runtime.status.state is RuntimeState.HALTED
    assert runtime.status.blockers == ("EXTERNAL_ACTIVE_ORDER",)
    assert workflow.decide_calls == 0
    assert gateway.submitted_commands == ()
    assert gateway.cancelled_order_ids == ()


def test_shadow_cancel_system_orders_is_an_explicit_mode_blocker() -> None:
    gateway = real_like_read_broker()
    runtime = Runtime(
        gateway=gateway,
        workflow=RecordingShadowWorkflow(),
        clock=lambda: NOW,
    )
    runtime.start(Mode.SHADOW)

    with pytest.raises(ModeWriteBlocked, match="SHADOW"):
        runtime.cancel_system_orders()

    assert gateway.submitted_commands == ()
    assert gateway.cancelled_order_ids == ()


def test_live_mode_cannot_reuse_read_only_shadow_composition() -> None:
    gateway = real_like_read_broker()
    runtime = Runtime(
        gateway=gateway,
        workflow=RecordingShadowWorkflow(),
        clock=lambda: NOW,
    )

    with pytest.raises(ModeCompositionError, match="SHADOW"):
        runtime.start(Mode.LIVE)

    assert gateway.health().connected is False
    assert gateway.submitted_commands == ()
    assert gateway.cancelled_order_ids == ()


def test_changing_broker_facts_halt_without_asserting_complete_snapshot() -> None:
    class ChangingBroker(FakeBroker):
        account_reads = 0

        def query_account(self):  # type: ignore[no-untyped-def]
            account = super().query_account()
            self.account_reads += 1
            if self.account_reads == 1:
                return account
            return replace(
                account,
                available_cash=Money(account.available_cash.value - Decimal("1.00")),
            )

    stable = execution_snapshot()
    original = stable.broker_snapshot
    gateway = ChangingBroker(
        account=original.account,
        positions=original.positions,
        orders=original.orders,
        fills=original.fills,
        instruments=stable.instruments,
        quotes=stable.quotes,
        market_status=stable.market_status,
        clock=lambda: NOW,
    )
    runtime = Runtime(
        gateway=gateway,
        workflow=RecordingShadowWorkflow(),
        clock=lambda: NOW,
    )

    with pytest.raises(RuntimeSessionError, match="startup failed"):
        runtime.start(Mode.SHADOW)

    assert runtime.status.state is RuntimeState.HALTED
    assert runtime.status.blockers == ("STARTUP_RECONCILIATION_EXCEPTION",)
    assert gateway.submitted_commands == ()
    assert gateway.cancelled_order_ids == ()
