from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from firmquant.broker.fake import BrokerOperation, FakeBroker, ScriptedOutcome
from firmquant.broker.gateway import BrokerHealth, BrokerOrderCommand
from firmquant.broker.normalization import normalize_order
from firmquant.config import (
    BrokerAdapter,
    BrokerSettings,
    ComplianceSettings,
    DeploymentCaps,
    Mode,
    Settings,
)
from firmquant.domain.broker_facts import BrokerOrderStatus, MarketSessionStatus, Side
from firmquant.domain.orders import OrderState
from firmquant.domain.states import RuntimeState
from firmquant.domain.values import Money
from firmquant.execution.live_controller import ExecutionWindowPolicy, LiveExecutionController
from firmquant.execution.planner import ExecutionPlanner
from firmquant.execution.policy import FeeSchedule
from firmquant.persistence.database import Database
from firmquant.persistence.production_repository import MonotonicExecutionLedgerRepository
from firmquant.persistence.repositories import DecisionSnapshotRepository
from firmquant.risk.arm import ArmBinding, ArmService
from firmquant.risk.capability import (
    WriteAuthorizationContext,
    WriteCapabilityFactory,
    WriteOperation,
)
from firmquant.risk.gate import GateAction, GateDecision
from firmquant.security.secrets import SecretBytes
from tests.fixtures.session_cases import NOW, decision_snapshot, execution_snapshot


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


def live_settings() -> Settings:
    caps = DeploymentCaps(
        max_order_notional=Decimal("10000"),
        max_daily_submitted_notional=Decimal("30000"),
        max_daily_filled_notional=Decimal("30000"),
        max_symbol_notional=Decimal("20000"),
        max_total_gross_notional=Decimal("50000"),
    )
    return Settings(
        mode=Mode.CANARY,
        live_trading_enabled=True,
        broker=BrokerSettings(adapter=BrokerAdapter.XTQUANT),
        compliance=ComplianceSettings(
            program_trading_report_confirmed=True,
            broker_api_authorized=True,
        ),
        canary_caps=caps,
    )


def fake_broker(clock: MutableClock) -> FakeBroker:
    facts = execution_snapshot()
    snapshot = facts.broker_snapshot
    account = replace(
        snapshot.account,
        available_cash=Money(Decimal("10000")),
        total_assets=Money(Decimal("20000")),
    )
    broker = FakeBroker(
        account=account,
        positions=snapshot.positions,
        orders=snapshot.orders,
        fills=snapshot.fills,
        instruments=facts.instruments,
        quotes=facts.quotes,
        market_status=MarketSessionStatus.OPEN,
        clock=clock,
    )
    broker.connect()
    return broker


def capability(
    broker: FakeBroker,
    clock: MutableClock,
    *,
    deny_submit: bool = False,
    deny_cancel: bool = False,
):
    service = ArmService(
        mac_key=SecretBytes(b"test-only-arm-mac-key-material-32"),
        lease_id_factory=lambda: "arm_" + "d" * 32,
    )
    binding = ArmBinding.create(
        mode=Mode.CANARY,
        hostname="host-a",
        account_id="account-a",
        firmquant_commit="f" * 40,
        uquant_commit="1" * 40,
        config_sha256="c" * 64,
    )
    lease = service.issue(
        binding,
        now=clock(),
        confirmation_reader=lambda: service.confirmation_phrase(Mode.CANARY),
        interactive_terminal=True,
        environment={},
    )

    def source(operation: WriteOperation, subject: object | None) -> WriteAuthorizationContext:
        gate = None
        if isinstance(subject, BrokerOrderCommand):
            gate = GateDecision(
                action=GateAction.ALLOW,
                authorized_shares=subject.requested_shares,
                reason_codes=("ALL_CHECKS_PASSED",),
            )
        return WriteAuthorizationContext(
            settings=live_settings(),
            lease=lease,
            binding=binding,
            now=clock(),
            runtime_state=(
                RuntimeState.HALTED
                if deny_submit and operation is WriteOperation.SUBMIT
                else RuntimeState.READY
            ),
            broker_health=BrokerHealth(
                connected=True,
                read_healthy=True,
                write_healthy=True,
                observed_at=clock(),
                diagnostic_code="CONNECTED",
            ),
            startup_reconciliation_passed=True,
            broker_snapshot_received_at=clock(),
            max_broker_snapshot_age=timedelta(seconds=10),
            quote_received_at=clock(),
            max_quote_age=timedelta(seconds=5),
            session_valid=True,
            market_status=MarketSessionStatus.OPEN,
            fingerprints_match=True,
            kill_switch_tripped=False,
            unresolved_order_count=0,
            submitting_unresolved_count=0,
            reconciliation_mismatch=False,
            external_activity_detected=False,
            gate_decision=gate,
            cancel_risk_approved=not deny_cancel,
            symbol_in_canonical_universe=True,
            symbol_in_deployment_allowlist=True,
            command_within_uquant_intent=True,
            cash_and_positions_safe=True,
            frequency_within_limits=True,
        )

    return WriteCapabilityFactory(arm_service=service).create(
        gateway=broker,
        context_provider=source,
    )


