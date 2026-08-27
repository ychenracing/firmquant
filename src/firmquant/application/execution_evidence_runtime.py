"""Production construction of immutable SHADOW and CANARY execution observations."""

from __future__ import annotations

import hashlib
import importlib
import json
from collections.abc import Mapping
from datetime import date, datetime
from decimal import ROUND_FLOOR, Decimal
from typing import Protocol, cast

from firmquant.application.execution_evidence import (
    BlockerCode,
    EvidenceIdentity,
    EvidenceStage,
    ExecutionObservation,
    FillObservation,
    OrderObservation,
    PlanningBlockerObservation,
    PositionObservation,
    TargetObservation,
)
from firmquant.broker.gateway import BrokerGateway, BrokerOrderCommand
from firmquant.broker.paper import PaperBroker
from firmquant.broker.xtquant_safety import XtQuantSafetyManifest
from firmquant.domain.broker_facts import (
    BrokerOrderStatus,
    BrokerSnapshot,
    InstrumentFact,
    PriceType,
    QuoteFact,
    Side,
)
from firmquant.domain.values import Symbol
from firmquant.execution.planner import ExecutionBrokerSnapshot, ExecutionPlan, PlannedOrder
from firmquant.execution.policy import ExecutionPolicy, FeeSchedule, FillModel
from firmquant.persistence.audit import AuditLedger
from firmquant.persistence.database import Database
from firmquant.persistence.repositories import canonical_sha256
from firmquant.strategy.snapshots import DecisionSnapshot

_ZERO = Decimal(0)


class RuntimeEvidenceError(RuntimeError):
    """Production execution evidence could not be proven complete and causal."""


class _UquantExecutionConfig(Protocol):
    max_volume_participation: float
    slippage: float


def _uquant_execution_config() -> _UquantExecutionConfig:
    module = importlib.import_module("uquant.config")
    config = vars(module).get("DEFAULT_CONFIG")
    if config is None:
        raise RuntimeEvidenceError("locked uquant execution configuration is unavailable")
    return cast(_UquantExecutionConfig, config)


def shadow_execution_policy(manifest: XtQuantSafetyManifest) -> ExecutionPolicy:
    """Use the live fee contract and locked uquant participation/slippage assumptions."""

    if not isinstance(manifest, XtQuantSafetyManifest):
        raise TypeError("SHADOW execution policy requires XtQuantSafetyManifest")
    config = _uquant_execution_config()
    return ExecutionPolicy(
        fee_schedule=FeeSchedule(
            commission_rate=manifest.commission_rate,
            minimum_commission=manifest.minimum_commission,
            stamp_duty_rate=manifest.stamp_duty_rate,
            transfer_fee_rate=manifest.transfer_fee_rate,
            fee_quantum=Decimal("0.0001"),
        ),
        fill_model=FillModel(
            max_volume_participation=Decimal(str(config.max_volume_participation)),
            slippage_bps=Decimal(str(config.slippage)) * Decimal("10000"),
        ),
    )


def _stable_execution_id(plan_id: str, order_id: str) -> str:
    digest = hashlib.sha256(f"shadow\0{plan_id}\0{order_id}".encode()).hexdigest()
    return "exec_" + digest


def _idempotency_key(plan_id: str, order: PlannedOrder) -> str:
    payload = {
        "schema": "firmquant.shadow-order-identity.v1",
        "plan_id": plan_id,
        "uquant_order_id": order.uquant_order_id,
        "symbol": order.symbol.canonical,
        "side": order.side.value,
        "shares": order.authorized_shares.value,
        "limit_price": order.limit_price.canonical,
        "strategy_session": order.strategy_session.isoformat(),
        "execution_session": order.execution_session.isoformat(),
    }
    return canonical_sha256(payload)


