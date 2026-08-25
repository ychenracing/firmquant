"""Append-only canonical audit hash chain with payload safety checks."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Never

from .database import Database, PersistenceError
from .repositories import PersistenceConflict, canonical_json


class AuditChainBroken(PersistenceError):
    """Raised when audit history cannot reproduce its recorded chain head."""


class AuditPayloadRejected(PersistenceError):
    """Raised before a sensitive field can enter the audit ledger."""


_ZERO_HASH: Final = "0" * 64
_SAFE_NAME: Final = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
_SENSITIVE_KEY_PARTS: Final = (
    "password",
    "secret",
    "token",
    "private_key",
    "account_number",
    "account_id",
    "userdata_path",
    "communication_key",
)


@dataclass(frozen=True, slots=True)
class AuditReceipt:
    sequence: int
    audit_event_id: str
    payload_sha256: str
    previous_hash: str
    chain_hash: str
    created_at: str


@dataclass(frozen=True, slots=True)
class AuditVerification:
    count: int
    head_hash: str


def _canonical_name(value: str, *, label: str) -> None:
    if not isinstance(value, str) or _SAFE_NAME.fullmatch(value) is None:
        raise AuditPayloadRejected(f"{label} must be canonical non-sensitive text")


def _assert_safe_payload(value: object, *, path: str = "payload") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise AuditPayloadRejected("audit payload keys must be text")
            normalized = key.lower()
            if not normalized.endswith(("_hash", "_sha", "_sha256")) and any(
                part in normalized for part in _SENSITIVE_KEY_PARTS
            ):
                raise AuditPayloadRejected(f"audit payload contains sensitive field: {path}.{key}")
            _assert_safe_payload(item, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_safe_payload(item, path=f"{path}[{index}]")


def _reject_constant(value: str) -> Never:
    raise AuditChainBroken(f"audit payload contains non-standard JSON constant: {value}")


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise AuditChainBroken(f"audit payload contains duplicate key: {key}")
        result[key] = value
    return result


def _parse_payload(payload_json: str) -> object:
    try:
        return json.loads(
            payload_json,
            object_pairs_hook=_pairs,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise AuditChainBroken("audit payload is not valid JSON") from exc


def _chain_hash(
    *,
    sequence: int,
    audit_event_id: str,
    category: str,
    actor: str,
    payload_sha256: str,
    previous_hash: str,
    created_at: str,
) -> str:
    envelope = {
        "schema": "firmquant.audit-event.v1",
        "sequence": sequence,
        "audit_event_id": audit_event_id,
        "category": category,
        "actor": actor,
        "payload_sha256": payload_sha256,
        "previous_hash": previous_hash,
        "created_at": created_at,
    }
    return hashlib.sha256(canonical_json(envelope).encode("utf-8")).hexdigest()


class AuditLedger:
    """Write and verify audit events on the caller's SQLite transaction."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def append(
        self,
        *,
        audit_event_id: str,
        category: str,
        actor: str,
        payload: Mapping[str, object],
        created_at: datetime,
    ) -> AuditReceipt:
        _canonical_name(audit_event_id, label="audit event id")
        _canonical_name(category, label="audit category")
        _canonical_name(actor, label="audit actor")
        if not isinstance(created_at, datetime):
            raise AuditPayloadRejected("audit created_at must be datetime")
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise AuditPayloadRejected("audit created_at must be timezone-aware")
        _assert_safe_payload(payload)
        payload_json = canonical_json(payload)
        payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        created_at_text = created_at.isoformat()

        existing = self._database.query_one(
            """
            SELECT sequence, category, actor, payload_json, payload_sha256,
                   previous_hash, chain_hash, created_at
            FROM audit_events WHERE audit_event_id = ?
            """,
            (audit_event_id,),
        )
        if existing is not None:
            if (
                existing["category"] == category
                and existing["actor"] == actor
                and existing["payload_json"] == payload_json
                and existing["payload_sha256"] == payload_sha256
                and existing["created_at"] == created_at_text
            ):
                return AuditReceipt(
                    sequence=int(existing["sequence"]),
                    audit_event_id=audit_event_id,
                    payload_sha256=payload_sha256,
                    previous_hash=str(existing["previous_hash"]),
                    chain_hash=str(existing["chain_hash"]),
                    created_at=created_at_text,
                )
            raise PersistenceConflict(f"audit event identity collision: {audit_event_id}")

        previous = self._database.query_one(
            "SELECT sequence, chain_hash FROM audit_events ORDER BY sequence DESC LIMIT 1"
        )
        sequence = 1 if previous is None else int(previous["sequence"]) + 1
        previous_hash = _ZERO_HASH if previous is None else str(previous["chain_hash"])
        chain_hash = _chain_hash(
            sequence=sequence,
            audit_event_id=audit_event_id,
            category=category,
            actor=actor,
            payload_sha256=payload_sha256,
            previous_hash=previous_hash,
            created_at=created_at_text,
        )
        self._database.write(
            """
            INSERT INTO audit_events(
                sequence, audit_event_id, category, actor, payload_json, payload_sha256,
                previous_hash, chain_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sequence,
                audit_event_id,
                category,
                actor,
                payload_json,
                payload_sha256,
                previous_hash,
                chain_hash,
                created_at_text,
            ),
        )
        return AuditReceipt(
            sequence=sequence,
            audit_event_id=audit_event_id,
            payload_sha256=payload_sha256,
            previous_hash=previous_hash,
            chain_hash=chain_hash,
            created_at=created_at_text,
        )

    def verify(
        self,
        *,
        expected_count: int | None = None,
        expected_head_hash: str | None = None,
    ) -> AuditVerification:
        """Rebuild every payload and link, optionally binding an external chain head."""

        rows = self._database.query_all(
            """
            SELECT sequence, audit_event_id, category, actor, payload_json, payload_sha256,
                   previous_hash, chain_hash, created_at
            FROM audit_events ORDER BY sequence
            """
        )
        previous_hash = _ZERO_HASH
        for expected_sequence, row in enumerate(rows, start=1):
            sequence = int(row["sequence"])
            if sequence != expected_sequence:
                raise AuditChainBroken("audit sequence is not contiguous")
            payload_json = str(row["payload_json"])
            payload = _parse_payload(payload_json)
            if canonical_json(payload) != payload_json:
                raise AuditChainBroken(f"audit payload is not canonical at sequence {sequence}")
            payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
            if payload_sha256 != row["payload_sha256"]:
                raise AuditChainBroken(f"audit payload hash mismatch at sequence {sequence}")
            if row["previous_hash"] != previous_hash:
                raise AuditChainBroken(f"audit previous hash mismatch at sequence {sequence}")
            observed_chain = _chain_hash(
                sequence=sequence,
                audit_event_id=str(row["audit_event_id"]),
                category=str(row["category"]),
                actor=str(row["actor"]),
                payload_sha256=payload_sha256,
                previous_hash=previous_hash,
                created_at=str(row["created_at"]),
            )
            if observed_chain != row["chain_hash"]:
                raise AuditChainBroken(f"audit chain hash mismatch at sequence {sequence}")
            previous_hash = observed_chain
        if expected_count is not None and len(rows) != expected_count:
            raise AuditChainBroken(
                f"audit count mismatch: expected {expected_count}, observed {len(rows)}"
            )
        if expected_head_hash is not None and previous_hash != expected_head_hash:
            raise AuditChainBroken(
                f"audit head mismatch: expected {expected_head_hash}, observed {previous_hash}"
            )
        return AuditVerification(count=len(rows), head_hash=previous_hash)


__all__ = (
    "AuditChainBroken",
    "AuditLedger",
    "AuditPayloadRejected",
    "AuditReceipt",
    "AuditVerification",
)
