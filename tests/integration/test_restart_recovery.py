from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from uquant.types import AccountState

from firmquant.domain.broker_facts import BrokerOrderStatus
from firmquant.domain.orders import OrderState
from firmquant.persistence.database import Database
from firmquant.persistence.recovery import (
    AccountOperation,
    AccountRecoveryClassification,
    RecoveryService,
    UquantAccountStateStore,
)
from tests.fixtures.recovery_cases import (
    NOW,
    JsonAccountStateStore,
    broker_fill,
    broker_order,
    cancelled_locally,
    create_submitting_case,
    fake_recovery_broker,
    write_account,
)


@pytest.fixture
def database(tmp_path: Path):
    opened = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        yield opened
    finally:
        opened.close()


@pytest.mark.parametrize(
    ("file_state", "expected"),
    [
        ("before", AccountRecoveryClassification.NOT_APPLIED),
        ("after", AccountRecoveryClassification.FILE_APPLIED_RECEIPT_MISSING),
        ("contradiction", AccountRecoveryClassification.CONTRADICTION),
    ],
)
def test_account_recovery_uses_only_before_or_expected_after_hash(
    database: Database,
    tmp_path: Path,
    file_state: str,
    expected: AccountRecoveryClassification,
) -> None:
    store = JsonAccountStateStore()
    path = tmp_path / "account.json"
    before = {"cash": "1000", "revision": 1}
    after = {"cash": "900", "revision": 2}
    write_account(path, before, store)
    AccountOperation.begin(
        database=database,
        store=store,
        account_path=path,
        prepared_account=after,
        expected_before_sha256=store.hash_state(before),
        operation_kind="BROKER_SYNC",
        evidence_sha256="e" * 64,
        now=NOW,
        operation_id="acctop_" + "1" * 64,
    )
    if file_state == "after":
        write_account(path, after, store)
    elif file_state == "contradiction":
        write_account(path, {"cash": "777", "revision": 99}, store)

    report = RecoveryService(
        database=database,
        account_store=store,
        account_path=path,
        gateway=None,
        clock=lambda: NOW,
    ).recover()

    assert report.account_receipts[0].classification is expected
    assert report.halt_required is (expected is AccountRecoveryClassification.CONTRADICTION)
    expected_stage = (
        "CONTRADICTION" if expected is AccountRecoveryClassification.CONTRADICTION else "RECEIPT_COMMITTED"
    )
    assert database.scalar("SELECT stage FROM account_operations") == expected_stage
    if expected is AccountRecoveryClassification.CONTRADICTION:
        repeated = RecoveryService(
            database=database,
            account_store=store,
            account_path=path,
            gateway=None,
            clock=lambda: NOW + timedelta(seconds=1),
        ).recover()
        assert repeated.account_receipts[0].classification is AccountRecoveryClassification.CONTRADICTION


def test_normal_account_commit_is_write_ahead_and_idempotent(database: Database, tmp_path: Path) -> None:
    store = JsonAccountStateStore()
    path = tmp_path / "account.json"
    before = {"cash": "1000", "revision": 1}
    after = {"cash": "900", "revision": 2}
    write_account(path, before, store)
    operation = AccountOperation.begin(
        database=database,
        store=store,
        account_path=path,
        prepared_account=after,
        expected_before_sha256=store.hash_state(before),
        operation_kind="DECISION",
        evidence_sha256="d" * 64,
        now=NOW,
        operation_id="acctop_" + "2" * 64,
    )

    operation.commit_file(now=NOW)
    operation.commit_receipt(now=NOW)
    operation.commit_receipt(now=NOW)

    assert store.hash_file(path) == store.hash_state(after)
    assert database.scalar("SELECT stage FROM account_operations") == "RECEIPT_COMMITTED"


def test_uquant_public_store_round_trips_strict_atomic_account_file(
    database: Database, tmp_path: Path
) -> None:
    store = UquantAccountStateStore()
    path = tmp_path / "uquant-account.json"
    before = AccountState.empty(1000.0)
    before.data_hash = "d" * 64
    before.data_hash_as_of = "2026-08-25"
    before.data_hash_symbols = []
    before.code_hash = "c" * 64
    after = deepcopy(before)
    after.cash = 900.0
    store.save(before, path)
    operation = AccountOperation.begin(
        database=database,
        store=store,
        account_path=path,
        prepared_account=after,
        expected_before_sha256=store.hash_state(before),
        operation_kind="BROKER_SYNC",
        evidence_sha256="f" * 64,
        now=NOW,
        operation_id="acctop_" + "3" * 64,
    )

    operation.commit_file(now=NOW)
    operation.commit_receipt(now=NOW)

    assert store.hash_file(path) == store.hash_state(after)