def fee_schedule() -> FeeSchedule:
    return FeeSchedule(
        commission_rate=Decimal("0.0003"),
        minimum_commission=Decimal("5"),
        stamp_duty_rate=Decimal("0.0005"),
        transfer_fee_rate=Decimal("0.00001"),
        fee_quantum=Decimal("0.0001"),
    )


def controller(
    database: Database,
    broker: FakeBroker,
    clock: MutableClock,
    *,
    deny_submit: bool = False,
    deny_cancel: bool = False,
) -> LiveExecutionController:
    return LiveExecutionController(
        capability=capability(
            broker,
            clock,
            deny_submit=deny_submit,
            deny_cancel=deny_cancel,
        ),
        ledger=MonotonicExecutionLedgerRepository(database),
        fee_schedule=fee_schedule(),
        clock=clock,
        window_policy=ExecutionWindowPolicy(
            sell_window=timedelta(seconds=2),
            buy_window=timedelta(seconds=2),
            minimum_order_lifetime=timedelta(seconds=1),
            poll_interval=timedelta(seconds=1),
        ),
        sleep=clock.sleep,
    )


def test_prewrite_capability_denial_is_definitely_not_accepted_not_unknown(tmp_path: Path) -> None:
    clock = MutableClock(NOW)
    broker = fake_broker(clock)
    database = Database.open(tmp_path / "firmquant.sqlite3")
    decision = decision_snapshot(include_sell=False, include_buy=True)
    plan = ExecutionPlanner().plan(decision, execution_snapshot())
    try:
        with database.transaction():
            DecisionSnapshotRepository(database).append(decision)
        result = controller(database, broker, clock, deny_submit=True).execute(plan)
        outcome = result.outcomes[0]
        assert outcome.final_state is OrderState.ARMED
        assert outcome.reason_code == "SUBMIT_NOT_ACCEPTED"
        assert result.unresolved_unknown is False
        assert broker.submitted_commands == ()
    finally:
        database.close()


def test_open_order_waits_finite_window_then_uses_capability_bound_cancel(tmp_path: Path) -> None:
    clock = MutableClock(NOW)
    broker = fake_broker(clock)
    database = Database.open(tmp_path / "firmquant.sqlite3")
    decision = decision_snapshot(include_sell=False, include_buy=True)
    plan = ExecutionPlanner().plan(decision, execution_snapshot())
    planned = plan.orders[0]
    accepted = normalize_order(
        {
            "broker_order_id": "live-order-1",
            "client_order_id": planned.uquant_order_id,
            "symbol": planned.symbol.canonical,
            "side": planned.side.value,
            "price_type": "LIMIT",
            "status": BrokerOrderStatus.ACKNOWLEDGED.value,
            "requested_shares": planned.uquant_authorized_shares.value,
            "filled_shares": 0,
            "limit_price": planned.limit_price.canonical,
            "session_date": plan.execution_session.isoformat(),
            "event_time": NOW.isoformat(),
            "event_sequence": 10,
        },
        received_at=NOW,
    )
    cancelled = replace(
        accepted,
        status=BrokerOrderStatus.CANCELLED,
        event_sequence=11,
    )
    broker.script(
        (
            ScriptedOutcome(BrokerOperation.SUBMIT, response=accepted),
            ScriptedOutcome(BrokerOperation.CANCEL, response=cancelled),
        )
    )
    try:
        with database.transaction():
            DecisionSnapshotRepository(database).append(decision)
        result = controller(database, broker, clock).execute(plan)
        outcome = result.outcomes[0]
        assert outcome.final_state is OrderState.CANCELLED
        assert result.submit_calls == 1
        assert result.cancel_calls == 1
        assert len(broker.submitted_commands) == 1
        assert broker.cancelled_order_ids == ("live-order-1",)
        assert clock.value >= NOW + timedelta(seconds=2)
    finally:
        database.close()


def test_window_policy_is_strict_and_side_specific() -> None:
    policy = ExecutionWindowPolicy(
        sell_window=timedelta(seconds=3),
        buy_window=timedelta(seconds=5),
        minimum_order_lifetime=timedelta(seconds=1),
        poll_interval=timedelta(seconds=1),
    )
    assert policy.window_for(Side.SELL) == timedelta(seconds=3)
    assert policy.window_for(Side.BUY) == timedelta(seconds=5)
    with pytest.raises(TypeError):
        policy.window_for("BUY")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ExecutionWindowPolicy(
            sell_window=timedelta(seconds=1),
            buy_window=timedelta(seconds=1),
            minimum_order_lifetime=timedelta(seconds=2),
            poll_interval=timedelta(seconds=1),
        )
