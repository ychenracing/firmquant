"""Monotonic production persistence over the generic execution ledger repository."""

from __future__ import annotations

import hashlib
from datetime import datetime

from firmquant.domain.broker_facts import BrokerOrderFact, BrokerOrderStatus
from firmquant.domain.events import CancelNotAccepted, SubmitNotAccepted
from firmquant.domain.orders import OrderAggregate

from .repositories import BrokerAttempt, ExecutionLedgerRepository, PersistenceConflict

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
    payload = f"{prefix}:{attempt.attempt_id}:{evidence_sha256}".encode("utf-8")
    return prefix + "-" + hashlib.sha256(payload).hexdigest()


class MonotonicExecutionLedgerRepository(ExecutionLedgerRepository):
    """Reject broker snapshots that regress cumulative fills or order lifecycle state."""

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
            "UPDATE broker_order_attempts SET state = 'FAILED_LOCAL', completed_at = ? "
            "WHERE attempt_id = ?",
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
            "UPDATE broker_order_attempts SET state = 'FAILED_LOCAL', completed_at = ? "
            "WHERE attempt_id = ?",
            (occurred_at.isoformat(), attempt.attempt_id),
        )
        return current


__all__ = ("MonotonicExecutionLedgerRepository",)
