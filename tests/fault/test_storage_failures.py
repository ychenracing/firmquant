from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from firmquant.domain.orders import OrderState
from firmquant.persistence.database import Database, DatabaseCorrupt
from firmquant.persistence.recovery import AccountOperation, RecoveryService
from firmquant.persistence.writer_lease import WriterLease, WriterLeaseBusy
from tests.fixtures.recovery_cases import (
    NOW,
    JsonAccountStateStore,
    create_submitting_case,
    fake_recovery_broker,
    write_account,
)


def test_sqlite_lock_prevents_recovery_without_any_broker_write(tmp_path: Path) -> None:
    path = tmp_path / "firmquant.sqlite3"
    database = Database.open(path, busy_timeout_ms=20)
    locker: sqlite3.Connection | None = None
    try:
        case = create_submitting_case(database)
        broker = fake_recovery_broker()
        recovery = RecoveryService(
            database=database,
            account_store=None,
            account_path=None,
            gateway=broker,
            clock=lambda: NOW,
        )
        locker = sqlite3.connect(path, isolation_level=None)
        locker.execute("BEGIN IMMEDIATE")

        with pytest.raises(sqlite3.OperationalError, match="locked"):
            recovery.recover()

        assert broker.submitted_commands == ()
        assert broker.cancelled_order_ids == ()
        locker.rollback()
        locker.close()
        locker = None

        report = recovery.recover()
        aggregate = case.repository.load(case.aggregate.intent.execution_id)
        assert aggregate is not None
        assert aggregate.state is OrderState.UNKNOWN
        assert report.halt_required is True
        assert broker.submitted_commands == ()
        assert broker.cancelled_order_ids == ()
    finally:
        if locker is not None:
            locker.rollback()
            locker.close()
        database.close()


def test_corrupt_database_is_preserved_as_incident_evidence(tmp_path: Path) -> None:
    path = tmp_path / "firmquant.sqlite3"
    incident = b"not-a-sqlite-database\x00incident-evidence"
    path.write_bytes(incident)

    with pytest.raises(DatabaseCorrupt):
        Database.open(path)

    assert path.read_bytes() == incident


def test_second_instance_cannot_obtain_account_writer_authority(tmp_path: Path) -> None:
    path = tmp_path / "firmquant.sqlite3"
    first = WriterLease.acquire(path, owner="first-instance")
    try:
        with pytest.raises(WriterLeaseBusy, match="writer lease"):
            WriterLease.acquire(path, owner="second-instance")
    finally:
        first.release()

    takeover = WriterLease.acquire(path, owner="second-instance")
    takeover.release()


def test_interrupted_account_file_halts_and_is_not_auto_overwritten(
    tmp_path: Path,
) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    account_path = tmp_path / "account.json"
    store = JsonAccountStateStore()
    before = {"cash": "1000"}
    after = {"cash": "900"}
    write_account(account_path, before, store)
    AccountOperation.begin(
        database=database,
        store=store,
        account_path=account_path,
        prepared_account=after,
        expected_before_sha256=store.hash_state(before),
        operation_kind="BROKER_SYNC",
        evidence_sha256="a" * 64,
        now=NOW,
        operation_id="acctop_" + "b" * 64,
    )
    interrupted = b'{"cash":'
    account_path.write_bytes(interrupted)
    try:
        report = RecoveryService(
            database=database,
            account_store=store,
            account_path=account_path,
            gateway=None,
            clock=lambda: NOW,
        ).recover()

        assert report.halt_required is True
        assert "ACCOUNT_OPERATION_CONTRADICTION" in report.blockers
        assert account_path.read_bytes() == interrupted
        row = database.query_one(
            "SELECT stage FROM account_operations WHERE operation_id = ?",
            ("acctop_" + "b" * 64,),
        )
        assert row is not None
        assert row["stage"] == "CONTRADICTION"
    finally:
        database.close()
