"""Identity-bound promotion aggregation derived from immutable session observations."""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import cast

from firmquant.application.execution_evidence import (
    BlockerCode,
    EvidenceIdentity,
    EvidenceStage,
    ExecutionEvidenceAggregate,
    ExecutionEvidenceStore,
    ExecutionObservation,
    FillObservation,
    OrderObservation,
    PlanningBlockerObservation,
    PositionObservation,
    TargetObservation,
    aggregate_observations,
)
from firmquant.persistence.database import Database


class PromotionStoreError(RuntimeError):
    """Stored promotion evidence is malformed, contradictory, or incomplete."""


def _mapping(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise PromotionStoreError(f"{label} must be a JSON object")
    return cast(dict[str, object], value)


def _array(value: object, *, label: str) -> list[object]:
    if not isinstance(value, list):
        raise PromotionStoreError(f"{label} must be a JSON array")
    return value


def _text(value: object, *, label: str, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value or value != value.strip():
        raise PromotionStoreError(f"{label} must be canonical text")
    return value


def _integer(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PromotionStoreError(f"{label} must be a nonnegative integer")
    return value


def _decimal(value: object, *, label: str) -> Decimal:
    if not isinstance(value, str):
        raise PromotionStoreError(f"{label} must be canonical Decimal text")
    try:
        observed = Decimal(value)
    except InvalidOperation as error:
        raise PromotionStoreError(f"{label} is not Decimal text") from error
    if not observed.is_finite():
        raise PromotionStoreError(f"{label} must be finite")
    return observed


def _stage(value: object) -> EvidenceStage:
    try:
        return EvidenceStage(str(value))
    except ValueError as error:
        raise PromotionStoreError("stored evidence stage is invalid") from error


def _blocker(value: object) -> BlockerCode | None:
    if value is None:
        return None
    try:
        return BlockerCode(str(value))
    except ValueError as error:
        raise PromotionStoreError("stored evidence blocker is invalid") from error


def _decode_identity(payload: object) -> EvidenceIdentity:
    value = _mapping(payload, label="execution observation identity")
    if value.get("schema") != "firmquant.execution-observation-identity.v1":
        raise PromotionStoreError("execution observation identity schema is invalid")
    try:
        execution_session = date.fromisoformat(
            cast(str, _text(value.get("execution_session"), label="session"))
        )
    except ValueError as error:
        raise PromotionStoreError("execution observation session is invalid") from error
    return EvidenceIdentity(
        stage=_stage(value.get("stage")),
        execution_session=execution_session,
        firmquant_commit=cast(str, _text(value.get("firmquant_commit"), label="firmquant commit")),
        uquant_commit=cast(str, _text(value.get("uquant_commit"), label="uquant commit")),
        promotion_config_sha256=cast(
            str,
            _text(value.get("promotion_config_sha256"), label="promotion configuration"),
        ),
        account_sha256=cast(str, _text(value.get("account_sha256"), label="account hash")),
        data_sha256=cast(str, _text(value.get("data_sha256"), label="data hash")),
        calendar_sha256=cast(str, _text(value.get("calendar_sha256"), label="calendar hash")),
    )


def _decode_order(payload: object) -> OrderObservation:
    value = _mapping(payload, label="execution order observation")
    return OrderObservation(
        execution_id=cast(str, _text(value.get("execution_id"), label="execution id")),
        uquant_order_id=cast(str, _text(value.get("uquant_order_id"), label="uquant order id")),
        symbol=cast(str, _text(value.get("symbol"), label="order symbol")),
        side=cast(str, _text(value.get("side"), label="order side")),
        planned_shares=_integer(value.get("planned_shares"), label="planned shares"),
        filled_shares=_integer(value.get("filled_shares"), label="filled shares"),
        reference_price=_decimal(value.get("reference_price"), label="order reference price"),
        blocker=_blocker(value.get("blocker")),
    )


def _decode_planning_blocker(payload: object) -> PlanningBlockerObservation:
    value = _mapping(payload, label="planning blocker observation")
    return PlanningBlockerObservation(
        uquant_order_id=cast(str, _text(value.get("uquant_order_id"), label="blocker order id")),
        symbol=cast(str, _text(value.get("symbol"), label="blocker symbol")),
        reason_code=cast(str, _text(value.get("reason_code"), label="blocker reason code")),
    )


def _decode_target(payload: object) -> TargetObservation:
    value = _mapping(payload, label="target observation")
    return TargetObservation(
        symbol=cast(str, _text(value.get("symbol"), label="target symbol")),
        target_shares=_integer(value.get("target_shares"), label="target shares"),
        target_weight=_decimal(value.get("target_weight"), label="target weight"),
        reference_price=_decimal(value.get("reference_price"), label="target reference price"),
    )


def _decode_fill(payload: object) -> FillObservation:
    value = _mapping(payload, label="fill observation")
    return FillObservation(
        fill_id=_text(value.get("fill_id"), label="fill id", optional=True),
        execution_id=cast(str, _text(value.get("execution_id"), label="fill execution id")),
        symbol=cast(str, _text(value.get("symbol"), label="fill symbol")),
        side=cast(str, _text(value.get("side"), label="fill side")),
        shares=_integer(value.get("shares"), label="fill shares"),
        price=_decimal(value.get("price"), label="fill price"),
        commission=_decimal(value.get("commission"), label="commission"),
        stamp_duty=_decimal(value.get("stamp_duty"), label="stamp duty"),
        transfer_fee=_decimal(value.get("transfer_fee"), label="transfer fee"),
        slippage=_decimal(value.get("slippage"), label="slippage"),
    )


def _decode_position(payload: object) -> PositionObservation:
    value = _mapping(payload, label="position observation")
    return PositionObservation(
        symbol=cast(str, _text(value.get("symbol"), label="position symbol")),
        shares=_integer(value.get("shares"), label="position shares"),
    )


def _decode_observation(payload: object) -> ExecutionObservation:
    value = _mapping(payload, label="execution observation")
    if value.get("schema") != "firmquant.execution-observation.v1":
        raise PromotionStoreError("execution observation schema is invalid")
    try:
        created_at = datetime.fromisoformat(cast(str, _text(value.get("created_at"), label="created at")))
    except ValueError as error:
        raise PromotionStoreError("execution observation timestamp is invalid") from error
    observation = ExecutionObservation(
        identity=_decode_identity(value.get("identity")),
        decision_id=cast(str, _text(value.get("decision_id"), label="decision id")),
        plan_id=cast(str, _text(value.get("plan_id"), label="plan id")),
        portfolio_equity=_decimal(value.get("portfolio_equity"), label="portfolio equity"),
        planned_orders=tuple(
            _decode_order(item) for item in _array(value.get("planned_orders"), label="planned orders")
        ),
        planning_blockers=tuple(
            _decode_planning_blocker(item)
            for item in _array(value.get("planning_blockers"), label="planning blockers")
        ),
        targets=tuple(_decode_target(item) for item in _array(value.get("targets"), label="targets")),
        fills=tuple(_decode_fill(item) for item in _array(value.get("fills"), label="fills")),
        actual_ending_positions=tuple(
            _decode_position(item)
            for item in _array(value.get("actual_ending_positions"), label="actual ending positions")
        ),
        hypothetical_ending_positions=tuple(
            _decode_position(item)
            for item in _array(
                value.get("hypothetical_ending_positions"),
                label="hypothetical ending positions",
            )
        ),
        submit_count=_integer(value.get("submit_count"), label="submit count"),
        cancel_count=_integer(value.get("cancel_count"), label="cancel count"),
        rejection_count=_integer(value.get("rejection_count"), label="rejection count"),
        unknown_count=_integer(value.get("unknown_count"), label="unknown count"),
        external_activity=_integer(value.get("external_activity"), label="external activity"),
        duplicate_economic_orders=_integer(
            value.get("duplicate_economic_orders"), label="duplicate economic orders"
        ),
        duplicate_fills=_integer(value.get("duplicate_fills"), label="duplicate fills"),
        data_quality_failures=_integer(value.get("data_quality_failures"), label="data quality failures"),
        created_at=created_at,
    )
    if value.get("content_sha256") != observation.content_sha256:
        raise PromotionStoreError("execution observation digest mismatch")
    return observation


class PromotionStore:
    """Query exact identity observations and derive SHADOW/CANARY qualification."""

    def __init__(self, database: Database) -> None:
        if not isinstance(database, Database):
            raise TypeError("promotion store requires Database")
        self._database = database
        self._observations = ExecutionEvidenceStore(database)

    def append(self, observation: ExecutionObservation) -> bool:
        return self._observations.append(observation)

    def observations(
        self,
        *,
        stage: EvidenceStage,
        firmquant_commit: str,
        uquant_commit: str,
        config_sha256: str,
        account_hash: str,
    ) -> tuple[ExecutionObservation, ...]:
        if not isinstance(stage, EvidenceStage):
            raise TypeError("promotion evidence stage must be typed")
        rows = self._database.query_all(
            "SELECT payload_json FROM audit_events WHERE category = 'EXECUTION_OBSERVATION' ORDER BY sequence"
        )
        matched: list[ExecutionObservation] = []
        for row in rows:
            try:
                raw: object = json.loads(str(row["payload_json"]), parse_float=Decimal)
            except (json.JSONDecodeError, ValueError) as error:
                raise PromotionStoreError("stored execution observation is invalid JSON") from error
            observation = _decode_observation(raw)
            identity = observation.identity
            if (
                identity.stage is stage
                and identity.firmquant_commit == firmquant_commit
                and identity.uquant_commit == uquant_commit
                and identity.promotion_config_sha256 == config_sha256
                and identity.account_sha256 == account_hash
            ):
                matched.append(observation)
        return tuple(matched)

    def aggregate(
        self,
        *,
        stage: EvidenceStage,
        firmquant_commit: str,
        uquant_commit: str,
        config_sha256: str,
        account_hash: str,
    ) -> ExecutionEvidenceAggregate | None:
        observations = self.observations(
            stage=stage,
            firmquant_commit=firmquant_commit,
            uquant_commit=uquant_commit,
            config_sha256=config_sha256,
            account_hash=account_hash,
        )
        return None if not observations else aggregate_observations(observations)

    def qualifies(
        self,
        *,
        stage: EvidenceStage,
        firmquant_commit: str,
        uquant_commit: str,
        config_sha256: str,
        account_hash: str,
        min_sessions: int,
        min_orders: int,
        max_tracking_error: Decimal,
        min_fills: int = 0,
    ) -> bool:
        for label, value in (
            ("minimum sessions", min_sessions),
            ("minimum orders", min_orders),
            ("minimum fills", min_fills),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{label} must be a nonnegative integer")
        if not isinstance(max_tracking_error, Decimal) or not max_tracking_error.is_finite():
            raise TypeError("maximum tracking error must be finite Decimal")
        aggregate = self.aggregate(
            stage=stage,
            firmquant_commit=firmquant_commit,
            uquant_commit=uquant_commit,
            config_sha256=config_sha256,
            account_hash=account_hash,
        )
        if aggregate is None:
            return False
        if not aggregate.qualified_cleanliness:
            return False
        if stage is EvidenceStage.CANARY and aggregate.rejection_count != 0:
            return False
        return (
            aggregate.observed_sessions >= min_sessions
            and aggregate.order_count >= min_orders
            and aggregate.fill_count >= min_fills
            and aggregate.max_tracking_error <= max_tracking_error
        )


__all__ = ("PromotionStore", "PromotionStoreError")