def _blocker_from_reason(reason: str | None, *, partial: bool = False) -> BlockerCode | None:
    if partial:
        return BlockerCode.VOLUME_LIMIT
    if reason is None or reason in {"FILLED", "ACKNOWLEDGED"}:
        return None
    if reason in {"CASH_INSUFFICIENT"}:
        return BlockerCode.INSUFFICIENT_CASH
    if reason in {"VOLUME_CAPACITY_EXHAUSTED"}:
        return BlockerCode.VOLUME_LIMIT
    if reason in {
        "UPPER_LIMIT_BUY_BLOCKED",
        "LOWER_LIMIT_SELL_BLOCKED",
        "LIMIT_PRICE_OUT_OF_BOUNDS",
        "FILL_PRICE_OUT_OF_BOUNDS",
        "ORDER_NOT_MARKETABLE",
        "SLIPPAGE_EXCEEDS_LIMIT",
    }:
        return BlockerCode.PRICE_LIMIT
    if reason in {
        "INSTRUMENT_NOT_TRADING",
        "MARKET_NOT_OPEN",
        "T1_SELLABLE_EXCEEDED",
        "POSITION_INSUFFICIENT",
        "BID_LIQUIDITY_MISSING",
        "ASK_LIQUIDITY_MISSING",
    }:
        return BlockerCode.NON_TRADABLE
    if reason in {
        "MARKET_FACT_SESSION_MISMATCH",
        "PRICE_LIMIT_FACT_MISSING",
        "PRICE_LIMIT_FACT_MISMATCH",
        "LIMIT_PRICE_PRECISION_INVALID",
        "LIMIT_PRICE_TICK_INVALID",
        "TRADING_UNIT_INVALID",
    }:
        return BlockerCode.STALE_QUOTE
    return BlockerCode.UNKNOWN


def _price_for(symbol: Symbol, quotes: Mapping[Symbol, QuoteFact], planned: PlannedOrder | None) -> Decimal:
    if planned is not None:
        return planned.limit_price.value
    quote = quotes.get(symbol)
    if quote is None:
        raise RuntimeEvidenceError(f"reference quote is unavailable for {symbol.canonical}")
    candidate = quote.last_price or quote.bid_price or quote.ask_price or quote.previous_close
    if candidate is None:
        raise RuntimeEvidenceError(f"reference price is unavailable for {symbol.canonical}")
    return candidate.value


def _target_observations(
    *,
    decision: DecisionSnapshot,
    plan: ExecutionPlan,
    instruments: Mapping[Symbol, InstrumentFact],
    quotes: Mapping[Symbol, QuoteFact],
    positions: tuple[PositionObservation, ...],
    portfolio_equity: Decimal,
) -> tuple[TargetObservation, ...]:
    if portfolio_equity <= 0:
        raise RuntimeEvidenceError("target tracking requires positive portfolio equity")
    planned = {item.symbol: item for item in plan.orders}
    targets: dict[Symbol, TargetObservation] = {}
    raw_targets = decision.uquant_payload.get("targets")
    if not isinstance(raw_targets, list):
        raise RuntimeEvidenceError("decision targets are unavailable")
    for item in raw_targets:
        if not isinstance(item, dict) or not isinstance(item.get("symbol"), str):
            raise RuntimeEvidenceError("decision target payload is malformed")
        raw_weight = item.get("weight")
        if isinstance(raw_weight, bool) or not isinstance(raw_weight, (int, float, str)):
            raise RuntimeEvidenceError("decision target weight is malformed")
        weight = Decimal(str(raw_weight))
        if not weight.is_finite() or weight < 0 or weight > 1:
            raise RuntimeEvidenceError("decision target weight is outside bounds")
        symbol = Symbol.parse(str(item["symbol"]))
        planned_order = planned.get(symbol)
        reference = _price_for(symbol, quotes, planned_order)
        instrument = instruments.get(symbol)
        if instrument is None:
            raise RuntimeEvidenceError(f"instrument metadata is unavailable for {symbol.canonical}")
        if planned_order is not None:
            target_shares = planned_order.target_shares.value
        else:
            unit = instrument.trading_unit.value
            raw = int((portfolio_equity * weight / reference).to_integral_value(rounding=ROUND_FLOOR))
            target_shares = raw - raw % unit
        targets[symbol] = TargetObservation(
            symbol=symbol.canonical,
            target_shares=target_shares,
            target_weight=weight,
            reference_price=reference,
        )
    for order in plan.orders:
        if order.symbol in targets:
            continue
        targets[order.symbol] = TargetObservation(
            symbol=order.symbol.canonical,
            target_shares=order.target_shares.value,
            target_weight=order.target_weight,
            reference_price=order.limit_price.value,
        )
    for position in positions:
        symbol = Symbol.parse(position.symbol)
        if symbol in targets:
            continue
        targets[symbol] = TargetObservation(
            symbol=symbol.canonical,
            target_shares=0,
            target_weight=_ZERO,
            reference_price=_price_for(symbol, quotes, planned.get(symbol)),
        )
    return tuple(targets[symbol] for symbol in sorted(targets, key=lambda value: value.canonical))