def test_matching_client_id_with_wrong_order_identity_is_not_adopted(
    database: Database,
) -> None:
    case = create_submitting_case(database)
    contradictory = replace(
        broker_order(case.command),
        symbol=case.command.symbol.parse("sh600000"),
    )
    broker = fake_recovery_broker(orders=(contradictory,))

    report = RecoveryService(
        database=database,
        account_store=None,
        account_path=None,
        gateway=broker,
        clock=lambda: NOW,
    ).recover()
    recovered = case.repository.load(case.aggregate.intent.execution_id)

    assert recovered is not None and recovered.state is OrderState.SUBMITTING
    assert "BROKER_RECOVERY_CONTRADICTION" in report.blockers
    assert database.scalar("SELECT count(*) FROM broker_orders") == 0
    assert broker.submitted_commands == ()


def test_accepted_submit_is_recovered_by_query_without_resubmit(database: Database) -> None:
    case = create_submitting_case(database)
    accepted = broker_order(case.command)
    broker = fake_recovery_broker(orders=(accepted,))

    report = RecoveryService(
        database=database,
        account_store=None,
        account_path=None,
        gateway=broker,
        clock=lambda: NOW,
    ).recover()
    recovered = case.repository.load(case.aggregate.intent.execution_id)

    assert recovered is not None
    assert recovered.state is OrderState.ACKNOWLEDGED
    assert recovered.broker_order_id == accepted.broker_order_id
    assert report.unresolved_order_ids == ()
    assert broker.submitted_commands == ()
    assert broker.cancelled_order_ids == ()


def test_absent_broker_order_remains_unknown_across_restarts(database: Database) -> None:
    case = create_submitting_case(database)
    broker = fake_recovery_broker()
    service = RecoveryService(
        database=database,
        account_store=None,
        account_path=None,
        gateway=broker,
        clock=lambda: NOW,
    )

    first = service.recover()
    second = service.recover()
    recovered = case.repository.load(case.aggregate.intent.execution_id)

    assert recovered is not None and recovered.state is OrderState.UNKNOWN
    assert first.unresolved_order_ids == (case.aggregate.intent.execution_id,)
    assert second.unresolved_order_ids == first.unresolved_order_ids
    assert recovered.submit_attempts == 1
    assert broker.submitted_commands == ()


def test_partial_fill_is_recovered_once_and_replay_is_idempotent(database: Database) -> None:
    case = create_submitting_case(database)
    fill = broker_fill(case.command)
    partial = broker_order(
        case.command,
        status=BrokerOrderStatus.PARTIALLY_FILLED,
        filled_shares=50,
        sequence=21,
    )
    broker = fake_recovery_broker(orders=(partial,), fills=(fill,))
    service = RecoveryService(
        database=database,
        account_store=None,
        account_path=None,
        gateway=broker,
        clock=lambda: NOW,
    )

    service.recover()
    service.recover()
    recovered = case.repository.load(case.aggregate.intent.execution_id)

    assert recovered is not None
    assert recovered.state is OrderState.PARTIALLY_FILLED
    assert recovered.filled_shares.value == 50
    assert len(recovered.fills) == 1
    assert database.scalar("SELECT count(*) FROM fills") == 1
    assert broker.submitted_commands == ()


def test_cancel_confirmation_and_late_fill_are_recovered_without_new_write(
    database: Database,
) -> None:
    case = create_submitting_case(database)
    cancelled, cancelled_fact = cancelled_locally(case)
    late_fill = broker_fill(case.command, shares=50, sequence=22, fill_id="late-fill")
    broker_fact = replace(
        cancelled_fact,
        filled_shares=late_fill.shares,
        event_sequence=22,
    )
    broker = fake_recovery_broker(orders=(broker_fact,), fills=(late_fill,))

    report = RecoveryService(
        database=database,
        account_store=None,
        account_path=None,
        gateway=broker,
        clock=lambda: NOW,
    ).recover()
    recovered = case.repository.load(cancelled.intent.execution_id)

    assert recovered is not None
    assert recovered.state is OrderState.CANCELLED
    assert recovered.filled_shares.value == 50
    assert recovered.late_fill_investigation_required is True
    assert "LATE_FILL_AFTER_CANCELLED" in recovered.anomalies
    assert report.halt_required is True
    assert broker.cancelled_order_ids == ()
