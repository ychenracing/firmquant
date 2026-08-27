"""Opaque broker-write capability that revalidates every real write operation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final, Never, SupportsIndex

from firmquant.broker.gateway import (
    BrokerEventSink,
    BrokerGateway,
    BrokerHealth,
    BrokerOrderCommand,
    _broker_write_authorization_scope,
)
from firmquant.config import Mode, Settings
from firmquant.domain.broker_facts import (
    BrokerAccountFact,
    BrokerFillFact,
    BrokerOrderFact,
    BrokerPositionFact,
    InstrumentFact,
    MarketSessionStatus,
    QuoteFact,
)
from firmquant.domain.errors import DomainTypeError, DomainValidationError
from firmquant.domain.states import RuntimeState
from firmquant.domain.values import Symbol
from firmquant.scheduling.clock import ClockReceipt

from .arm import ArmBinding, ArmLease, ArmLeaseDenied, ArmService
from .gate import GateAction, GateDecision


class WriteOperation(StrEnum):
    CONSTRUCT = "CONSTRUCT"
    SUBMIT = "SUBMIT"
    CANCEL = "CANCEL"


class WriteCapabilityDenied(RuntimeError):
    """Fail-closed denial carrying stable, non-sensitive reason codes."""

    def __init__(self, reason_codes: tuple[str, ...]) -> None:
        if not isinstance(reason_codes, tuple) or not reason_codes:
            raise DomainValidationError("write denial requires reason codes")
        self.reason_codes = tuple(dict.fromkeys(reason_codes))
        super().__init__(",".join(self.reason_codes))


@dataclass(frozen=True, slots=True)
class WriteAuthorizationContext:
    """One fresh observation of every fact required before a broker write."""

    settings: Settings
    lease: ArmLease | None
    binding: ArmBinding
    now: datetime
    runtime_state: RuntimeState
    broker_health: BrokerHealth
    startup_reconciliation_passed: bool
    broker_snapshot_received_at: datetime | None
    max_broker_snapshot_age: timedelta
    quote_received_at: datetime | None
    max_quote_age: timedelta
    session_valid: bool
    market_status: MarketSessionStatus
    fingerprints_match: bool
    kill_switch_tripped: bool
    unresolved_order_count: int
    submitting_unresolved_count: int
    reconciliation_mismatch: bool
    external_activity_detected: bool
    gate_decision: GateDecision | None
    cancel_risk_approved: bool
    symbol_in_canonical_universe: bool
    symbol_in_deployment_allowlist: bool
    command_within_uquant_intent: bool
    cash_and_positions_safe: bool
    frequency_within_limits: bool
    clock_receipt: ClockReceipt | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.settings, Settings):
            raise DomainTypeError("write context settings must be Settings")
        if self.lease is not None and not isinstance(self.lease, ArmLease):
            raise DomainTypeError("write context lease must be ArmLease or null")
        if not isinstance(self.binding, ArmBinding):
            raise DomainTypeError("write context binding must be ArmBinding")
        _aware(self.now, label="write context now")
        if not isinstance(self.runtime_state, RuntimeState):
            raise DomainTypeError("write context runtime state must be RuntimeState")
        if not isinstance(self.broker_health, BrokerHealth):
            raise DomainTypeError("write context broker health must be BrokerHealth")
        for timestamp_label, timestamp_value in (
            ("broker snapshot time", self.broker_snapshot_received_at),
            ("quote time", self.quote_received_at),
        ):
            if timestamp_value is not None:
                _aware(timestamp_value, label=timestamp_label)
        for duration_label, duration_value in (
            ("broker snapshot max age", self.max_broker_snapshot_age),
            ("quote max age", self.max_quote_age),
        ):
            if not isinstance(duration_value, timedelta) or duration_value <= timedelta(0):
                raise DomainValidationError(f"{duration_label} must be positive timedelta")
        if not isinstance(self.market_status, MarketSessionStatus):
            raise DomainTypeError("write context market status must be MarketSessionStatus")
        for count_label, count_value in (
            ("unresolved order count", self.unresolved_order_count),
            ("submitting unresolved count", self.submitting_unresolved_count),
        ):
            if isinstance(count_value, bool) or not isinstance(count_value, int) or count_value < 0:
                raise DomainValidationError(f"{count_label} must be a nonnegative integer")
        if self.gate_decision is not None and not isinstance(self.gate_decision, GateDecision):
            raise DomainTypeError("write context gate decision must be GateDecision or null")
        if self.clock_receipt is not None and not isinstance(self.clock_receipt, ClockReceipt):
            raise DomainTypeError("write context clock receipt must be ClockReceipt or null")
        boolean_values = (
            self.startup_reconciliation_passed,
            self.session_valid,
            self.fingerprints_match,
            self.kill_switch_tripped,
            self.reconciliation_mismatch,
            self.external_activity_detected,
            self.cancel_risk_approved,
            self.symbol_in_canonical_universe,
            self.symbol_in_deployment_allowlist,
            self.command_within_uquant_intent,
            self.cash_and_positions_safe,
            self.frequency_within_limits,
        )
        if not all(isinstance(value, bool) for value in boolean_values):
            raise DomainTypeError("write context gate flags must all be bool")


def _aware(value: datetime, *, label: str) -> None:
    if not isinstance(value, datetime):
        raise DomainTypeError(f"{label} must be datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise DomainValidationError(f"{label} must be timezone-aware")


ContextProvider = Callable[[WriteOperation, object | None], WriteAuthorizationContext]
_FACTORY_TOKEN: Final = object()


class _Authorizer:
    __slots__ = ("_arm_service",)

    def __init__(self, arm_service: ArmService) -> None:
        self._arm_service = arm_service

    def authorize(
        self,
        context: WriteAuthorizationContext,
        *,
        operation: WriteOperation,
        subject: object | None,
    ) -> None:
        if not isinstance(context, WriteAuthorizationContext):
            raise WriteCapabilityDenied(("AUTHORIZATION_CONTEXT_INVALID",))
        reasons: list[str] = []
        settings = context.settings
        if settings.mode not in {Mode.CANARY, Mode.LIVE}:
            reasons.append("MODE_NOT_LIVE_WRITABLE")
        if not settings.live_trading_enabled:
            reasons.append("LIVE_TRADING_DISABLED")
        if settings.mode is not context.binding.mode:
            reasons.append("MODE_BINDING_MISMATCH")
        if not settings.compliance.program_trading_report_confirmed:
            reasons.append("PROGRAM_TRADING_REPORT_UNCONFIRMED")
        if not settings.compliance.broker_api_authorized:
            reasons.append("BROKER_API_UNAUTHORIZED")
        if context.lease is None:
            reasons.append("ARM_LEASE_MISSING")
        else:
            try:
                self._arm_service.verify(
                    context.lease,
                    binding=context.binding,
                    now=context.now,
                )
            except ArmLeaseDenied:
                reasons.append("ARM_LEASE_INVALID")
        if context.runtime_state not in {RuntimeState.READY, RuntimeState.EXECUTING}:
            reasons.append("RUNTIME_NOT_WRITABLE")
        health = context.broker_health
        if not health.connected:
            reasons.append("BROKER_DISCONNECTED")
        if not health.read_healthy:
            reasons.append("BROKER_READ_UNHEALTHY")
        if not health.write_healthy:
            reasons.append("BROKER_WRITE_UNHEALTHY")
        if health.observed_at > context.now:
            reasons.append("BROKER_HEALTH_TIME_IN_FUTURE")
        elif context.now - health.observed_at > context.max_broker_snapshot_age:
            reasons.append("BROKER_HEALTH_STALE")
        if not context.startup_reconciliation_passed:
            reasons.append("STARTUP_RECONCILIATION_REQUIRED")
        if operation in {WriteOperation.SUBMIT, WriteOperation.CANCEL}:
            receipt = context.clock_receipt
            if receipt is None:
                reasons.append("CLOCK_DRIFT_UNVERIFIED")
            else:
                maximum_drift_ms = context.settings.execution.max_clock_drift_seconds * 1000
                if receipt.drift_milliseconds > maximum_drift_ms:
                    reasons.append("CLOCK_DRIFT_LIMIT")
                if receipt.system_time > context.now:
                    reasons.append("CLOCK_RECEIPT_TIME_IN_FUTURE")
                elif context.now - receipt.system_time > context.max_quote_age:
                    reasons.append("CLOCK_RECEIPT_STALE")
        self._freshness_reasons(
            reasons,
            observed=context.broker_snapshot_received_at,
            now=context.now,
            maximum=context.max_broker_snapshot_age,
            missing="BROKER_SNAPSHOT_MISSING",
            stale="BROKER_SNAPSHOT_STALE",
            future="BROKER_SNAPSHOT_TIME_IN_FUTURE",
        )
        self._freshness_reasons(
            reasons,
            observed=context.quote_received_at,
            now=context.now,
            maximum=context.max_quote_age,
            missing="QUOTE_MISSING",
            stale="QUOTE_STALE",
            future="QUOTE_TIME_IN_FUTURE",
        )
        if not context.session_valid:
            reasons.append("SESSION_INVALID")
        if context.market_status not in {
            MarketSessionStatus.OPEN,
            MarketSessionStatus.AUCTION,
        }:
            reasons.append("MARKET_NOT_TRADABLE")
        if not context.fingerprints_match:
            reasons.append("IDENTITY_MISMATCH")
        if context.kill_switch_tripped:
            reasons.append("KILL_SWITCH_TRIPPED")
        if context.unresolved_order_count > 0:
            reasons.append("UNRESOLVED_ORDER_STATE")
        if context.submitting_unresolved_count > 0:
            reasons.append("SUBMITTING_UNRESOLVED")
        if context.reconciliation_mismatch:
            reasons.append("RECONCILIATION_MISMATCH")
        if context.external_activity_detected:
            reasons.append("EXTERNAL_ACTIVITY")
        if not context.symbol_in_canonical_universe:
            reasons.append("SYMBOL_NOT_CANONICAL")
        if not context.symbol_in_deployment_allowlist:
            reasons.append("SYMBOL_NOT_DEPLOYMENT_ALLOWED")
        if not context.command_within_uquant_intent:
            reasons.append("COMMAND_EXCEEDS_UQUANT_INTENT")
        if not context.cash_and_positions_safe:
            reasons.append("CASH_OR_POSITION_UNSAFE")
        if not context.frequency_within_limits:
            reasons.append("FREQUENCY_LIMIT")
        if operation is WriteOperation.SUBMIT:
            if not isinstance(subject, BrokerOrderCommand):
                reasons.append("SUBMIT_COMMAND_INVALID")
            self._submit_gate_reasons(reasons, context.gate_decision, subject)
        elif operation is WriteOperation.CANCEL:
            if not isinstance(subject, str) or not subject:
                reasons.append("CANCEL_ID_INVALID")
            if not context.cancel_risk_approved:
                reasons.append("CANCEL_RISK_GATE_DENIED")
        if reasons:
            raise WriteCapabilityDenied(tuple(reasons))

    @staticmethod
    def _freshness_reasons(
        reasons: list[str],
        *,
        observed: datetime | None,
        now: datetime,
        maximum: timedelta,
        missing: str,
        stale: str,
        future: str,
    ) -> None:
        if observed is None:
            reasons.append(missing)
        elif observed > now:
            reasons.append(future)
        elif now - observed > maximum:
            reasons.append(stale)

    @staticmethod
    def _submit_gate_reasons(
        reasons: list[str],
        decision: GateDecision | None,
        subject: object | None,
    ) -> None:
        if decision is None:
            reasons.append("EXECUTION_RISK_GATE_MISSING")
            return
        if decision.action not in {GateAction.ALLOW, GateAction.SHRINK}:
            reasons.append("EXECUTION_RISK_GATE_DENIED")
            return
        if not isinstance(subject, BrokerOrderCommand):
            return
        if decision.authorized_shares != subject.requested_shares:
            reasons.append("RISK_GATE_QUANTITY_MISMATCH")


class BrokerWriteCapability:
    """BrokerGateway view whose two write methods have dynamic authorization."""

    __slots__ = ("_authorizer", "_context_provider", "_gateway")

    def __init__(
        self,
        *,
        token: object,
        gateway: BrokerGateway,
        context_provider: ContextProvider,
        authorizer: _Authorizer,
    ) -> None:
        if token is not _FACTORY_TOKEN:
            raise WriteCapabilityDenied(("CAPABILITY_FACTORY_REQUIRED",))
        self._gateway = gateway
        self._context_provider = context_provider
        self._authorizer = authorizer

    def _authorize(self, operation: WriteOperation, subject: object | None) -> None:
        try:
            context = self._context_provider(operation, subject)
        except Exception as error:
            raise WriteCapabilityDenied(("AUTHORIZATION_CONTEXT_UNAVAILABLE",)) from error
        self._authorizer.authorize(context, operation=operation, subject=subject)

    def connect(self) -> None:
        self._gateway.connect()

    def disconnect(self) -> None:
        self._gateway.disconnect()

    def health(self) -> BrokerHealth:
        return self._gateway.health()

    def query_account(self) -> BrokerAccountFact:
        return self._gateway.query_account()

    def query_positions(self) -> tuple[BrokerPositionFact, ...]:
        return self._gateway.query_positions()

    def query_orders(self) -> tuple[BrokerOrderFact, ...]:
        return self._gateway.query_orders()

    def query_fills(self) -> tuple[BrokerFillFact, ...]:
        return self._gateway.query_fills()

    def query_instrument(self, symbol: Symbol) -> InstrumentFact:
        return self._gateway.query_instrument(symbol)

    def query_quote(self, symbol: Symbol) -> QuoteFact:
        return self._gateway.query_quote(symbol)

    def query_market_status(self) -> MarketSessionStatus:
        return self._gateway.query_market_status()

    def submit_order(self, command: BrokerOrderCommand) -> BrokerOrderFact:
        self._authorize(WriteOperation.SUBMIT, command)
        with _broker_write_authorization_scope():
            return self._gateway.submit_order(command)

    def cancel_order(self, broker_order_id: str) -> BrokerOrderFact:
        self._authorize(WriteOperation.CANCEL, broker_order_id)
        with _broker_write_authorization_scope():
            return self._gateway.cancel_order(broker_order_id)

    def subscribe(self, callback_sink: BrokerEventSink) -> None:
        self._gateway.subscribe(callback_sink)

    def __repr__(self) -> str:
        return "<BrokerWriteCapability opaque>"

    def __reduce_ex__(self, protocol: SupportsIndex, /) -> Never:
        del protocol
        raise TypeError("BrokerWriteCapability is not serializable")

    def __getstate__(self) -> Never:
        raise TypeError("BrokerWriteCapability is not serializable")


class WriteCapabilityFactory:
    """Only supported constructor for a dynamically checked write capability."""

    __slots__ = ("_authorizer",)

    def __init__(self, *, arm_service: ArmService) -> None:
        if not isinstance(arm_service, ArmService):
            raise DomainTypeError("write capability factory requires ArmService")
        self._authorizer = _Authorizer(arm_service)

    def create(
        self,
        *,
        gateway: BrokerGateway,
        context_provider: ContextProvider,
    ) -> BrokerWriteCapability:
        if not isinstance(gateway, BrokerGateway):
            raise DomainTypeError("write capability gateway must satisfy BrokerGateway")
        if not callable(context_provider):
            raise DomainTypeError("write capability context provider must be callable")
        try:
            context = context_provider(WriteOperation.CONSTRUCT, None)
        except Exception as error:
            raise WriteCapabilityDenied(("AUTHORIZATION_CONTEXT_UNAVAILABLE",)) from error
        self._authorizer.authorize(
            context,
            operation=WriteOperation.CONSTRUCT,
            subject=None,
        )
        return BrokerWriteCapability(
            token=_FACTORY_TOKEN,
            gateway=gateway,
            context_provider=context_provider,
            authorizer=self._authorizer,
        )


__all__ = (
    "BrokerWriteCapability",
    "ContextProvider",
    "WriteAuthorizationContext",
    "WriteCapabilityDenied",
    "WriteCapabilityFactory",
    "WriteOperation",
)