def _planning_blockers(plan: ExecutionPlan) -> tuple[PlanningBlockerObservation, ...]:
    return tuple(
        PlanningBlockerObservation(
            uquant_order_id=item.uquant_order_id,
            symbol=item.symbol,
            reason_code=item.reason_code,
        )
        for item in plan.blockers
    )


def _positions(values: tuple[object, ...]) -> tuple[PositionObservation, ...]:
    observed: list[PositionObservation] = []
    for item in values:
        symbol = getattr(item, "symbol", None)
        shares = getattr(item, "total_shares", None)
        canonical = getattr(symbol, "canonical", None)
        value = getattr(shares, "value", None)
        if not isinstance(canonical, str) or isinstance(value, bool) or not isinstance(value, int):
            raise RuntimeEvidenceError("broker position fact is malformed")
        if value > 0:
            observed.append(PositionObservation(symbol=canonical, shares=value))
    return tuple(sorted(observed, key=lambda item: item.symbol))


def _market_facts(
    broker: BrokerGateway,
    facts: ExecutionBrokerSnapshot,
    decision: DecisionSnapshot,
) -> tuple[dict[Symbol, InstrumentFact], dict[Symbol, QuoteFact]]:
    instruments = {item.symbol: item for item in facts.instruments}
    quotes = {item.symbol: item for item in facts.quotes}
    symbols = {item.symbol for item in facts.broker_snapshot.positions}
    for raw in decision.uquant_payload.get("targets", []):
        if isinstance(raw, dict) and isinstance(raw.get("symbol"), str):
            symbols.add(Symbol.parse(str(raw["symbol"])))
    for symbol in sorted(symbols, key=lambda value: value.canonical):
        if symbol not in instruments:
            instruments[symbol] = broker.query_instrument(symbol)
        if symbol not in quotes:
            quotes[symbol] = broker.query_quote(symbol)
    return instruments, quotes


def _duplicate_economic_orders(database: Database) -> int:
    value = database.scalar(
        "SELECT count(*) FROM (SELECT decision_id,uquant_order_id FROM execution_intents "
        "GROUP BY decision_id,uquant_order_id HAVING count(*) > 1)"
    )
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeEvidenceError("duplicate economic order count is invalid")
    return value


def _duplicate_fills(database: Database, *, session: date) -> int:
    value = database.scalar(
        "SELECT count(*) FROM (SELECT broker_order_id,symbol,side,shares,price,session_date,event_time "
        "FROM fills WHERE session_date = ? GROUP BY broker_order_id,symbol,side,shares,price,session_date,event_time "
        "HAVING count(*) > 1)",
        (session.isoformat(),),
    )
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeEvidenceError("duplicate fill count is invalid")
    return value


def _known_client_ids(database: Database) -> frozenset[str]:
    return frozenset(
        str(row["uquant_order_id"])
        for row in database.query_all("SELECT uquant_order_id FROM execution_intents")
    )


def _external_activity(database: Database, broker: BrokerGateway, *, session: date) -> int:
    known = _known_client_ids(database)
    return sum(
        1
        for item in broker.query_orders()
        if item.session_date == session
        and (item.client_order_id is None or item.client_order_id not in known)
    )


