from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from firmquant.persistence.audit import (
    AuditChainBroken,
    AuditLedger,
    AuditPayloadRejected,
)
from firmquant.persistence.database import Database
from firmquant.persistence.repositories import PersistenceConflict


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        yield database
    finally:
        database.close()


def test_audit_chain_round_trips_and_duplicate_identity_is_idempotent(db: Database) -> None:
    ledger = AuditLedger(db)
    created_at = datetime(2026, 8, 25, 1, tzinfo=UTC)
    with db.transaction():
        first = ledger.append(
            audit_event_id="audit-1",
            category="RUNTIME",
            actor="system",
            payload={"state": "STARTING"},
            created_at=created_at,
        )
        second = ledger.append(
            audit_event_id="audit-2",
            category="RECONCILIATION",
            actor="system",
            payload={"passed": True},
            created_at=created_at,
        )
    with db.transaction():
        assert (
            ledger.append(
                audit_event_id="audit-2",
                category="RECONCILIATION",
                actor="system",
                payload={"passed": True},
                created_at=created_at,
            )
            == second
        )

    verification = ledger.verify(
        expected_count=2,
        expected_head_hash=second.chain_hash,
    )
    assert verification.count == 2
    assert verification.head_hash == second.chain_hash
    assert second.previous_hash == first.chain_hash


def test_audit_event_id_collision_is_rejected(db: Database) -> None:
    ledger = AuditLedger(db)
    created_at = datetime(2026, 8, 25, 1, tzinfo=UTC)
    with db.transaction():
        ledger.append(
            audit_event_id="audit-1",
            category="RUNTIME",
            actor="system",
            payload={"state": "STARTING"},
            created_at=created_at,
        )

    with db.transaction(), pytest.raises(PersistenceConflict, match="audit event identity"):
        ledger.append(
            audit_event_id="audit-1",
            category="RUNTIME",
            actor="system",
            payload={"state": "HALTED"},
            created_at=created_at,
        )


def test_audit_chain_detects_database_tampering(db: Database) -> None:
    ledger = AuditLedger(db)
    with db.transaction():
        receipt = ledger.append(
            audit_event_id="audit-1",
            category="RUNTIME",
            actor="system",
            payload={"state": "STARTING"},
            created_at=datetime(2026, 8, 25, 1, tzinfo=UTC),
        )
    with db.transaction():
        db.write("DROP TRIGGER audit_events_reject_update")
        db.write(
            "UPDATE audit_events SET payload_json = ? WHERE audit_event_id = ?",
            ('{"state":"HALTED"}', "audit-1"),
        )

    with pytest.raises(AuditChainBroken, match="payload hash"):
        ledger.verify(expected_head_hash=receipt.chain_hash)


@pytest.mark.parametrize(
    "payload",
    [
        {"password": "do-not-store"},
        {"nested": {"account_number": "123456"}},
        {"webhook_token": "do-not-store"},
        {"xtquant_userdata_path": "C:/private"},
    ],
)
def test_audit_rejects_sensitive_payload_fields(db: Database, payload: dict[str, object]) -> None:
    ledger = AuditLedger(db)

    with db.transaction(), pytest.raises(AuditPayloadRejected, match="sensitive"):
        ledger.append(
            audit_event_id="audit-sensitive",
            category="SECURITY",
            actor="system",
            payload=payload,
            created_at=datetime(2026, 8, 25, 1, tzinfo=UTC),
        )
