from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from firmquant.config import Mode
from firmquant.persistence.database import Database, TransactionRequired
from firmquant.persistence.operational_authority import (
    ModeTransitionOperation,
    OperationalAuthorityStore,
    RebaselineOperation,
)
from firmquant.persistence.repositories import PersistenceConflict

NOW = datetime(2026, 8, 30, 8, tzinfo=UTC)


def _seed_legacy(database: Database) -> None:
    from firmquant.domain.broker_facts import AccountType
    from firmquant.persistence.account_authority import AccountBinding, AccountBindingRepository

    binding = AccountBinding.create(
        account_id_hash="a" * 64,
        account_type=AccountType.CASH,
        broker_snapshot_sha256="b" * 64,
        account_state_sha256="c" * 64,
        uquant_commit="1" * 40,
        uquant_code_fingerprint="d" * 64,
        data_hash="e" * 64,
        data_as_of="2026-08-29",
        data_symbols=("sz300308",),
        created_at=NOW,
    )
    AccountBindingRepository(database).bind(binding)
    with database.transaction():
        database.write(
            """
            INSERT INTO runtime_state(
                singleton_id,mode,state,revision,reason,blockers_json,updated_at
            ) VALUES(1,'SHADOW','DISARMED',0,'test','[]',?)
            """,
            (NOW.isoformat(),),
        )


