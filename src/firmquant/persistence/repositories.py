"""Transactional repositories and canonical JSON for the operational ledger."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final

from firmquant.broker.gateway import BrokerOrderCommand
from firmquant.domain.broker_facts import (
    BrokerFillFact,
    BrokerOrderFact,
    BrokerOrderStatus,
    FillStatus,
    Side,
)
from firmquant.domain.events import (
    BrokerAcknowledged,
    BrokerRejected,
    CancelConfirmed,
    CancelRequested,
    FillReported,
    OrderArmed,
    OrderEvent,
    OrderExpired,
    OrderValidated,
    SubmitOutcomeUnknown,
    SubmitStarted,
)
from firmquant.domain.orders import (
    AppliedEventIdentity,
    AppliedFill,
    ExecutionIntent,
    OrderAggregate,
    OrderState,
)
from firmquant.domain.values import Money, Price, Shares, Symbol
from firmquant.strategy.snapshots import DecisionSnapshot

from .database import Database, PersistenceError


class PersistenceConflict(PersistenceError):
    """Raised when a stable identity is reused with different facts."""


_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")


def _json_value(value: object) -> object:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        raise TypeError("canonical ledger JSON rejects binary float")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("canonical ledger JSON rejects non-finite Decimal")
        return format(value, "f")
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Symbol):
        return value.canonical
    if isinstance(value, (Money, Price)):
        return value.canonical
    if isinstance(value, Shares):
        return value.value
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("canonical ledger JSON requires timezone-aware datetime")
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("canonical ledger JSON object keys must be text")
            normalized[key] = _json_value(item)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    raise TypeError(f"unsupported canonical ledger JSON value: {type(value).__name__}")


def canonical_json(value: object) -> str:
    """Encode deterministic JSON while rejecting float and ambiguous containers."""

    return json.dumps(
        _json_value(value),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _require_sha256(value: str, *, label: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be lowercase SHA-256")


def _require_text(value: str, *, label: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be canonical non-empty text")


def _aware(value: datetime, *, label: str) -> str:
    if not isinstance(value, datetime):
        raise TypeError(f"{label} must be datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.isoformat()


@dataclass(frozen=True, slots=True)
class BrokerEventRepository:
    database: Database

    def append(
        self,
        *,
        broker_event_id: str,
        event_type: str,
        broker_sequence: int | None,
        session_date: date,
        event_time: datetime,
        received_at: datetime,
        safe_payload: Mapping[str, object],
        raw_payload_sha256: str,
    ) -> bool:
        """Append a sanitized raw event once; reject same-id payload conflicts."""

        _require_text(broker_event_id, label="broker event id")
        _require_text(event_type, label="broker event type")
        if broker_sequence is not None and (
            isinstance(broker_sequence, bool)
            or not isinstance(broker_sequence, int)
            or broker_sequence < 0
        ):
            raise ValueError("broker event sequence must be a nonnegative integer or null")
        if isinstance(session_date, datetime) or not isinstance(session_date, date):
            raise TypeError("broker event session date must be date")
        event_time_text = _aware(event_time, label="broker event time")
        received_at_text = _aware(received_at, label="broker event received time")
        _require_sha256(raw_payload_sha256, label="raw payload hash")
        payload_json = canonical_json(safe_payload)
        payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        stable_values = (
            event_type,
            broker_sequence,
            session_date.isoformat(),
            event_time_text,
            received_at_text,
            payload_json,
            payload_sha256,
            raw_payload_sha256,
        )
        existing = self.database.query_one(
            """
            SELECT event_type, broker_sequence, session_date, event_time, received_at,
                   safe_payload_json, safe_payload_sha256, raw_payload_sha256
            FROM broker_events WHERE broker_event_id = ?
            """,
            (broker_event_id,),
        )
        if existing is not None:
            if tuple(existing) == stable_values:
                return False
            raise PersistenceConflict(
                f"broker event identity collision: {broker_event_id}"
            )
        self.database.write(
            """
            INSERT INTO broker_events(
                broker_event_id, event_type, broker_sequence, session_date, event_time,
                received_at, safe_payload_json, safe_payload_sha256, raw_payload_sha256,
                recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                broker_event_id,
                *stable_values,
                datetime.now(UTC).isoformat(),
            ),
        )
        return True


