from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import firmquant.persistence.recovery as recovery_module
from firmquant.domain.errors import DomainTypeError, DomainValidationError
from firmquant.persistence.database import Database
from firmquant.persistence.recovery import (
    AccountOperation,
    AccountRecoveryClassification,
    OrderRecoveryClassification,
    RecoveryContradiction,
    RecoveryError,
    RecoveryService,
    UquantAccountStateStore,
)
from firmquant.persistence.repositories import ExecutionLedgerRepository, PersistenceConflict
from tests.fixtures.recovery_cases import (
    NOW,
    JsonAccountStateStore,
    acknowledge_locally,
    broker_fill,
    broker_order,
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


def _account_operation(
    database: Database,
    tmp_path: Path,
    *,
    store: JsonAccountStateStore | None = None,
    operation_id: str = "acctop_" + "8" * 64,
) -> tuple[AccountOperation, JsonAccountStateStore, Path, object, object]:
    selected_store = JsonAccountStateStore() if store is None else store
    path = tmp_path / "account.json"
    before = {"cash": "1000", "revision": 1}
    after = {"cash": "900", "revision": 2}
    write_account(path, before, selected_store)
    operation = AccountOperation.begin(
        database=database,
        store=selected_store,
        account_path=path,
        prepared_account=after,
        expected_before_sha256=selected_store.hash_state(before),
        operation_kind="BROKER_SYNC",
        evidence_sha256="e" * 64,
        now=NOW,
        operation_id=operation_id,
    )
    return operation, selected_store, path, before, after


@pytest.mark.parametrize(
    ("overrides", "error", "message"),
    [
        ({"expected_before_sha256": "BAD"}, DomainValidationError, "before digest"),
        ({"evidence_sha256": "BAD"}, DomainValidationError, "evidence digest"),
        ({"operation_kind": None}, DomainTypeError, "kind must be text"),
        ({"operation_kind": ""}, DomainValidationError, "canonical non-empty"),
        ({"operation_kind": " BROKER_SYNC"}, DomainValidationError, "canonical non-empty"),
        ({"operation_kind": "BROKER\nSYNC"}, DomainValidationError, "control characters"),
        ({"operation_kind": "x" * 65}, DomainValidationError, "canonical non-empty"),
        ({"now": "2026-08-25"}, DomainTypeError, "begin time must be datetime"),
        ({"now": datetime(2026, 8, 25, 1, 32)}, DomainValidationError, "timezone-aware"),
        ({"operation_id": "account-operation"}, DomainValidationError, "id is not canonical"),
    ],
)
def test_account_operation_rejects_untrusted_identity_and_time_fields(
    database: Database,
    tmp_path: Path,
    overrides: dict[str, object],
    error: type[Exception],
    message: str,
) -> None:
    store = JsonAccountStateStore()
    path = tmp_path / "account.json"
    before = {"cash": "1000"}
    write_account(path, before, store)
    arguments: dict[str, object] = {
        "database": database,
        "store": store,
        "account_path": path,
        "prepared_account": {"cash": "900"},
        "expected_before_sha256": store.hash_state(before),
        "operation_kind": "BROKER_SYNC",
        "evidence_sha256": "e" * 64,
        "now": NOW,
        "operation_id": "acctop_" + "7" * 64,
    }
    arguments.update(overrides)

    with pytest.raises(error, match=message):
        AccountOperation.begin(**arguments)  # type: ignore[arg-type]


def test_account_operation_requires_database_and_store_protocol(database: Database, tmp_path: Path) -> None:
    path = tmp_path / "account.json"
    path.write_text("{}", encoding="utf-8")
    common = {
        "account_path": path,
        "prepared_account": {},
        "expected_before_sha256": "a" * 64,
        "operation_kind": "BROKER_SYNC",
        "evidence_sha256": "e" * 64,
        "now": NOW,
    }
    with pytest.raises(DomainTypeError, match="database"):
        AccountOperation.begin(database=object(), store=JsonAccountStateStore(), **common)  # type: ignore[arg-type]
    with pytest.raises(DomainTypeError, match="store"):
        AccountOperation.begin(database=database, store=object(), **common)  # type: ignore[arg-type]


@pytest.mark.parametrize("path_kind", ["missing", "directory", "symlink"])
def test_account_operation_rejects_unsafe_account_path(
    database: Database, tmp_path: Path, path_kind: str
) -> None:
    path = tmp_path / "account.json"
    if path_kind == "directory":
        path.mkdir()
    elif path_kind == "symlink":
        target = tmp_path / "target.json"
        target.write_text("{}", encoding="utf-8")
        path.symlink_to(target)

    with pytest.raises(RecoveryError, match=r"path cannot be resolved|regular non-symlink"):
        AccountOperation.begin(
            database=database,
            store=JsonAccountStateStore(),
            account_path=path,
            prepared_account={},
            expected_before_sha256="a" * 64,
            operation_kind="BROKER_SYNC",
            evidence_sha256="e" * 64,
            now=NOW,
        )


def test_account_operation_rejects_file_precondition_mismatch(database: Database, tmp_path: Path) -> None:
    store = JsonAccountStateStore()
    path = tmp_path / "account.json"
    write_account(path, {"cash": "1000"}, store)

    with pytest.raises(RecoveryContradiction, match="precondition"):
        AccountOperation.begin(
            database=database,
            store=store,
            account_path=path,
            prepared_account={"cash": "900"},
            expected_before_sha256="f" * 64,
            operation_kind="BROKER_SYNC",
            evidence_sha256="e" * 64,
            now=NOW,
        )


def test_account_operation_identity_is_idempotent_but_collision_is_rejected(
    database: Database, tmp_path: Path
) -> None:
    operation, store, path, before, after = _account_operation(database, tmp_path)
    repeated = AccountOperation.begin(
        database=database,
        store=store,
        account_path=path,
        prepared_account=after,
        expected_before_sha256=store.hash_state(before),
        operation_kind="BROKER_SYNC",
        evidence_sha256="e" * 64,
        now=NOW,
        operation_id=operation.operation_id,
    )
    assert repeated.operation_id == operation.operation_id

    with pytest.raises(PersistenceConflict, match="identity collision"):
        AccountOperation.begin(
            database=database,
            store=store,
            account_path=path,
            prepared_account=after,
            expected_before_sha256=store.hash_state(before),
            operation_kind="DECISION",
            evidence_sha256="e" * 64,
            now=NOW,
            operation_id=operation.operation_id,
        )


def test_account_operation_detects_missing_write_ahead_row(database: Database, tmp_path: Path) -> None:
    operation, _, _, _, _ = _account_operation(database, tmp_path)
    with database.transaction():
        database.write("DELETE FROM account_operations WHERE operation_id = ?", (operation.operation_id,))

    with pytest.raises(RecoveryError, match="receipt disappeared"):
        operation.commit_file(now=NOW)


def test_account_file_commit_recovers_file_saved_before_stage_update(
    database: Database, tmp_path: Path
) -> None:
    operation, store, path, _, after = _account_operation(database, tmp_path)
    write_account(path, after, store)

    operation.commit_file(now=NOW)

    assert database.scalar("SELECT stage FROM account_operations") == "FILE_COMMITTED"
    assert store.hash_file(path) == operation.expected_account_after_sha256


class WrongSaveStore(JsonAccountStateStore):
    sabotage = False

    def save(self, state: object, path: Path) -> None:
        if self.sabotage:
            state = {"unexpected": True}
        super().save(state, path)


@pytest.mark.parametrize("failure", ["changed-before", "wrong-save", "already-contradictory"])
def test_account_file_commit_marks_every_ambiguous_state_contradictory(
    database: Database, tmp_path: Path, failure: str
) -> None:
    store = WrongSaveStore() if failure == "wrong-save" else JsonAccountStateStore()
    operation, selected, path, _, _ = _account_operation(database, tmp_path, store=store)
    if failure == "changed-before":
        write_account(path, {"cash": "777"}, selected)
    elif failure == "wrong-save":
        assert isinstance(selected, WrongSaveStore)
        selected.sabotage = True
    elif failure == "already-contradictory":
        with database.transaction():
            database.write(
                "UPDATE account_operations SET stage = 'CONTRADICTION' WHERE operation_id = ?",
                (operation.operation_id,),
            )

    with pytest.raises(RecoveryContradiction):
        operation.commit_file(now=NOW)
    assert database.scalar("SELECT stage FROM account_operations") == "CONTRADICTION"


def test_repeated_file_commit_detects_post_commit_tampering(database: Database, tmp_path: Path) -> None:
    operation, store, path, _, _ = _account_operation(database, tmp_path)
    operation.commit_file(now=NOW)
    write_account(path, {"cash": "777"}, store)

    with pytest.raises(RecoveryContradiction, match="committed account file"):
        operation.commit_file(now=NOW)
    assert database.scalar("SELECT stage FROM account_operations") == "CONTRADICTION"


def test_account_file_and_receipt_reject_path_identity_change(database: Database, tmp_path: Path) -> None:
    operation, store, _, _, _ = _account_operation(database, tmp_path)
    other = tmp_path / "other-account.json"
    write_account(other, {"cash": "1000"}, store)

    with pytest.raises(RecoveryContradiction, match="path identity changed"):
        replace(operation, account_path=other).commit_file(now=NOW)

    operation2, store2, _, _, _ = _account_operation(database, tmp_path, operation_id="acctop_" + "9" * 64)
    operation2.commit_file(now=NOW)
    write_account(other, {"cash": "900", "revision": 2}, store2)
    with pytest.raises(RecoveryContradiction, match="path identity changed"):
        replace(operation2, account_path=other).commit_receipt(now=NOW)


def test_account_receipt_requires_committed_and_unchanged_file(database: Database, tmp_path: Path) -> None:
    operation, store, path, _, _ = _account_operation(database, tmp_path)
    with pytest.raises(RecoveryError, match="requires a committed file"):
        operation.commit_receipt(now=NOW)

    operation.commit_file(now=NOW)
    write_account(path, {"cash": "777"}, store)
    with pytest.raises(RecoveryContradiction, match="changed before receipt"):
        operation.commit_receipt(now=NOW)
    assert database.scalar("SELECT stage FROM account_operations") == "CONTRADICTION"


def _uquant_module(
    *,
    economic: object,
    load: object = lambda path, **kwargs: object(),
    save: object = lambda state, path: None,
) -> SimpleNamespace:
    return SimpleNamespace(economic_state_sha256=economic, load_account=load, save_account=save)


def test_uquant_account_store_requires_complete_public_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        recovery_module.importlib,
        "import_module",
        lambda name: _uquant_module(economic=None),
    )
    with pytest.raises(RecoveryError, match="contract is unavailable"):
        UquantAccountStateStore().hash_state(object())


