from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import firmquant.persistence.backup as backup
from firmquant.persistence.backup import BackupBundleInputs, BackupError, BackupVerification
from tests.integration.test_backup_v3_restore import NOW, _v3_case


class PublicationDatabase:
    """Minimal durable-state substitute for publication contract branches."""

    def __init__(
        self,
        *,
        operation_row: dict[str, object] | None,
        receipt_row: tuple[object, ...] | None = None,
    ) -> None:
        self.operation_row = operation_row
        self.receipt_row = receipt_row
        self.writes: list[tuple[str, tuple[object, ...]]] = []

    @contextmanager
    def transaction(self) -> Iterator[None]:
        yield

    def query_one(
        self,
        sql: str,
        _parameters: Sequence[object] = (),
    ) -> dict[str, object] | tuple[object, ...] | None:
        if "FROM backup_publication_operations" in sql:
            return self.operation_row
        if "FROM backup_receipts" in sql:
            return self.receipt_row
        raise AssertionError(f"unexpected publication query: {sql}")

    def write(self, sql: str, parameters: Sequence[object] = ()) -> Any:
        values = tuple(parameters)
        self.writes.append((sql, values))
        if "SET stage='CONTRADICTION'" in sql and self.operation_row is not None:
            self.operation_row["stage"] = "CONTRADICTION"
        return None


@pytest.fixture
def publication_case(
    tmp_path: Path,
) -> tuple[Path, Path, BackupBundleInputs, BackupVerification]:
    setup_database, _account, inputs, root = _v3_case(tmp_path)
    setup_database.close()
    final_bundle = root / backup._v3_backup_id(inputs)
    final_bundle.mkdir()
    (final_bundle / "manifest.json").write_text(
        backup.canonical_json({"created_at": NOW}),
        encoding="utf-8",
    )
    deployment = inputs.deployment_identity
    evidence = inputs.operational_evidence_identity
    assert deployment is not None
    assert evidence is not None
    assert inputs.reason is not None
    verification = BackupVerification(
        backup_id=final_bundle.name,
        database_sha256="1" * 64,
        account_state_sha256=inputs.account_sha256,
        manifest_sha256="2" * 64,
        audit_count=1,
        audit_head_hash="3" * 64,
        schema_version=3,
        operational_schema_version=6,
        complete_bundle=True,
        production_authority=True,
        decision_id=inputs.decision_id,
        reason=inputs.reason,
        deployment_identity_sha256=deployment.sha256,
        operational_evidence_identity_sha256=evidence.sha256,
        account_authority_epoch=deployment.account_authority_epoch,
        mode_epoch=deployment.mode_epoch,
        broker_snapshot_id=evidence.broker_snapshot_id,
        broker_snapshot_sha256=evidence.broker_snapshot_sha256,
    )
    return final_bundle, root, inputs, verification


def _operation_row(
    inputs: BackupBundleInputs,
    verification: BackupVerification,
    *,
    stage: str,
) -> dict[str, object]:
    deployment = inputs.deployment_identity
    evidence = inputs.operational_evidence_identity
    assert deployment is not None
    assert evidence is not None
    assert inputs.reason is not None
    payload, payload_sha256 = backup._publication_payload(
        backup_id=verification.backup_id,
        inputs=inputs,
        manifest_sha256=verification.manifest_sha256,
        database_sha256=verification.database_sha256,
    )
    return {
        "operation_id": "backup-publication-" + payload_sha256,
        "stage": stage,
        "reason": inputs.reason.value,
        "manifest_sha256": verification.manifest_sha256,
        "database_sha256": verification.database_sha256,
        "account_state_sha256": inputs.account_sha256,
        "deployment_identity_sha256": deployment.sha256,
        "operational_evidence_identity_sha256": evidence.sha256,
        "account_authority_epoch": deployment.account_authority_epoch,
        "mode_epoch": deployment.mode_epoch,
        "payload_json": payload,
        "payload_sha256": payload_sha256,
        "bundle_name": verification.backup_id,
    }


def _resume(
    monkeypatch: pytest.MonkeyPatch,
    database: PublicationDatabase,
    *,
    bundle: Path,
    final_bundle: Path,
    inputs: BackupBundleInputs,
    verification: BackupVerification,
) -> backup.BackupReceipt:
    monkeypatch.setattr(backup, "verify_backup", lambda _bundle: verification)
    monkeypatch.setattr(
        backup,
        "_verify_private_v3_staging",
        lambda *_args, **_kwargs: verification,
    )
    monkeypatch.setattr(backup, "_sha256_file", lambda _path: verification.manifest_sha256)
    return backup._resume_v3_backup_publication(
        database,  # type: ignore[arg-type]
        bundle=bundle,
        final_bundle=final_bundle,
        inputs=inputs,
    )