@dataclass(frozen=True, slots=True)
class DecisionSnapshotRepository:
    """Append and restore immutable strategy snapshots from their indexed columns."""

    database: Database

    @staticmethod
    def _from_row(row: sqlite3.Row) -> DecisionSnapshot:
        try:
            payload: object = json.loads(str(row["payload_json"]))
            if not isinstance(payload, dict):
                raise ValueError("snapshot payload root is not an object")
            request_fingerprint = payload["request_fingerprint"]
            if not isinstance(request_fingerprint, str):
                raise ValueError("snapshot request fingerprint is not text")
            return DecisionSnapshot(
                strategy_session=date.fromisoformat(str(row["strategy_session"])),
                decision_id=str(row["decision_id"]),
                request_fingerprint=request_fingerprint,
                input_fingerprint=str(row["input_fingerprint"]),
                firmquant_commit=str(row["firmquant_commit"]),
                uquant_commit=str(row["uquant_commit"]),
                uquant_code_fingerprint=str(row["uquant_code_fingerprint"]),
                uquant_config_fingerprint=str(row["uquant_config_fingerprint"]),
                data_manifest_sha256=str(row["data_manifest_sha256"]),
                universe_manifest_sha256=str(row["universe_manifest_sha256"]),
                broker_snapshot_sha256=str(row["broker_snapshot_sha256"]),
                account_before_sha256=str(row["account_before_sha256"]),
                account_after_sha256=str(row["account_after_sha256"]),
                payload_json=str(row["payload_json"]),
                payload_sha256=str(row["payload_sha256"]),
                created_at=datetime.fromisoformat(str(row["created_at"])),
                supersedes_decision_id=(
                    None
                    if row["supersedes_decision_id"] is None
                    else str(row["supersedes_decision_id"])
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PersistenceConflict("stored decision snapshot is malformed") from exc

    def find_by_input(
        self,
        *,
        strategy_session: date,
        input_fingerprint: str,
    ) -> DecisionSnapshot | None:
        row = self.database.query_one(
            """
            SELECT * FROM decision_snapshots
            WHERE strategy_session = ? AND input_fingerprint = ?
            """,
            (strategy_session.isoformat(), input_fingerprint),
        )
        return None if row is None else self._from_row(row)

    def for_session(self, strategy_session: date) -> tuple[DecisionSnapshot, ...]:
        rows = self.database.query_all(
            """
            SELECT * FROM decision_snapshots
            WHERE strategy_session = ? ORDER BY created_at, decision_id
            """,
            (strategy_session.isoformat(),),
        )
        return tuple(self._from_row(row) for row in rows)

    def append(self, snapshot: DecisionSnapshot) -> bool:
        existing = self.database.query_one(
            "SELECT payload_sha256 FROM decision_snapshots WHERE decision_id = ?",
            (snapshot.decision_id,),
        )
        if existing is not None:
            if existing["payload_sha256"] == snapshot.payload_sha256:
                return False
            raise PersistenceConflict(
                f"decision snapshot identity collision: {snapshot.decision_id}"
            )
        try:
            self.database.write(
                """
                INSERT INTO decision_snapshots(
                    decision_id, strategy_session, input_fingerprint, firmquant_commit,
                    uquant_commit, uquant_code_fingerprint, uquant_config_fingerprint,
                    data_manifest_sha256, universe_manifest_sha256, broker_snapshot_sha256,
                    account_before_sha256, account_after_sha256, payload_json, payload_sha256,
                    created_at, supersedes_decision_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.decision_id,
                    snapshot.strategy_session.isoformat(),
                    snapshot.input_fingerprint,
                    snapshot.firmquant_commit,
                    snapshot.uquant_commit,
                    snapshot.uquant_code_fingerprint,
                    snapshot.uquant_config_fingerprint,
                    snapshot.data_manifest_sha256,
                    snapshot.universe_manifest_sha256,
                    snapshot.broker_snapshot_sha256,
                    snapshot.account_before_sha256,
                    snapshot.account_after_sha256,
                    snapshot.payload_json,
                    snapshot.payload_sha256,
                    snapshot.created_at.isoformat(),
                    snapshot.supersedes_decision_id,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise PersistenceConflict("decision snapshot violates append-only identity") from exc
        return True


@dataclass(frozen=True, slots=True)
class BrokerAttempt:
    attempt_id: str
    execution_id: str
    attempt_number: int
    command_kind: str


def _aggregate_payload(aggregate: OrderAggregate) -> dict[str, object]:
    intent = aggregate.intent
    return {
        "schema": "firmquant.order-aggregate.v1",
        "intent": {
            "execution_id": intent.execution_id,
            "idempotency_key": intent.idempotency_key,
            "decision_id": intent.decision_id,
            "uquant_order_id": intent.uquant_order_id,
            "symbol": intent.symbol,
            "side": intent.side,
            "requested_shares": intent.requested_shares,
            "strategy_session": intent.strategy_session,
            "uquant_source_sha": intent.uquant_source_sha,
        },
        "state": aggregate.state,
        "broker_order_id": aggregate.broker_order_id,
        "filled_shares": aggregate.filled_shares,
        "fills": [
            {
                "broker_fill_id": item.broker_fill_id,
                "broker_order_id": item.broker_order_id,
                "shares": item.shares,
                "price": item.price,
            }
            for item in aggregate.fills
        ],
        "applied_events": [
            {"event_id": item.event_id, "fingerprint": item.fingerprint}
            for item in aggregate.applied_events
        ],
        "submit_attempts": aggregate.submit_attempts,
        "cancel_requests": aggregate.cancel_requests,
        "late_fill_investigation_required": aggregate.late_fill_investigation_required,
        "anomalies": aggregate.anomalies,
        "version": aggregate.version,
    }


def _aggregate_from_json(payload_json: str) -> OrderAggregate:
    try:
        raw: object = json.loads(payload_json)
        if not isinstance(raw, dict) or raw.get("schema") != "firmquant.order-aggregate.v1":
            raise ValueError("aggregate schema is invalid")
        intent_raw = raw["intent"]
        if not isinstance(intent_raw, dict):
            raise TypeError("aggregate intent is not an object")
        intent = ExecutionIntent(
            execution_id=str(intent_raw["execution_id"]),
            idempotency_key=str(intent_raw["idempotency_key"]),
            decision_id=str(intent_raw["decision_id"]),
            uquant_order_id=str(intent_raw["uquant_order_id"]),
            symbol=Symbol.parse(str(intent_raw["symbol"])),
            side=Side(str(intent_raw["side"])),
            requested_shares=Shares(int(intent_raw["requested_shares"])),
            strategy_session=date.fromisoformat(str(intent_raw["strategy_session"])),
            uquant_source_sha=str(intent_raw["uquant_source_sha"]),
        )
        fills_raw = raw["fills"]
        events_raw = raw["applied_events"]
        anomalies_raw = raw["anomalies"]
        if not isinstance(fills_raw, list) or not isinstance(events_raw, list):
            raise TypeError("aggregate retained evidence must be arrays")
        if not isinstance(anomalies_raw, list):
            raise TypeError("aggregate anomalies must be an array")
        retained_fills = tuple(
            AppliedFill(
                broker_fill_id=str(item["broker_fill_id"]),
                broker_order_id=str(item["broker_order_id"]),
                shares=Shares(int(item["shares"])),
                price=Price(Decimal(str(item["price"]))),
            )
            for item in fills_raw
            if isinstance(item, dict)
        )
        if len(retained_fills) != len(fills_raw):
            raise TypeError("aggregate fill item is not an object")
        retained_events = tuple(
            AppliedEventIdentity(
                event_id=str(item["event_id"]),
                fingerprint=str(item["fingerprint"]),
            )
            for item in events_raw
            if isinstance(item, dict)
        )
        if len(retained_events) != len(events_raw):
            raise TypeError("aggregate event identity is not an object")
        aggregate = OrderAggregate(
            intent=intent,
            state=OrderState(str(raw["state"])),
            broker_order_id=(
                None if raw["broker_order_id"] is None else str(raw["broker_order_id"])
            ),
            filled_shares=Shares(int(raw["filled_shares"])),
            fills=retained_fills,
            applied_events=retained_events,
            submit_attempts=int(raw["submit_attempts"]),
            cancel_requests=int(raw["cancel_requests"]),
            late_fill_investigation_required=bool(
                raw["late_fill_investigation_required"]
            ),
            anomalies=tuple(str(item) for item in anomalies_raw),
            version=int(raw["version"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise PersistenceConflict("stored execution aggregate is malformed") from error
    if canonical_json(_aggregate_payload(aggregate)) != payload_json:
        raise PersistenceConflict("stored execution aggregate is not canonical")
    return aggregate


def _event_payload(event: OrderEvent) -> dict[str, object]:
    return {
        "schema": "firmquant.domain-order-event.v1",
        "event_type": type(event).__name__,
        "fields": {field.name: getattr(event, field.name) for field in fields(event)},
    }


def _stable_event_id(prefix: str, *parts: object) -> str:
    payload = canonical_json({"prefix": prefix, "parts": parts})
    return prefix + "-" + hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ExecutionLedgerRepository:
    """Optimistic, transactional persistence for one durable order aggregate."""

    database: Database

    def load(self, execution_id: str) -> OrderAggregate | None:
        _require_text(execution_id, label="execution id")
        row = self.database.query_one(
            "SELECT aggregate_json FROM execution_intents WHERE execution_id = ?",
            (execution_id,),
        )
        return None if row is None else _aggregate_from_json(str(row["aggregate_json"]))

    def find_economic_order(
        self, *, decision_id: str, uquant_order_id: str
    ) -> OrderAggregate | None:
        _require_text(decision_id, label="decision id")
        _require_text(uquant_order_id, label="uquant order id")
        row = self.database.query_one(
            """
            SELECT aggregate_json FROM execution_intents
            WHERE decision_id = ? AND uquant_order_id = ?
            """,
            (decision_id, uquant_order_id),
        )
        return None if row is None else _aggregate_from_json(str(row["aggregate_json"]))

    def append_intent(self, intent: ExecutionIntent, *, created_at: datetime) -> OrderAggregate:
        if not isinstance(intent, ExecutionIntent):
            raise TypeError("execution repository requires ExecutionIntent")
        created = _aware(created_at, label="execution intent created_at")
        aggregate = OrderAggregate.from_intent(intent)
        payload_json = canonical_json(_aggregate_payload(aggregate))
        existing = self.find_economic_order(
            decision_id=intent.decision_id,
            uquant_order_id=intent.uquant_order_id,
        )
        if existing is not None:
            if existing.intent == intent:
                return existing
            raise PersistenceConflict("economic order identity already has another intent")
        try:
            self.database.write(
                """
                INSERT INTO execution_intents(
                    execution_id, decision_id, idempotency_key, uquant_order_id, symbol,
                    side, requested_shares, filled_shares, state, strategy_session,
                    uquant_source_sha, aggregate_json, version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    intent.execution_id,
                    intent.decision_id,
                    intent.idempotency_key,
                    intent.uquant_order_id,
                    intent.symbol.canonical,
                    intent.side.value,
                    intent.requested_shares.value,
                    0,
                    aggregate.state.value,
                    intent.strategy_session.isoformat(),
                    intent.uquant_source_sha,
                    payload_json,
                    aggregate.version,
                    created,
                    created,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise PersistenceConflict("execution intent violates stable identity") from error
        return aggregate

    def transition(
        self,
        aggregate: OrderAggregate,
        event: OrderEvent,
        *,
        occurred_at: datetime,
        broker_event_id: str | None = None,
    ) -> OrderAggregate:
        occurred = _aware(occurred_at, label="domain event occurred_at")
        next_aggregate = aggregate.apply(event)  # type: ignore[arg-type]
        if next_aggregate is aggregate:
            return aggregate
        payload = _event_payload(event)
        payload_json = canonical_json(payload)
        payload_sha256 = hashlib.sha256(payload_json.encode()).hexdigest()
        if broker_event_id is not None:
            _require_text(broker_event_id, label="broker event id")
        try:
            cursor = self.database.write(
                """
                UPDATE execution_intents
                SET filled_shares = ?, state = ?, aggregate_json = ?, version = ?, updated_at = ?
                WHERE execution_id = ? AND version = ?
                """,
                (
                    next_aggregate.filled_shares.value,
                    next_aggregate.state.value,
                    canonical_json(_aggregate_payload(next_aggregate)),
                    next_aggregate.version,
                    occurred,
                    aggregate.intent.execution_id,
                    aggregate.version,
                ),
            )
            if cursor.rowcount != 1:
                raise PersistenceConflict("execution aggregate optimistic version conflict")
            self.database.write(
                """
                INSERT INTO domain_events(
                    domain_event_id, broker_event_id, aggregate_type, aggregate_id,
                    event_type, payload_json, payload_sha256, occurred_at, recorded_at
                ) VALUES (?, ?, 'ORDER', ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    broker_event_id,
                    aggregate.intent.execution_id,
                    type(event).__name__,
                    payload_json,
                    payload_sha256,
                    occurred,
                    occurred,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise PersistenceConflict("domain order event violates stable identity") from error
        return next_aggregate

    def validate_and_arm(
        self, aggregate: OrderAggregate, *, occurred_at: datetime
    ) -> OrderAggregate:
        current = aggregate
        if current.state is OrderState.PLANNED:
            current = self.transition(
                current,
                OrderValidated(
                    event_id=_stable_event_id("validate", current.intent.execution_id)
                ),
                occurred_at=occurred_at,
            )
        if current.state is OrderState.VALIDATED:
            current = self.transition(
                current,
                OrderArmed(event_id=_stable_event_id("arm", current.intent.execution_id)),
                occurred_at=occurred_at,
            )
        return current

    def _next_attempt_number(self, execution_id: str) -> int:
        value = self.database.scalar(
            "SELECT COALESCE(MAX(attempt_number), 0) FROM broker_order_attempts WHERE execution_id = ?",
            (execution_id,),
        )
        if isinstance(value, bool) or not isinstance(value, int):
            raise PersistenceConflict("stored broker attempt number is not an integer")
        return value + 1

    def _insert_attempt(
        self,
        *,
        aggregate: OrderAggregate,
        command_kind: str,
        command_payload: Mapping[str, object],
        started_at: datetime,
    ) -> BrokerAttempt:
        attempt_number = self._next_attempt_number(aggregate.intent.execution_id)
        attempt_id = _stable_event_id(
            "attempt", aggregate.intent.execution_id, attempt_number, command_kind
        )
        payload_json = canonical_json(command_payload)
        payload_sha256 = hashlib.sha256(payload_json.encode()).hexdigest()
        self.database.write(
            """
            INSERT INTO broker_order_attempts(
                attempt_id, execution_id, attempt_number, state, started_at
            ) VALUES (?, ?, ?, 'SUBMITTING', ?)
            """,
            (
                attempt_id,
                aggregate.intent.execution_id,
                attempt_number,
                _aware(started_at, label="broker attempt started_at"),
            ),
        )
        self.database.write(
            """
            INSERT INTO order_commands(
                command_id, attempt_id, command_kind, payload_json, payload_sha256, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                _stable_event_id("command", attempt_id, command_kind),
                attempt_id,
                command_kind,
                payload_json,
                payload_sha256,
                _aware(started_at, label="order command created_at"),
            ),
        )
        return BrokerAttempt(
            attempt_id=attempt_id,
            execution_id=aggregate.intent.execution_id,
            attempt_number=attempt_number,
            command_kind=command_kind,
        )

    def begin_submit(
        self,
        aggregate: OrderAggregate,
        command: BrokerOrderCommand,
        *,
        started_at: datetime,
    ) -> tuple[OrderAggregate, BrokerAttempt]:
        if aggregate.state is not OrderState.ARMED:
            raise PersistenceConflict("submit can begin only from ARMED")
        if command.execution_id != aggregate.intent.execution_id:
            raise PersistenceConflict("broker command execution identity mismatch")
        next_aggregate = self.transition(
            aggregate,
            SubmitStarted(
                event_id=_stable_event_id(
                    "submit-started",
                    aggregate.intent.execution_id,
                    aggregate.submit_attempts + 1,
                )
            ),
            occurred_at=started_at,
        )
        attempt = self._insert_attempt(
            aggregate=next_aggregate,
            command_kind="SUBMIT",
            command_payload={
                "execution_id": command.execution_id,
                "idempotency_key": command.idempotency_key,
                "client_order_id": command.client_order_id,
                "symbol": command.symbol,
                "side": command.side,
                "price_type": command.price_type,
                "requested_shares": command.requested_shares,
                "limit_price": command.limit_price,
                "strategy_session": command.strategy_session,
            },
            started_at=started_at,
        )
        return next_aggregate, attempt

    def begin_cancel(
        self, aggregate: OrderAggregate, *, started_at: datetime
    ) -> tuple[OrderAggregate, BrokerAttempt]:
        if aggregate.broker_order_id is None:
            raise PersistenceConflict("cancel requires known broker order id")
        next_aggregate = self.transition(
            aggregate,
            CancelRequested(
                event_id=_stable_event_id(
                    "cancel-requested",
                    aggregate.intent.execution_id,
                    aggregate.cancel_requests + 1,
                )
            ),
            occurred_at=started_at,
        )
        attempt = self._insert_attempt(
            aggregate=next_aggregate,
            command_kind="CANCEL",
            command_payload={"broker_order_id": aggregate.broker_order_id},
            started_at=started_at,
        )
        return next_aggregate, attempt

    def mark_attempt_unknown(
        self,
        aggregate: OrderAggregate,
        attempt: BrokerAttempt,
        *,
        diagnostic_code: str,
        occurred_at: datetime,
    ) -> OrderAggregate:
        next_aggregate = self.transition(
            aggregate,
            SubmitOutcomeUnknown(
                event_id=_stable_event_id("unknown", attempt.attempt_id, diagnostic_code),
                diagnostic_code=diagnostic_code,
            ),
            occurred_at=occurred_at,
        )
        self.database.write(
            """
            UPDATE broker_order_attempts
            SET state = 'UNKNOWN', completed_at = ? WHERE attempt_id = ?
            """,
            (_aware(occurred_at, label="unknown attempt completed_at"), attempt.attempt_id),
        )
        return next_aggregate

    def _record_broker_order(
        self, fact: BrokerOrderFact, *, execution_id: str
    ) -> None:
        existing = self.database.query_one(
            "SELECT * FROM broker_orders WHERE broker_order_id = ?",
            (fact.broker_order_id,),
        )
        fixed = (
            execution_id,
            "SYSTEM",
            fact.client_order_id,
            fact.symbol.canonical,
            fact.side.value,
            fact.requested_shares.value,
            fact.limit_price.canonical,
            fact.session_date.isoformat(),
        )
        if existing is None:
            self.database.write(
                """
                INSERT INTO broker_orders(
                    broker_order_id, execution_id, ownership, client_order_id, symbol,
                    side, status, requested_shares, filled_shares, limit_price,
                    session_date, last_event_sequence, event_time, received_at,
                    raw_payload_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fact.broker_order_id,
                    execution_id,
                    "SYSTEM",
                    fact.client_order_id,
                    fact.symbol.canonical,
                    fact.side.value,
                    fact.status.value,
                    fact.requested_shares.value,
                    fact.filled_shares.value,
                    fact.limit_price.canonical,
                    fact.session_date.isoformat(),
                    fact.event_sequence,
                    fact.event_time.isoformat(),
                    fact.received_at.isoformat(),
                    fact.raw_payload_sha256,
                ),
            )
            return
        observed_fixed = (
            existing["execution_id"],
            existing["ownership"],
            existing["client_order_id"],
            existing["symbol"],
            existing["side"],
            existing["requested_shares"],
            existing["limit_price"],
            existing["session_date"],
        )
        if observed_fixed != fixed:
            raise PersistenceConflict("broker order identity changed")
        previous_sequence = existing["last_event_sequence"]
        if previous_sequence is not None and int(previous_sequence) > fact.event_sequence:
            raise PersistenceConflict("broker order event sequence regressed")
        self.database.write(
            """
            UPDATE broker_orders SET status = ?, filled_shares = ?, last_event_sequence = ?,
                event_time = ?, received_at = ?, raw_payload_sha256 = ?
            WHERE broker_order_id = ?
            """,
            (
                fact.status.value,
                fact.filled_shares.value,
                fact.event_sequence,
                fact.event_time.isoformat(),
                fact.received_at.isoformat(),
                fact.raw_payload_sha256,
                fact.broker_order_id,
            ),
        )

    def _complete_attempt(
        self,
        attempt: BrokerAttempt,
        fact: BrokerOrderFact,
        *,
        response_kind: str,
        received_at: datetime,
    ) -> None:
        payload = {
            "broker_order_id": fact.broker_order_id,
            "client_order_id": fact.client_order_id,
            "symbol": fact.symbol,
            "side": fact.side,
            "status": fact.status,
            "requested_shares": fact.requested_shares,
            "filled_shares": fact.filled_shares,
            "limit_price": fact.limit_price,
            "event_sequence": fact.event_sequence,
            "raw_payload_sha256": fact.raw_payload_sha256,
        }
        payload_json = canonical_json(payload)
        payload_sha256 = hashlib.sha256(payload_json.encode()).hexdigest()
        self.database.write(
            """
            INSERT INTO broker_responses(
                response_id, attempt_id, response_kind, payload_json,
                payload_sha256, received_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                _stable_event_id("response", attempt.attempt_id, payload_sha256),
                attempt.attempt_id,
                response_kind,
                payload_json,
                payload_sha256,
                _aware(received_at, label="broker response received_at"),
            ),
        )
        self.database.write(
            """
            UPDATE broker_order_attempts
            SET state = 'RETURNED', completed_at = ?, broker_order_id = ?
            WHERE attempt_id = ?
            """,
            (
                _aware(received_at, label="broker attempt completed_at"),
                fact.broker_order_id,
                attempt.attempt_id,
            ),
        )

    def _record_fill(self, fill: BrokerFillFact, *, execution_id: str) -> None:
        existing = self.database.query_one(
            "SELECT * FROM fills WHERE broker_fill_id = ?", (fill.broker_fill_id,)
        )
        stable = (
            "BROKER",
            fill.broker_order_id,
            execution_id,
            None,
            fill.symbol.canonical,
            fill.side.value,
            fill.shares.value,
            fill.price.canonical,
            fill.commission.canonical,
            fill.stamp_duty.canonical,
            fill.transfer_fee.canonical,
            fill.session_date.isoformat(),
            fill.event_time.isoformat(),
            fill.raw_payload_sha256,
        )
        if existing is not None:
            observed = tuple(existing[key] for key in (
                "identity_kind", "broker_order_id", "execution_id", "broker_event_id",
                "symbol", "side", "shares", "price", "commission", "stamp_duty",
                "transfer_fee", "session_date", "event_time", "raw_payload_sha256"
            ))
            if observed != stable:
                raise PersistenceConflict("broker fill identity collision")
            return
        self.database.write(
            """
            INSERT INTO fills(
                broker_fill_id, identity_kind, broker_order_id, execution_id,
                broker_event_id, symbol, side, shares, price, commission,
                stamp_duty, transfer_fee, session_date, event_time, raw_payload_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (fill.broker_fill_id, *stable),
        )

    def reconcile_broker_fact(
        self,
        aggregate: OrderAggregate,
        fact: BrokerOrderFact,
        fills: tuple[BrokerFillFact, ...],
        *,
        received_at: datetime,
    ) -> OrderAggregate:
        """Apply queried broker truth idempotently without creating a write attempt."""

        if aggregate.broker_order_id not in {None, fact.broker_order_id}:
            raise PersistenceConflict("queried broker order identity changed")
        if (
            fact.client_order_id != aggregate.intent.uquant_order_id
            or fact.symbol != aggregate.intent.symbol
            or fact.side is not aggregate.intent.side
            or fact.requested_shares != aggregate.intent.requested_shares
        ):
            raise PersistenceConflict("queried broker order contradicts execution intent")
        ordered_fills = tuple(
            sorted(fills, key=lambda item: (item.event_sequence, item.broker_fill_id))
        )
        if any(
            fill.broker_order_id != fact.broker_order_id
            or fill.symbol != fact.symbol
            or fill.side is not fact.side
            or fill.status is not FillStatus.CONFIRMED
            for fill in ordered_fills
        ):
            raise PersistenceConflict("queried broker fill contradicts mapped order")
        if sum(fill.shares.value for fill in ordered_fills) != fact.filled_shares.value:
            raise PersistenceConflict("queried broker fills do not prove cumulative shares")

        self._record_broker_order(fact, execution_id=aggregate.intent.execution_id)
        current = aggregate
        if fact.status is BrokerOrderStatus.REJECTED:
            return self.transition(
                current,
                BrokerRejected(
                    event_id=_stable_event_id(
                        "recovery-rejected",
                        fact.broker_order_id,
                        fact.event_sequence,
                    ),
                    reason_code="BROKER_REJECTED",
                ),
                occurred_at=received_at,
            )
        if fact.status is BrokerOrderStatus.EXPIRED:
            return self.transition(
                current,
                OrderExpired(
                    event_id=_stable_event_id(
                        "recovery-expired",
                        fact.broker_order_id,
                        fact.event_sequence,
                    ),
                    reason_code="BROKER_EXPIRED",
                ),
                occurred_at=received_at,
            )
        if fact.status is BrokerOrderStatus.UNKNOWN:
            return self.transition(
                current,
                SubmitOutcomeUnknown(
                    event_id=_stable_event_id(
                        "recovery-unknown",
                        fact.broker_order_id,
                        fact.event_sequence,
                    ),
                    diagnostic_code="BROKER_STATUS_UNKNOWN",
                ),
                occurred_at=received_at,
            )
        if fact.status is not BrokerOrderStatus.CANCELLED:
            current = self.transition(
                current,
                BrokerAcknowledged(
                    event_id=_stable_event_id(
                        "ack", fact.broker_order_id, fact.event_sequence
                    ),
                    broker_order_id=fact.broker_order_id,
                ),
                occurred_at=received_at,
            )
        for fill in ordered_fills:
            self._record_fill(fill, execution_id=current.intent.execution_id)
            current = self.transition(
                current,
                FillReported(
                    event_id=_stable_event_id("fill", fill.broker_fill_id),
                    broker_fill_id=fill.broker_fill_id,
                    broker_order_id=fill.broker_order_id,
                    shares=fill.shares,
                    price=fill.price,
                ),
                occurred_at=fill.event_time,
            )
        if fact.status is BrokerOrderStatus.CANCELLED:
            current = self.transition(
                current,
                CancelConfirmed(
                    event_id=_stable_event_id(
                        "recovery-cancelled",
                        fact.broker_order_id,
                        fact.event_sequence,
                    ),
                    broker_order_id=fact.broker_order_id,
                ),
                occurred_at=received_at,
            )
        if current.filled_shares != fact.filled_shares:
            raise PersistenceConflict("recovered aggregate differs from broker cumulative fill")
        return current

    def record_submit_result(
        self,
        aggregate: OrderAggregate,
        attempt: BrokerAttempt,
        fact: BrokerOrderFact,
        fills: tuple[BrokerFillFact, ...],
        *,
        received_at: datetime,
    ) -> OrderAggregate:
        self._record_broker_order(fact, execution_id=aggregate.intent.execution_id)
        self._complete_attempt(
            attempt, fact, response_kind="SUBMIT_RETURN", received_at=received_at
        )
        current = aggregate
        if fact.status is BrokerOrderStatus.REJECTED:
            return self.transition(
                current,
                BrokerRejected(
                    event_id=_stable_event_id("rejected", attempt.attempt_id),
                    reason_code="BROKER_REJECTED",
                ),
                occurred_at=received_at,
            )
        if fact.status is BrokerOrderStatus.UNKNOWN:
            return self.mark_attempt_unknown(
                current,
                attempt,
                diagnostic_code="BROKER_STATUS_UNKNOWN",
                occurred_at=received_at,
            )
        if fact.status is BrokerOrderStatus.CANCELLED:
            return self.transition(
                current,
                CancelConfirmed(
                    event_id=_stable_event_id("cancelled", attempt.attempt_id),
                    broker_order_id=fact.broker_order_id,
                ),
                occurred_at=received_at,
            )
        if fact.status is BrokerOrderStatus.EXPIRED:
            return self.transition(
                current,
                OrderExpired(
                    event_id=_stable_event_id("expired", attempt.attempt_id),
                    reason_code="BROKER_EXPIRED",
                ),
                occurred_at=received_at,
            )
        current = self.transition(
            current,
            BrokerAcknowledged(
                event_id=_stable_event_id(
                    "ack", fact.broker_order_id, fact.event_sequence
                ),
                broker_order_id=fact.broker_order_id,
            ),
            occurred_at=received_at,
        )
        for fill in sorted(fills, key=lambda item: (item.event_sequence, item.broker_fill_id)):
            if fill.broker_order_id != fact.broker_order_id:
                raise PersistenceConflict("submit result contains fill for another order")
            self._record_fill(fill, execution_id=current.intent.execution_id)
            current = self.transition(
                current,
                FillReported(
                    event_id=_stable_event_id("fill", fill.broker_fill_id),
                    broker_fill_id=fill.broker_fill_id,
                    broker_order_id=fill.broker_order_id,
                    shares=fill.shares,
                    price=fill.price,
                ),
                occurred_at=fill.event_time,
            )
        if current.filled_shares != fact.filled_shares:
            self.database.write(
                "UPDATE broker_order_attempts SET state = 'UNKNOWN' WHERE attempt_id = ?",
                (attempt.attempt_id,),
            )
            current = self.transition(
                current,
                SubmitOutcomeUnknown(
                    event_id=_stable_event_id("missing-fill", attempt.attempt_id),
                    diagnostic_code="BROKER_FILL_MISSING",
                ),
                occurred_at=received_at,
            )
        return current

    def record_cancel_result(
        self,
        aggregate: OrderAggregate,
        attempt: BrokerAttempt,
        fact: BrokerOrderFact,
        fills: tuple[BrokerFillFact, ...],
        *,
        received_at: datetime,
    ) -> OrderAggregate:
        self._record_broker_order(fact, execution_id=aggregate.intent.execution_id)
        self._complete_attempt(
            attempt, fact, response_kind="CANCEL_RETURN", received_at=received_at
        )
        current = aggregate
        for fill in sorted(fills, key=lambda item: (item.event_sequence, item.broker_fill_id)):
            if any(existing.broker_fill_id == fill.broker_fill_id for existing in current.fills):
                continue
            self._record_fill(fill, execution_id=current.intent.execution_id)
            current = self.transition(
                current,
                FillReported(
                    event_id=_stable_event_id("fill", fill.broker_fill_id),
                    broker_fill_id=fill.broker_fill_id,
                    broker_order_id=fill.broker_order_id,
                    shares=fill.shares,
                    price=fill.price,
                ),
                occurred_at=fill.event_time,
            )
        if fact.status is BrokerOrderStatus.CANCELLED:
            return self.transition(
                current,
                CancelConfirmed(
                    event_id=_stable_event_id("cancel-confirmed", attempt.attempt_id),
                    broker_order_id=fact.broker_order_id,
                ),
                occurred_at=received_at,
            )
        if fact.status is BrokerOrderStatus.FILLED and current.state is OrderState.FILLED:
            return current
        self.database.write(
            "UPDATE broker_order_attempts SET state = 'UNKNOWN' WHERE attempt_id = ?",
            (attempt.attempt_id,),
        )
        return self.transition(
            current,
            SubmitOutcomeUnknown(
                event_id=_stable_event_id("cancel-unknown", attempt.attempt_id),
                diagnostic_code="CANCEL_OUTCOME_UNKNOWN",
            ),
            occurred_at=received_at,
        )


@dataclass(frozen=True, slots=True)
class Repositories:
    broker_events: BrokerEventRepository
    decision_snapshots: DecisionSnapshotRepository
    execution: ExecutionLedgerRepository

    @classmethod
    def bind(cls, database: Database) -> Repositories:
        return cls(
            broker_events=BrokerEventRepository(database),
            decision_snapshots=DecisionSnapshotRepository(database),
            execution=ExecutionLedgerRepository(database),
        )


__all__ = (
    "BrokerAttempt",
    "BrokerEventRepository",
    "DecisionSnapshotRepository",
    "ExecutionLedgerRepository",
    "PersistenceConflict",
    "Repositories",
    "canonical_json",
    "canonical_sha256",
)
