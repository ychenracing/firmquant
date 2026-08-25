from __future__ import annotations

import json
import pickle
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from firmquant.broker.fake import BrokerOperation, FakeBroker, ScriptedOutcome
from firmquant.broker.gateway import BrokerHealth
from firmquant.broker.normalization import normalize_order
from firmquant.config import (
    BrokerAdapter,
    BrokerSettings,
    ComplianceSettings,
    DeploymentCaps,
    Mode,
    Settings,
)
from firmquant.domain.broker_facts import MarketSessionStatus
from firmquant.domain.states import RuntimeState
from firmquant.domain.values import Shares
from firmquant.risk.arm import ArmBinding, ArmService
from firmquant.risk.capability import (
    WriteAuthorizationContext,
    WriteCapabilityDenied,
    WriteCapabilityFactory,
    WriteOperation,
)
from firmquant.risk.gate import GateAction, GateDecision
from firmquant.risk.kill_switch import KillSwitch
from firmquant.security.secrets import SecretBytes
from tests.fixtures.broker_contract import (
    gateway_facts,
    order_command,
    order_payload,
)

NOW = datetime(2026, 8, 25, 1, 31, tzinfo=UTC)


def live_settings(mode: Mode = Mode.CANARY) -> Settings:
    caps = DeploymentCaps(
        max_order_notional=Decimal("10000"),
        max_daily_submitted_notional=Decimal("30000"),
        max_daily_filled_notional=Decimal("30000"),
        max_symbol_notional=Decimal("20000"),
        max_total_gross_notional=Decimal("50000"),
    )
    return Settings(
        mode=mode,
        live_trading_enabled=True,
        broker=BrokerSettings(adapter=BrokerAdapter.XTQUANT),
        compliance=ComplianceSettings(
            program_trading_report_confirmed=True,
            broker_api_authorized=True,
        ),
        canary_caps=caps if mode is Mode.CANARY else None,
    )


def arm_service() -> ArmService:
    return ArmService(
        mac_key=SecretBytes(b"test-only-arm-mac-key-material-32"),
        lease_id_factory=lambda: "arm_" + "b" * 32,
    )


def arm_binding(*, config_sha256: str = "c" * 64) -> ArmBinding:
    return ArmBinding.create(
        mode=Mode.CANARY,
        hostname="execution-host-a",
        account_id="account-001",
        firmquant_commit="f" * 40,
        uquant_commit="1" * 40,
        config_sha256=config_sha256,
    )


def context(
    *,
    service: ArmService,
    binding: ArmBinding | None = None,
) -> WriteAuthorizationContext:
    current_binding = binding or arm_binding()
    lease = service.issue(
        current_binding,
        now=NOW,
        interactive_terminal=True,
        environment={},
        confirmation_reader=lambda: service.confirmation_phrase(current_binding.mode),
    )
    return WriteAuthorizationContext(
        settings=live_settings(),
        lease=lease,
        binding=current_binding,
        now=NOW,
        runtime_state=RuntimeState.READY,
        broker_health=BrokerHealth(
            connected=True,
            read_healthy=True,
            write_healthy=True,
            observed_at=NOW,
            diagnostic_code="CONNECTED",
        ),
        startup_reconciliation_passed=True,
        broker_snapshot_received_at=NOW,
        max_broker_snapshot_age=timedelta(seconds=10),
        quote_received_at=NOW,
        max_quote_age=timedelta(seconds=5),
        session_valid=True,
        market_status=MarketSessionStatus.OPEN,
        fingerprints_match=True,
        kill_switch_tripped=False,
        unresolved_order_count=0,
        submitting_unresolved_count=0,
        reconciliation_mismatch=False,
        external_activity_detected=False,
        gate_decision=GateDecision(
            action=GateAction.ALLOW,
            authorized_shares=Shares(100),
            reason_codes=("ALL_CHECKS_PASSED",),
        ),
        cancel_risk_approved=True,
        symbol_in_canonical_universe=True,
        symbol_in_deployment_allowlist=True,
        command_within_uquant_intent=True,
        cash_and_positions_safe=True,
        frequency_within_limits=True,
    )


def fake_broker() -> FakeBroker:
    facts = gateway_facts()
    broker = FakeBroker(
        account=facts.account,
        positions=(),
        orders=(),
        fills=(),
        instruments=(facts.instrument,),
        quotes=(facts.quote,),
        market_status=MarketSessionStatus.OPEN,
        clock=lambda: NOW,
    )
    broker.connect()
    return broker


def capability_harness():
    service = arm_service()
    state = {"context": context(service=service)}
    broker = fake_broker()

    def source(operation: WriteOperation, subject: object | None) -> WriteAuthorizationContext:
        assert isinstance(operation, WriteOperation)
        del subject
        return state["context"]

    capability = WriteCapabilityFactory(arm_service=service).create(
        gateway=broker,
        context_provider=source,
    )
    return capability, broker, state