def build_shadow_observation(
    *,
    database: Database,
    broker: BrokerGateway,
    facts: ExecutionBrokerSnapshot,
    plan: ExecutionPlan,
    decision: DecisionSnapshot,
    firmquant_commit: str,
    uquant_commit: str,
    promotion_config_sha256: str,
    calendar_sha256: str,
    safety_manifest: XtQuantSafetyManifest,
    created_at: datetime,
) -> ExecutionObservation:
    """Run the production policy against an isolated PaperBroker; never call the real broker write surface."""

    if facts.broker_snapshot.session_date != plan.execution_session:
        raise RuntimeEvidenceError("SHADOW plan and broker snapshot sessions differ")
    instruments, quotes = _market_facts(broker, facts, decision)
    paper = PaperBroker(
        account=facts.broker_snapshot.account,
        positions=facts.broker_snapshot.positions,
        instruments=tuple(instruments.values()),
        quotes=tuple(quotes.values()),
        market_status=facts.market_status,
        policy=shadow_execution_policy(safety_manifest),
        clock=lambda: created_at,
    )
    paper.connect()
    order_observations: list[OrderObservation] = []
    fill_observations: list[FillObservation] = []
    incomplete_sell = False
    try:
        for planned in plan.orders:
            if planned.side is Side.BUY and incomplete_sell:
                order_observations.append(
                    OrderObservation(
                        execution_id=_stable_execution_id(plan.plan_id, planned.uquant_order_id),
                        uquant_order_id=planned.uquant_order_id,
                        symbol=planned.symbol.canonical,
                        side=planned.side.value,
                        planned_shares=planned.authorized_shares.value,
                        filled_shares=0,
                        reference_price=planned.limit_price.value,
                        blocker=BlockerCode.INCOMPLETE_SELL,
                    )
                )
                continue
            execution_id = _stable_execution_id(plan.plan_id, planned.uquant_order_id)
            command = BrokerOrderCommand(
                execution_id=execution_id,
                idempotency_key=_idempotency_key(plan.plan_id, planned),
                client_order_id=planned.uquant_order_id,
                symbol=planned.symbol,
                side=planned.side,
                price_type=PriceType.LIMIT,
                requested_shares=planned.authorized_shares,
                limit_price=planned.limit_price,
                strategy_session=planned.execution_session,
            )
            result = paper.submit_order(command)
            filled = result.filled_shares.value
            reason = paper.reason_for(result.broker_order_id)
            blocker = _blocker_from_reason(
                reason,
                partial=0 < filled < planned.authorized_shares.value,
            )
            order_observations.append(
                OrderObservation(
                    execution_id=execution_id,
                    uquant_order_id=planned.uquant_order_id,
                    symbol=planned.symbol.canonical,
                    side=planned.side.value,
                    planned_shares=planned.authorized_shares.value,
                    filled_shares=filled,
                    reference_price=planned.limit_price.value,
                    blocker=blocker,
                )
            )
            for fill in paper.query_fills():
                if fill.broker_order_id != result.broker_order_id:
                    continue
                fill_observations.append(
                    FillObservation(
                        fill_id=None,
                        execution_id=execution_id,
                        symbol=fill.symbol.canonical,
                        side=fill.side.value,
                        shares=fill.shares.value,
                        price=fill.price.value,
                        commission=fill.commission.value,
                        stamp_duty=fill.stamp_duty.value,
                        transfer_fee=fill.transfer_fee.value,
                        slippage=abs(fill.price.value - planned.limit_price.value)
                        * Decimal(fill.shares.value),
                    )
                )
            if planned.side is Side.SELL and result.status is not BrokerOrderStatus.FILLED:
                incomplete_sell = True
        hypothetical = _positions(cast(tuple[object, ...], paper.query_positions()))
    finally:
        paper.disconnect()
    actual = _positions(cast(tuple[object, ...], facts.broker_snapshot.positions))
    targets = _target_observations(
        decision=decision,
        plan=plan,
        instruments=instruments,
        quotes=quotes,
        positions=tuple({item.symbol: item for item in (*actual, *hypothetical)}.values()),
        portfolio_equity=facts.broker_snapshot.account.total_assets.value,
    )
    unresolved = database.scalar(
        "SELECT count(*) FROM execution_intents WHERE state IN ('SUBMITTING','CANCEL_REQUESTED','UNKNOWN')"
    )
    if isinstance(unresolved, bool) or not isinstance(unresolved, int):
        raise RuntimeEvidenceError("unresolved order count is invalid")
    return ExecutionObservation(
        identity=EvidenceIdentity(
            stage=EvidenceStage.SHADOW,
            execution_session=plan.execution_session,
            firmquant_commit=firmquant_commit,
            uquant_commit=uquant_commit,
            promotion_config_sha256=promotion_config_sha256,
            account_sha256=facts.broker_snapshot.account.account_id_hash,
            data_sha256=decision.data_manifest_sha256,
            calendar_sha256=calendar_sha256,
        ),
        decision_id=decision.decision_id,
        plan_id=plan.plan_id,
        portfolio_equity=facts.broker_snapshot.account.total_assets.value,
        planned_orders=tuple(order_observations),
        planning_blockers=_planning_blockers(plan),
        targets=targets,
        fills=tuple(fill_observations),
        actual_ending_positions=actual,
        hypothetical_ending_positions=hypothetical,
        submit_count=0,
        cancel_count=0,
        rejection_count=0,
        unknown_count=unresolved,
        external_activity=_external_activity(database, broker, session=plan.execution_session),
        duplicate_economic_orders=_duplicate_economic_orders(database),
        duplicate_fills=_duplicate_fills(database, session=plan.execution_session),
        data_quality_failures=0,
        created_at=created_at,
    )


