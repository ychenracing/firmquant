"""Deterministic next-session projection of frozen uquant order intents."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_FLOOR, Decimal
from typing import Never

from firmquant.domain.broker_facts import (
    BrokerSnapshot,
    InstrumentFact,
    MarketSessionStatus,
    QuoteFact,
    SecurityStatus,
    SecurityType,
    Side,
)
from firmquant.domain.errors import DomainTypeError, DomainValidationError
from firmquant.domain.values import Price, Shares, Symbol
from firmquant.persistence.repositories import canonical_json
from firmquant.strategy.snapshots import DecisionSnapshot


class ExecutionPlanningError(RuntimeError):
    """Raised when frozen strategy intent cannot be safely projected to broker facts."""


def _reject_constant(value: str) -> Never:
    raise ExecutionPlanningError(f"decision payload contains non-standard constant: {value}")


@dataclass(frozen=True, slots=True)
class ExecutionBrokerSnapshot:
    """Complete pre-execution account plus instrument and quote facts."""

    broker_snapshot: BrokerSnapshot
    instruments: tuple[InstrumentFact, ...]
    quotes: tuple[QuoteFact, ...]
    market_status: MarketSessionStatus

    def __post_init__(self) -> None:
        if not isinstance(self.broker_snapshot, BrokerSnapshot):
            raise DomainTypeError("execution broker snapshot must contain BrokerSnapshot")
        if not isinstance(self.instruments, tuple) or not all(
            isinstance(item, InstrumentFact) for item in self.instruments
        ):
            raise DomainTypeError("execution instruments must be a typed tuple")
        if not isinstance(self.quotes, tuple) or not all(
            isinstance(item, QuoteFact) for item in self.quotes
        ):
            raise DomainTypeError("execution quotes must be a typed tuple")
        if not isinstance(self.market_status, MarketSessionStatus):
            raise DomainTypeError("execution market status must be MarketSessionStatus")
        instrument_symbols = [item.symbol for item in self.instruments]
        quote_symbols = [item.symbol for item in self.quotes]
        if len(instrument_symbols) != len(set(instrument_symbols)):
            raise DomainValidationError("execution snapshot has duplicate instruments")
        if len(quote_symbols) != len(set(quote_symbols)):
            raise DomainValidationError("execution snapshot has duplicate quotes")
        session = self.broker_snapshot.session_date
        if any(item.session_date != session for item in self.instruments):
            raise DomainValidationError("execution instrument session mismatch")
        if any(item.session_date != session for item in self.quotes):
            raise DomainValidationError("execution quote session mismatch")
        if any(item.market_status is not self.market_status for item in self.quotes):
            raise DomainValidationError("execution quote market status mismatch")

    @property
    def sha256(self) -> str:
        payload = {
            "schema": "firmquant.execution-broker-snapshot.v1",
            "broker_snapshot_sha256": self.broker_snapshot.raw_payload_sha256,
            "session_date": self.broker_snapshot.session_date,
            "captured_at": self.broker_snapshot.captured_at,
            "market_status": self.market_status,
            "instruments": [
                {
                    "symbol": item.symbol,
                    "security_type": item.security_type,
                    "status": item.status,
                    "trading_unit": item.trading_unit,
                    "price_tick": item.price_tick,
                    "price_precision": item.price_precision,
                    "lower_limit": item.lower_limit,
                    "upper_limit": item.upper_limit,
                    "observed_at": item.observed_at,
                }
                for item in sorted(self.instruments, key=lambda fact: fact.symbol.canonical)
            ],
            "quotes": [
                {
                    "symbol": item.symbol,
                    "last_price": item.last_price,
                    "previous_close": item.previous_close,
                    "bid_price": item.bid_price,
                    "ask_price": item.ask_price,
                    "volume": item.volume,
                    "turnover": item.turnover,
                    "lower_limit": item.lower_limit,
                    "upper_limit": item.upper_limit,
                    "sequence": item.sequence,
                    "event_time": item.event_time,
                    "received_at": item.received_at,
                }
                for item in sorted(self.quotes, key=lambda fact: fact.symbol.canonical)
            ],
        }
        return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class PlanningBlocker:
    uquant_order_id: str
    symbol: str
    reason_code: str


@dataclass(frozen=True, slots=True)
class PlannedOrder:
    decision_id: str
    uquant_order_id: str
    symbol: Symbol
    side: Side
    target_weight: Decimal
    uquant_authorized_shares: Shares
    current_shares: Shares
    target_shares: Shares
    trading_unit: Shares
    limit_price: Price
    strategy_session: date
    execution_session: date
    uquant_source_sha: str
    reason_code: str

    def __post_init__(self) -> None:
        if not self.uquant_authorized_shares.is_positive:
            raise DomainValidationError("planned authorization must be positive")
        if self.uquant_authorized_shares.value > abs(
            self.target_shares.value - self.current_shares.value
        ) and not (self.side is Side.SELL and self.target_shares.value == 0):
            raise DomainValidationError("planned order expands target gap")


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    plan_id: str
    decision_id: str
    strategy_session: date
    execution_session: date
    broker_snapshot_sha256: str
    orders: tuple[PlannedOrder, ...]
    blockers: tuple[PlanningBlocker, ...]
    created_at: datetime


def _text(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ExecutionPlanningError(f"{label} must be canonical non-empty text")
    return value


def _weight(value: object) -> Decimal:
    if isinstance(value, bool):
        raise ExecutionPlanningError("target weight cannot be boolean")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    else:
        raise ExecutionPlanningError("target weight must be canonical JSON number")
    if not result.is_finite() or not Decimal(0) <= result <= Decimal(1):
        raise ExecutionPlanningError("target weight must be finite between zero and one")
    return result


class ExecutionPlanner:
    """Derive maximum share authorizations without changing uquant direction or target."""

    def plan(
        self,
        snapshot: DecisionSnapshot,
        broker_snapshot: ExecutionBrokerSnapshot,
    ) -> ExecutionPlan:
        if not isinstance(snapshot, DecisionSnapshot):
            raise DomainTypeError("execution planner requires DecisionSnapshot")
        if not isinstance(broker_snapshot, ExecutionBrokerSnapshot):
            raise DomainTypeError("execution planner requires ExecutionBrokerSnapshot")
        if broker_snapshot.broker_snapshot.session_date <= snapshot.strategy_session:
            raise ExecutionPlanningError("execution session must follow strategy session")
        try:
            payload: object = json.loads(
                snapshot.payload_json,
                parse_float=Decimal,
                parse_constant=_reject_constant,
            )
        except json.JSONDecodeError as error:
            raise ExecutionPlanningError("decision snapshot JSON cannot be parsed") from error
        if not isinstance(payload, dict):
            raise ExecutionPlanningError("decision snapshot root must be an object")
        pending = payload.get("pending_orders")
        sentinel = payload.get("sentinel")
        if not isinstance(pending, list):
            raise ExecutionPlanningError("decision pending_orders must be an array")
        if not isinstance(sentinel, dict):
            raise ExecutionPlanningError("decision sentinel evidence must be an object")
        freeze_new_risk = sentinel.get("freeze_new_risk")
        if not isinstance(freeze_new_risk, bool):
            raise ExecutionPlanningError("sentinel freeze_new_risk must be boolean")
        instruments = {item.symbol: item for item in broker_snapshot.instruments}
        quotes = {item.symbol: item for item in broker_snapshot.quotes}
        positions = {
            item.symbol: item for item in broker_snapshot.broker_snapshot.positions
        }
        broker_client_ids = {
            item.client_order_id
            for item in broker_snapshot.broker_snapshot.orders
            if item.client_order_id is not None
        }
        seen_order_ids: set[str] = set()
        planned: list[PlannedOrder] = []
        blockers: list[PlanningBlocker] = []
        for raw in pending:
            if not isinstance(raw, dict):
                raise ExecutionPlanningError("pending order item must be an object")
            order_id = _text(raw.get("order_id"), label="uquant order id")
            symbol_text = _text(raw.get("symbol"), label="uquant order symbol")
            if order_id in seen_order_ids:
                raise ExecutionPlanningError(f"duplicate uquant order id: {order_id}")
            seen_order_ids.add(order_id)
            try:
                symbol = Symbol.parse(symbol_text)
                side = Side(_text(raw.get("side"), label="uquant order side").upper())
            except (TypeError, ValueError) as error:
                raise ExecutionPlanningError(f"invalid uquant order identity: {order_id}") from error
            weight = _weight(raw.get("target_weight"))
            reason_code = _text(
                raw.get("reason_code"), label="uquant order reason code"
            )
            if order_id in broker_client_ids:
                blockers.append(
                    PlanningBlocker(order_id, symbol.canonical, "EXISTING_BROKER_ORDER")
                )
                continue
            if freeze_new_risk and side is Side.BUY:
                blockers.append(
                    PlanningBlocker(order_id, symbol.canonical, "SENTINEL_FREEZE_NEW_RISK")
                )
                continue
            instrument = instruments.get(symbol)
            quote = quotes.get(symbol)
            if instrument is None:
                blockers.append(
                    PlanningBlocker(order_id, symbol.canonical, "INSTRUMENT_FACT_MISSING")
                )
                continue
            if quote is None:
                blockers.append(PlanningBlocker(order_id, symbol.canonical, "QUOTE_FACT_MISSING"))
                continue
            if (
                instrument.security_type is not SecurityType.EQUITY
                or instrument.status is not SecurityStatus.TRADING
            ):
                blockers.append(
                    PlanningBlocker(order_id, symbol.canonical, "INSTRUMENT_NOT_TRADING")
                )
                continue
            if broker_snapshot.market_status not in {
                MarketSessionStatus.OPEN,
                MarketSessionStatus.AUCTION,
            }:
                blockers.append(
                    PlanningBlocker(order_id, symbol.canonical, "MARKET_NOT_TRADABLE")
                )
                continue
            reference = quote.ask_price if side is Side.BUY else quote.bid_price
            if reference is None:
                blockers.append(
                    PlanningBlocker(order_id, symbol.canonical, "REFERENCE_PRICE_MISSING")
                )
                continue
            if quote.lower_limit is None or quote.upper_limit is None:
                blockers.append(
                    PlanningBlocker(order_id, symbol.canonical, "PRICE_LIMIT_FACT_MISSING")
                )
                continue
            current = positions.get(symbol)
            current_shares = 0 if current is None else current.total_shares.value
            equity = broker_snapshot.broker_snapshot.account.total_assets.value
            unit = instrument.trading_unit.value
            desired_units = (
                weight * equity / reference.value / unit
            ).to_integral_value(rounding=ROUND_FLOOR)
            desired_shares = int(desired_units) * unit
            if side is Side.BUY:
                authorized = max(0, desired_shares - current_shares)
            else:
                authorized = (
                    current_shares
                    if weight == 0
                    else max(0, current_shares - desired_shares)
                )
                if authorized != current_shares:
                    authorized -= authorized % unit
            if authorized <= 0:
                blockers.append(
                    PlanningBlocker(order_id, symbol.canonical, "TARGET_ALREADY_SATISFIED")
                )
                continue
            expected_side = Side.BUY if desired_shares > current_shares else Side.SELL
            if weight == 0 and current_shares > 0:
                expected_side = Side.SELL
            if side is not expected_side:
                blockers.append(
                    PlanningBlocker(order_id, symbol.canonical, "UQUANT_DIRECTION_CONTRADICTION")
                )
                continue
            planned.append(
                PlannedOrder(
                    decision_id=snapshot.decision_id,
                    uquant_order_id=order_id,
                    symbol=symbol,
                    side=side,
                    target_weight=weight,
                    uquant_authorized_shares=Shares(authorized),
                    current_shares=Shares(current_shares),
                    target_shares=Shares(desired_shares),
                    trading_unit=instrument.trading_unit,
                    limit_price=reference,
                    strategy_session=snapshot.strategy_session,
                    execution_session=broker_snapshot.broker_snapshot.session_date,
                    uquant_source_sha=snapshot.uquant_commit,
                    reason_code=reason_code,
                )
            )
        planned.sort(key=lambda item: (item.side is not Side.SELL, item.symbol.canonical))
        blockers.sort(key=lambda item: (item.uquant_order_id, item.reason_code))
        identity = {
            "schema": "firmquant.execution-plan.v1",
            "decision_id": snapshot.decision_id,
            "strategy_session": snapshot.strategy_session,
            "execution_session": broker_snapshot.broker_snapshot.session_date,
            "broker_snapshot_sha256": broker_snapshot.sha256,
            "orders": [
                {
                    "uquant_order_id": item.uquant_order_id,
                    "symbol": item.symbol,
                    "side": item.side,
                    "target_weight": item.target_weight,
                    "authorized_shares": item.uquant_authorized_shares,
                    "limit_price": item.limit_price,
                }
                for item in planned
            ],
            "blockers": [
                {
                    "uquant_order_id": item.uquant_order_id,
                    "symbol": item.symbol,
                    "reason_code": item.reason_code,
                }
                for item in blockers
            ],
        }
        plan_id = "plan_" + hashlib.sha256(canonical_json(identity).encode()).hexdigest()
        return ExecutionPlan(
            plan_id=plan_id,
            decision_id=snapshot.decision_id,
            strategy_session=snapshot.strategy_session,
            execution_session=broker_snapshot.broker_snapshot.session_date,
            broker_snapshot_sha256=broker_snapshot.sha256,
            orders=tuple(planned),
            blockers=tuple(blockers),
            created_at=broker_snapshot.broker_snapshot.captured_at,
        )


__all__ = (
    "ExecutionBrokerSnapshot",
    "ExecutionPlan",
    "ExecutionPlanner",
    "ExecutionPlanningError",
    "PlannedOrder",
    "PlanningBlocker",
)
