"""Monotonic production persistence over the generic execution ledger repository."""

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
)
from firmquant.domain.orders import OrderAggregate

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


def _write_event_id(prefix: str, attempt: BrokerAttempt, evidence_sha256: str) -> str:
    payload = f"{prefix}:{attempt.attempt_id}:{evidence_sha256}".encode()
    return prefix + "-" + hashlib.sha256(payload).hexdigest()


def _stable_event_id(prefix: str, *parts: object) -> str:
    payload = canonical_json({"prefix": prefix, "parts": parts})
    return prefix + "-" + hashlib.sha256(payload.encode()).hexdigest()


def _fill_identity_payload(fill: BrokerFillFact, execution_id: str) -> dict[str, object]:
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
            "identity": _fill_identity_payload(fill, execution_id),
            "event_time": fill.event_time,
            "received_at": fill.received_at,
            "raw_payload_sha256": fill.raw_payload_sha256,
        }
    )


class MonotonicExecutionLedgerRepository(ExecutionLedgerRepository):
    """Reject broker snapshots that regress order, fill, or execution sequence truth."""

    def _record_broker_order(self, fact: BrokerOrderFact, *, execution_id: str) -> None:
        existing = self.database.query_one(
            "SELECT * FROM broker_orders WHERE broker_order_id = ?",
            (fact.broker_order_id,),
        )
        if existing is None:
            super()._record_broker_order(fact, execution_id=execution_id)
            return

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
            and fact.status is not previous_status
            and _STATUS_RANK[fact.status] < _STATUS_RANK[previous_status]
        ):
            raise PersistenceConflict("broker order lifecycle status regressed")
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
        identity = _fill_identity_payload(fill, execution_id)
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

        identity = _fill_identity_payload(fill, execution_id)
        identity_sha256 = canonical_sha256(identity)
        existing = self.database.query_one(
            "SELECT * FROM fills WHERE broker_fill_id = ?",
            (fill.broker_fill_id,),
        )
        if existing is not None:
            observed = (
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
            expected = (
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
            if observed != expected:
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
        if latest is not None and latest["max_sequence"] is not None:
            if fill.event_sequence <= int(latest["max_sequence"]):
                raise PersistenceConflict("broker fill execution sequence regressed")

        self._record_fill_evidence(fill, execution_id=execution_id)
        super()._record_fill(fill, execution_id=execution_id)

    def reconcile_broker_fact(
        self,
        aggregate: OrderAggregate,
        fact: BrokerOrderFact,
        fills: tuple[BrokerFillFact, ...],
        *,
        received_at: datetime,
    ) -> OrderAggregate:
        """Apply complete queried broker truth without dropping terminal or late fills."""

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
            sorted(
                fills,
                key=lambda item: (item.session_date, item.event_sequence, item.broker_fill_id),
            )
        )
        if any(
            fill.broker_order_id != fact.broker_order_id
            or fill.symbol != fact.symbol
            or fill.side is not fact.side
            or fill.status is not FillStatus.CONFIRMED
            or fill.session_date != fact.session_date
            or fill.event_sequence <= 0
            for fill in ordered_fills
        ):
            raise PersistenceConflict("queried broker fill contradicts mapped order")
        if sum(fill.shares.value for fill in ordered_fills) != fact.filled_shares.value:
            raise PersistenceConflict("queried broker fills do not prove cumulative shares")

        self._record_broker_order(fact, execution_id=aggregate.intent.execution_id)
        current = self.transition(
            aggregate,
            BrokerAcknowledged(
                event_id=_stable_event_id("ack", fact.broker_order_id, fact.event_sequence),
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

        if fact.status is BrokerOrderStatus.REJECTED:
            current = self.transition(
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
        elif fact.status is BrokerOrderStatus.EXPIRED:
            current = self.transition(
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
        elif fact.status is BrokerOrderStatus.CANCELLED:
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
        elif fact.status is BrokerOrderStatus.UNKNOWN:
            current = self.transition(
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
        if current.filled_shares != fact.filled_shares:
            raise PersistenceConflict("recovered aggregate differs from broker cumulative fill")
        return current

    def resolve_submit_not_accepted(
        self,
        aggregate: OrderAggregate,
        attempt: BrokerAttempt,
        *,
        evidence_sha256: str,
        occurred_at: datetime,
    ) -> OrderAggregate:
        """Return SUBMITTING to ARMED when rejection happened before broker acceptance."""

        current = self.transition(
            aggregate,
            SubmitNotAccepted(
                event_id=_write_event_id("submit-not-accepted", attempt, evidence_sha256),
                evidence_sha256=evidence_sha256,
            ),
            occurred_at=occurred_at,
        )
        self.database.write(
            "UPDATE broker_order_attempts SET state = 'FAILED_LOCAL', completed_at = ? WHERE attempt_id = ?",
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
        """Restore the open order state after a definitely rejected cancel attempt."""

        current = self.transition(
            aggregate,
            CancelNotAccepted(
                event_id=_write_event_id("cancel-not-accepted", attempt, evidence_sha256),
                evidence_sha256=evidence_sha256,
            ),
            occurred_at=occurred_at,
        )
        self.database.write(
            "UPDATE broker_order_attempts SET state = 'FAILED_LOCAL', completed_at = ? WHERE attempt_id = ?",
            (occurred_at.isoformat(), attempt.attempt_id),
        )
        return current


__all__ = ("MonotonicExecutionLedgerRepository",)