def _plan_payload(
    *,
    plan: ExecutionPlan,
    decision: DecisionSnapshot,
    facts: ExecutionBrokerSnapshot,
    firmquant_commit: str,
    uquant_commit: str,
    promotion_config_sha256: str,
    calendar_sha256: str,
    targets: tuple[TargetObservation, ...],
    created_at: datetime,
) -> dict[str, object]:
    return {
        "schema": "firmquant.canary-plan-evidence.v1",
        "firmquant_commit": firmquant_commit,
        "uquant_commit": uquant_commit,
        "promotion_config_sha256": promotion_config_sha256,
        "account_sha256": facts.broker_snapshot.account.account_id_hash,
        "data_sha256": decision.data_manifest_sha256,
        "calendar_sha256": calendar_sha256,
        "execution_session": plan.execution_session.isoformat(),
        "decision_id": decision.decision_id,
        "plan_id": plan.plan_id,
        "portfolio_equity": format(facts.broker_snapshot.account.total_assets.value, "f"),
        "broker_snapshot_sha256": facts.broker_snapshot.raw_payload_sha256,
        "orders": [
            {
                "uquant_order_id": item.uquant_order_id,
                "symbol": item.symbol.canonical,
                "side": item.side.value,
                "planned_shares": item.authorized_shares.value,
                "reference_price": item.limit_price.canonical,
            }
            for item in plan.orders
        ],
        "blockers": [item.payload() for item in _planning_blockers(plan)],
        "targets": [item.payload() for item in targets],
        "created_at": created_at.isoformat(),
    }


def record_canary_plan(
    *,
    database: Database,
    broker: BrokerGateway,
    facts: ExecutionBrokerSnapshot,
    plan: ExecutionPlan,
    decision: DecisionSnapshot,
    firmquant_commit: str,
    uquant_commit: str,
    promotion_config_sha256: str,
    calendar_sha256: str,
    created_at: datetime,
) -> None:
    """Persist immutable pre-submit CANARY plan facts for later EOD finalization."""

    instruments, quotes = _market_facts(broker, facts, decision)
    positions = _positions(cast(tuple[object, ...], facts.broker_snapshot.positions))
    targets = _target_observations(
        decision=decision,
        plan=plan,
        instruments=instruments,
        quotes=quotes,
        positions=positions,
        portfolio_equity=facts.broker_snapshot.account.total_assets.value,
    )
    payload = _plan_payload(
        plan=plan,
        decision=decision,
        facts=facts,
        firmquant_commit=firmquant_commit,
        uquant_commit=uquant_commit,
        promotion_config_sha256=promotion_config_sha256,
        calendar_sha256=calendar_sha256,
        targets=targets,
        created_at=created_at,
    )
    event_id = "canary-plan:" + plan.plan_id
    existing = database.query_one(
        "SELECT payload_sha256 FROM audit_events WHERE audit_event_id = ?",
        (event_id,),
    )
    digest = canonical_sha256(payload)
    if existing is not None:
        if str(existing["payload_sha256"]) != digest:
            raise RuntimeEvidenceError("CANARY plan evidence identity conflict")
        return
    with database.transaction():
        AuditLedger(database).append(
            audit_event_id=event_id,
            category="CANARY_PLAN_EVIDENCE",
            actor="execution-evidence",
            payload=payload,
            created_at=created_at,
        )


