"""Immutable per-session SHADOW and CANARY execution evidence."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Final

from firmquant.persistence.audit import AuditLedger
from firmquant.persistence.database import Database

_GIT_SHA: Final = re.compile(r"^[0-9a-f]{40}$")
_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
_ZERO = Decimal(0)
_ONE = Decimal(1)


class EvidenceConflictError(RuntimeError):
    """The same immutable session identity was observed with different content."""


class EvidenceStage(str, Enum):
    SHADOW = "SHADOW"
    CANARY = "CANARY"


class BlockerCode(str, Enum):
    TARGET_ALREADY_SATISFIED = "TARGET_ALREADY_SATISFIED"
    NON_TRADABLE = "NON_TRADABLE"
    PRICE_LIMIT = "PRICE_LIMIT"
    VOLUME_LIMIT = "VOLUME_LIMIT"
    STALE_QUOTE = "STALE_QUOTE"
    INSUFFICIENT_CASH = "INSUFFICIENT_CASH"
    INCOMPLETE_SELL = "INCOMPLETE_SELL"
    UNKNOWN = "UNKNOWN"
    EXTERNAL_ACTIVITY = "EXTERNAL_ACTIVITY"


def _digest(value: str, pattern: re.Pattern[str], *, label: str) -> None:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"{label} must be a canonical lowercase digest")


def _text(value: str | None, *, label: str, optional: bool = False) -> None:
    if optional and value is None:
        return
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be non-empty canonical text")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{label} contains control characters")


def _count(value: int, *, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")


def _decimal(
    value: Decimal,
    *,
    label: str,
    minimum: Decimal = _ZERO,
    maximum: Decimal | None = None,
    positive: bool = False,
) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{label} must be a finite Decimal")
    if value < minimum or (positive and value == minimum):
        raise ValueError(f"{label} is below its permitted bound")
    if maximum is not None and value > maximum:
        raise ValueError(f"{label} exceeds its permitted bound")


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _sha256(payload: object) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class EvidenceIdentity:
    stage: EvidenceStage
    execution_session: date
    firmquant_commit: str
    uquant_commit: str
    promotion_config_sha256: str
    account_sha256: str
    data_sha256: str
    calendar_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.stage, EvidenceStage):
            raise TypeError("evidence stage must be typed")
        if type(self.execution_session) is not date:
            raise TypeError("execution session must be a calendar date")
        _digest(self.firmquant_commit, _GIT_SHA, label="firmquant commit")
        _digest(self.uquant_commit, _GIT_SHA, label="uquant commit")
        for label, value in (
            ("promotion configuration", self.promotion_config_sha256),
            ("account", self.account_sha256),
            ("data", self.data_sha256),
            ("calendar", self.calendar_sha256),
        ):
            _digest(value, _SHA256, label=f"{label} SHA-256")

    @property
    def stable_payload(self) -> dict[str, object]:
        """Return the identity fields that define one immutable session observation."""

        return {
            "schema": "firmquant.execution-observation-identity.v1",
            "stage": self.stage.value,
            "execution_session": self.execution_session.isoformat(),
            "firmquant_commit": self.firmquant_commit,
            "uquant_commit": self.uquant_commit,
            "promotion_config_sha256": self.promotion_config_sha256,
            "account_sha256": self.account_sha256,
        }

    @property
    def sha256(self) -> str:
        return _sha256(self.stable_payload)

    def payload(self) -> dict[str, object]:
        return {
            **self.stable_payload,
            "data_sha256": self.data_sha256,
            "calendar_sha256": self.calendar_sha256,
        }


@dataclass(frozen=True, slots=True)
class OrderObservation:
    execution_id: str
    uquant_order_id: str
    symbol: str
    side: str
    planned_shares: int
    filled_shares: int
    reference_price: Decimal
    blocker: BlockerCode | None = None

    def __post_init__(self) -> None:
        for label, value in (
            ("execution id", self.execution_id),
            ("uquant order id", self.uquant_order_id),
            ("order symbol", self.symbol),
        ):
            _text(value, label=label)
        if self.side not in {"BUY", "SELL"}:
            raise ValueError("order side must be BUY or SELL")
        _count(self.planned_shares, label="planned shares")
        _count(self.filled_shares, label="filled shares")
        if self.planned_shares <= 0:
            raise ValueError("planned shares must be positive")
        if self.filled_shares > self.planned_shares:
            raise ValueError("filled shares exceed planned shares")
        _decimal(self.reference_price, label="order reference price", positive=True)
        if self.blocker is not None and not isinstance(self.blocker, BlockerCode):
            raise TypeError("order blocker must be typed")

    @property
    def unfilled_shares(self) -> int:
        return self.planned_shares - self.filled_shares

    def payload(self) -> dict[str, object]:
        return {
            "execution_id": self.execution_id,
            "uquant_order_id": self.uquant_order_id,
            "symbol": self.symbol,
            "side": self.side,
            "planned_shares": self.planned_shares,
            "filled_shares": self.filled_shares,
            "unfilled_shares": self.unfilled_shares,
            "reference_price": _decimal_text(self.reference_price),
            "blocker": None if self.blocker is None else self.blocker.value,
        }


@dataclass(frozen=True, slots=True)
class TargetObservation:
    symbol: str
    target_shares: int
    target_weight: Decimal
    reference_price: Decimal

    def __post_init__(self) -> None:
        _text(self.symbol, label="target symbol")
        _count(self.target_shares, label="target shares")
        _decimal(self.target_weight, label="target weight", maximum=_ONE)
        _decimal(self.reference_price, label="target reference price", positive=True)

    def payload(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "target_shares": self.target_shares,
            "target_weight": _decimal_text(self.target_weight),
            "reference_price": _decimal_text(self.reference_price),
        }


@dataclass(frozen=True, slots=True)
class FillObservation:
    fill_id: str | None
    execution_id: str
    symbol: str
    side: str
    shares: int
    price: Decimal
    commission: Decimal
    stamp_duty: Decimal
    transfer_fee: Decimal
    slippage: Decimal

    def __post_init__(self) -> None:
        _text(self.fill_id, label="fill id", optional=True)
        for label, value in (
            ("fill execution id", self.execution_id),
            ("fill symbol", self.symbol),
        ):
            _text(value, label=label)
        if self.side not in {"BUY", "SELL"}:
            raise ValueError("fill side must be BUY or SELL")
        _count(self.shares, label="fill shares")
        if self.shares <= 0:
            raise ValueError("fill shares must be positive")
        _decimal(self.price, label="fill price", positive=True)
        for label, value in (
            ("commission", self.commission),
            ("stamp duty", self.stamp_duty),
            ("transfer fee", self.transfer_fee),
            ("slippage", self.slippage),
        ):
            _decimal(value, label=label)

    def payload(self) -> dict[str, object]:
        return {
            "fill_id": self.fill_id,
            "execution_id": self.execution_id,
            "symbol": self.symbol,
            "side": self.side,
            "shares": self.shares,
            "price": _decimal_text(self.price),
            "commission": _decimal_text(self.commission),
            "stamp_duty": _decimal_text(self.stamp_duty),
            "transfer_fee": _decimal_text(self.transfer_fee),
            "slippage": _decimal_text(self.slippage),
        }


@dataclass(frozen=True, slots=True)
class PositionObservation:
    symbol: str
    shares: int

    def __post_init__(self) -> None:
        _text(self.symbol, label="position symbol")
        _count(self.shares, label="position shares")

    def payload(self) -> dict[str, object]:
        return {"symbol": self.symbol, "shares": self.shares}


def _unique_symbols(
    values: tuple[TargetObservation, ...] | tuple[PositionObservation, ...], *, label: str
) -> None:
    symbols = tuple(item.symbol for item in values)
    if len(set(symbols)) != len(symbols):
        raise ValueError(f"{label} contain duplicate symbols")


@dataclass(frozen=True, slots=True)
class ExecutionObservation:
    identity: EvidenceIdentity
    decision_id: str
    plan_id: str
    portfolio_equity: Decimal
    planned_orders: tuple[OrderObservation, ...]
    targets: tuple[TargetObservation, ...]
    fills: tuple[FillObservation, ...]
    actual_ending_positions: tuple[PositionObservation, ...]
    hypothetical_ending_positions: tuple[PositionObservation, ...]
    submit_count: int
    cancel_count: int
    rejection_count: int
    unknown_count: int
    external_activity: int
    duplicate_economic_orders: int
    duplicate_fills: int
    data_quality_failures: int
    created_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.identity, EvidenceIdentity):
            raise TypeError("observation identity must be typed")
        _text(self.decision_id, label="decision id")
        _text(self.plan_id, label="plan id")
        _decimal(self.portfolio_equity, label="portfolio equity", positive=True)
        for label, values, expected in (
            ("planned orders", self.planned_orders, OrderObservation),
            ("targets", self.targets, TargetObservation),
            ("fills", self.fills, FillObservation),
            ("actual ending positions", self.actual_ending_positions, PositionObservation),
            ("hypothetical ending positions", self.hypothetical_ending_positions, PositionObservation),
        ):
            if not isinstance(values, tuple) or any(not isinstance(item, expected) for item in values):
                raise TypeError(f"{label} must be a typed tuple")
        _unique_symbols(self.targets, label="targets")
        _unique_symbols(self.actual_ending_positions, label="actual ending positions")
        _unique_symbols(self.hypothetical_ending_positions, label="hypothetical ending positions")
        execution_ids = tuple(item.execution_id for item in self.planned_orders)
        if len(set(execution_ids)) != len(execution_ids):
            raise ValueError("planned orders contain duplicate execution ids")
        fill_ids = tuple(item.fill_id for item in self.fills if item.fill_id is not None)
        if len(set(fill_ids)) != len(fill_ids):
            raise ValueError("observation contains duplicate fill ids")
        for label, value in (
            ("submit count", self.submit_count),
            ("cancel count", self.cancel_count),
            ("rejection count", self.rejection_count),
            ("unknown count", self.unknown_count),
            ("external activity", self.external_activity),
            ("duplicate economic orders", self.duplicate_economic_orders),
            ("duplicate fills", self.duplicate_fills),
            ("data quality failures", self.data_quality_failures),
        ):
            _count(value, label=label)
        if (
            not isinstance(self.created_at, datetime)
            or self.created_at.tzinfo is None
            or self.created_at.utcoffset() is None
        ):
            raise ValueError("observation time must be timezone-aware")
        target_symbols = {target.symbol for target in self.targets}
        ending = (
            self.hypothetical_ending_positions
            if self.identity.stage is EvidenceStage.SHADOW
            else self.actual_ending_positions
        )
        if any(position.symbol not in target_symbols for position in ending):
            raise ValueError("ending positions must be covered by explicit targets")
        if self.identity.stage is EvidenceStage.SHADOW:
            if self.submit_count != 0 or self.cancel_count != 0:
                raise ValueError("SHADOW observation cannot contain broker write calls")
            if any(fill.fill_id is not None for fill in self.fills):
                raise ValueError("SHADOW fills must remain hypothetical")
        else:
            if any(fill.fill_id is None for fill in self.fills):
                raise ValueError("CANARY fills require real broker fill identifiers")
            if self.fills and self.submit_count == 0:
                raise ValueError("CANARY fills require a real submit count")

    def content_payload(self) -> dict[str, object]:
        return {
            "schema": "firmquant.execution-observation.v1",
            "identity": self.identity.payload(),
            "decision_id": self.decision_id,
            "plan_id": self.plan_id,
            "portfolio_equity": _decimal_text(self.portfolio_equity),
            "planned_orders": [item.payload() for item in self.planned_orders],
            "targets": [item.payload() for item in self.targets],
            "fills": [item.payload() for item in self.fills],
            "actual_ending_positions": [item.payload() for item in self.actual_ending_positions],
            "hypothetical_ending_positions": [item.payload() for item in self.hypothetical_ending_positions],
            "submit_count": self.submit_count,
            "cancel_count": self.cancel_count,
            "rejection_count": self.rejection_count,
            "unknown_count": self.unknown_count,
            "external_activity": self.external_activity,
            "duplicate_economic_orders": self.duplicate_economic_orders,
            "duplicate_fills": self.duplicate_fills,
            "data_quality_failures": self.data_quality_failures,
        }

    @property
    def content_sha256(self) -> str:
        return _sha256(self.content_payload())

    def payload(self) -> dict[str, object]:
        return {
            **self.content_payload(),
            "created_at": self.created_at.isoformat(),
            "content_sha256": self.content_sha256,
        }


@dataclass(frozen=True, slots=True)
class ExecutionEvidenceAggregate:
    stage: EvidenceStage
    observed_sessions: int
    order_count: int
    fill_count: int
    submit_count: int
    cancel_count: int
    rejection_count: int
    unresolved_count: int
    external_activity: int
    duplicate_economic_orders: int
    duplicate_fills: int
    data_quality_failures: int
    max_tracking_error: Decimal
    mean_tracking_error: Decimal
    notional_weighted_tracking_error: Decimal
    unfilled_notional: Decimal
    commissions: Decimal
    stamp_duty: Decimal
    transfer_fees: Decimal
    slippage_cost: Decimal
    partial_fill_count: int
    blocker_counts: dict[BlockerCode, int]
    latest_session: date
    latest_data_sha256: str
    latest_calendar_sha256: str

    @property
    def qualified_cleanliness(self) -> bool:
        return (
            self.unresolved_count == 0
            and self.external_activity == 0
            and self.duplicate_economic_orders == 0
            and self.duplicate_fills == 0
            and self.data_quality_failures == 0
        )


def _tracking_errors(observation: ExecutionObservation) -> tuple[list[Decimal], Decimal, Decimal]:
    ending_values = (
        observation.hypothetical_ending_positions
        if observation.identity.stage is EvidenceStage.SHADOW
        else observation.actual_ending_positions
    )
    ending = {item.symbol: item.shares for item in ending_values}
    errors: list[Decimal] = []
    weighted_error = _ZERO
    weight_notional = _ZERO
    for target in observation.targets:
        actual_shares = ending.get(target.symbol, 0)
        actual_notional = Decimal(actual_shares) * target.reference_price
        actual_weight = actual_notional / observation.portfolio_equity
        error = abs(target.target_weight - actual_weight)
        errors.append(error)
        target_notional = Decimal(target.target_shares) * target.reference_price
        symbol_notional = max(target_notional, actual_notional)
        weighted_error += error * symbol_notional
        weight_notional += symbol_notional
    return errors, weighted_error, weight_notional


def aggregate_observations(observations: tuple[ExecutionObservation, ...]) -> ExecutionEvidenceAggregate:
    if not isinstance(observations, tuple) or not observations:
        raise ValueError("execution evidence aggregation requires observations")
    if any(not isinstance(item, ExecutionObservation) for item in observations):
        raise TypeError("execution evidence aggregation requires typed observations")
    first = observations[0]
    for observation in observations[1:]:
        if observation.identity.stage is not first.identity.stage:
            raise ValueError("SHADOW and CANARY evidence must aggregate independently")
        if (
            observation.identity.firmquant_commit != first.identity.firmquant_commit
            or observation.identity.uquant_commit != first.identity.uquant_commit
            or observation.identity.promotion_config_sha256 != first.identity.promotion_config_sha256
            or observation.identity.account_sha256 != first.identity.account_sha256
        ):
            raise ValueError("execution evidence deployment identity changed inside aggregate")
    if len({item.identity.execution_session for item in observations}) != len(observations):
        raise ValueError("execution evidence contains duplicate sessions")

    ordered = tuple(sorted(observations, key=lambda item: item.identity.execution_session))
    errors: list[Decimal] = []
    weighted_error = _ZERO
    weighted_notional = _ZERO
    unfilled_notional = _ZERO
    commissions = _ZERO
    stamp_duty = _ZERO
    transfer_fees = _ZERO
    slippage_cost = _ZERO
    blocker_counts = {code: 0 for code in BlockerCode}
    partial_fill_count = 0
    for observation in ordered:
        session_errors, session_weighted, session_notional = _tracking_errors(observation)
        errors.extend(session_errors)
        weighted_error += session_weighted
        weighted_notional += session_notional
        for order in observation.planned_orders:
            unfilled_notional += Decimal(order.unfilled_shares) * order.reference_price
            if 0 < order.filled_shares < order.planned_shares:
                partial_fill_count += 1
            if order.blocker is not None:
                blocker_counts[order.blocker] += 1
        for fill in observation.fills:
            commissions += fill.commission
            stamp_duty += fill.stamp_duty
            transfer_fees += fill.transfer_fee
            slippage_cost += fill.slippage

    latest = ordered[-1]
    maximum = max(errors, default=_ZERO)
    mean = _ZERO if not errors else sum(errors, start=_ZERO) / Decimal(len(errors))
    notional_weighted = _ZERO if weighted_notional == 0 else weighted_error / weighted_notional
    return ExecutionEvidenceAggregate(
        stage=first.identity.stage,
        observed_sessions=len(ordered),
        order_count=sum(len(item.planned_orders) for item in ordered),
        fill_count=sum(len(item.fills) for item in ordered),
        submit_count=sum(item.submit_count for item in ordered),
        cancel_count=sum(item.cancel_count for item in ordered),
        rejection_count=sum(item.rejection_count for item in ordered),
        unresolved_count=sum(item.unknown_count for item in ordered),
        external_activity=sum(item.external_activity for item in ordered),
        duplicate_economic_orders=sum(item.duplicate_economic_orders for item in ordered),
        duplicate_fills=sum(item.duplicate_fills for item in ordered),
        data_quality_failures=sum(item.data_quality_failures for item in ordered),
        max_tracking_error=maximum,
        mean_tracking_error=mean,
        notional_weighted_tracking_error=notional_weighted,
        unfilled_notional=unfilled_notional,
        commissions=commissions,
        stamp_duty=stamp_duty,
        transfer_fees=transfer_fees,
        slippage_cost=slippage_cost,
        partial_fill_count=partial_fill_count,
        blocker_counts=blocker_counts,
        latest_session=latest.identity.execution_session,
        latest_data_sha256=latest.identity.data_sha256,
        latest_calendar_sha256=latest.identity.calendar_sha256,
    )


class ExecutionEvidenceStore:
    """Store one immutable observation per stable deployment/session identity in the audit ledger."""

    def __init__(self, database: Database) -> None:
        if not isinstance(database, Database):
            raise TypeError("execution evidence store requires Database")
        self._database = database
        self._audit = AuditLedger(database)

    @staticmethod
    def _event_id(observation: ExecutionObservation) -> str:
        return "execution-observation:" + observation.identity.sha256

    def append(self, observation: ExecutionObservation) -> bool:
        if not isinstance(observation, ExecutionObservation):
            raise TypeError("execution evidence store requires ExecutionObservation")
        event_id = self._event_id(observation)
        existing = self._database.query_one(
            "SELECT payload_json FROM audit_events WHERE audit_event_id = ?",
            (event_id,),
        )
        if existing is not None:
            try:
                payload = json.loads(str(existing["payload_json"]))
            except json.JSONDecodeError as error:
                raise EvidenceConflictError("stored execution observation is invalid") from error
            if not isinstance(payload, dict) or payload.get("content_sha256") != observation.content_sha256:
                raise EvidenceConflictError("execution observation identity has conflicting content")
            return False

        def write() -> None:
            self._audit.append(
                audit_event_id=event_id,
                category="EXECUTION_OBSERVATION",
                actor="execution-evidence",
                payload=observation.payload(),
                created_at=observation.created_at,
            )

        if self._database.in_transaction:
            write()
        else:
            with self._database.transaction():
                write()
        return True


__all__ = (
    "BlockerCode",
    "EvidenceConflictError",
    "EvidenceIdentity",
    "EvidenceStage",
    "ExecutionEvidenceAggregate",
    "ExecutionEvidenceStore",
    "ExecutionObservation",
    "FillObservation",
    "OrderObservation",
    "PositionObservation",
    "TargetObservation",
    "aggregate_observations",
)
