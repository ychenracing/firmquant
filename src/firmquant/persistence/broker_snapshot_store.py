"""Idempotent durable storage for complete broker account snapshots."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from firmquant.domain.broker_facts import AccountType, BrokerSnapshot
from firmquant.persistence.audit import AuditLedger
from firmquant.persistence.database import Database

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class StoredBrokerSnapshot:
    snapshot_id: str
    account_id_hash: str
    account_type: AccountType
    captured_at: datetime
    broker_event_watermark: int
    raw_payload_sha256: str
    started_at: datetime | None
    completed_at: datetime | None
    duration_ms: int | None


def _timestamp(value: object, *, utc: bool) -> datetime:
    if not isinstance(value, str):
        raise ValueError
    parsed = datetime.fromisoformat(value)
    if parsed.isoformat() != value or parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError
    if utc and parsed.utcoffset() != timedelta(0):
        raise ValueError
    return parsed


def _timing_values(
    *,
    started_at: datetime | None,
    completed_at: datetime | None,
    duration_ms: int | None,
) -> tuple[str | None, str | None, int | None]:
    values = (started_at, completed_at, duration_ms)
    if all(value is None for value in values):
        return None, None, None
    if any(value is None for value in values):
        raise ValueError("snapshot timing must be all null or all set")
    if not isinstance(started_at, datetime) or not isinstance(completed_at, datetime):
        raise TypeError("snapshot timing values must be datetime")
    if started_at.utcoffset() != timedelta(0) or completed_at.utcoffset() != timedelta(0):
        raise ValueError("snapshot timing values must be UTC")
    if completed_at < started_at:
        raise ValueError("snapshot completion precedes start")
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, int):
        raise TypeError("snapshot duration must be an integer")
    if duration_ms < 0:
        raise ValueError("snapshot duration must be nonnegative")
    return started_at.isoformat(), completed_at.isoformat(), duration_ms


class BrokerSnapshotStore:
    def __init__(self, database: Database) -> None:
        if not isinstance(database, Database):
            raise TypeError("broker snapshot store requires Database")
        self._database = database

    def previous_account_identity(self, snapshot: BrokerSnapshot) -> tuple[str, AccountType]:
        if not isinstance(snapshot, BrokerSnapshot):
            raise TypeError("broker snapshot identity requires BrokerSnapshot")
        stored = self.latest()
        if stored is None:
            return snapshot.account.account_id_hash, snapshot.account.account_type
        return stored.account_id_hash, stored.account_type

    @staticmethod
    def _parent(
        snapshot: BrokerSnapshot,
        *,
        timing: tuple[str | None, str | None, int | None],
    ) -> tuple[object, ...]:
        return (
            snapshot.account.account_id_hash,
            snapshot.account.account_type.value,
            snapshot.session_date.isoformat(),
            snapshot.captured_at.astimezone(UTC).isoformat(),
            snapshot.broker_event_watermark,
            snapshot.raw_payload_sha256,
            1,
            *timing,
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

    @staticmethod
    def _stored(row: object) -> StoredBrokerSnapshot:
        if not hasattr(row, "__getitem__"):
            raise RuntimeError("stored broker snapshot is invalid")
        try:
            snapshot_id = row["snapshot_id"]
            account_id_hash = row["account_id_hash"]
            raw_payload_sha256 = row["raw_payload_sha256"]
            watermark = row["broker_event_watermark"]
            complete = row["complete"]
            if (
                not isinstance(snapshot_id, str)
                or not snapshot_id
                or snapshot_id != snapshot_id.strip()
                or not isinstance(account_id_hash, str)
                or _SHA256.fullmatch(account_id_hash) is None
                or not isinstance(raw_payload_sha256, str)
                or _SHA256.fullmatch(raw_payload_sha256) is None
                or type(watermark) is not int
                or watermark < 0
                or type(complete) is not int
                or complete != 1
                or row["cash_snapshot_id"] != snapshot_id
            ):
                raise ValueError

            started_raw = row["started_at"]
            completed_raw = row["completed_at"]
            duration_raw = row["duration_ms"]
            if duration_raw is not None and type(duration_raw) is not int:
                raise ValueError
            timing = _timing_values(
                started_at=(None if started_raw is None else _timestamp(started_raw, utc=True)),
                completed_at=(None if completed_raw is None else _timestamp(completed_raw, utc=True)),
                duration_ms=duration_raw,
            )
            captured_at = _timestamp(row["captured_at"], utc=False).astimezone(UTC)
            stored = StoredBrokerSnapshot(
                snapshot_id=snapshot_id,
                account_id_hash=account_id_hash,
                account_type=AccountType(str(row["account_type"])),
                captured_at=captured_at,
                broker_event_watermark=watermark,
                raw_payload_sha256=raw_payload_sha256,
                started_at=None if timing[0] is None else datetime.fromisoformat(timing[0]),
                completed_at=None if timing[1] is None else datetime.fromisoformat(timing[1]),
                duration_ms=timing[2],
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError("stored broker snapshot is invalid") from error
        return stored

    def load(self, snapshot_id: str) -> StoredBrokerSnapshot | None:
        if not isinstance(snapshot_id, str):
            raise TypeError("snapshot id must be text")
        if not snapshot_id:
            raise ValueError("snapshot id must be non-empty")
        row = self._database.query_one(
            """
            SELECT b.snapshot_id,b.account_id_hash,b.account_type,b.captured_at,
                   b.broker_event_watermark,b.raw_payload_sha256,b.complete,
                   b.started_at,b.completed_at,b.duration_ms,
                   c.snapshot_id AS cash_snapshot_id
            FROM broker_snapshots AS b
            LEFT JOIN cash_snapshots AS c ON c.snapshot_id=b.snapshot_id
            WHERE b.snapshot_id=?
            """,
            (snapshot_id,),
        )
        return None if row is None else self._stored(row)

    def latest(self) -> StoredBrokerSnapshot | None:
        rows = self._database.query_all("SELECT snapshot_id,captured_at FROM broker_snapshots")
        if not rows:
            return None
        try:
            row = max(
                rows,
                key=lambda item: (
                    _timestamp(item["captured_at"], utc=False),
                    str(item["snapshot_id"]),
                ),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError("stored broker snapshot is invalid") from error
        stored = self.load(str(row["snapshot_id"]))
        if stored is None:
            raise RuntimeError("stored broker snapshot is invalid")
        return stored

    def persist(
        self,
        snapshot: BrokerSnapshot,
        *,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
        duration_ms: int | None = None,
    ) -> bool:
        if not isinstance(snapshot, BrokerSnapshot) or not snapshot.complete:
            raise TypeError("broker snapshot store requires complete BrokerSnapshot")
        timing = _timing_values(
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=duration_ms,
        )
        parent = self._parent(snapshot, timing=timing)
        cash = (
            snapshot.account.available_cash.canonical,
            snapshot.account.total_assets.canonical,
        )
        positions = self._positions(snapshot)
        with self._database.transaction():
            existing = self._database.query_one(
                "SELECT account_id_hash, account_type, session_date, captured_at, "
                "broker_event_watermark, raw_payload_sha256, complete, "
                "started_at, completed_at, duration_ms "
                "FROM broker_snapshots WHERE snapshot_id = ?",
                (snapshot.snapshot_id,),
            )
            if existing is not None:
                existing_parent = list(existing)
                try:
                    existing_parent[3] = _timestamp(existing_parent[3], utc=False).astimezone(UTC).isoformat()
                except (IndexError, TypeError, ValueError) as error:
                    raise RuntimeError("stored broker snapshot is invalid") from error
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
                    tuple(existing_parent) != parent
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
                    broker_event_watermark, raw_payload_sha256, complete,
                    started_at, completed_at, duration_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                created_at=snapshot.captured_at.astimezone(UTC),
            )
        return True


__all__ = ("BrokerSnapshotStore", "StoredBrokerSnapshot")