def test_healthy_capability_submits_and_cancels_through_same_gate() -> None:
    capability, broker, _ = capability_harness()
    command = order_command()
    accepted = normalize_order(order_payload(), received_at=NOW)
    cancelled = normalize_order(
        order_payload(status="CANCELLED", sequence=21), received_at=NOW
    )
    broker.script(
        (
            ScriptedOutcome(BrokerOperation.SUBMIT, response=accepted),
            ScriptedOutcome(BrokerOperation.CANCEL, response=cancelled),
        )
    )

    assert capability.submit_order(command) == accepted
    assert capability.cancel_order(accepted.broker_order_id) == cancelled
    assert broker.submitted_commands == (command,)
    assert broker.cancelled_order_ids == (accepted.broker_order_id,)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"runtime_state": RuntimeState.HALTED}, "RUNTIME_NOT_WRITABLE"),
        ({"startup_reconciliation_passed": False}, "STARTUP_RECONCILIATION_REQUIRED"),
        (
            {"broker_snapshot_received_at": NOW - timedelta(seconds=11)},
            "BROKER_SNAPSHOT_STALE",
        ),
        ({"quote_received_at": NOW - timedelta(seconds=6)}, "QUOTE_STALE"),
        ({"session_valid": False}, "SESSION_INVALID"),
        ({"market_status": MarketSessionStatus.CLOSED}, "MARKET_NOT_TRADABLE"),
        ({"fingerprints_match": False}, "IDENTITY_MISMATCH"),
        ({"kill_switch_tripped": True}, "KILL_SWITCH_TRIPPED"),
        ({"unresolved_order_count": 1}, "UNRESOLVED_ORDER_STATE"),
        ({"submitting_unresolved_count": 1}, "SUBMITTING_UNRESOLVED"),
        ({"reconciliation_mismatch": True}, "RECONCILIATION_MISMATCH"),
        ({"external_activity_detected": True}, "EXTERNAL_ACTIVITY"),
        ({"symbol_in_canonical_universe": False}, "SYMBOL_NOT_CANONICAL"),
        ({"symbol_in_deployment_allowlist": False}, "SYMBOL_NOT_DEPLOYMENT_ALLOWED"),
        ({"command_within_uquant_intent": False}, "COMMAND_EXCEEDS_UQUANT_INTENT"),
        ({"cash_and_positions_safe": False}, "CASH_OR_POSITION_UNSAFE"),
        ({"frequency_within_limits": False}, "FREQUENCY_LIMIT"),
    ],
)
def test_each_dynamic_gate_denies_before_submit_side_effect(
    changes: dict[str, object], reason: str
) -> None:
    capability, broker, state = capability_harness()
    state["context"] = replace(state["context"], **changes)

    with pytest.raises(WriteCapabilityDenied, match=reason):
        capability.submit_order(order_command())

    assert broker.submitted_commands == ()


def test_broker_health_and_compliance_are_rechecked() -> None:
    capability, broker, state = capability_harness()
    unhealthy = replace(
        state["context"].broker_health,
        write_healthy=False,
    )
    settings = state["context"].settings.model_copy(
        update={
            "compliance": ComplianceSettings(
                program_trading_report_confirmed=False,
                broker_api_authorized=True,
            )
        }
    )

    state["context"] = replace(
        state["context"], broker_health=unhealthy, settings=settings
    )
    with pytest.raises(WriteCapabilityDenied) as captured:
        capability.submit_order(order_command())

    assert "BROKER_WRITE_UNHEALTHY" in captured.value.reason_codes
    assert "PROGRAM_TRADING_REPORT_UNCONFIRMED" in captured.value.reason_codes
    assert broker.submitted_commands == ()


@pytest.mark.parametrize("operation", [WriteOperation.SUBMIT, WriteOperation.CANCEL])
@pytest.mark.parametrize("revocation", ["expired", "config", "disarm", "halt", "kill"])
def test_expiry_drift_disarm_halt_and_kill_revoke_both_write_paths(
    operation: WriteOperation, revocation: str
) -> None:
    capability, broker, state = capability_harness()
    current = state["context"]
    if revocation == "expired":
        current = replace(current, now=current.lease.expires_at)
    elif revocation == "config":
        current = replace(current, binding=arm_binding(config_sha256="d" * 64))
    elif revocation == "disarm":
        current = replace(current, lease=None)
    elif revocation == "halt":
        current = replace(current, runtime_state=RuntimeState.HALTED)
    else:
        current = replace(current, kill_switch_tripped=True)
    state["context"] = current

    with pytest.raises(WriteCapabilityDenied):
        if operation is WriteOperation.SUBMIT:
            capability.submit_order(order_command())
        else:
            capability.cancel_order("broker-order-1")

    assert broker.submitted_commands == ()
    assert broker.cancelled_order_ids == ()


def test_submit_requires_exact_positive_execution_gate_authorization() -> None:
    capability, broker, state = capability_harness()
    state["context"] = replace(
        state["context"],
        gate_decision=GateDecision(
            action=GateAction.SHRINK,
            authorized_shares=Shares(50),
            reason_codes=("AVAILABLE_CASH_SHRINK",),
        ),
    )

    with pytest.raises(WriteCapabilityDenied, match="RISK_GATE_QUANTITY_MISMATCH"):
        capability.submit_order(order_command(shares=100))

    assert broker.submitted_commands == ()


def test_capability_is_opaque_and_non_serializable() -> None:
    capability, _, _ = capability_harness()

    with pytest.raises(TypeError, match="not serializable"):
        pickle.dumps(capability)
    with pytest.raises(TypeError):
        json.dumps(capability)
    assert repr(capability) == "<BrokerWriteCapability opaque>"


def test_kill_switch_is_sticky_until_explicit_safe_reset() -> None:
    switch = KillSwitch()
    status = switch.trip(reason="operator emergency stop", now=NOW)

    assert status.tripped is True
    assert switch.status().reason == "operator emergency stop"
    with pytest.raises(ValueError, match="reconciliation"):
        switch.reset(
            reason="unsafe reset",
            now=NOW + timedelta(seconds=1),
            operator_confirmed=True,
            reconciliation_passed=False,
        )
    reset = switch.reset(
        reason="operator confirmed after reconciliation",
        now=NOW + timedelta(seconds=2),
        operator_confirmed=True,
        reconciliation_passed=True,
    )
    assert reset.tripped is False
