"""Monotonic production persistence over the generic execution ledger repository."""

from __future__ import annotations

from firmquant.domain.broker_facts import BrokerOrderFact, BrokerOrderStatus

from .repositories import ExecutionLedgerRepository, PersistenceConflict

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


__all__ = ("MonotonicExecutionLedgerRepository",)
