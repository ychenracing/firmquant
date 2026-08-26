"""Ordered, fail-closed pre-trade checks that can never expand uquant intent."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum

from firmquant.broker.gateway import BrokerOrderCommand
from firmquant.config import Mode
from firmquant.domain.broker_facts import (
    AccountType,
    BrokerPositionFact,
    InstrumentFact,
    MarketSessionStatus,
    QuoteFact,
    SecurityStatus,
    SecurityType,
    Side,
)
from firmquant.domain.errors import DomainTypeError, DomainValidationError
from firmquant.domain.states import RuntimeState
from firmquant.domain.values import Money, Shares, Symbol


class GateAction(StrEnum):
    ALLOW = "ALLOW"
    SHRINK = "SHRINK"
    DELAY = "DELAY"
    BLOCK = "BLOCK"
    HALT = "HALT"


@dataclass(frozen=True, slots=True)
class RiskCommand:
    command: BrokerOrderCommand
    uquant_authorized_shares: Shares
    estimated_fees: Money

    def __post_init__(self) -> None:
        if not isinstance(self.command, BrokerOrderCommand):
            raise DomainTypeError("risk command must contain BrokerOrderCommand")
        if not isinstance(self.uquant_authorized_shares, Shares):
            raise DomainTypeError("uquant authorized shares must be Shares")
        if not self.uquant_authorized_shares.is_positive:
            raise DomainValidationError("uquant authorized shares must be positive")
        if not isinstance(self.estimated_fees, Money):
            raise DomainTypeError("risk estimated fees must be Money")


def _positive_int(value: object, *, label: str, allow_zero: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DomainTypeError(f"{label} must be an integer")
    if value < 0 or (value == 0 and not allow_zero):
        raise DomainValidationError(f"{label} must be {'nonnegative' if allow_zero else 'positive'}")


def _duration(value: object, *, label: str, allow_zero: bool = False) -> None:
    if not isinstance(value, timedelta):
        raise DomainTypeError(f"{label} must be timedelta")
    if value < timedelta(0) or (value == timedelta(0) and not allow_zero):
        raise DomainValidationError(f"{label} must be {'nonnegative' if allow_zero else 'positive'}")


def _fraction(value: object, *, label: str, maximum: Decimal = Decimal(1), allow_zero: bool = True) -> None:
    if not isinstance(value, Decimal):
        raise DomainTypeError(f"{label} must be Decimal")
    if not value.is_finite() or value < 0 or value > maximum or (not allow_zero and value == 0):
        raise DomainValidationError(f"{label} is outside its safe range")


@dataclass(frozen=True, slots=True)
class RiskLimits:
    max_order_notional: Money
    max_daily_submitted_notional: Money
    max_daily_filled_notional: Money
    max_symbol_notional: Money
    max_total_gross_notional: Money
    max_open_orders: int
    max_consecutive_rejections: int
    max_disconnect_duration: timedelta
    max_order_lifetime: timedelta
    max_replacements: int
    max_submit_count_window: int
    max_cancel_count_window: int
    max_quote_age: timedelta
    max_clock_drift: timedelta
    max_price_deviation_bps: Decimal
    max_equity_change_fraction: Decimal
    max_intraday_loss_fraction: Decimal
    max_capital_drawdown_fraction: Decimal

    def __post_init__(self) -> None:
        notionals = (
            self.max_order_notional,
            self.max_daily_submitted_notional,
            self.max_daily_filled_notional,
            self.max_symbol_notional,
            self.max_total_gross_notional,
        )
        if not all(isinstance(item, Money) and item.value > 0 for item in notionals):
            raise DomainValidationError("risk notional limits must be positive Money")
        for integer_label, integer_value in (
            ("max open orders", self.max_open_orders),
            ("max consecutive rejections", self.max_consecutive_rejections),
            ("max replacements", self.max_replacements),
            ("max submit count", self.max_submit_count_window),
            ("max cancel count", self.max_cancel_count_window),
        ):
            _positive_int(integer_value, label=integer_label)
        for duration_label, duration_value in (
            ("max disconnect duration", self.max_disconnect_duration),
            ("max order lifetime", self.max_order_lifetime),
            ("max quote age", self.max_quote_age),
            ("max clock drift", self.max_clock_drift),
        ):
            _duration(duration_value, label=duration_label)
        _fraction(
            self.max_price_deviation_bps,
            label="max price deviation bps",
            maximum=Decimal("10000"),
        )
        for fraction_label, fraction_value in (
            ("max equity change fraction", self.max_equity_change_fraction),
            ("max intraday loss fraction", self.max_intraday_loss_fraction),
            ("max capital drawdown fraction", self.max_capital_drawdown_fraction),
        ):
            _fraction(fraction_value, label=fraction_label, allow_zero=False)


@dataclass(frozen=True, slots=True)
class ExecutionRiskContext:
    mode: Mode
    runtime_state: RuntimeState
    now: datetime
    account_type: AccountType
    available_cash: Money
    total_assets: Money
    position: BrokerPositionFact | None
    instrument: InstrumentFact | None
    quote: QuoteFact | None
    market_status: MarketSessionStatus
    canonical_universe: frozenset[Symbol]
    deployment_allowlist: frozenset[Symbol]
    uquant_target_shares: Shares
    uquant_target_weight: Decimal
    uquant_target_gross: Decimal
    uquant_target_gross_cap: Decimal
    freeze_new_risk: bool
    actual_symbol_notional: Money
    actual_gross_notional: Money
    daily_submitted_notional: Money
    daily_filled_notional: Money
    open_order_count: int
    consecutive_rejections: int
    broker_connected: bool
    disconnect_duration: timedelta
    existing_order_age: timedelta | None
    replacement_count: int
    submit_count_window: int
    cancel_count_window: int
    uquant_max_volume_participation: Decimal
    equity_change_fraction: Decimal
    intraday_loss_fraction: Decimal
    capital_drawdown_fraction: Decimal
    reconciliation_healthy: bool
    external_active_order_count: int
    unexplained_position_change: bool
    corporate_action_suspected: bool
    clock_drift: timedelta
    data_identity_matches: bool
    config_identity_matches: bool
    unresolved_order_count: int
    kill_switch_tripped: bool
    auction_allowed: bool
    limits: RiskLimits

    def __post_init__(self) -> None:
        if not isinstance(self.mode, Mode):
            raise DomainTypeError("risk mode must be Mode")
        if not isinstance(self.runtime_state, RuntimeState):
            raise DomainTypeError("risk runtime state must be RuntimeState")
        if not isinstance(self.now, datetime) or self.now.tzinfo is None or self.now.utcoffset() is None:
            raise DomainValidationError("risk now must be timezone-aware")
        if not isinstance(self.account_type, AccountType):
            raise DomainTypeError("risk account type must be AccountType")
        for money_label, money_value in (
            ("available cash", self.available_cash),
            ("total assets", self.total_assets),
            ("actual symbol notional", self.actual_symbol_notional),
            ("actual gross notional", self.actual_gross_notional),
            ("daily submitted notional", self.daily_submitted_notional),
            ("daily filled notional", self.daily_filled_notional),
        ):
            if not isinstance(money_value, Money):
                raise DomainTypeError(f"risk {money_label} must be Money")
        if self.position is not None and not isinstance(self.position, BrokerPositionFact):
            raise DomainTypeError("risk position must be BrokerPositionFact or null")
        if self.instrument is not None and not isinstance(self.instrument, InstrumentFact):
            raise DomainTypeError("risk instrument must be InstrumentFact or null")
        if self.quote is not None and not isinstance(self.quote, QuoteFact):
            raise DomainTypeError("risk quote must be QuoteFact or null")
        if not isinstance(self.market_status, MarketSessionStatus):
            raise DomainTypeError("risk market status must be MarketSessionStatus")
        for universe_label, universe_values in (
            ("canonical universe", self.canonical_universe),
            ("deployment allowlist", self.deployment_allowlist),
        ):
            if not isinstance(universe_values, frozenset) or not all(
                isinstance(item, Symbol) for item in universe_values
            ):
                raise DomainTypeError(f"risk {universe_label} must be frozenset[Symbol]")
        if not isinstance(self.uquant_target_shares, Shares):
            raise DomainTypeError("uquant target shares must be Shares")
        for fraction_label, fraction_value in (
            ("uquant target weight", self.uquant_target_weight),
            ("uquant target gross", self.uquant_target_gross),
            ("uquant target gross cap", self.uquant_target_gross_cap),
            ("uquant max volume participation", self.uquant_max_volume_participation),
            ("equity change fraction", self.equity_change_fraction),
            ("intraday loss fraction", self.intraday_loss_fraction),
            ("capital drawdown fraction", self.capital_drawdown_fraction),
        ):
            _fraction(fraction_value, label=fraction_label)
        for count_label, count_value in (
            ("open order count", self.open_order_count),
            ("consecutive rejections", self.consecutive_rejections),
            ("replacement count", self.replacement_count),
            ("submit count window", self.submit_count_window),
            ("cancel count window", self.cancel_count_window),
            ("external active order count", self.external_active_order_count),
            ("unresolved order count", self.unresolved_order_count),
        ):
            _positive_int(count_value, label=count_label, allow_zero=True)
        _duration(self.disconnect_duration, label="disconnect duration", allow_zero=True)
        _duration(self.clock_drift, label="clock drift", allow_zero=True)
        if self.existing_order_age is not None:
            _duration(self.existing_order_age, label="existing order age", allow_zero=True)
        for bool_label, bool_value in (
            ("freeze new risk", self.freeze_new_risk),
            ("broker connected", self.broker_connected),
            ("reconciliation healthy", self.reconciliation_healthy),
            ("unexplained position change", self.unexplained_position_change),
            ("corporate action suspected", self.corporate_action_suspected),
            ("data identity matches", self.data_identity_matches),
            ("config identity matches", self.config_identity_matches),
            ("kill switch tripped", self.kill_switch_tripped),
            ("auction allowed", self.auction_allowed),
        ):
            if not isinstance(bool_value, bool):
                raise DomainTypeError(f"risk {bool_label} must be bool")
        if not isinstance(self.limits, RiskLimits):
            raise DomainTypeError("risk limits must be RiskLimits")


@dataclass(frozen=True, slots=True)
class GateDecision:
    action: GateAction
    authorized_shares: Shares
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.action, GateAction):
            raise DomainTypeError("gate action must be GateAction")
        if not isinstance(self.authorized_shares, Shares):
            raise DomainTypeError("gate authorized shares must be Shares")
        if not isinstance(self.reason_codes, tuple) or not self.reason_codes:
            raise DomainValidationError("gate decision requires reason codes")
        if self.action in {GateAction.DELAY, GateAction.BLOCK, GateAction.HALT} and (
            self.authorized_shares.value != 0
        ):
            raise DomainValidationError("non-authorizing gate action must have zero shares")


def _zero(action: GateAction, reasons: list[str]) -> GateDecision:
    return GateDecision(
        action=action,
        authorized_shares=Shares(0),
        reason_codes=tuple(dict.fromkeys(reasons)),
    )


def _max_shares_for_notional(notional: Decimal, price: Decimal) -> int:
    if notional <= 0:
        return 0
    return int((notional / price).to_integral_value(rounding=ROUND_FLOOR))


class ExecutionRiskGate:
    """Evaluate all known safety facts in fixed precedence without increasing shares."""

    def evaluate(self, command: RiskCommand, context: ExecutionRiskContext) -> GateDecision:
        if not isinstance(command, RiskCommand):
            raise DomainTypeError("execution risk gate requires RiskCommand")
        if not isinstance(context, ExecutionRiskContext):
            raise DomainTypeError("execution risk gate requires ExecutionRiskContext")
        broker_command = command.command
        limits = context.limits
        halt: list[str] = []
        if context.kill_switch_tripped:
            halt.append("KILL_SWITCH_TRIPPED")
        if not context.data_identity_matches:
            halt.append("DATA_IDENTITY_DRIFT")
        if not context.config_identity_matches:
            halt.append("CONFIG_IDENTITY_DRIFT")
        if context.unresolved_order_count > 0:
            halt.append("UNRESOLVED_ORDER_STATE")
        if not context.reconciliation_healthy:
            halt.append("RECONCILIATION_UNHEALTHY")
        if context.external_active_order_count > 0:
            halt.append("EXTERNAL_ACTIVE_ORDER")
        if context.unexplained_position_change:
            halt.append("UNEXPLAINED_POSITION_CHANGE")
        if context.corporate_action_suspected:
            halt.append("CORPORATE_ACTION_SUSPECTED")
        if context.consecutive_rejections >= limits.max_consecutive_rejections:
            halt.append("CONSECUTIVE_REJECTION_LIMIT")
        if context.equity_change_fraction > limits.max_equity_change_fraction:
            halt.append("ACCOUNT_EQUITY_ANOMALY")
        if context.intraday_loss_fraction > limits.max_intraday_loss_fraction:
            halt.append("INTRADAY_LOSS_LIMIT")
        if context.capital_drawdown_fraction > limits.max_capital_drawdown_fraction:
            halt.append("CAPITAL_DRAWDOWN_LIMIT")
        if context.clock_drift > limits.max_clock_drift:
            halt.append("CLOCK_DRIFT_LIMIT")
        if context.runtime_state not in {RuntimeState.READY, RuntimeState.EXECUTING}:
            halt.append("RUNTIME_NOT_WRITABLE")
        if not context.deployment_allowlist.issubset(context.canonical_universe):
            halt.append("DEPLOYMENT_ALLOWLIST_EXPANDS_UNIVERSE")
        if broker_command.requested_shares.value > command.uquant_authorized_shares.value:
            halt.append("COMMAND_EXCEEDS_UQUANT_AUTHORIZATION")
        if not context.broker_connected and (context.disconnect_duration > limits.max_disconnect_duration):
            halt.append("BROKER_DISCONNECT_LIMIT")
        if context.quote is not None and (context.quote.received_at - context.now > limits.max_clock_drift):
            halt.append("QUOTE_TIME_IN_FUTURE")
        if halt:
            return _zero(GateAction.HALT, halt)

        block: list[str] = []
        symbol = broker_command.symbol
        if symbol not in context.canonical_universe:
            block.append("SYMBOL_NOT_CANONICAL_AI_UNIVERSE")
        if symbol not in context.deployment_allowlist:
            block.append("SYMBOL_NOT_DEPLOYMENT_ALLOWED")
        if context.account_type is not AccountType.CASH:
            block.append("ACCOUNT_NOT_CASH")
        if context.instrument is None:
            block.append("INSTRUMENT_FACT_MISSING")
        if context.quote is None:
            block.append("QUOTE_FACT_MISSING")
        if context.freeze_new_risk and broker_command.side is Side.BUY:
            block.append("FREEZE_NEW_RISK")
        instrument = context.instrument
        quote = context.quote
        if instrument is not None:
            if instrument.symbol != symbol:
                block.append("INSTRUMENT_SYMBOL_MISMATCH")
            if instrument.security_type is not SecurityType.EQUITY:
                block.append("SECURITY_TYPE_NOT_EQUITY")
            if instrument.status is SecurityStatus.SUSPENDED:
                block.append("INSTRUMENT_NOT_TRADING")
            elif instrument.status is not SecurityStatus.TRADING:
                block.append("INSTRUMENT_RISK_STATUS")
        if quote is not None and quote.symbol != symbol:
            block.append("QUOTE_SYMBOL_MISMATCH")
        if instrument is not None and quote is not None:
            price_limits = (
                instrument.lower_limit,
                instrument.upper_limit,
                quote.lower_limit,
                quote.upper_limit,
            )
            if any(item is None for item in price_limits):
                block.append("PRICE_LIMIT_FACT_MISSING")
            elif instrument.lower_limit != quote.lower_limit or instrument.upper_limit != quote.upper_limit:
                block.append("PRICE_LIMIT_FACT_MISMATCH")
            lower = instrument.lower_limit
            upper = instrument.upper_limit
            if (
                lower is not None
                and upper is not None
                and not (lower.value <= broker_command.limit_price.value <= upper.value)
            ):
                block.append("LIMIT_PRICE_OUT_OF_BOUNDS")
            if broker_command.limit_price.decimal_places > instrument.price_precision:
                block.append("LIMIT_PRICE_PRECISION_INVALID")
            if broker_command.limit_price.value % instrument.price_tick.value != 0:
                block.append("LIMIT_PRICE_TICK_INVALID")
            anchors = (quote.last_price, quote.previous_close)
            if any(anchor is None for anchor in anchors):
                block.append("PRICE_REFERENCE_MISSING")
            else:
                for anchor in anchors:
                    if anchor is None:
                        continue
                    deviation = (
                        abs(broker_command.limit_price.value - anchor.value) / anchor.value * Decimal(10000)
                    )
                    if deviation > limits.max_price_deviation_bps:
                        block.append("PRICE_DEVIATION_LIMIT")
                        break
        if context.existing_order_age is not None and (
            context.existing_order_age > limits.max_order_lifetime
        ):
            block.append("ORDER_LIFETIME_LIMIT")
        if context.replacement_count >= limits.max_replacements:
            block.append("REPLACEMENT_LIMIT")
        current_shares = 0 if context.position is None else context.position.total_shares.value
        if context.position is not None and context.position.symbol != symbol:
            block.append("POSITION_SYMBOL_MISMATCH")
        if broker_command.side is Side.BUY:
            if context.uquant_target_shares.value <= current_shares:
                block.append("BUY_TARGET_ALREADY_SATISFIED")
        else:
            if context.position is None or current_shares <= context.uquant_target_shares.value:
                block.append("SELL_TARGET_ALREADY_SATISFIED")
        if block:
            return _zero(GateAction.BLOCK, block)

        delay: list[str] = []
        if not context.broker_connected:
            delay.append("BROKER_DISCONNECTED")
        tradable_statuses = {MarketSessionStatus.OPEN}
        if context.auction_allowed:
            tradable_statuses.add(MarketSessionStatus.AUCTION)
        if context.market_status not in tradable_statuses or (
            quote is not None and quote.market_status not in tradable_statuses
        ):
            delay.append("MARKET_NOT_TRADABLE")
        if quote is not None and context.now - quote.received_at > limits.max_quote_age:
            delay.append("QUOTE_STALE")
        if context.open_order_count >= limits.max_open_orders:
            delay.append("OPEN_ORDER_LIMIT")
        if context.submit_count_window >= limits.max_submit_count_window:
            delay.append("SUBMIT_RATE_LIMIT")
        if context.cancel_count_window >= limits.max_cancel_count_window:
            delay.append("CANCEL_RATE_LIMIT")
        if delay:
            return _zero(GateAction.DELAY, delay)

        if instrument is None or quote is None:
            raise DomainValidationError("blocked missing market facts reached quantity evaluation")
        requested = broker_command.requested_shares.value
        candidate = min(requested, command.uquant_authorized_shares.value)
        shrink_reasons: list[str] = []

        def apply_cap(maximum: int, reason: str) -> None:
            nonlocal candidate
            bounded = max(0, maximum)
            if bounded < candidate:
                candidate = bounded
                shrink_reasons.append(reason)

        if broker_command.side is Side.BUY:
            apply_cap(
                context.uquant_target_shares.value - current_shares,
                "UQUANT_TARGET_SHARES_SHRINK",
            )
            target_symbol_notional = context.uquant_target_weight * context.total_assets.value
            apply_cap(
                _max_shares_for_notional(
                    target_symbol_notional - context.actual_symbol_notional.value,
                    broker_command.limit_price.value,
                ),
                "UQUANT_TARGET_WEIGHT_SHRINK",
            )
            target_gross = (
                min(context.uquant_target_gross, context.uquant_target_gross_cap) * context.total_assets.value
            )
            apply_cap(
                _max_shares_for_notional(
                    target_gross - context.actual_gross_notional.value,
                    broker_command.limit_price.value,
                ),
                "UQUANT_TARGET_GROSS_SHRINK",
            )
            apply_cap(
                _max_shares_for_notional(
                    limits.max_symbol_notional.value - context.actual_symbol_notional.value,
                    broker_command.limit_price.value,
                ),
                "SYMBOL_NOTIONAL_CAP_SHRINK",
            )
            apply_cap(
                _max_shares_for_notional(
                    limits.max_total_gross_notional.value - context.actual_gross_notional.value,
                    broker_command.limit_price.value,
                ),
                "TOTAL_GROSS_CAP_SHRINK",
            )
            cash_for_gross = context.available_cash.value - command.estimated_fees.value
            apply_cap(
                _max_shares_for_notional(cash_for_gross, broker_command.limit_price.value),
                "AVAILABLE_CASH_SHRINK",
            )
        else:
            target_gap = current_shares - context.uquant_target_shares.value
            apply_cap(target_gap, "UQUANT_TARGET_SHARES_SHRINK")
            if context.position is None:
                apply_cap(0, "POSITION_QUANTITY_SHRINK")
            else:
                apply_cap(
                    context.position.total_shares.value,
                    "POSITION_QUANTITY_SHRINK",
                )
                apply_cap(
                    context.position.sellable_shares.value,
                    "SELLABLE_QUANTITY_SHRINK",
                )
        apply_cap(
            _max_shares_for_notional(limits.max_order_notional.value, broker_command.limit_price.value),
            "ORDER_NOTIONAL_CAP_SHRINK",
        )
        apply_cap(
            _max_shares_for_notional(
                limits.max_daily_submitted_notional.value - context.daily_submitted_notional.value,
                broker_command.limit_price.value,
            ),
            "DAILY_SUBMITTED_CAP_SHRINK",
        )
        apply_cap(
            _max_shares_for_notional(
                limits.max_daily_filled_notional.value - context.daily_filled_notional.value,
                broker_command.limit_price.value,
            ),
            "DAILY_FILLED_CAP_SHRINK",
        )
        volume_cap = int(
            (Decimal(quote.volume.value) * context.uquant_max_volume_participation).to_integral_value(
                rounding=ROUND_FLOOR
            )
        )
        apply_cap(volume_cap, "VOLUME_PARTICIPATION_SHRINK")
        unit = instrument.trading_unit.value
        full_odd_lot_sell = (
            broker_command.side is Side.SELL
            and context.position is not None
            and candidate == context.position.total_shares.value
            and context.uquant_target_shares.value == 0
        )
        rounded = candidate if full_odd_lot_sell else candidate - candidate % unit
        if rounded < candidate:
            shrink_reasons.append("TRADING_UNIT_SHRINK")
            candidate = rounded
        if candidate <= 0:
            reasons = shrink_reasons or ["AUTHORIZED_QUANTITY_ZERO"]
            return _zero(GateAction.BLOCK, reasons)
        if candidate < requested:
            return GateDecision(
                action=GateAction.SHRINK,
                authorized_shares=Shares(candidate),
                reason_codes=tuple(dict.fromkeys(shrink_reasons)),
            )
        return GateDecision(
            action=GateAction.ALLOW,
            authorized_shares=Shares(requested),
            reason_codes=("ALL_CHECKS_PASSED",),
        )


__all__ = (
    "ExecutionRiskContext",
    "ExecutionRiskGate",
    "GateAction",
    "GateDecision",
    "RiskCommand",
    "RiskLimits",
)
