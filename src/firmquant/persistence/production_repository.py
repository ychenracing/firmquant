"""Monotonic production persistence for broker order and fill truth."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime

from firmquant.domain.broker_facts import (
    BrokerFillFact,
    BrokerOrderFact,
    BrokerOrderStatus,
    FillStatus,
)
from firmquant.domain.events import (
    BrokerAcknowledged,
    BrokerRejected,
    CancelConfirmed,
    CancelNotAccepted,
    FillReported,
    OrderExpired,
    SubmitNotAccepted,
    SubmitOutcomeUnknown,
    UnknownResolvedNotAccepted,
)
from firmquant.domain.orders import OrderAggregate, OrderState

from .repositories import (
    BrokerAttempt,
    BrokerEventRepository,
    ExecutionLedgerRepository,
    PersistenceConflict,
    canonical_json,
    canonical_sha256,
)

_TERMINAL = frozenset(
    {
        BrokerOrderStatus.FILLED,
        BrokerOrderStatus.CANCELLED,
        BrokerOrderStatus.REJECTED,
        BrokerOrderStatus.EXPIRED,
    }
)
_STATUS_RANK = {
    BrokerOrderStatus.UNKNOWN: 0,
    BrokerOrderStatus.PENDING_NEW: 10,
    BrokerOrderStatus.ACKNOWLEDGED: 20,
    BrokerOrderStatus.PARTIALLY_FILLED: 30,
    BrokerOrderStatus.PENDING_CANCEL: 40,
    BrokerOrderStatus.CANCELLED: 50,
    BrokerOrderStatus.REJECTED: 50,
    BrokerOrderStatus.EXPIRED: 50,
    BrokerOrderStatus.FILLED: 60,
}
_TERMINAL_STATE = {
    BrokerOrderStatus.FILLED: OrderState.FILLED,
    BrokerOrderStatus.CANCELLED: OrderState.CANCELLED,
    BrokerOrderStatus.REJECTED: OrderState.REJECTED,
    BrokerOrderStatus.EXPIRED: OrderState.EXPIRED,
}


def _write_event_id(prefix: str, attempt: BrokerAttempt, evidence_sha256: str) -> str:
    payload = f"{prefix}:{attempt.attempt_id}:{evidence_sha256}".encode()
    return prefix + "-" + hashlib.sha256(payload).hexdigest()


def _stable_event_id(prefix: str, *parts: object) -> str:
    payload = canonical_json({"prefix": prefix, "parts": parts})
    return prefix + "-" + hashlib.sha256(payload.encode()).hexdigest()


def _fill_identity(fill: BrokerFillFact, execution_id: str) -> dict[str, object]:
    return {
        "broker_fill_id": fill.broker_fill_id,
        "broker_order_id": fill.broker_order_id,
        "execution_id": execution_id,
        "symbol": fill.symbol,
        "side": fill.side,
        "status": fill.status,
        "shares": fill.shares,
        "price": fill.price,
        "commission": fill.commission,
        "stamp_duty": fill.stamp_duty,
        "transfer_fee": fill.transfer_fee,
        "session_date": fill.session_date,
        "execution_sequence": fill.event_sequence,
    }


def _fill_evidence_id(fill: BrokerFillFact, execution_id: str) -> str:
    return "fill-evidence-" + canonical_sha256(
        {
            "identity": _fill_identity(fill, execution_id),
            "event_time": fill.event_time,
            "received_at": fill.received_at,
            "raw_payload_sha256": fill.raw_payload_sha256,
        }
    )


class MonotonicExecutionLedgerRepository(ExecutionLedgerRepository):
    """Operational ledger that fails closed on regressing or ambiguous broker truth."""

    def _record_broker_order(self, fact: BrokerOrderFact, *, execution_id: str) -> None:
        existing = self.database.query_one(
            "SELECT * FROM broker_orders WHERE broker_order_id = ?",
            (fact.broker_order_id,),
        )
        if existing is None:
            super()._record_broker_order(fact, execution_id=execution_id)
            return

        expected_identity = (
            execution_id,
            "SYSTEM",
            fact.client_order_id,
            fact.symbol.canonical,
            fact.side.value,
            fact.requested_shares.value,
            fact.limit_price.canonical,
            fact.session_date.isoformat(),
        )
        stored_identity = (
            existing["execution_id"],
            existing["ownership"],
            existing["client_order_id"],
            existing["symbol"],
            existing["side"],
            existing["requested_shares"],
            existing["limit_price"],
            existing["session_date"],
        )
        if stored_identity != expected_identity:
            raise PersistenceConflict("broker order identity changed")
        try:
            previous_status = BrokerOrderStatus(str(existing["status"]))
            previous_filled = int(existing["filled_shares"])
        except (TypeError, ValueError) as error:
            raise PersistenceConflict("stored broker order status is malformed") from error
        if fact.filled_shares.value < previous_filled:
            raise PersistenceConflict("broker order cumulative fill regressed")
        if previous_status in _TERMINAL and fact.status is not previous_status:
            raise PersistenceConflict("broker order terminal status regressed")
        if (
            previous_status is not BrokerOrderStatus.UNKNOWN
            and fact.status is not BrokerOrderStatus.UNKNOWN
            and fact.status is not previous_status
            and _STATUS_RANK[fact.status] < _STATUS_RANK[previous_status]
        ):
            raise PersistenceConflict("broker order lifecycle status regressed")
        previous_sequence = existing["last_event_sequence"]
        if previous_sequence is not None:
            stored_sequence = int(previous_sequence)
            if fact.event_sequence < stored_sequence:
                raise PersistenceConflict("broker order event sequence regressed")
            if fact.event_sequence == stored_sequence and (
                fact.status.value != str(existing["status"]) or fact.filled_shares.value != previous_filled
            ):
                raise PersistenceConflict("broker order sequence was reused for different truth")
        self.database.write(
            """
            UPDATE broker_orders
            SET status = ?, filled_shares = ?, last_event_sequence = ?,
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

    def _fill_evidence_rows(self, broker_fill_id: str) -> tuple[sqlite3.Row, ...]:
        return self.database.query_all(
            """
            SELECT broker_sequence, safe_payload_json
            FROM broker_events
            WHERE event_type = 'FILL_EVIDENCE'
              AND json_extract(safe_payload_json, '$.broker_fill_id') = ?
            ORDER BY recorded_at, broker_event_id
            """,
            (broker_fill_id,),
        )

    def _record_fill_evidence(self, fill: BrokerFillFact, *, execution_id: str) -> None:
        identity = _fill_identity(fill, execution_id)
        BrokerEventRepository(self.database).append(
            broker_event_id=_fill_evidence_id(fill, execution_id),
            event_type="FILL_EVIDENCE",
            broker_sequence=fill.event_sequence,
            session_date=fill.session_date,
            event_time=fill.event_time,
            received_at=fill.received_at,
            safe_payload={**identity, "identity_sha256": canonical_sha256(identity)},
            raw_payload_sha256=fill.raw_payload_sha256,
        )

    def _record_fill(self, fill: BrokerFillFact, *, execution_id: str) -> None:
        if fill.status is not FillStatus.CONFIRMED:
            raise PersistenceConflict("only confirmed fills may enter the operational ledger")
        if fill.event_sequence <= 0:
            raise PersistenceConflict("broker fill execution sequence must be positive")
        identity_sha256 = canonical_sha256(_fill_identity(fill, execution_id))
        existing = self.database.query_one(
            "SELECT * FROM fills WHERE broker_fill_id = ?",
            (fill.broker_fill_id,),
        )
        if existing is not None:
            stored_economics = (
                existing["identity_kind"],
                existing["broker_order_id"],
                existing["execution_id"],
                existing["symbol"],
                existing["side"],
                existing["shares"],
                existing["price"],
                existing["commission"],
                existing["stamp_duty"],
                existing["transfer_fee"],
                existing["session_date"],
            )
            observed_economics = (
                "BROKER",
                fill.broker_order_id,
                execution_id,
                fill.symbol.canonical,
                fill.side.value,
                fill.shares.value,
                fill.price.canonical,
                fill.commission.canonical,
                fill.stamp_duty.canonical,
                fill.transfer_fee.canonical,
                fill.session_date.isoformat(),
            )
            if stored_economics != observed_economics:
                raise PersistenceConflict("broker fill identity collision")
            evidence = self._fill_evidence_rows(fill.broker_fill_id)
            if not evidence:
                raise PersistenceConflict("stored broker fill lacks execution sequence evidence")
            for row in evidence:
                try:
                    payload = json.loads(str(row["safe_payload_json"]))
                    sequence = int(row["broker_sequence"])
                except (KeyError, TypeError, ValueError) as error:
                    raise PersistenceConflict("stored broker fill sequence evidence is malformed") from error
                if sequence != fill.event_sequence or payload.get("identity_sha256") != identity_sha256:
                    raise PersistenceConflict("broker fill identity collision")
            self._record_fill_evidence(fill, execution_id=execution_id)
            return

        latest = self.database.query_one(
            """
            SELECT MAX(broker_sequence) AS max_sequence
            FROM broker_events
            WHERE event_type = 'FILL_EVIDENCE'
              AND session_date = ?
              AND json_extract(safe_payload_json, '$.broker_order_id') = ?
            """,
            (fill.session_date.isoformat(), fill.broker_order_id),
        )
        if (
            latest is not None
            and latest["max_sequence"] is not None
            and fill.event_sequence <= int(latest["max_sequence"])
        ):
            raise PersistenceConflict("broker fill execution sequence regressed")
        self._record_fill_evidence(fill, execution_id=execution_id)
        super()._record_fill(fill, execution_id=execution_id)

    def _validate_fact(self, aggregate: OrderAggregate, fact: BrokerOrderFact) -> None:
        if aggregate.broker_order_id not in {None, fact.broker_order_id}:
            raise PersistenceConflict("queried broker order identity changed")
        if (
            fact.client_order_id != aggregate.intent.uquant_order_id
            or fact.symbol != aggregate.intent.symbol
            or fact.side is not aggregate.intent.side
            or fact.requested_shares != aggregate.intent.requested_shares
            or fact.session_date != aggregate.intent.strategy_session
        ):
            raise PersistenceConflict("queried broker order contradicts execution intent")
        if fact.status is BrokerOrderStatus.FILLED and fact.filled_shares != fact.requested_shares:
            raise PersistenceConflict("FILLED broker order has contradictory cumulative shares")
        if fact.status is BrokerOrderStatus.PARTIALLY_FILLED and not (
            0 < fact.filled_shares.value < fact.requested_shares.value
        ):
            raise PersistenceConflict("PARTIALLY_FILLED broker order has contradictory cumulative shares")

    def _durable_command_payload(self, attempt: BrokerAttempt) -> dict[str, object]:
        row = self.database.query_one(
            """
            SELECT command_kind, payload_json
            FROM order_commands
            WHERE attempt_id = ?
            """,
            (attempt.attempt_id,),
        )
        if row is None or str(row["command_kind"]) != attempt.command_kind:
            raise PersistenceConflict("durable broker command is missing or inconsistent")
        try:
            payload = json.loads(str(row["payload_json"]))
        except (TypeError, ValueError) as error:
            raise PersistenceConflict("stored broker command payload is malformed") from error
        if not isinstance(payload, dict):
            raise PersistenceConflict("stored broker command payload is not an object")
        return payload

    def _validate_attempt_fact(
        self,
        aggregate: OrderAggregate,
        attempt: BrokerAttempt,
        fact: BrokerOrderFact,
        *,
        command_kind: str,
    ) -> None:
        self._validate_fact(aggregate, fact)
        if attempt.execution_id != aggregate.intent.execution_id or attempt.command_kind != command_kind:
            raise PersistenceConflict("broker attempt does not belong to the execution command")
        payload = self._durable_command_payload(attempt)
        if command_kind == "SUBMIT":
            expected = {
                "execution_id": aggregate.intent.execution_id,
                "client_order_id": fact.client_order_id,
                "symbol": fact.symbol.canonical,
                "side": fact.side.value,
                "price_type": fact.price_type.value,
                "requested_shares": fact.requested_shares.value,
                "limit_price": fact.limit_price.canonical,
                "strategy_session": fact.session_date.isoformat(),
            }
            if {key: payload.get(key) for key in expected} != expected:
                raise PersistenceConflict("broker submit result contradicts durable command")
        elif payload.get("broker_order_id") != fact.broker_order_id:
            raise PersistenceConflict("broker cancel result contradicts durable command")

    def _ordered_fills(
        self,
        aggregate: OrderAggregate,
        fact: BrokerOrderFact,
        fills: tuple[BrokerFillFact, ...],
    ) -> tuple[BrokerFillFact, ...]:
        deduplicated: dict[str, BrokerFillFact] = {}
        for fill in fills:
            if (
                fill.broker_order_id != fact.broker_order_id
                or fill.symbol != fact.symbol
                or fill.side is not fact.side
                or fill.status is not FillStatus.CONFIRMED
                or fill.session_date != fact.session_date
                or fill.event_sequence <= 0
            ):
                raise PersistenceConflict("queried broker fill contradicts mapped order")
            existing = deduplicated.get(fill.broker_fill_id)
            if existing is not None:
                if canonical_sha256(
                    _fill_identity(existing, aggregate.intent.execution_id)
                ) != canonical_sha256(_fill_identity(fill, aggregate.intent.execution_id)):
                    raise PersistenceConflict("broker fill identity collision")
                continue
            deduplicated[fill.broker_fill_id] = fill
        return tuple(
            sorted(
                deduplicated.values(),
                key=lambda item: (item.session_date, item.event_sequence, item.broker_fill_id),
            )
        )

    @staticmethod
    def _proven_cumulative_shares(
        aggregate: OrderAggregate,
        fills: tuple[BrokerFillFact, ...],
    ) -> int:
        known_ids = {fill.broker_fill_id for fill in aggregate.fills}
        return aggregate.filled_shares.value + sum(
            fill.shares.value for fill in fills if fill.broker_fill_id not in known_ids
        )

    def _apply_broker_economics(
        self,
        aggregate: OrderAggregate,
        fact: BrokerOrderFact,
        fills: tuple[BrokerFillFact, ...],
        *,
        received_at: datetime,
    ) -> OrderAggregate:
        current = aggregate
        if current.broker_order_id is None or current.state not in {
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
        }:
            current = self.transition(
                current,
                BrokerAcknowledged(
                    event_id=_stable_event_id("ack", fact.broker_order_id, fact.event_sequence),
                    broker_order_id=fact.broker_order_id,
                ),
                occurred_at=received_at,
            )
        for fill in fills:
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
        return current

    def _apply_terminal_fact(
        self,
        current: OrderAggregate,
        fact: BrokerOrderFact,
        *,
        received_at: datetime,
    ) -> OrderAggregate:
        expected = _TERMINAL_STATE.get(fact.status)
        if expected is not None and current.state is expected:
            return current
        if fact.status is BrokerOrderStatus.REJECTED:
            return self.transition(
                current,
                BrokerRejected(
                    event_id=_stable_event_id("recovery-rejected", fact.broker_order_id, fact.event_sequence),
                    reason_code="BROKER_REJECTED",
                ),
                occurred_at=received_at,
            )
        if fact.status is BrokerOrderStatus.EXPIRED:
            return self.transition(
                current,
                OrderExpired(
                    event_id=_stable_event_id("recovery-expired", fact.broker_order_id, fact.event_sequence),
                    reason_code="BROKER_EXPIRED",
                ),
                occurred_at=received_at,
            )
        if fact.status is BrokerOrderStatus.CANCELLED:
            return self.transition(
                current,
                CancelConfirmed(
                    event_id=_stable_event_id(
                        "recovery-cancelled", fact.broker_order_id, fact.event_sequence
                    ),
                    broker_order_id=fact.broker_order_id,
                ),
                occurred_at=received_at,
            )
        return current

    def reconcile_broker_fact(
        self,
        aggregate: OrderAggregate,
        fact: BrokerOrderFact,
        fills: tuple[BrokerFillFact, ...],
        *,
        received_at: datetime,
    ) -> OrderAggregate:
        """Apply one complete authoritative broker observation without losing late fills."""

        self._validate_fact(aggregate, fact)
        ordered = self._ordered_fills(aggregate, fact, fills)
        if self._proven_cumulative_shares(aggregate, ordered) != fact.filled_shares.value:
            raise PersistenceConflict("queried broker fills do not prove cumulative shares")
        self._record_broker_order(fact, execution_id=aggregate.intent.execution_id)
        current = self._apply_broker_economics(aggregate, fact, ordered, received_at=received_at)
        if fact.status is BrokerOrderStatus.UNKNOWN:
            current = self.transition(
                current,
                SubmitOutcomeUnknown(
                    event_id=_stable_event_id("recovery-unknown", fact.broker_order_id, fact.event_sequence),
                    diagnostic_code="BROKER_STATUS_UNKNOWN",
                ),
                occurred_at=received_at,
            )
        else:
            current = self._apply_terminal_fact(current, fact, received_at=received_at)
        if current.filled_shares != fact.filled_shares:
            raise PersistenceConflict("recovered aggregate differs from broker cumulative fill")
        return current

    def _record_returned_result(
        self,
        aggregate: OrderAggregate,
        attempt: BrokerAttempt,
        fact: BrokerOrderFact,
        fills: tuple[BrokerFillFact, ...],
        *,
        command_kind: str,
        response_kind: str,
        received_at: datetime,
    ) -> OrderAggregate:
        self._validate_attempt_fact(aggregate, attempt, fact, command_kind=command_kind)
        ordered = self._ordered_fills(aggregate, fact, fills)
        proven = self._proven_cumulative_shares(aggregate, ordered)
        if proven > fact.filled_shares.value:
            raise PersistenceConflict("broker cumulative fill contradicts confirmed fills")
        self._complete_attempt(attempt, fact, response_kind=response_kind, received_at=received_at)
        current = self._apply_broker_economics(aggregate, fact, ordered, received_at=received_at)
        if proven < fact.filled_shares.value:
            return self.mark_attempt_unknown(
                current,
                attempt,
                diagnostic_code="BROKER_FILL_MISSING",
                occurred_at=received_at,
            )
        if fact.status is BrokerOrderStatus.UNKNOWN:
            return self.mark_attempt_unknown(
                current,
                attempt,
                diagnostic_code="BROKER_STATUS_UNKNOWN",
                occurred_at=received_at,
            )
        if command_kind == "CANCEL" and fact.status not in _TERMINAL:
            return self.mark_attempt_unknown(
                current,
                attempt,
                diagnostic_code="CANCEL_ACCEPTANCE_UNPROVEN",
                occurred_at=received_at,
            )
        current = self._apply_terminal_fact(current, fact, received_at=received_at)
        if current.filled_shares != fact.filled_shares:
            raise PersistenceConflict("returned broker result differs from cumulative fill")
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
        return self._record_returned_result(
            aggregate,
            attempt,
            fact,
            fills,
            command_kind="SUBMIT",
            response_kind="SUBMIT_RETURN",
            received_at=received_at,
        )

    def record_cancel_result(
        self,
        aggregate: OrderAggregate,
        attempt: BrokerAttempt,
        fact: BrokerOrderFact,
        fills: tuple[BrokerFillFact, ...],
        *,
        received_at: datetime,
    ) -> OrderAggregate:
        return self._record_returned_result(
            aggregate,
            attempt,
            fact,
            fills,
            command_kind="CANCEL",
            response_kind="CANCEL_RETURN",
            received_at=received_at,
        )

    def resolve_submit_not_accepted(
        self,
        aggregate: OrderAggregate,
        attempt: BrokerAttempt,
        *,
        evidence_sha256: str,
        occurred_at: datetime,
    ) -> OrderAggregate:
        """Resolve only explicit non-acceptance; UNKNOWN requires dedicated evidence."""

        event = (
            UnknownResolvedNotAccepted(
                event_id=_write_event_id("submit-not-accepted", attempt, evidence_sha256),
                evidence_sha256=evidence_sha256,
            )
            if aggregate.state is OrderState.UNKNOWN
            else SubmitNotAccepted(
                event_id=_write_event_id("submit-not-accepted", attempt, evidence_sha256),
                evidence_sha256=evidence_sha256,
            )
        )
        current = self.transition(aggregate, event, occurred_at=occurred_at)
        self.database.write(
            """
            UPDATE broker_order_attempts
            SET state = 'FAILED_LOCAL', completed_at = ?
            WHERE attempt_id = ?
            """,
            (occurred_at.isoformat(), attempt.attempt_id),
        )
        return current

    def resolve_cancel_not_accepted(
        self,
        aggregate: OrderAggregate,
        attempt: BrokerAttempt,
        *,
        evidence_sha256: str,
        occurred_at: datetime,
    ) -> OrderAggregate:
        """Restore the prior open state only for explicit cancel non-acceptance."""

        current = self.transition(
            aggregate,
            CancelNotAccepted(
                event_id=_write_event_id("cancel-not-accepted", attempt, evidence_sha256),
                evidence_sha256=evidence_sha256,
            ),
            occurred_at=occurred_at,
        )
        self.database.write(
            """
            UPDATE broker_order_attempts
            SET state = 'FAILED_LOCAL', completed_at = ?
            WHERE attempt_id = ?
            """,
            (occurred_at.isoformat(), attempt.attempt_id),
        )
        return current


__all__ = ("MonotonicExecutionLedgerRepository",)
