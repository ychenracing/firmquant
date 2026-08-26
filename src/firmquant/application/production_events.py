"""Single-writer durable journal for normalized production broker callbacks."""

from __future__ import annotations

import hashlib

from firmquant.broker.normalization import (
    BrokerEventEnvelope,
    BrokerEventType,
    BrokerOperationalFact,
)
from firmquant.persistence.audit import AuditLedger
from firmquant.persistence.database import Database
from firmquant.persistence.repositories import BrokerEventRepository, canonical_json


class ProductionEventJournal:
    """Persist callbacks idempotently and surface the first operational halt reason."""

    def __init__(self, database: Database) -> None:
        if not isinstance(database, Database):
            raise TypeError("production event journal requires Database")
        self._database = database
        self._events = BrokerEventRepository(database)
        self._pending_halt_reason: str | None = None

    @property
    def pending_halt_reason(self) -> str | None:
        return self._pending_halt_reason

    @staticmethod
    def _risk_code(event_type: BrokerEventType) -> str | None:
        return {
            BrokerEventType.ORDER_ERROR: "BROKER_ORDER_ERROR",
            BrokerEventType.CANCEL_ERROR: "BROKER_CANCEL_ERROR",
            BrokerEventType.DISCONNECTED: "BROKER_DISCONNECTED",
        }.get(event_type)

    def _append_risk(self, event: BrokerEventEnvelope, fact: BrokerOperationalFact) -> str:
        code = self._risk_code(event.event_type)
        if code is None:
            raise ValueError("economic broker event cannot create operational risk")
        payload = {
            "schema": "firmquant.broker-operational-risk.v1",
            "broker_event_id": event.broker_event_id,
            "event_type": event.event_type.value,
            "broker_order_id": fact.broker_order_id,
            "client_order_id": fact.client_order_id,
            "error_code": fact.error_code,
            "message_sha256": fact.message_sha256,
        }
        payload_json = canonical_json(payload)
        payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        risk_event_id = "broker-risk-" + hashlib.sha256(event.broker_event_id.encode()).hexdigest()
        self._database.write(
            """
            INSERT INTO risk_events(
                risk_event_id, severity, code, execution_id, symbol,
                payload_json, payload_sha256, created_at
            ) VALUES (?, 'CRITICAL', ?, NULL, NULL, ?, ?, ?)
            """,
            (
                risk_event_id,
                code,
                payload_json,
                payload_sha256,
                event.received_at.isoformat(),
            ),
        )
        AuditLedger(self._database).append(
            audit_event_id=risk_event_id + ":audit",
            category="RISK",
            actor="production-event-journal",
            payload={
                "schema": "firmquant.broker-operational-risk.v1",
                "risk_event_id": risk_event_id,
                "broker_event_id": event.broker_event_id,
                "code": code,
                "payload_sha256": payload_sha256,
            },
            created_at=event.received_at,
        )
        return code

    def append(self, event: BrokerEventEnvelope) -> bool:
        if not isinstance(event, BrokerEventEnvelope):
            raise TypeError("production event journal requires BrokerEventEnvelope")
        with self._database.transaction():
            inserted = self._events.append(
                broker_event_id=event.broker_event_id,
                event_type=event.event_type.value,
                broker_sequence=event.broker_sequence,
                session_date=event.session_date,
                event_time=event.event_time,
                received_at=event.received_at,
                safe_payload=event.safe_payload,
                raw_payload_sha256=event.raw_payload_sha256,
            )
            if not inserted:
                return False
            if isinstance(event.fact, BrokerOperationalFact):
                reason = self._append_risk(event, event.fact)
                if self._pending_halt_reason is None:
                    self._pending_halt_reason = reason
        return True


__all__ = ("ProductionEventJournal",)
