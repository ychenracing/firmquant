"""Ordered durable checkpoints for the single end-of-day close session."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from firmquant.persistence.audit import AuditLedger
from firmquant.persistence.database import Database
from firmquant.persistence.repositories import canonical_json


class CloseCheckpointError(RuntimeError):
    """Close-session checkpoint evidence is missing, corrupt, or contradictory."""


class CloseStep(StrEnum):
    EOD_RECONCILED = "EOD_RECONCILED"
    DATA_VALIDATED = "DATA_VALIDATED"
    DECISION_COMMITTED = "DECISION_COMMITTED"
    REPORT_PUBLISHED = "REPORT_PUBLISHED"
    BACKUP_VERIFIED = "BACKUP_VERIFIED"
    COMPLETED = "COMPLETED"


_STEP_ORDER = tuple(CloseStep)


def _event_id(session: date, step: CloseStep) -> str:
    return f"close-session:{session.isoformat()}:{step.value}"


def _decode(value: object) -> dict[str, object]:
    if not isinstance(value, str):
        raise CloseCheckpointError("close-session checkpoint payload is not JSON text")
    try:
        parsed: object = json.loads(value)
    except json.JSONDecodeError as error:
        raise CloseCheckpointError("close-session checkpoint payload is invalid JSON") from error
    if not isinstance(parsed, dict) or not all(isinstance(key, str) for key in parsed):
        raise CloseCheckpointError("close-session checkpoint payload is not an object")
    return parsed


@dataclass(frozen=True, slots=True)
class CloseCheckpoint:
    session: date
    step: CloseStep
    evidence: Mapping[str, object]
    evidence_sha256: str
    created_at: datetime


class CloseCheckpointStore:
    """Append exactly one immutable receipt per ordered close-session boundary."""

    def __init__(self, database: Database) -> None:
        if not isinstance(database, Database):
            raise TypeError("close-session checkpoint store requires Database")
        self._database = database

    def load(self, session: date, step: CloseStep) -> CloseCheckpoint | None:
        if type(session) is not date or not isinstance(step, CloseStep):
            raise TypeError("close-session checkpoint identity is invalid")
        row = self._database.query_one(
            "SELECT payload_json, created_at FROM audit_events WHERE audit_event_id = ?",
            (_event_id(session, step),),
        )
        if row is None:
            return None
        payload = _decode(row["payload_json"])
        if (
            payload.get("schema") != "firmquant.close-session-checkpoint.v1"
            or payload.get("session") != session.isoformat()
            or payload.get("step") != step.value
        ):
            raise CloseCheckpointError("close-session checkpoint identity changed")
        evidence = payload.get("evidence")
        evidence_sha256 = payload.get("evidence_sha256")
        if not isinstance(evidence, dict) or not isinstance(evidence_sha256, str):
            raise CloseCheckpointError("close-session checkpoint evidence is malformed")
        observed = hashlib.sha256(canonical_json(evidence).encode()).hexdigest()
        if observed != evidence_sha256:
            raise CloseCheckpointError("close-session checkpoint evidence digest changed")
        try:
            created_at = datetime.fromisoformat(str(row["created_at"]))
        except ValueError as error:
            raise CloseCheckpointError("close-session checkpoint timestamp is invalid") from error
        return CloseCheckpoint(
            session=session,
            step=step,
            evidence=evidence,
            evidence_sha256=evidence_sha256,
            created_at=created_at,
        )

    def _require_predecessors(self, session: date, step: CloseStep) -> None:
        index = _STEP_ORDER.index(step)
        for predecessor in _STEP_ORDER[:index]:
            if self.load(session, predecessor) is None:
                raise CloseCheckpointError(
                    f"close-session checkpoint predecessor is missing: {predecessor.value}"
                )

    def append(
        self,
        session: date,
        step: CloseStep,
        *,
        evidence: Mapping[str, Any],
        created_at: datetime,
    ) -> CloseCheckpoint:
        if type(session) is not date or not isinstance(step, CloseStep):
            raise TypeError("close-session checkpoint identity is invalid")
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("close-session checkpoint time must be timezone-aware")
        if not isinstance(evidence, Mapping) or not all(isinstance(key, str) for key in evidence):
            raise TypeError("close-session checkpoint evidence must be a text-keyed mapping")
        self._require_predecessors(session, step)
        normalized = json.loads(canonical_json(dict(evidence)))
        if not isinstance(normalized, dict):
            raise CloseCheckpointError("close-session checkpoint evidence is not canonical")
        evidence_sha256 = hashlib.sha256(canonical_json(normalized).encode()).hexdigest()
        existing = self.load(session, step)
        if existing is not None:
            if existing.evidence_sha256 != evidence_sha256:
                raise CloseCheckpointError("close-session checkpoint conflicts with durable evidence")
            return existing
        payload = {
            "schema": "firmquant.close-session-checkpoint.v1",
            "session": session.isoformat(),
            "step": step.value,
            "evidence": normalized,
            "evidence_sha256": evidence_sha256,
        }
        with self._database.transaction():
            AuditLedger(self._database).append(
                audit_event_id=_event_id(session, step),
                category="CLOSE_SESSION",
                actor="production-services",
                payload=payload,
                created_at=created_at,
            )
        stored = self.load(session, step)
        if stored is None:
            raise CloseCheckpointError("close-session checkpoint was not durable")
        return stored

    def completed(self, session: date) -> CloseCheckpoint | None:
        return self.load(session, CloseStep.COMPLETED)

    def latest_completed_session(self) -> date | None:
        row = self._database.query_one(
            "SELECT payload_json FROM audit_events WHERE category = 'CLOSE_SESSION' "
            "AND json_extract(payload_json, '$.step') = 'COMPLETED' "
            "ORDER BY created_at DESC, sequence DESC LIMIT 1"
        )
        if row is None:
            return None
        payload = _decode(row["payload_json"])
        try:
            return date.fromisoformat(str(payload["session"]))
        except (KeyError, ValueError) as error:
            raise CloseCheckpointError("latest close-session receipt has invalid session") from error


__all__ = (
    "CloseCheckpoint",
    "CloseCheckpointError",
    "CloseCheckpointStore",
    "CloseStep",
)
