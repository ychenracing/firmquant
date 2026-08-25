"""Transactional repositories and canonical JSON for the operational ledger."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final

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
class Repositories:
    broker_events: BrokerEventRepository
    decision_snapshots: DecisionSnapshotRepository

    @classmethod
    def bind(cls, database: Database) -> Repositories:
        return cls(
            broker_events=BrokerEventRepository(database),
            decision_snapshots=DecisionSnapshotRepository(database),
        )


__all__ = (
    "BrokerEventRepository",
    "DecisionSnapshotRepository",
    "PersistenceConflict",
    "Repositories",
    "canonical_json",
    "canonical_sha256",
)
