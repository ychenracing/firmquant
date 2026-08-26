"""Idempotent durable storage for complete broker account snapshots."""

from __future__ import annotations

from firmquant.domain.broker_facts import AccountType, BrokerSnapshot
from firmquant.persistence.audit import AuditLedger
from firmquant.persistence.database import Database


class BrokerSnapshotStore:
    def __init__(self, database: Database) -> None:
        if not isinstance(database, Database):
            raise TypeError("broker snapshot store requires Database")
        self._database = database

    def previous_account_identity(self, snapshot: BrokerSnapshot) -> tuple[str, AccountType]:
        if not isinstance(snapshot, BrokerSnapshot):
            raise TypeError("broker snapshot identity requires BrokerSnapshot")
        row = self._database.query_one(
            "SELECT account_id_hash, account_type FROM broker_snapshots "
            "ORDER BY captured_at DESC, snapshot_id DESC LIMIT 1"
        )
        if row is None:
            return snapshot.account.account_id_hash, snapshot.account.account_type
        try:
            return str(row["account_id_hash"]), AccountType(str(row["account_type"]))
        except ValueError as error:
            raise RuntimeError("stored broker account identity is invalid") from error

    @staticmethod
    def _parent(snapshot: BrokerSnapshot) -> tuple[object, ...]:
        return (
            snapshot.account.account_id_hash,
            snapshot.account.account_type.value,
            snapshot.session_date.isoformat(),
            snapshot.captured_at.isoformat(),
            snapshot.broker_event_watermark,
            snapshot.raw_payload_sha256,
            1,
        )

    @staticmethod
    def _positions(snapshot: BrokerSnapshot) -> tuple[tuple[object, ...], ...]:
        return tuple(
            (
                position.symbol.canonical,
                position.total_shares.value,
                position.sellable_shares.value,
                None if position.average_cost is None else position.average_cost.canonical,
                position.market_value.canonical,
            )
            for position in sorted(snapshot.positions, key=lambda item: item.symbol.canonical)
        )

    def persist(self, snapshot: BrokerSnapshot) -> bool:
        if not isinstance(snapshot, BrokerSnapshot) or not snapshot.complete:
            raise TypeError("broker snapshot store requires complete BrokerSnapshot")
        parent = self._parent(snapshot)
        cash = (
            snapshot.account.available_cash.canonical,
            snapshot.account.total_assets.canonical,
        )
        positions = self._positions(snapshot)
        with self._database.transaction():
            existing = self._database.query_one(
                "SELECT account_id_hash, account_type, session_date, captured_at, "
                "broker_event_watermark, raw_payload_sha256, complete "
                "FROM broker_snapshots WHERE snapshot_id = ?",
                (snapshot.snapshot_id,),
            )
            if existing is not None:
                stored_cash = self._database.query_one(
                    "SELECT available_cash, total_assets FROM cash_snapshots WHERE snapshot_id = ?",
                    (snapshot.snapshot_id,),
                )
                stored_positions = self._database.query_all(
                    "SELECT symbol, total_shares, sellable_shares, average_cost, market_value "
                    "FROM position_snapshots WHERE snapshot_id = ? ORDER BY symbol",
                    (snapshot.snapshot_id,),
                )
                if (
                    tuple(existing) != parent
                    or stored_cash is None
                    or tuple(stored_cash) != cash
                    or tuple(tuple(row) for row in stored_positions) != positions
                ):
                    raise RuntimeError("broker snapshot identity collision")
                return False
            self._database.write(
                """
                INSERT INTO broker_snapshots(
                    snapshot_id, account_id_hash, account_type, session_date, captured_at,
                    broker_event_watermark, raw_payload_sha256, complete
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (snapshot.snapshot_id, *parent),
            )
            self._database.write(
                "INSERT INTO cash_snapshots(snapshot_id, available_cash, total_assets) VALUES (?, ?, ?)",
                (snapshot.snapshot_id, *cash),
            )
            for position in positions:
                self._database.write(
                    """
                    INSERT INTO position_snapshots(
                        snapshot_id, symbol, total_shares, sellable_shares,
                        average_cost, market_value
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (snapshot.snapshot_id, *position),
                )
            AuditLedger(self._database).append(
                audit_event_id="broker-snapshot:" + snapshot.raw_payload_sha256,
                category="BROKER_SNAPSHOT",
                actor="production-snapshot-store",
                payload={
                    "schema": "firmquant.broker-snapshot-receipt.v1",
                    "snapshot_id": snapshot.snapshot_id,
                    "raw_payload_sha256": snapshot.raw_payload_sha256,
                    "session_date": snapshot.session_date,
                    "broker_event_watermark": snapshot.broker_event_watermark,
                    "position_count": len(snapshot.positions),
                    "order_count": len(snapshot.orders),
                    "fill_count": len(snapshot.fills),
                },
                created_at=snapshot.captured_at,
            )
        return True


__all__ = ("BrokerSnapshotStore",)