def _load_canary_plan(database: Database, *, session: date) -> dict[str, object] | None:
    rows = database.query_all(
        "SELECT payload_json FROM audit_events WHERE category = 'CANARY_PLAN_EVIDENCE' ORDER BY sequence DESC"
    )
    for row in rows:
        try:
            raw: object = json.loads(str(row["payload_json"]))
        except json.JSONDecodeError as error:
            raise RuntimeEvidenceError("CANARY plan evidence is invalid JSON") from error
        if not isinstance(raw, dict):
            raise RuntimeEvidenceError("CANARY plan evidence is not an object")
        if raw.get("execution_session") == session.isoformat():
            return cast(dict[str, object], raw)
    return None


def _plan_blockers(payload: Mapping[str, object]) -> tuple[PlanningBlockerObservation, ...]:
    raw = payload.get("blockers")
    if not isinstance(raw, list):
        raise RuntimeEvidenceError("CANARY plan blockers are missing")
    blockers: list[PlanningBlockerObservation] = []
    for item in raw:
        if not isinstance(item, dict):
            raise RuntimeEvidenceError("CANARY blocker evidence is malformed")
        blockers.append(
            PlanningBlockerObservation(
                uquant_order_id=str(item.get("uquant_order_id", "")),
                symbol=str(item.get("symbol", "")),
                reason_code=str(item.get("reason_code", "")),
            )
        )
    return tuple(blockers)


def _plan_targets(payload: Mapping[str, object]) -> tuple[TargetObservation, ...]:
    raw = payload.get("targets")
    if not isinstance(raw, list):
        raise RuntimeEvidenceError("CANARY plan targets are missing")
    targets: list[TargetObservation] = []
    for item in raw:
        if not isinstance(item, dict):
            raise RuntimeEvidenceError("CANARY target evidence is malformed")
        try:
            symbol = str(item["symbol"])
            target_shares = int(item["target_shares"])
            target_weight = Decimal(str(item["target_weight"]))
            reference_price = Decimal(str(item["reference_price"]))
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeEvidenceError("CANARY target evidence is malformed") from error
        targets.append(TargetObservation(symbol, target_shares, target_weight, reference_price))
    return tuple(targets)


def _plan_orders(payload: Mapping[str, object]) -> tuple[dict[str, object], ...]:
    raw = payload.get("orders")
    if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
        raise RuntimeEvidenceError("CANARY plan order evidence is malformed")
    return tuple(cast(dict[str, object], item) for item in raw)


