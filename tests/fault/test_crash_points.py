from __future__ import annotations

import sqlite3
from dataclasses import replace
from enum import StrEnum
from pathlib import Path

import pytest

from firmquant.domain.broker_facts import BrokerOrderStatus
from firmquant.domain.orders import OrderState
from firmquant.persistence.database import Database, DatabaseCorrupt
from firmquant.persistence.recovery import AccountOperation, RecoveryService
from tests.fixtures.recovery_cases import (
    NOW,
    JsonAccountStateStore,
    acknowledge_locally,
    broker_fill,
    broker_order,
    cancelled_locally,
    create_submitting_case,
    fake_recovery_broker,
    write_account,
)


class CrashPoint(StrEnum):
    BEFORE_INTENT_PERSIST = "BEFORE_INTENT_PERSIST"
    SUBMITTING_BEFORE_BROKER = "SUBMITTING_BEFORE_BROKER"
    BROKER_ACCEPTED_BEFORE_ID = "BROKER_ACCEPTED_BEFORE_ID"
    PARTIAL_FILL_BEFORE_TRANSACTION = "PARTIAL_FILL_BEFORE_TRANSACTION"
    CANCEL_BEFORE_CONFIRMATION = "CANCEL_BEFORE_CONFIRMATION"
    LATE_FILL_ON_RESTART = "LATE_FILL_ON_RESTART"
    SQLITE_LOCKED = "SQLITE_LOCKED"
    SQLITE_CORRUPT = "SQLITE_CORRUPT"
    ACCOUNT_WRITE_INTERRUPTED = "ACCOUNT_WRITE_INTERRUPTED"
    BROKER_CLIENT_RESTART = "BROKER_CLIENT_RESTART"


CRITICAL_CRASH_POINTS = tuple(CrashPoint)


@pytest.mark.parametrize("point", CRITICAL_CRASH_POINTS)
def test_restart_never_duplicates_order_or_fill(point: CrashPoint, tmp_path: Path) -> None:
    path = tmp_path / "firmquant.sqlite3"
    if point is CrashPoint.SQLITE_CORRUPT:
        path.write_bytes(b"not a sqlite database")
        with pytest.raises(DatabaseCorrupt):
            Database.open(path)
        return

    database = Database.open(path, busy_timeout_ms=20)
    broker = None
    try:
        if point is CrashPoint.BEFORE_INTENT_PERSIST:
            report = RecoveryService(
                database=database,
                account_store=None,
                account_path=None,
                gateway=None,
                clock=lambda: NOW,
            ).recover()
            assert report.unresolved_order_ids == ()
        elif point is CrashPoint.ACCOUNT_WRITE_INTERRUPTED:
            store = JsonAccountStateStore()
            account_path = tmp_path / "account.json"
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
                operation_id="acctop_" + "a" * 64,
            )
            account_path.write_text("{interrupted", encoding="utf-8")
            report = RecoveryService(
                database=database,
                account_store=store,
                account_path=account_path,
                gateway=None,
                clock=lambda: NOW,
            ).recover()
            assert report.halt_required is True
        else:
            case = create_submitting_case(database)
            orders = ()
            fills = ()
            connected = point is not CrashPoint.BROKER_CLIENT_RESTART
            if point is CrashPoint.BROKER_ACCEPTED_BEFORE_ID:
                orders = (broker_order(case.command),)
            elif point is CrashPoint.CANCEL_BEFORE_CONFIRMATION:
                accepted = broker_order(case.command)
                acknowledged = acknowledge_locally(case, accepted)
                with database.transaction():
                    case.repository.begin_cancel(acknowledged, started_at=NOW)
                orders = (
                    replace(
                        accepted,
                        status=BrokerOrderStatus.CANCELLED,
                        event_sequence=21,
                    ),
                )
            elif point is CrashPoint.LATE_FILL_ON_RESTART:
                _, cancelled_fact = cancelled_locally(case)
                late = broker_fill(
                    case.command,
                    shares=50,
                    sequence=22,
                    fill_id="late-restart-fill",
                )
                orders = (
                    replace(
                        cancelled_fact,
                        filled_shares=late.shares,
                        event_sequence=22,
                    ),
                )
                fills = (late,)
            elif point is CrashPoint.PARTIAL_FILL_BEFORE_TRANSACTION:
                fills = (broker_fill(case.command),)
                orders = (
                    broker_order(
                        case.command,
                        status=BrokerOrderStatus.PARTIALLY_FILLED,
                        filled_shares=50,
                        sequence=21,
                    ),
                )
            broker = fake_recovery_broker(
                orders=orders,
                fills=fills,
                connected=connected,
            )
            recovery = RecoveryService(
                database=database,
                account_store=None,
                account_path=None,
                gateway=broker,
                clock=lambda: NOW,
            )
            if point is CrashPoint.SQLITE_LOCKED:
                locker = sqlite3.connect(path, isolation_level=None)
                locker.execute("BEGIN IMMEDIATE")
                try:
                    with pytest.raises(sqlite3.OperationalError, match="locked"):
                        recovery.recover()
                finally:
                    locker.rollback()
                    locker.close()
                assert broker.submitted_commands == ()
                assert broker.cancelled_order_ids == ()
                return
            report = recovery.recover()
            recovered = case.repository.load(case.aggregate.intent.execution_id)
            assert recovered is not None
            if not orders or not connected:
                assert recovered.state is OrderState.UNKNOWN
            assert report.duplicate_orders == 0
            assert report.duplicate_fills == 0

        duplicate_orders = database.scalar(
            "SELECT count(*) FROM (SELECT decision_id, uquant_order_id "
            "FROM execution_intents GROUP BY decision_id, uquant_order_id HAVING count(*) > 1)"
        )
        duplicate_fills = database.scalar(
            "SELECT count(*) FROM (SELECT broker_fill_id FROM fills "
            "GROUP BY broker_fill_id HAVING count(*) > 1)"
        )
        assert duplicate_orders == 0
        assert duplicate_fills == 0
        if broker is not None:
            assert broker.submitted_commands == ()
            assert broker.cancelled_order_ids == ()
    finally:
        database.close()
