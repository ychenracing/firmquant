"""Deterministic A-share paper broker using the production BrokerGateway contract."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation

from firmquant.domain.broker_facts import (
    AccountType,
    BrokerAccountFact,
    BrokerFillFact,
    BrokerOrderFact,
    BrokerOrderStatus,
    BrokerPositionFact,
    FillStatus,
    InstrumentFact,
    MarketSessionStatus,
    PriceType,
    QuoteFact,
    SecurityStatus,
    SecurityType,
    Side,
)
from firmquant.domain.errors import DomainTypeError, DomainValidationError
from firmquant.domain.values import Money, Price, Shares, Symbol
from firmquant.execution.policy import ExecutionPolicy, FeeBreakdown

from .gateway import (
    BrokerDisconnected,
    BrokerEventSink,
    BrokerFactUnavailable,
    BrokerHealth,
    BrokerOrderCommand,
)
from .normalization import (
    canonical_raw_payload_sha256,
    normalize_broker_event,
    normalize_fill,
    normalize_order,
)

_MONEY_QUANTUM = Decimal("0.0001")
_CLOSED_STATUSES = frozenset(
    {
        BrokerOrderStatus.FILLED,
        BrokerOrderStatus.CANCELLED,
        BrokerOrderStatus.REJECTED,
        BrokerOrderStatus.EXPIRED,
    }
)


class PaperCallbackDeliveryError(RuntimeError):
    """Raised after broker state advances but a callback sink fails."""


@dataclass(frozen=True, slots=True)
class PaperMatchResult:
    order: BrokerOrderFact
    fill: BrokerFillFact | None
    reason_code: str


def _typed_tuple[T](values: tuple[T, ...], expected: type[T], *, label: str) -> tuple[T, ...]:
    if not isinstance(values, tuple) or not all(isinstance(value, expected) for value in values):
        raise DomainTypeError(f"paper broker {label} must be a typed tuple")
    return values


def _money(value: Decimal, *, label: str) -> Money:
    try:
        rounded = value.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    except InvalidOperation as error:
        raise DomainValidationError(f"{label} exceeds Decimal bounds") from error
    return Money(rounded)


def _stable_id(prefix: str, payload: str) -> str:
    return prefix + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _command_fingerprint(command: BrokerOrderCommand) -> str:
    payload = {
        "execution_id": command.execution_id,
        "idempotency_key": command.idempotency_key,
        "client_order_id": command.client_order_id,
        "symbol": command.symbol.canonical,
        "side": command.side.value,
        "price_type": command.price_type.value,
        "requested_shares": command.requested_shares.value,
        "limit_price": command.limit_price.canonical,
        "strategy_session": command.strategy_session.isoformat(),
    }
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class PaperBroker:
    """Single-account cash broker simulation with conservative exchange facts."""

    def __init__(
        self,
        *,
        account: BrokerAccountFact,
        positions: tuple[BrokerPositionFact, ...],
        instruments: tuple[InstrumentFact, ...],
        quotes: tuple[QuoteFact, ...],
        market_status: MarketSessionStatus,
        policy: ExecutionPolicy,
        clock: Callable[[], datetime],
    ) -> None:
        if not isinstance(account, BrokerAccountFact):
            raise DomainTypeError("paper account must be BrokerAccountFact")
        if account.account_type is not AccountType.CASH:
            raise DomainValidationError("paper broker supports CASH accounts only")
        if not isinstance(policy, ExecutionPolicy):
            raise DomainTypeError("paper policy must be ExecutionPolicy")
        if not isinstance(market_status, MarketSessionStatus):
            raise DomainTypeError("paper market status must be MarketSessionStatus")
        if not callable(clock):
            raise DomainTypeError("paper clock must be callable")
        position_values = _typed_tuple(positions, BrokerPositionFact, label="positions")
        instrument_values = _typed_tuple(instruments, InstrumentFact, label="instruments")
        quote_values = _typed_tuple(quotes, QuoteFact, label="quotes")
        self._positions = {position.symbol: position for position in position_values}
        self._instruments = {
            instrument.symbol: instrument for instrument in instrument_values
        }
        self._quotes = {quote.symbol: quote for quote in quote_values}
        if len(self._positions) != len(position_values):
            raise DomainValidationError("paper broker contains duplicate positions")
        if len(self._instruments) != len(instrument_values):
            raise DomainValidationError("paper broker contains duplicate instruments")
        if len(self._quotes) != len(quote_values):
            raise DomainValidationError("paper broker contains duplicate quotes")
        if set(self._positions) - set(self._instruments):
            raise DomainValidationError("paper position is missing instrument metadata")
        if set(self._positions) - set(self._quotes):
            raise DomainValidationError("paper position is missing quote metadata")
        for symbol, position in self._positions.items():
            quote = self._quotes[symbol]
            if position.total_shares.is_positive and position.average_cost is None:
                raise DomainValidationError("paper positive position requires average cost")
            if quote.last_price is None and quote.previous_close is None:
                raise DomainValidationError("paper position requires a valuation price")
        self._account = account
        self._market_status = market_status
        self._policy = policy
        self._clock = clock
        self._orders: dict[str, BrokerOrderFact] = {}
        self._commands: dict[str, BrokerOrderCommand] = {}
        self._command_fingerprints: dict[str, str] = {}
        self._idempotency_orders: dict[str, str] = {}
        self._fills: list[BrokerFillFact] = []
        self._reasons: dict[str, str] = {}
        self._match_counts: dict[str, int] = {}
        self._consumed_volume: dict[tuple[date, Symbol], int] = {}
        self._event_sequence = max(
            (quote.sequence for quote in quote_values),
            default=0,
        )
        self._connected = False
        self._sink: BrokerEventSink | None = None
        self._pending_callbacks: list[dict[str, object]] = []
        self._callback_error: Exception | None = None
        self._callback_delivery_failed = False

    @property
    def state_sha256(self) -> str:
        payload = {
            "schema": "firmquant.paper-state.v1",
            "account": {
                "account_id_hash": self._account.account_id_hash,
                "available_cash": self._account.available_cash.canonical,
                "total_assets": self._account.total_assets.canonical,
            },
            "positions": [
                {
                    "symbol": position.symbol.canonical,
                    "total_shares": position.total_shares.value,
                    "sellable_shares": position.sellable_shares.value,
                    "average_cost": (
                        None
                        if position.average_cost is None
                        else position.average_cost.canonical
                    ),
                    "market_value": position.market_value.canonical,
                }
                for position in self.query_positions(connected_required=False)
            ],
            "orders": [
                {
                    "broker_order_id": order.broker_order_id,
                    "client_order_id": order.client_order_id,
                    "status": order.status.value,
                    "filled_shares": order.filled_shares.value,
                    "event_sequence": order.event_sequence,
                }
                for order in self._orders.values()
            ],
            "fills": [
                {
                    "broker_fill_id": fill.broker_fill_id,
                    "broker_order_id": fill.broker_order_id,
                    "shares": fill.shares.value,
                    "price": fill.price.canonical,
                    "fees": fill.total_fees.canonical,
                    "event_sequence": fill.event_sequence,
                }
                for fill in self._fills
            ],
            "reasons": dict(sorted(self._reasons.items())),
            "consumed_volume": [
                {
                    "session_date": session.isoformat(),
                    "symbol": symbol.canonical,
                    "shares": shares,
                }
                for (session, symbol), shares in sorted(
                    self._consumed_volume.items(),
                    key=lambda item: (item[0][0], item[0][1].canonical),
                )
            ],
        }
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def health(self) -> BrokerHealth:
        connected = self._connected
        return BrokerHealth(
            connected=connected,
            read_healthy=connected,
            write_healthy=connected,
            observed_at=self._clock(),
            diagnostic_code="PAPER_CONNECTED" if connected else "DISCONNECTED",
        )

    def _require_connected(self) -> None:
        if not self._connected:
            raise BrokerDisconnected("paper broker is disconnected")

    def query_account(self) -> BrokerAccountFact:
        self._require_connected()
        return self._account

    def query_positions(
        self, *, connected_required: bool = True
    ) -> tuple[BrokerPositionFact, ...]:
        if connected_required:
            self._require_connected()
        return tuple(
            self._positions[symbol]
            for symbol in sorted(self._positions, key=lambda item: item.canonical)
        )

    def query_orders(self) -> tuple[BrokerOrderFact, ...]:
        self._require_connected()
        return tuple(self._orders.values())

    def query_fills(self) -> tuple[BrokerFillFact, ...]:
        self._require_connected()
        return tuple(self._fills)

    def query_instrument(self, symbol: Symbol) -> InstrumentFact:
        self._require_connected()
        if not isinstance(symbol, Symbol):
            raise DomainTypeError("paper instrument query symbol must be Symbol")
        try:
            return self._instruments[symbol]
        except KeyError as error:
            raise BrokerFactUnavailable(f"paper instrument unavailable: {symbol}") from error

    def query_quote(self, symbol: Symbol) -> QuoteFact:
        self._require_connected()
        if not isinstance(symbol, Symbol):
            raise DomainTypeError("paper quote query symbol must be Symbol")
        try:
            return self._quotes[symbol]
        except KeyError as error:
            raise BrokerFactUnavailable(f"paper quote unavailable: {symbol}") from error

    def query_market_status(self) -> MarketSessionStatus:
        self._require_connected()
        return self._market_status

    def subscribe(self, callback_sink: BrokerEventSink) -> None:
        self._require_connected()
        if not callable(callback_sink):
            raise DomainTypeError("paper callback sink must be callable")
        pending = tuple(self._pending_callbacks)
        try:
            for event in pending:
                callback_sink(copy.deepcopy(event))
        except Exception as error:
            self._callback_error = error
            self._callback_delivery_failed = True
            raise PaperCallbackDeliveryError(
                "paper callback recovery failed; reconcile before retry"
            ) from error
        self._pending_callbacks.clear()
        self._sink = callback_sink
        self._callback_error = None
        self._callback_delivery_failed = False

    def set_quote(self, quote: QuoteFact) -> None:
        if not isinstance(quote, QuoteFact):
            raise DomainTypeError("paper quote update must be QuoteFact")
        instrument = self._instruments.get(quote.symbol)
        if instrument is None:
            raise BrokerFactUnavailable(f"paper instrument unavailable: {quote.symbol}")
        if quote.session_date != instrument.session_date:
            raise DomainValidationError("paper quote and instrument session dates differ")
        previous = self._quotes.get(quote.symbol)
        if previous is not None and quote.session_date == previous.session_date:
            if quote.sequence < previous.sequence:
                raise DomainValidationError("paper quote sequence cannot regress")
            if quote.sequence == previous.sequence and quote != previous:
                raise DomainValidationError("paper quote sequence identity collision")
        self._quotes[quote.symbol] = quote

    def set_market_status(self, status: MarketSessionStatus) -> None:
        if not isinstance(status, MarketSessionStatus):
            raise DomainTypeError("paper market status must be MarketSessionStatus")
        self._market_status = status

    def reason_for(self, broker_order_id: str) -> str | None:
        if not isinstance(broker_order_id, str) or not broker_order_id:
            raise DomainValidationError("paper reason broker order id must be non-empty text")
        return self._reasons.get(broker_order_id)

    def _next_sequence(self) -> int:
        self._event_sequence += 1
        return self._event_sequence

    def _emit(self, *, event_type: str, payload: dict[str, object]) -> None:
        digest = canonical_raw_payload_sha256(payload)
        event_id = _stable_id(f"paper-{event_type.lower()}-event-", digest)
        event: dict[str, object] = {
            "event_id": event_id,
            "event_type": event_type,
            "payload": copy.deepcopy(payload),
        }
        normalize_broker_event(event, received_at=self._clock())
        if self._sink is None:
            self._pending_callbacks.append(event)
            return
        try:
            self._sink(copy.deepcopy(event))
        except Exception as error:
            self._pending_callbacks.append(event)
            self._callback_error = error
            self._callback_delivery_failed = True

    def _require_callback_delivery(self) -> None:
        if not self._callback_delivery_failed:
            return
        error = self._callback_error
        if error is None:
            raise PaperCallbackDeliveryError(
                "paper callback delivery is degraded; reconcile before retry"
            )
        raise PaperCallbackDeliveryError(
            "paper callback delivery is degraded; reconcile before retry"
        ) from error

    def _raise_callback_error(self) -> None:
        if self._callback_error is None:
            return
        error = self._callback_error
        raise PaperCallbackDeliveryError(
            "paper state advanced but callback delivery failed; reconcile before retry"
        ) from error

    def _order_payload(
        self,
        *,
        command: BrokerOrderCommand,
        broker_order_id: str,
        status: BrokerOrderStatus,
        filled_shares: int,
        sequence: int,
        event_time: datetime,
    ) -> dict[str, object]:
        quote = self._quotes[command.symbol]
        return {
            "broker_order_id": broker_order_id,
            "client_order_id": command.client_order_id,
            "symbol": command.symbol.canonical,
            "side": command.side.value,
            "price_type": PriceType.LIMIT.value,
            "status": status.value,
            "requested_shares": command.requested_shares.value,
            "filled_shares": filled_shares,
            "limit_price": command.limit_price.canonical,
            "session_date": quote.session_date.isoformat(),
            "event_time": event_time.isoformat(),
            "event_sequence": sequence,
        }

    def _record_order(
        self,
        *,
        command: BrokerOrderCommand,
        broker_order_id: str,
        status: BrokerOrderStatus,
        filled_shares: int,
        reason_code: str,
    ) -> BrokerOrderFact:
        now = self._clock()
        payload = self._order_payload(
            command=command,
            broker_order_id=broker_order_id,
            status=status,
            filled_shares=filled_shares,
            sequence=self._next_sequence(),
            event_time=now,
        )
        fact = normalize_order(payload, received_at=now)
        self._orders[broker_order_id] = fact
        self._reasons[broker_order_id] = reason_code
        self._emit(event_type="ORDER", payload=payload)
        return fact

    def _submission_reason(self, command: BrokerOrderCommand) -> str | None:
        instrument = self._instruments.get(command.symbol)
        quote = self._quotes.get(command.symbol)
        if instrument is None:
            return "INSTRUMENT_FACT_MISSING"
        if quote is None:
            return "QUOTE_FACT_MISSING"
        allowed_statuses = {MarketSessionStatus.OPEN}
        if self._policy.allow_auction:
            allowed_statuses.add(MarketSessionStatus.AUCTION)
        if self._market_status not in allowed_statuses or quote.market_status not in allowed_statuses:
            return "MARKET_NOT_OPEN"
        if self._market_status is not quote.market_status:
            return "MARKET_STATUS_MISMATCH"
        if instrument.security_type is not SecurityType.EQUITY:
            return "SECURITY_TYPE_NOT_EQUITY"
        if instrument.status is not SecurityStatus.TRADING:
            return "INSTRUMENT_NOT_TRADING"
        limits = (
            instrument.lower_limit,
            instrument.upper_limit,
            quote.lower_limit,
            quote.upper_limit,
        )
        if any(limit is None for limit in limits):
            return "PRICE_LIMIT_FACT_MISSING"
        if (
            instrument.lower_limit != quote.lower_limit
            or instrument.upper_limit != quote.upper_limit
        ):
            return "PRICE_LIMIT_FACT_MISMATCH"
        if instrument.session_date != quote.session_date:
            return "MARKET_FACT_SESSION_MISMATCH"
        lower = instrument.lower_limit
        upper = instrument.upper_limit
        assert lower is not None and upper is not None
        if not lower.value <= command.limit_price.value <= upper.value:
            return "LIMIT_PRICE_OUT_OF_BOUNDS"
        if command.limit_price.decimal_places > instrument.price_precision:
            return "LIMIT_PRICE_PRECISION_INVALID"
        if command.limit_price.value % instrument.price_tick.value != 0:
            return "LIMIT_PRICE_TICK_INVALID"
        unit = instrument.trading_unit.value
        requested = command.requested_shares.value
        position = self._positions.get(command.symbol)
        full_odd_lot_sell = (
            command.side is Side.SELL
            and position is not None
            and requested == position.total_shares.value
        )
        if requested % unit != 0 and not full_odd_lot_sell:
            return "TRADING_UNIT_INVALID"
        if command.side is Side.SELL:
            if position is None or requested > position.total_shares.value:
                return "POSITION_INSUFFICIENT"
            if requested > position.sellable_shares.value:
                return "T1_SELLABLE_EXCEEDED"
        return None

    def submit_order(self, command: BrokerOrderCommand) -> BrokerOrderFact:
        self._require_connected()
        self._require_callback_delivery()
        if not isinstance(command, BrokerOrderCommand):
            raise DomainTypeError("paper submit requires BrokerOrderCommand")
        fingerprint = _command_fingerprint(command)
        existing_id = self._idempotency_orders.get(command.idempotency_key)
        if existing_id is not None:
            if self._command_fingerprints[existing_id] != fingerprint:
                raise DomainValidationError("paper idempotency key identity collision")
            return self._orders[existing_id]
        broker_order_id = _stable_id("paper-order-", command.idempotency_key)
        self._idempotency_orders[command.idempotency_key] = broker_order_id
        self._commands[broker_order_id] = command
        self._command_fingerprints[broker_order_id] = fingerprint
        self._match_counts[broker_order_id] = 0
        rejection = self._submission_reason(command)
        if rejection is not None:
            rejected_order = self._record_order(
                command=command,
                broker_order_id=broker_order_id,
                status=BrokerOrderStatus.REJECTED,
                filled_shares=0,
                reason_code=rejection,
            )
            self._raise_callback_error()
            return rejected_order
        self._record_order(
            command=command,
            broker_order_id=broker_order_id,
            status=BrokerOrderStatus.ACKNOWLEDGED,
            filled_shares=0,
            reason_code="ACKNOWLEDGED",
        )
        match_result = self.match(broker_order_id)
        self._raise_callback_error()
        return match_result.order

    def _fill_price(
        self, *, command: BrokerOrderCommand, instrument: InstrumentFact, quote: QuoteFact
    ) -> tuple[Price | None, str | None]:
        if command.side is Side.BUY:
            if quote.upper_limit is not None and (
                quote.last_price == quote.upper_limit or quote.ask_price == quote.upper_limit
            ):
                return None, "UPPER_LIMIT_BUY_BLOCKED"
            base = quote.ask_price
            if base is None:
                return None, "ASK_LIQUIDITY_MISSING"
            if command.limit_price.value < base.value:
                return None, "ORDER_NOT_MARKETABLE"
            multiplier = Decimal(1) + self._policy.fill_model.slippage_bps / Decimal(10000)
        else:
            if quote.lower_limit is not None and (
                quote.last_price == quote.lower_limit or quote.bid_price == quote.lower_limit
            ):
                return None, "LOWER_LIMIT_SELL_BLOCKED"
            base = quote.bid_price
            if base is None:
                return None, "BID_LIQUIDITY_MISSING"
            if command.limit_price.value > base.value:
                return None, "ORDER_NOT_MARKETABLE"
            multiplier = Decimal(1) - self._policy.fill_model.slippage_bps / Decimal(10000)
        raw_price = base.value * multiplier
        tick_units = (raw_price / instrument.price_tick.value).quantize(
            Decimal(1), rounding=ROUND_HALF_UP
        )
        price = Price(tick_units * instrument.price_tick.value)
        if command.side is Side.BUY and price.value > command.limit_price.value:
            return None, "SLIPPAGE_EXCEEDS_LIMIT"
        if command.side is Side.SELL and price.value < command.limit_price.value:
            return None, "SLIPPAGE_EXCEEDS_LIMIT"
        lower = instrument.lower_limit
        upper = instrument.upper_limit
        if lower is None or upper is None or not lower.value <= price.value <= upper.value:
            return None, "FILL_PRICE_OUT_OF_BOUNDS"
        return price, None

    def _participation_capacity(self, *, command: BrokerOrderCommand, quote: QuoteFact) -> int:
        maximum = int(
            (
                Decimal(quote.volume.value)
                * self._policy.fill_model.max_volume_participation
            ).to_integral_value(rounding=ROUND_FLOOR)
        )
        consumed = self._consumed_volume.get((quote.session_date, command.symbol), 0)
        return max(0, maximum - consumed)

    def _round_fill_quantity(
        self,
        *,
        command: BrokerOrderCommand,
        instrument: InstrumentFact,
        candidate: int,
    ) -> int:
        if candidate <= 0:
            return 0
        unit = instrument.trading_unit.value
        if command.side is Side.SELL:
            position = self._positions.get(command.symbol)
            if (
                position is not None
                and candidate >= position.total_shares.value
                and command.requested_shares.value == position.total_shares.value
            ):
                return position.total_shares.value
        return candidate - candidate % unit

    def _affordable_buy_shares(
        self, *, price: Price, candidate: int, trading_unit: int
    ) -> int:
        available = self._account.available_cash.value
        maximum = min(candidate, int((available / price.value).to_integral_value(ROUND_FLOOR)))
        maximum -= maximum % trading_unit
        while maximum > 0:
            shares = Shares(maximum)
            fees = self._policy.fee_schedule.calculate(
                side=Side.BUY, price=price, shares=shares
            )
            if price.value * maximum + fees.total.value <= available:
                return maximum
            maximum -= trading_unit
        return 0

    def _fill_payload(
        self,
        *,
        command: BrokerOrderCommand,
        broker_order_id: str,
        fill_id: str,
        shares: int,
        price: Price,
        fees: FeeBreakdown,
        event_time: datetime,
    ) -> dict[str, object]:
        return {
            "broker_fill_id": fill_id,
            "broker_order_id": broker_order_id,
            "symbol": command.symbol.canonical,
            "side": command.side.value,
            "status": FillStatus.CONFIRMED.value,
            "shares": shares,
            "price": price.canonical,
            "commission": fees.commission.canonical,
            "stamp_duty": fees.stamp_duty.canonical,
            "transfer_fee": fees.transfer_fee.canonical,
            "session_date": self._quotes[command.symbol].session_date.isoformat(),
            "event_time": event_time.isoformat(),
            "event_sequence": self._next_sequence(),
        }

    def _valuation_price(self, symbol: Symbol) -> Price:
        quote = self._quotes[symbol]
        price = quote.last_price or quote.previous_close
        if price is None:
            raise BrokerFactUnavailable(f"paper valuation price unavailable: {symbol}")
        return price

    def _apply_economics(self, fill: BrokerFillFact) -> None:
        gross = fill.price.value * fill.shares.value
        fees = fill.total_fees.value
        cash = self._account.available_cash.value
        position = self._positions.get(fill.symbol)
        if fill.side is Side.BUY:
            new_cash = cash - gross - fees
            if new_cash < 0:
                raise DomainValidationError("paper BUY would create negative cash")
            old_shares = 0 if position is None else position.total_shares.value
            old_cost = Decimal(0)
            sellable = Shares(0)
            if position is not None:
                if position.average_cost is None:
                    raise DomainValidationError("paper existing position has unknown cost")
                old_cost = position.average_cost.value * old_shares
                sellable = position.sellable_shares
            total_shares = old_shares + fill.shares.value
            average = ((old_cost + gross + fees) / total_shares).quantize(
                Decimal("0.00000001"), rounding=ROUND_HALF_UP
            )
            valuation = self._valuation_price(fill.symbol).value * total_shares
            self._positions[fill.symbol] = BrokerPositionFact(
                symbol=fill.symbol,
                total_shares=Shares(total_shares),
                sellable_shares=sellable,
                average_cost=Price(average),
                market_value=_money(valuation, label="paper position market value"),
            )
        else:
            if position is None:
                raise DomainValidationError("paper SELL position disappeared")
            if (
                fill.shares.value > position.total_shares.value
                or fill.shares.value > position.sellable_shares.value
            ):
                raise DomainValidationError("paper SELL exceeds position or sellable shares")
            new_cash = cash + gross - fees
            remaining = position.total_shares.value - fill.shares.value
            remaining_sellable = position.sellable_shares.value - fill.shares.value
            if remaining == 0:
                del self._positions[fill.symbol]
            else:
                valuation = self._valuation_price(fill.symbol).value * remaining
                self._positions[fill.symbol] = replace(
                    position,
                    total_shares=Shares(remaining),
                    sellable_shares=Shares(remaining_sellable),
                    market_value=_money(valuation, label="paper position market value"),
                )
        available_cash = _money(new_cash, label="paper available cash")
        total_assets = available_cash.value + sum(
            position_fact.market_value.value for position_fact in self._positions.values()
        )
        self._account = replace(
            self._account,
            available_cash=available_cash,
            total_assets=_money(total_assets, label="paper total assets"),
        )

    def match(
        self, broker_order_id: str, *, quote: QuoteFact | None = None
    ) -> PaperMatchResult:
        self._require_connected()
        self._require_callback_delivery()
        if not isinstance(broker_order_id, str) or not broker_order_id:
            raise DomainValidationError("paper match broker order id must be non-empty text")
        try:
            order = self._orders[broker_order_id]
            command = self._commands[broker_order_id]
        except KeyError as error:
            raise BrokerFactUnavailable(f"paper order unavailable: {broker_order_id}") from error
        if quote is not None:
            if quote.symbol != command.symbol:
                raise DomainValidationError("paper match quote symbol contradicts order")
            self.set_quote(quote)
        current_quote = self._quotes[command.symbol]
        instrument = self._instruments[command.symbol]
        if order.status in _CLOSED_STATUSES:
            return PaperMatchResult(
                order=order,
                fill=None,
                reason_code=self._reasons[broker_order_id],
            )
        status_reason = self._submission_reason(command)
        if status_reason is not None:
            self._reasons[broker_order_id] = status_reason
            return PaperMatchResult(order=order, fill=None, reason_code=status_reason)
        price, price_reason = self._fill_price(
            command=command,
            instrument=instrument,
            quote=current_quote,
        )
        if price is None:
            assert price_reason is not None
            self._reasons[broker_order_id] = price_reason
            return PaperMatchResult(order=order, fill=None, reason_code=price_reason)
        remaining = order.requested_shares.value - order.filled_shares.value
        capacity = self._participation_capacity(command=command, quote=current_quote)
        candidate = min(remaining, capacity)
        if command.side is Side.SELL:
            position = self._positions.get(command.symbol)
            candidate = min(
                candidate,
                0 if position is None else position.sellable_shares.value,
            )
        quantity = self._round_fill_quantity(
            command=command,
            instrument=instrument,
            candidate=candidate,
        )
        if command.side is Side.BUY:
            quantity = self._affordable_buy_shares(
                price=price,
                candidate=quantity,
                trading_unit=instrument.trading_unit.value,
            )
        if quantity <= 0:
            reason = "CASH_INSUFFICIENT" if command.side is Side.BUY and candidate > 0 else "VOLUME_CAPACITY_EXHAUSTED"
            self._reasons[broker_order_id] = reason
            return PaperMatchResult(order=order, fill=None, reason_code=reason)
        shares = Shares(quantity)
        fees = self._policy.fee_schedule.calculate(
            side=command.side,
            price=price,
            shares=shares,
        )
        match_count = self._match_counts[broker_order_id] + 1
        self._match_counts[broker_order_id] = match_count
        fill_identity = (
            f"{broker_order_id}\0{match_count}\0{quantity}\0{price.canonical}"
        )
        fill_id = _stable_id("paper-fill-", fill_identity)
        now = self._clock()
        fill_payload = self._fill_payload(
            command=command,
            broker_order_id=broker_order_id,
            fill_id=fill_id,
            shares=quantity,
            price=price,
            fees=fees,
            event_time=now,
        )
        fill = normalize_fill(fill_payload, received_at=now)
        self._apply_economics(fill)
        self._fills.append(fill)
        volume_key = (current_quote.session_date, command.symbol)
        self._consumed_volume[volume_key] = self._consumed_volume.get(volume_key, 0) + quantity
        self._emit(event_type="FILL", payload=fill_payload)
        total_filled = order.filled_shares.value + quantity
        status = (
            BrokerOrderStatus.FILLED
            if total_filled == order.requested_shares.value
            else BrokerOrderStatus.PARTIALLY_FILLED
        )
        updated = self._record_order(
            command=command,
            broker_order_id=broker_order_id,
            status=status,
            filled_shares=total_filled,
            reason_code="FILLED" if status is BrokerOrderStatus.FILLED else "PARTIAL_FILL",
        )
        self._raise_callback_error()
        return PaperMatchResult(
            order=updated,
            fill=fill,
            reason_code=self._reasons[broker_order_id],
        )

    def cancel_order(self, broker_order_id: str) -> BrokerOrderFact:
        self._require_connected()
        self._require_callback_delivery()
        if not isinstance(broker_order_id, str) or not broker_order_id:
            raise DomainValidationError("paper cancel broker order id must be non-empty text")
        try:
            current = self._orders[broker_order_id]
            command = self._commands[broker_order_id]
        except KeyError as error:
            raise BrokerFactUnavailable(f"paper order unavailable: {broker_order_id}") from error
        if current.status in _CLOSED_STATUSES:
            return current
        cancelled = self._record_order(
            command=command,
            broker_order_id=broker_order_id,
            status=BrokerOrderStatus.CANCELLED,
            filled_shares=current.filled_shares.value,
            reason_code="CANCELLED_BY_SYSTEM",
        )
        self._raise_callback_error()
        return cancelled


__all__ = ("PaperBroker", "PaperCallbackDeliveryError", "PaperMatchResult")