@pytest.mark.parametrize("result", ["bad", RuntimeError("economic failure")])
def test_uquant_account_store_rejects_failed_or_invalid_economic_hash(
    monkeypatch: pytest.MonkeyPatch, result: object
) -> None:
    def economic(state: object) -> str:
        del state
        if isinstance(result, Exception):
            raise result
        return str(result)

    monkeypatch.setattr(
        recovery_module.importlib,
        "import_module",
        lambda name: _uquant_module(economic=economic),
    )
    message = "economic hash failed" if isinstance(result, Exception) else "lowercase SHA-256"
    with pytest.raises((RecoveryError, DomainValidationError), match=message):
        UquantAccountStateStore().hash_state(object())


def test_uquant_account_store_wraps_load_and_save_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def load(path: Path, **kwargs: object) -> object:
        del path, kwargs
        raise OSError("unavailable")

    def save(state: object, path: Path) -> None:
        del state, path
        raise ValueError("invalid")

    monkeypatch.setattr(
        recovery_module.importlib,
        "import_module",
        lambda name: _uquant_module(economic=lambda state: "a" * 64, load=load, save=save),
    )
    store = UquantAccountStateStore()
    with pytest.raises(RecoveryError, match="missing or corrupt"):
        store.hash_file(tmp_path / "missing.json")
    with pytest.raises(RecoveryError, match="atomic account save failed"):
        store.save(object(), tmp_path / "account.json")