@pytest.mark.parametrize(
    ("changes", "label"),
    [
        ({"schema_version": 2}, "schema"),
        ({"reason": None}, "reason"),
        ({"deployment_identity_sha256": "0" * 64}, "deployment"),
        ({"operational_evidence_identity_sha256": "0" * 64}, "evidence"),
        ({"account_state_sha256": "0" * 64}, "account"),
    ],
)
def test_publication_rejects_verification_identity_mismatch_before_database_access(
    publication_case: tuple[Path, Path, BackupBundleInputs, BackupVerification],
    monkeypatch: pytest.MonkeyPatch,
    changes: dict[str, object],
    label: str,
) -> None:
    final_bundle, _root, inputs, verification = publication_case
    mismatched = replace(verification, **changes)

    class UnreachableDatabase:
        def query_one(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError(f"{label} mismatch must fail before database access")

    with pytest.raises(BackupError, match="staging identity conflicts"):
        _resume(
            monkeypatch,
            UnreachableDatabase(),  # type: ignore[arg-type]
            bundle=final_bundle,
            final_bundle=final_bundle,
            inputs=inputs,
            verification=mismatched,
        )


@pytest.mark.parametrize("stage", ["PREPARED", "PUBLISHED"])
def test_colliding_nonterminal_publication_identity_is_durably_contradictory(
    publication_case: tuple[Path, Path, BackupBundleInputs, BackupVerification],
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    final_bundle, _root, inputs, verification = publication_case
    row = _operation_row(inputs, verification, stage=stage)
    row["manifest_sha256"] = "0" * 64
    database = PublicationDatabase(operation_row=row)

    with pytest.raises(BackupError, match="publication identity collision"):
        _resume(
            monkeypatch,
            database,
            bundle=final_bundle,
            final_bundle=final_bundle,
            inputs=inputs,
            verification=verification,
        )

    assert row["stage"] == "CONTRADICTION"
    assert any("SET stage='CONTRADICTION'" in sql for sql, _values in database.writes)


def test_matching_terminal_contradiction_remains_fail_closed(
    publication_case: tuple[Path, Path, BackupBundleInputs, BackupVerification],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    final_bundle, _root, inputs, verification = publication_case
    database = PublicationDatabase(operation_row=_operation_row(inputs, verification, stage="CONTRADICTION"))

    with pytest.raises(BackupError, match="publication is contradictory"):
        _resume(
            monkeypatch,
            database,
            bundle=final_bundle,
            final_bundle=final_bundle,
            inputs=inputs,
            verification=verification,
        )
    assert database.writes == []


def test_preserved_final_bundle_collision_blocks_staging_publication(
    publication_case: tuple[Path, Path, BackupBundleInputs, BackupVerification],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    final_bundle, root, inputs, verification = publication_case
    staging = root / f".{verification.backup_id}.staging"
    staging.mkdir()
    (staging / "manifest.json").write_text(
        backup.canonical_json({"created_at": NOW}),
        encoding="utf-8",
    )
    database = PublicationDatabase(operation_row=_operation_row(inputs, verification, stage="PREPARED"))

    with pytest.raises(BackupError, match="final bundle collides with preserved evidence"):
        _resume(
            monkeypatch,
            database,
            bundle=staging,
            final_bundle=final_bundle,
            inputs=inputs,
            verification=verification,
        )
    assert database.writes == []


def test_terminal_publication_without_receipt_is_rejected(
    publication_case: tuple[Path, Path, BackupBundleInputs, BackupVerification],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    final_bundle, _root, inputs, verification = publication_case
    database = PublicationDatabase(
        operation_row=_operation_row(inputs, verification, stage="RECEIPT_COMMITTED")
    )

    with pytest.raises(BackupError, match="receipt is missing after terminal stage"):
        _resume(
            monkeypatch,
            database,
            bundle=final_bundle,
            final_bundle=final_bundle,
            inputs=inputs,
            verification=verification,
        )
    assert database.writes == []


def test_colliding_receipt_blocks_terminal_publication_retry(
    publication_case: tuple[Path, Path, BackupBundleInputs, BackupVerification],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    final_bundle, _root, inputs, verification = publication_case
    database = PublicationDatabase(
        operation_row=_operation_row(inputs, verification, stage="RECEIPT_COMMITTED"),
        receipt_row=("0" * 64, verification.database_sha256, 3),
    )

    with pytest.raises(BackupError, match="receipt collides with publication"):
        _resume(
            monkeypatch,
            database,
            bundle=final_bundle,
            final_bundle=final_bundle,
            inputs=inputs,
            verification=verification,
        )
    assert database.writes == []


def test_receipt_committed_exact_retry_is_read_only_and_idempotent(
    publication_case: tuple[Path, Path, BackupBundleInputs, BackupVerification],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    final_bundle, _root, inputs, verification = publication_case
    database = PublicationDatabase(
        operation_row=_operation_row(inputs, verification, stage="RECEIPT_COMMITTED"),
        receipt_row=(verification.manifest_sha256, verification.database_sha256, 3),
    )

    receipt = _resume(
        monkeypatch,
        database,
        bundle=final_bundle,
        final_bundle=final_bundle,
        inputs=inputs,
        verification=verification,
    )

    assert receipt.backup_id == verification.backup_id
    assert receipt.bundle_path == final_bundle
    assert receipt.manifest_sha256 == verification.manifest_sha256
    assert receipt.production_authority is True
    assert database.writes == []
