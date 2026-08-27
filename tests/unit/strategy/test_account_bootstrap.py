from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from uquant.account import load_account, save_account
from uquant.types import AccountState

from firmquant.domain.values import Money
from firmquant.persistence.audit import AuditLedger
from firmquant.persistence.database import Database
from firmquant.strategy.account_bootstrap import (
    AccountBootstrapDenied,
    AccountBootstrapService,
    BootstrapDataIdentity,
)
from firmquant.strategy.identity import StrategyIdentity
from tests.fixtures.broker_snapshots import completed_buy_snapshot

NOW = datetime(2026, 1, 6, 3, tzinfo=UTC)


def _data_identity(_snapshot) -> BootstrapDataIdentity:
    return BootstrapDataIdentity(
        data_hash="d" * 64,
        as_of="2026-01-06",
        symbols=("sz300308",),
    )


def _empty_snapshot():
    snapshot = completed_buy_snapshot()
    return replace(
        snapshot,
        snapshot_id="bootstrap-empty",
        account=replace(
            snapshot.account,
            available_cash=Money(Decimal("1000")),
            total_assets=Money(Decimal("1000")),
        ),
        positions=(),
        orders=(),
        fills=(),
        raw_payload_sha256="a" * 64,
    )


def _nonempty_snapshot():
    snapshot = completed_buy_snapshot()
    return replace(
        snapshot,
        snapshot_id="bootstrap-nonempty",
        orders=(),
        fills=(),
        raw_payload_sha256="b" * 64,
    )


def _service(tmp_path: Path, database: Database) -> AccountBootstrapService:
    return AccountBootstrapService(
        database=database,
        account_path=tmp_path / "uquant-account.json",
        data_identity_provider=_data_identity,
        clock=lambda: NOW,
    )


def test_empty_cash_account_bootstrap_creates_strict_uquant_state_and_binding(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    service = _service(tmp_path, database)
    try:
        receipt = service.bootstrap(_empty_snapshot())
        account = load_account(
            tmp_path / "uquant-account.json",
            require_hashes=True,
            allow_legacy_schema=False,
        )

        assert account.cash == 1000.0
        assert account.positions == {}
        assert account.code_hash == StrategyIdentity.locked().economic_code_fingerprint
        assert account.data_hash == "d" * 64
        assert receipt.account_state_sha256
        assert database.scalar("SELECT count(*) FROM account_bindings") == 1
        assert database.scalar("SELECT count(*) FROM audit_events WHERE category = 'account.binding'") == 1

        with pytest.raises(AccountBootstrapDenied, match="ACCOUNT_ALREADY_BOUND"):
            service.bootstrap(_empty_snapshot())
        assert database.scalar("SELECT count(*) FROM account_bindings") == 1
    finally:
        database.close()


def test_nonempty_broker_account_requires_explicit_seed_without_partial_write(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    service = _service(tmp_path, database)
    try:
        with pytest.raises(AccountBootstrapDenied, match="ACCOUNT_STATE_SEED_REQUIRED"):
            service.bootstrap(_nonempty_snapshot())
        assert not (tmp_path / "uquant-account.json").exists()
        assert database.scalar("SELECT count(*) FROM account_bindings") == 0
    finally:
        database.close()


def test_nonempty_seed_mismatch_is_rejected_without_partial_write(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    service = _service(tmp_path, database)
    seed_path = tmp_path / "reviewed-seed.json"
    seed = AccountState.empty(994.9)
    seed.code_hash = StrategyIdentity.locked().economic_code_fingerprint
    seed.data_hash = "d" * 64
    seed.data_hash_as_of = "2026-01-06"
    seed.data_hash_symbols = ["sz300308"]
    save_account(seed, seed_path)
    try:
        with pytest.raises(AccountBootstrapDenied, match="SEED_POSITION_MISMATCH"):
            service.bootstrap(_nonempty_snapshot(), seed_path=seed_path)
        assert not (tmp_path / "uquant-account.json").exists()
        assert database.scalar("SELECT count(*) FROM account_bindings") == 0
        assert database.scalar("SELECT count(*) FROM account_bootstrap_operations") == 0
    finally:
        database.close()


def test_bootstrap_resumes_prepared_operation_after_crash_before_file_save(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    service = _service(tmp_path, database)
    store_type = type(service._store)
    original_save = store_type.save

    def crash_before_save(self, state, path) -> None:
        del self, state, path
        raise SystemExit("simulated crash before account save")

    try:
        monkeypatch.setattr(store_type, "save", crash_before_save)
        with pytest.raises(SystemExit, match="simulated crash"):
            service.bootstrap(_empty_snapshot())

        assert not (tmp_path / "uquant-account.json").exists()
        assert database.scalar("SELECT stage FROM account_bootstrap_operations") == "PREPARED"
        assert database.scalar("SELECT count(*) FROM account_bindings") == 0

        monkeypatch.setattr(store_type, "save", original_save)
        recovered = _service(tmp_path, database).bootstrap(_empty_snapshot())

        assert recovered.account_state_sha256
        assert database.scalar("SELECT count(*) FROM account_bootstrap_operations") == 1
        assert database.scalar("SELECT stage FROM account_bootstrap_operations") == "BINDING_COMMITTED"
        assert database.scalar("SELECT count(*) FROM account_bindings") == 1
        assert database.scalar("SELECT count(*) FROM audit_events WHERE category = 'account.binding'") == 1
        assert database.scalar("SELECT count(*) FROM audit_events WHERE category = 'account.bootstrap'") == 1
    finally:
        database.close()


def test_bootstrap_recovers_file_applied_before_atomic_binding_finalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    service = _service(tmp_path, database)
    original_append = AuditLedger.append

    def crash_during_finalization(self, **kwargs):
        if kwargs.get("category") == "account.bootstrap":
            raise SystemExit("simulated crash during bootstrap finalization")
        return original_append(self, **kwargs)

    try:
        monkeypatch.setattr(AuditLedger, "append", crash_during_finalization)
        with pytest.raises(SystemExit, match="simulated crash"):
            service.bootstrap(_empty_snapshot())

        account_path = tmp_path / "uquant-account.json"
        durable_hash = service._store.hash_file(account_path)
        assert database.scalar("SELECT count(*) FROM account_bindings") == 0
        assert database.scalar("SELECT stage FROM account_bootstrap_operations") == "PREPARED"
        assert database.scalar("SELECT count(*) FROM audit_events WHERE category = 'account.binding'") == 0
        assert database.scalar("SELECT count(*) FROM audit_events WHERE category = 'account.bootstrap'") == 0

        monkeypatch.setattr(AuditLedger, "append", original_append)
        recovered = _service(tmp_path, database).bootstrap(_empty_snapshot())

        assert recovered.account_state_sha256 == durable_hash
        assert service._store.hash_file(account_path) == durable_hash
        assert database.scalar("SELECT stage FROM account_bootstrap_operations") == "BINDING_COMMITTED"
        assert database.scalar("SELECT count(*) FROM account_bindings") == 1
        assert database.scalar("SELECT count(*) FROM audit_events WHERE category = 'account.binding'") == 1
        assert database.scalar("SELECT count(*) FROM audit_events WHERE category = 'account.bootstrap'") == 1
    finally:
        database.close()