def finalize_canary_observation(
    *,
    database: Database,
    eod_snapshot: BrokerSnapshot,
    session: date,
    created_at: datetime,
) -> ExecutionObservation | None:
    """Build a CANARY observation only from durable real orders/fills and the EOD broker snapshot."""

    plan = _load_canary_plan(database, session=session)
    if plan is None:
        return None
    decision_id = str(plan.get("decision_id"))
    plan_id = str(plan.get("plan_id"))
    planned_rows = _plan_orders(plan)
    order_observations: list[OrderObservation] = []
    fill_observations: list[FillObservation] = []
    execution_ids: list[str] = []
    rejection_count = 0
    unknown_count = 0
    for planned in planned_rows:
        order_id = str(planned.get("uquant_order_id"))
        execution = database.query_one(
            "SELECT execution_id,filled_shares,state FROM execution_intents "
            "WHERE decision_id = ? AND uquant_order_id = ?",
            (decision_id, order_id),
        )
        execution_id = "missing:" + order_id if execution is None else str(execution["execution_id"])
        filled = 0 if execution is None else int(execution["filled_shares"])
        state = "UNKNOWN" if execution is None else str(execution["state"])
        execution_ids.append(execution_id)
        if state == "REJECTED":
            rejection_count += 1
        if state in {"UNKNOWN", "SUBMITTING", "CANCEL_REQUESTED"}:
            unknown_count += 1
        planned_shares = int(planned["planned_shares"])
        blocker = None
        if state == "UNKNOWN":
            blocker = BlockerCode.UNKNOWN
        elif 0 < filled < planned_shares:
            blocker = BlockerCode.VOLUME_LIMIT
        order_observations.append(
            OrderObservation(
                execution_id=execution_id,
                uquant_order_id=order_id,
                symbol=str(planned["symbol"]),
                side=str(planned["side"]),
                planned_shares=planned_shares,
                filled_shares=filled,
                reference_price=Decimal(str(planned["reference_price"])),
                blocker=blocker,
            )
        )
        if execution is None:
            continue
        fill_rows = database.query_all(
            "SELECT broker_fill_id,symbol,side,shares,price,commission,stamp_duty,transfer_fee "
            "FROM fills WHERE execution_id = ? ORDER BY event_time,broker_fill_id",
            (execution_id,),
        )
        reference = Decimal(str(planned["reference_price"]))
        for row in fill_rows:
            price = Decimal(str(row["price"]))
            shares = int(row["shares"])
            fill_observations.append(
                FillObservation(
                    fill_id=str(row["broker_fill_id"]),
                    execution_id=execution_id,
                    symbol=str(row["symbol"]),
                    side=str(row["side"]),
                    shares=shares,
                    price=price,
                    commission=Decimal(str(row["commission"])),
                    stamp_duty=Decimal(str(row["stamp_duty"])),
                    transfer_fee=Decimal(str(row["transfer_fee"])),
                    slippage=abs(price - reference) * Decimal(shares),
                )
            )
    valid_execution_ids = tuple(item for item in execution_ids if item.startswith("exec_"))
    submit_count = cancel_count = 0
    for execution_id in valid_execution_ids:
        submit = database.scalar(
            "SELECT count(*) FROM order_commands c JOIN broker_order_attempts a ON a.attempt_id=c.attempt_id "
            "WHERE c.command_kind='SUBMIT' AND a.execution_id = ?",
            (execution_id,),
        )
        cancel = database.scalar(
            "SELECT count(*) FROM order_commands c JOIN broker_order_attempts a ON a.attempt_id=c.attempt_id "
            "WHERE c.command_kind='CANCEL' AND a.execution_id = ?",
            (execution_id,),
        )
        if isinstance(submit, bool) or not isinstance(submit, int):
            raise RuntimeEvidenceError("CANARY submit count is invalid")
        if isinstance(cancel, bool) or not isinstance(cancel, int):
            raise RuntimeEvidenceError("CANARY cancel count is invalid")
        submit_count += submit
        cancel_count += cancel
    external = database.scalar(
        "SELECT count(*) FROM broker_orders WHERE session_date = ? AND ownership IN ('EXTERNAL','UNKNOWN')",
        (session.isoformat(),),
    )
    if isinstance(external, bool) or not isinstance(external, int):
        raise RuntimeEvidenceError("CANARY external activity count is invalid")
    targets = _plan_targets(plan)
    actual = _positions(cast(tuple[object, ...], eod_snapshot.positions))
    observed = ExecutionObservation(
        identity=EvidenceIdentity(
            stage=EvidenceStage.CANARY,
            execution_session=session,
            firmquant_commit=str(plan.get("firmquant_commit")),
            uquant_commit=str(plan.get("uquant_commit")),
            promotion_config_sha256=str(plan.get("promotion_config_sha256")),
            account_sha256=str(plan.get("account_sha256")),
            data_sha256=str(plan.get("data_sha256")),
            calendar_sha256=str(plan.get("calendar_sha256")),
        ),
        decision_id=decision_id,
        plan_id=plan_id,
        portfolio_equity=Decimal(str(plan.get("portfolio_equity"))),
        planned_orders=tuple(order_observations),
        planning_blockers=_plan_blockers(plan),
        targets=targets,
        fills=tuple(fill_observations),
        actual_ending_positions=actual,
        hypothetical_ending_positions=(),
        submit_count=submit_count,
        cancel_count=cancel_count,
        rejection_count=rejection_count,
        unknown_count=unknown_count,
        external_activity=external,
        duplicate_economic_orders=_duplicate_economic_orders(database),
        duplicate_fills=_duplicate_fills(database, session=session),
        data_quality_failures=0,
        created_at=created_at,
    )
    if eod_snapshot.account.account_id_hash != observed.identity.account_sha256:
        raise RuntimeEvidenceError("CANARY EOD account identity changed")
    return observed


__all__ = (
    "RuntimeEvidenceError",
    "build_shadow_observation",
    "finalize_canary_observation",
    "record_canary_plan",
    "shadow_execution_policy",
)