@pytest.mark.parametrize(
    "arguments",
    [
        {"database": object()},
        {"account_store": JsonAccountStateStore(), "account_path": None},
        {"account_store": None, "account_path": Path("account.json")},
        {"account_store": object(), "account_path": Path("account.json")},
        {"gateway": object()},
        {"clock": None},
    ],
)
def test_recovery_service_rejects_incomplete_or_untyped_ports(
    database: Database, arguments: dict[str, object]
) -> None:
    values: dict[str, object] = {
        "database": database,
        "account_store": None,
        "account_path": None,
        "gateway": None,
        "clock": lambda: NOW,
    }
    values.update(arguments)
    with pytest.raises((DomainTypeError, DomainValidationError)):
        RecoveryService(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("clock", [lambda: "now", lambda: datetime(2026, 8, 25, 1, 32)])
def test_recovery_service_requires_aware_clock_result(database: Database, clock: object) -> None:
    service = RecoveryService(
        database=database,
        account_store=None,
        account_path=None,
        gateway=None,
        clock=clock,  # type: ignore[arg-type]
    )
    with pytest.raises((DomainTypeError, DomainValidationError)):
        service.recover()


def test_recovery_service_rejects_invalid_database_count(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Database, "scalar", lambda self, sql, parameters=(): None)
    service = RecoveryService(
        database=database,
        account_store=None,
        account_path=None,
        gateway=None,
        clock=lambda: NOW,
    )
    with pytest.raises(RecoveryError, match="database count"):
        service.recover()


@pytest.mark.parametrize("payload", ["[]", '{"account_path_sha256":"' + "f" * 64 + '"}'])
def test_account_recovery_rejects_payload_without_matching_path_identity(
    database: Database, tmp_path: Path, payload: str
) -> None:
    operation, store, path, _, _ = _account_operation(database, tmp_path)
    with database.transaction():
        database.write(
            "UPDATE account_operations SET payload_json = ? WHERE operation_id = ?",
            (payload, operation.operation_id),
        )

    report = RecoveryService(
        database=database,
        account_store=store,
        account_path=path,
        gateway=None,
        clock=lambda: NOW,
    ).recover()

    assert report.account_receipts[0].classification is AccountRecoveryClassification.CONTRADICTION
    assert "ACCOUNT_OPERATION_CONTRADICTION" in report.blockers


def test_account_recovery_classifies_store_read_failure_as_contradiction(
    database: Database, tmp_path: Path
) -> None:
    operation, store, path, _, _ = _account_operation(database, tmp_path)
    path.unlink()

    report = RecoveryService(
        database=database,
        account_store=store,
        account_path=path,
        gateway=None,
        clock=lambda: NOW,
    ).recover()

    assert report.account_receipts == (
        replace(
            report.account_receipts[0],
            operation_id=operation.operation_id,
            classification=AccountRecoveryClassification.CONTRADICTION,
            actual_account_sha256=None,
        ),
    )
    assert report.halt_required is True


def test_pending_submit_without_recovery_gateway_remains_unknown(
    database: Database,
) -> None:
    case = create_submitting_case(database)

    report = RecoveryService(
        database=database,
        account_store=None,
        account_path=None,
        gateway=None,
        clock=lambda: NOW,
    ).recover()

    recovered = case.repository.load(case.aggregate.intent.execution_id)
    assert recovered is not None and recovered.state.value == "UNKNOWN"
    assert report.order_receipts[0].classification is OrderRecoveryClassification.REMAINS_UNKNOWN
    assert report.order_receipts[0].reason_code == "BROKER_RECOVERY_UNAVAILABLE"
    assert report.halt_required is True


def test_multiple_broker_matches_are_never_adopted(database: Database) -> None:
    case = create_submitting_case(database)
    first = broker_order(case.command)
    second = replace(first, broker_order_id="broker-recovery-order-2", raw_payload_sha256="f" * 64)

    report = RecoveryService(
        database=database,
        account_store=None,
        account_path=None,
        gateway=fake_recovery_broker(orders=(first, second)),
        clock=lambda: NOW,
    ).recover()

    assert "MULTIPLE_BROKER_ORDER_MATCHES" in report.blockers
    assert report.order_receipts[0].classification is OrderRecoveryClassification.CONTRADICTION
    assert case.repository.load(case.aggregate.intent.execution_id) == case.aggregate


def test_mismatched_fill_identity_is_never_adopted(database: Database) -> None:
    case = create_submitting_case(database)
    order = replace(broker_order(case.command), filled_shares=case.command.requested_shares)
    fill = replace(
        broker_fill(case.command, shares=100),
        symbol=case.command.symbol.parse("sh600000"),
    )

    report = RecoveryService(
        database=database,
        account_store=None,
        account_path=None,
        gateway=fake_recovery_broker(orders=(order,), fills=(fill,)),
        clock=lambda: NOW,
    ).recover()

    assert "BROKER_RECOVERY_CONTRADICTION" in report.blockers
    assert database.scalar("SELECT count(*) FROM fills") == 0


def test_repository_conflict_during_recovery_is_classified_not_raised(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = create_submitting_case(database)
    monkeypatch.setattr(
        ExecutionLedgerRepository,
        "record_submit_result",
        lambda *args, **kwargs: (_ for _ in ()).throw(PersistenceConflict("conflict")),
    )

    report = RecoveryService(
        database=database,
        account_store=None,
        account_path=None,
        gateway=fake_recovery_broker(orders=(broker_order(case.command),)),
        clock=lambda: NOW,
    ).recover()

    assert "BROKER_RECOVERY_CONTRADICTION" in report.blockers
    assert report.order_receipts[0].classification is OrderRecoveryClassification.CONTRADICTION


def _orphan_pending_attempt(database: Database) -> str:
    case = create_submitting_case(database)
    connection = database._connection
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute(
        "DELETE FROM execution_intents WHERE execution_id = ?",
        (case.aggregate.intent.execution_id,),
    )
    connection.execute("PRAGMA foreign_keys = ON")
    return case.aggregate.intent.execution_id


@pytest.mark.parametrize("with_gateway", [False, True])
def test_orphaned_attempt_is_reported_as_recovery_contradiction(
    database: Database, with_gateway: bool
) -> None:
    execution_id = _orphan_pending_attempt(database)
    gateway = fake_recovery_broker() if with_gateway else None

    report = RecoveryService(
        database=database,
        account_store=None,
        account_path=None,
        gateway=gateway,
        clock=lambda: NOW,
    ).recover()

    matching = [receipt for receipt in report.order_receipts if receipt.execution_id == execution_id]
    assert matching[0].classification is OrderRecoveryClassification.CONTRADICTION
    assert matching[0].reason_code == "RECOVERY_AGGREGATE_MISSING"
    assert report.halt_required is True


def test_late_fact_scan_blocks_external_orders_and_unmapped_fills(database: Database) -> None:
    case = create_submitting_case(database)
    local_fact = broker_order(case.command)
    acknowledge_locally(case, local_fact)
    manual_fact = replace(
        local_fact,
        broker_order_id="manual-order",
        client_order_id="MANUAL-ORDER",
        raw_payload_sha256="f" * 64,
    )
    manual_fill = replace(
        broker_fill(case.command, fill_id="manual-fill"),
        broker_order_id="manual-order",
        raw_payload_sha256="d" * 64,
    )

    report = RecoveryService(
        database=database,
        account_store=None,
        account_path=None,
        gateway=fake_recovery_broker(orders=(local_fact, manual_fact), fills=(manual_fill,)),
        clock=lambda: NOW,
    ).recover()

    assert "EXTERNAL_BROKER_ORDER" in report.blockers
    assert "UNMAPPED_BROKER_FILL" in report.blockers
    assert report.halt_required is True


def test_broker_evidence_defensively_rejects_nonaggregate(database: Database) -> None:
    case = create_submitting_case(database)
    assert RecoveryService._broker_evidence_matches(object(), broker_order(case.command), ()) is False