def test_active_epochs_recompute_and_cross_check_canonical_payloads(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        _seed_legacy(database)
        store = OperationalAuthorityStore(database)

        account = store.active_account_epoch()
        mode = store.active_mode_epoch()

        assert account.epoch == 1
        assert account.account_id_hash == "a" * 64
        assert account.account_state_sha256 == "c" * 64
        assert account.deployment_identity_sha256 is None
        assert mode.epoch == 1
        assert mode.mode is Mode.SHADOW
        assert mode.deployment_identity_sha256 is None
        assert mode.caps_sha256 is None
    finally:
        database.close()


def test_active_epoch_read_fails_closed_on_payload_mismatch(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        _seed_legacy(database)
        with database.transaction():
            database.write(
                """
                INSERT INTO mode_epochs(
                    epoch,mode,deployment_identity_sha256,payload_json,payload_sha256,created_at
                ) VALUES(2,'CANARY',NULL,'{}',?,'2026-08-30T08:01:00+00:00')
                """,
                ("f" * 64,),
            )
            database.write("UPDATE mode_epoch_active SET epoch=2 WHERE singleton_id=1")

        with pytest.raises(PersistenceConflict, match="mode epoch payload"):
            OperationalAuthorityStore(database).active_mode_epoch()
    finally:
        database.close()


def test_active_legacy_epoch_revalidates_complete_source_binding(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        _seed_legacy(database)
        with database.transaction():
            database.write("DROP TRIGGER account_bindings_reject_update")
            database.write("UPDATE account_bindings SET data_hash=? WHERE singleton_id=1", ("9" * 64,))

        with pytest.raises(PersistenceConflict, match="source binding"):
            OperationalAuthorityStore(database).active_account_epoch()
    finally:
        database.close()


def _rebaseline(operation_id: str = "rebaseline-1", *, reason: str = "reviewed") -> RebaselineOperation:
    return RebaselineOperation.create(
        operation_id=operation_id,
        source_epoch=1,
        target_epoch=2,
        account_id_hash="a" * 64,
        account_before_sha256="c" * 64,
        candidate_account_state_sha256="d" * 64,
        deployment_identity_sha256="e" * 64,
        broker_snapshot_sha256="f" * 64,
        broker_snapshot_id="snapshot-rebaseline-1",
        backup_id="backup-rebaseline-1",
        reviewed_evidence_sha256="1" * 64,
        account_path_sha256="4" * 64,
        reason=reason,
        created_at=NOW,
    )


def _transition(operation_id: str = "transition-1", *, target: Mode = Mode.CANARY) -> ModeTransitionOperation:
    return ModeTransitionOperation.create(
        operation_id=operation_id,
        source_epoch=1,
        target_epoch=2,
        source_mode=Mode.SHADOW,
        target_mode=target,
        deployment_identity_sha256="2" * 64,
        backup_id="backup-transition-1",
        evidence_sha256="3" * 64,
        created_at=NOW,
    )


def test_prepare_operations_are_idempotent_and_payload_collisions_fail_closed(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        store = OperationalAuthorityStore(database)
        rebaseline = _rebaseline()
        transition = _transition()

        assert store.prepare_rebaseline(rebaseline) == rebaseline
        assert store.prepare_rebaseline(rebaseline).payload_sha256 == rebaseline.payload_sha256
        assert store.prepare_transition(transition).payload_sha256 == transition.payload_sha256
        assert store.prepare_transition(transition) == transition

        with database.transaction():
            database.write(
                """
                UPDATE account_rebaseline_operations
                SET stage='FILE_COMMITTED',actual_account_after_sha256=?,
                    updated_at='2026-08-30T08:01:00+00:00'
                WHERE operation_id=?
                """,
                ("5" * 64, rebaseline.operation_id),
            )
            database.write(
                """
                UPDATE mode_transition_operations
                SET stage='EPOCH_COMMITTED',updated_at='2026-08-30T08:01:00+00:00'
                WHERE operation_id=?
                """,
                (transition.operation_id,),
            )
        assert store.prepare_rebaseline(rebaseline).stage.value == "FILE_COMMITTED"
        assert store.prepare_transition(transition).stage.value == "EPOCH_COMMITTED"

        with pytest.raises(PersistenceConflict, match="rebaseline operation identity collision"):
            store.prepare_rebaseline(_rebaseline(reason="different reviewed reason"))
        with pytest.raises(PersistenceConflict, match="mode transition operation identity collision"):
            store.prepare_transition(_transition(target=Mode.LIVE))
        assert store.prepare_rebaseline(rebaseline).stage.value == "FILE_COMMITTED"
        assert store.prepare_transition(transition).stage.value == "EPOCH_COMMITTED"
    finally:
        database.close()


def test_transaction_aware_prepare_variants_do_not_nest_transactions(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        store = OperationalAuthorityStore(database)
        with database.transaction():
            assert store.prepare_rebaseline_in_transaction(_rebaseline()) == _rebaseline()
            assert store.prepare_transition_in_transaction(_transition()) == _transition()
        with database.transaction(), pytest.raises(TransactionRequired, match="nested"):
            store.prepare_rebaseline(_rebaseline("rebaseline-2"))
    finally:
        database.close()


def test_operation_constructors_require_real_canonical_identity_and_adjacent_epochs() -> None:
    with pytest.raises(ValueError, match="adjacent"):
        RebaselineOperation.create(
            operation_id="rebaseline-1",
            source_epoch=1,
            target_epoch=3,
            account_id_hash="a" * 64,
            account_before_sha256="b" * 64,
            candidate_account_state_sha256="c" * 64,
            deployment_identity_sha256="d" * 64,
            broker_snapshot_sha256="e" * 64,
            broker_snapshot_id="snapshot-1",
            backup_id="backup-1",
            reviewed_evidence_sha256="f" * 64,
            account_path_sha256="1" * 64,
            reason="reviewed",
            created_at=NOW,
        )


def test_operation_text_rejects_unsafe_unicode() -> None:
    with pytest.raises(ValueError, match="canonical"):
        _rebaseline(reason="reviewed\u202ereason")
    with pytest.raises(ValueError, match="canonical"):
        _rebaseline(reason="reviewe\u0301d")


def test_store_rejects_typed_operation_that_bypasses_canonical_constructor(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        store = OperationalAuthorityStore(database)
        with pytest.raises(PersistenceConflict, match="canonical PREPARED"):
            store.prepare_rebaseline(replace(_rebaseline(), deployment_identity_sha256="z" * 64))
        with pytest.raises(PersistenceConflict, match="canonical PREPARED"):
            store.prepare_transition(replace(_transition(), evidence_sha256="z" * 64))
        assert database.scalar("SELECT count(*) FROM account_rebaseline_operations") == 0
        assert database.scalar("SELECT count(*) FROM mode_transition_operations") == 0
    finally:
        database.close()
    with pytest.raises(ValueError, match="deployment identity"):
        _transition().__class__.create(
            operation_id="transition-2",
            source_epoch=1,
            target_epoch=2,
            source_mode=Mode.SHADOW,
            target_mode=Mode.CANARY,
            deployment_identity_sha256="not-a-sha",
            backup_id="backup-2",
            evidence_sha256="3" * 64,
            created_at=NOW,
        )


@pytest.mark.parametrize("malformation", ["incomplete-stage", "backwards-time"])
def test_rebaseline_reader_rejects_structurally_malformed_rows(tmp_path: Path, malformation: str) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    operation = _rebaseline()
    store = OperationalAuthorityStore(database)
    try:
        assert store.prepare_rebaseline(operation) == operation
        with database.transaction():
            database.write("DROP TRIGGER account_rebaseline_operations_forward_only")
            if malformation == "incomplete-stage":
                database.write("DROP TRIGGER account_rebaseline_operations_stage_output_guard")
                database.write(
                    "UPDATE account_rebaseline_operations SET stage='FILE_COMMITTED' WHERE operation_id=?",
                    (operation.operation_id,),
                )
            else:
                database.write(
                    "UPDATE account_rebaseline_operations "
                    "SET updated_at='2026-08-30T07:59:59+00:00' WHERE operation_id=?",
                    (operation.operation_id,),
                )
        with pytest.raises(PersistenceConflict, match="malformed"):
            store.prepare_rebaseline(operation)
    finally:
        database.close()


def test_mode_transition_reader_rejects_updated_at_before_created_at(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    operation = _transition()
    store = OperationalAuthorityStore(database)
    try:
        assert store.prepare_transition(operation) == operation
        with database.transaction():
            database.write("DROP TRIGGER mode_transition_operations_forward_only")
            database.write(
                "UPDATE mode_transition_operations "
                "SET updated_at='2026-08-30T07:59:59+00:00' WHERE operation_id=?",
                (operation.operation_id,),
            )
        with pytest.raises(PersistenceConflict, match="malformed"):
            store.prepare_transition(operation)
    finally:
        database.close()
