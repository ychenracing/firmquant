from __future__ import annotations

import copy
import hashlib
import os
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

import firmquant.persistence.backup as backup
from firmquant.application.production_identity import (
    DeploymentIdentity,
    OperationalEvidenceIdentity,
    deployment_caps_sha256,
    semantic_config_sha256,
)
from firmquant.config import Mode, Settings
from firmquant.risk.production_policy import ProductionSafetyPolicy

NOW = datetime(2026, 8, 25, 8, tzinfo=UTC)
SESSION = date(2026, 8, 25)


def _inputs(tmp_path: Path) -> backup.BackupBundleInputs:
    tmp_path.mkdir(parents=True, exist_ok=True)
    config = tmp_path / "production.toml"
    config.write_text("", encoding="utf-8")
    members: list[Path] = []
    for name in ("safety.json", "calendar.json", "active.json", "strategy.json"):
        path = tmp_path / name
        path.write_text("{}", encoding="utf-8")
        members.append(path)
    return backup.BackupBundleInputs(
        settings=Settings(),
        config_path=config,
        config_sha256=hashlib.sha256(config.read_bytes()).hexdigest(),
        safety_manifest_path=members[0],
        calendar_manifest_path=members[1],
        active_data_manifest_path=members[2],
        strategy_data_manifest_path=members[3],
        firmquant_commit="a" * 40,
        uquant_commit="b" * 40,
        account_sha256="c" * 64,
        decision_id="decision-test",
        strategy_session=SESSION,
    )


def _v3_inputs(
    tmp_path: Path,
    *,
    reason: backup.BackupReason = backup.BackupReason.ACCOUNT_REBASELINE,
    decision_id: str | None = None,
) -> backup.BackupBundleInputs:
    inputs = _inputs(tmp_path)
    settings = inputs.settings
    deployment = DeploymentIdentity(
        firmquant_commit=inputs.firmquant_commit,
        uquant_commit=inputs.uquant_commit,
        uquant_tree="d" * 40,
        uquant_package_manifest_sha256="e" * 64,
        uquant_code_fingerprint="f" * 64,
        uquant_config_fingerprint="1" * 64,
        semantic_config_sha256=semantic_config_sha256(settings),
        raw_config_sha256=inputs.config_sha256,
        xtquant_safety_manifest_sha256="2" * 64,
        account_id_hash="3" * 64,
        account_authority_epoch=2,
        mode_epoch=3,
        mode=Mode.PAPER,
        caps_sha256=deployment_caps_sha256(settings),
        production_policy_sha256=ProductionSafetyPolicy.from_settings(settings).sha256,
    )
    evidence = OperationalEvidenceIdentity(
        deployment_identity=deployment,
        account_state_sha256=inputs.account_sha256,
        broker_snapshot_id="snapshot-coverage",
        broker_snapshot_sha256="4" * 64,
        broker_event_watermark=7,
        snapshot_started_at=datetime(2026, 8, 25, 7, 59, 59, tzinfo=UTC),
        snapshot_completed_at=datetime(2026, 8, 25, 8, tzinfo=UTC),
        snapshot_duration_ms=1_000,
        calendar_sha256="5" * 64,
        active_data_generation_sha256="6" * 64,
        strategy_data_manifest_sha256="7" * 64,
        strategy_session=inputs.strategy_session,
        decision_id=decision_id,
        phase={
            backup.BackupReason.SESSION_CLOSE: "EOD",
            backup.BackupReason.MODE_TRANSITION: "MODE_TRANSITION",
            backup.BackupReason.ACCOUNT_REBASELINE: "ACCOUNT_REBASELINE",
        }[reason],
        kind="BACKUP",
    )
    return replace(
        inputs,
        decision_id=decision_id,
        reason=reason,
        deployment_identity=deployment,
        operational_evidence_identity=evidence,
    )


def _write_canonical(path: Path, payload: object) -> None:
    path.write_text(backup.canonical_json(payload), encoding="utf-8")


def _hardlink_or_skip(source: Path, destination: Path) -> None:
    try:
        os.link(source, destination)
    except OSError as exc:
        pytest.skip(f"hard links are unavailable on this test filesystem: {exc}")


def _symlink_or_skip(source: Path, destination: Path, *, target_is_directory: bool = False) -> None:
    try:
        destination.symlink_to(source, target_is_directory=target_is_directory)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable on this test filesystem: {exc}")


def _data_manifest_payloads() -> tuple[dict[str, object], dict[str, object]]:
    members = {"sz300308.csv": "c" * 64}
    generation_id = "gen-" + "c" * 24
    active: dict[str, object] = {
        "schema": "firmquant.data-generation.v1",
        "generation_id": generation_id,
        "source": "reviewed-test",
        "created_at": NOW.isoformat(),
        "members": members,
        "data_sha256": hashlib.sha256(
            backup.canonical_json(dict(sorted(members.items()))).encode("utf-8")
        ).hexdigest(),
    }
    strategy: dict[str, object] = {
        "schema": "firmquant.daily-data-manifest.v2",
        "target_session": SESSION.isoformat(),
        "source": "xtquant",
        "uquant_manifest_sha256": "7" * 64,
        "data_generation_id": generation_id,
        "observations": [
            {
                "symbol": "sz300308",
                "latest_observed_session": SESSION.isoformat(),
                "suspension_evidence_sha256": None,
            }
        ],
    }
    return active, strategy


def test_backup_bundle_inputs_reject_noncanonical_identity_and_member_types(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    cases: tuple[tuple[dict[str, object], type[Exception], str], ...] = (
        ({"settings": object()}, TypeError, "validated Settings"),
        ({"config_sha256": "g" * 64}, ValueError, "config SHA-256"),
        ({"account_sha256": "z" * 64}, ValueError, "account SHA-256"),
        ({"firmquant_commit": "a" * 39}, ValueError, "firmquant commit"),
        ({"uquant_commit": "b" * 39}, ValueError, "uquant commit"),
        ({"decision_id": ""}, ValueError, "decision id"),
        ({"strategy_session": datetime(2026, 8, 25, tzinfo=UTC)}, TypeError, "strategy session"),
        ({"safety_manifest_path": "not-a-path"}, TypeError, "member paths"),
    )
    for changes, error_type, pattern in cases:
        with pytest.raises(error_type, match=pattern):
            replace(inputs, **changes)
    with pytest.raises(ValueError, match="schema-v2 complete backup requires a frozen decision"):
        replace(inputs, decision_id=None)


def test_backup_file_copy_and_write_helpers_fail_closed(tmp_path: Path) -> None:
    destination = tmp_path / "member.bin"
    backup._write_fsynced(destination, b"first")
    assert destination.read_bytes() == b"first"
    with pytest.raises(backup.BackupError, match="cannot write backup member"):
        backup._write_fsynced(destination, b"second")

    with pytest.raises(backup.BackupError, match="regular non-symlink"):
        backup._copy_fsynced(tmp_path / "missing", tmp_path / "copy.bin", label="test member")

    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    copied = tmp_path / "copied.bin"
    backup._copy_fsynced(source, copied, label="test member")
    assert copied.read_bytes() == b"payload"
    backup._fsync_directory(tmp_path)


def test_backup_config_digest_mismatch_is_rejected(tmp_path: Path) -> None:
    inputs = replace(_inputs(tmp_path), config_sha256="0" * 64)
    with pytest.raises(backup.BackupError, match="identity does not match"):
        backup._validated_config_bytes(inputs)


def test_verify_backup_rejects_external_digest_and_unknown_schema(tmp_path: Path) -> None:
    with pytest.raises(backup.BackupVerificationError, match="regular directory"):
        backup.verify_backup(tmp_path / "missing-bundle")

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    manifest = bundle / "manifest.json"
    manifest.write_text('{"schema_version":4}', encoding="utf-8")
    observed = hashlib.sha256(manifest.read_bytes()).hexdigest()

    with pytest.raises(backup.BackupVerificationError, match="external receipt"):
        backup.verify_backup(bundle, expected_manifest_sha256="0" * 64)
    with pytest.raises(backup.BackupVerificationError, match="unsupported backup manifest"):
        backup.verify_backup(bundle, expected_manifest_sha256=observed)


def test_legacy_bundle_manifest_contract_rejects_early_shape_errors(tmp_path: Path) -> None:
    with pytest.raises(backup.BackupVerificationError, match="manifest fields"):
        backup._verify_legacy_bundle(tmp_path, {}, manifest_sha256="0" * 64)

    root = {
        "schema_version": 1,
        "backup_id": "backup-test",
        "created_at": NOW.isoformat(),
        "database": {},
        "account_state": None,
        "operational_schema_version": 1,
        "audit": {"count": 0, "head_hash": "0" * 64},
    }
    with pytest.raises(backup.BackupVerificationError, match="database manifest fields"):
        backup._verify_legacy_bundle(tmp_path, root, manifest_sha256="0" * 64)

    root["database"] = {"filename": "wrong.sqlite3", "sha256": "0" * 64}
    with pytest.raises(backup.BackupVerificationError, match="filename is not canonical"):
        backup._verify_legacy_bundle(tmp_path, root, manifest_sha256="0" * 64)


def test_complete_bundle_manifest_contract_rejects_early_shape_errors(tmp_path: Path) -> None:
    with pytest.raises(backup.BackupVerificationError, match="manifest fields"):
        backup._verify_complete_bundle(tmp_path, {}, manifest_sha256="0" * 64)

    root = {
        "schema_version": 2,
        "backup_id": "backup-test",
        "created_at": NOW.isoformat(),
        "members": {},
        "operational_schema_version": 1,
        "audit": {"count": 0, "head_hash": "0" * 64},
        "deployment": {},
    }
    with pytest.raises(backup.BackupVerificationError, match="member set"):
        backup._verify_complete_bundle(tmp_path, root, manifest_sha256="0" * 64)


def test_backup_state_preflight_rejects_invalid_destination_time_account_and_overwrite(
    tmp_path: Path,
) -> None:
    database = object()
    with pytest.raises(backup.BackupError, match="destination"):
        backup.backup_state(database, tmp_path / "missing")  # type: ignore[arg-type]

    root = tmp_path / "backups"
    root.mkdir()
    with pytest.raises(backup.BackupError, match="timezone-aware"):
        backup.backup_state(
            database,  # type: ignore[arg-type]
            root,
            created_at=datetime(2026, 8, 25, 8),
        )

    inputs = _inputs(tmp_path / "complete")
    with pytest.raises(backup.BackupError, match="requires uquant AccountState"):
        backup.backup_state(
            database,  # type: ignore[arg-type]
            root,
            created_at=NOW,
            complete_inputs=inputs,
        )

    existing = root / ("backup-" + NOW.strftime("%Y%m%dT%H%M%S%fZ"))
    existing.mkdir()
    with pytest.raises(backup.BackupError, match="overwrite"):
        backup.backup_state(database, root, created_at=NOW)  # type: ignore[arg-type]


def test_database_schema_version_requires_integer() -> None:
    class MissingSchema:
        def scalar(self, _query: str) -> object:
            return None

    with pytest.raises(backup.BackupVerificationError, match="schema version"):
        backup._database_schema_version(MissingSchema())  # type: ignore[arg-type]


def test_schema_v3_bundle_inputs_bind_every_authority_axis(tmp_path: Path) -> None:
    inputs = _v3_inputs(tmp_path)
    deployment = inputs.deployment_identity
    evidence = inputs.operational_evidence_identity
    assert deployment is not None
    assert evidence is not None

    incomplete_cases = (
        {"reason": None},
        {"deployment_identity": None},
        {"operational_evidence_identity": None},
    )
    for changes in incomplete_cases:
        with pytest.raises(ValueError, match="identity inputs must be complete"):
            replace(inputs, **changes)

    typed_cases: tuple[tuple[dict[str, object], type[Exception], str], ...] = (
        ({"reason": "ACCOUNT_REBASELINE"}, TypeError, "reason must be typed"),
        ({"deployment_identity": object()}, TypeError, "deployment identity must be typed"),
        (
            {"operational_evidence_identity": object()},
            TypeError,
            "operational evidence identity must be typed",
        ),
    )
    for changes, error_type, pattern in typed_cases:
        with pytest.raises(error_type, match=pattern):
            replace(inputs, **changes)

    other_deployment = replace(deployment, account_authority_epoch=4)
    other_evidence = replace(evidence, deployment_identity=other_deployment)
    cases: tuple[tuple[dict[str, object], str], ...] = (
        ({"operational_evidence_identity": other_evidence}, "same deployment payload"),
        ({"firmquant_commit": "8" * 40}, "firmquant commit differs"),
        ({"uquant_commit": "9" * 40}, "uquant commit differs"),
        ({"config_sha256": "a" * 64}, "config digest differs"),
        ({"account_sha256": "b" * 64}, "account state differs"),
        ({"strategy_session": date(2026, 8, 26)}, "strategy session differs"),
        ({"decision_id": "unexpected"}, "decision differs"),
        (
            {"settings": inputs.settings.model_copy(update={"mode": Mode.SHADOW})},
            "deployment mode differs",
        ),
        (
            {"operational_evidence_identity": replace(evidence, phase="EOD")},
            "reason phase/kind facts",
        ),
        (
            {"operational_evidence_identity": replace(evidence, kind="READINESS")},
            "reason phase/kind facts",
        ),
    )
    for changes, pattern in cases:
        with pytest.raises(ValueError, match=pattern):
            replace(inputs, **changes)

    with pytest.raises(ValueError, match="requires a frozen decision"):
        _v3_inputs(tmp_path / "session-close", reason=backup.BackupReason.SESSION_CLOSE)


@pytest.mark.parametrize(
    ("case", "pattern"),
    (
        ("active-contract", "active data source manifest contract"),
        ("generation-id", "generation id"),
        ("source-name", "source name"),
        ("created-invalid", "creation time is invalid"),
        ("created-naive", "creation time must be timezone-aware"),
        ("created-offset", "creation time is not canonical UTC"),
        ("members-empty", "has no members"),
        ("member-name", "generation member"),
        ("member-digest", "canonical lowercase digest"),
        ("data-digest", "generation digest"),
        ("strategy-contract", "strategy data manifest contract"),
        ("target-session", "target session"),
        ("strategy-source", "source/session"),
        ("uquant-digest", "uquant data manifest"),
        ("generation-binding", "generation identity"),
        ("observations-empty", "observations"),
        ("observation-contract", "observation contract"),
        ("symbol", "observation symbol"),
        ("observation-session", "observation session"),
        ("observation-future", "observation timing"),
        ("suspension-digest", "suspension evidence"),
        ("observation-order", "observations are not canonical"),
    ),
)
def test_data_manifest_contract_rejects_noncanonical_operational_evidence(
    tmp_path: Path,
    case: str,
    pattern: str,
) -> None:
    active, strategy = _data_manifest_payloads()
    observations = strategy["observations"]
    assert isinstance(observations, list)
    observation = observations[0]
    assert isinstance(observation, dict)

    if case == "active-contract":
        active["extra"] = True
    elif case == "generation-id":
        active["generation_id"] = "gen-not-hex"
    elif case == "source-name":
        active["source"] = " reviewed-test"
    elif case == "created-invalid":
        active["created_at"] = "not-a-time"
    elif case == "created-naive":
        active["created_at"] = "2026-08-25T08:00:00"
    elif case == "created-offset":
        active["created_at"] = "2026-08-25T16:00:00+08:00"
    elif case == "members-empty":
        active["members"] = {}
    elif case == "member-name":
        active["members"] = {"../prices.csv": "c" * 64}
    elif case == "member-digest":
        active["members"] = {"sz300308.csv": "C" * 64}
    elif case == "data-digest":
        active["data_sha256"] = "0" * 64
    elif case == "strategy-contract":
        strategy["extra"] = True
    elif case == "target-session":
        strategy["target_session"] = "2026-02-30"
    elif case == "strategy-source":
        strategy["source"] = "reviewed-test"
    elif case == "uquant-digest":
        strategy["uquant_manifest_sha256"] = "G" * 64
    elif case == "generation-binding":
        strategy["data_generation_id"] = "gen-" + "d" * 24
    elif case == "observations-empty":
        strategy["observations"] = []
    elif case == "observation-contract":
        observation["extra"] = True
    elif case == "symbol":
        observation["symbol"] = "hk000001"
    elif case == "observation-session":
        observation["latest_observed_session"] = "2026-02-30"
    elif case == "observation-future":
        observation["latest_observed_session"] = "2026-08-26"
    elif case == "suspension-digest":
        observation["latest_observed_session"] = "2026-08-24"
        observation["suspension_evidence_sha256"] = "G" * 64
    elif case == "observation-order":
        strategy["observations"] = [
            {
                "symbol": "sz300309",
                "latest_observed_session": SESSION.isoformat(),
                "suspension_evidence_sha256": None,
            },
            copy.deepcopy(observation),
        ]
    else:
        raise AssertionError(case)

    active_path = tmp_path / "active.json"
    strategy_path = tmp_path / "strategy.json"
    _write_canonical(active_path, active)
    _write_canonical(strategy_path, strategy)
    with pytest.raises(backup.BackupVerificationError, match=pattern):
        backup._validate_data_manifests(active_path, strategy_path)


def test_data_manifest_contract_accepts_exact_canonical_evidence(tmp_path: Path) -> None:
    active, strategy = _data_manifest_payloads()
    active_path = tmp_path / "active.json"
    strategy_path = tmp_path / "strategy.json"
    _write_canonical(active_path, active)
    _write_canonical(strategy_path, strategy)

    assert backup._validate_data_manifests(active_path, strategy_path) == SESSION

    active_path.write_text(backup.canonical_json(active) + "\n", encoding="utf-8")
    with pytest.raises(backup.BackupVerificationError, match="strict canonical JSON"):
        backup._validate_data_manifests(active_path, strategy_path)

    surrogate = tmp_path / "surrogate.json"
    surrogate.write_bytes(b'{"value":"\\ud800"}')
    with pytest.raises(backup.BackupVerificationError, match="strict canonical JSON"):
        backup._canonical_object(surrogate, label="surrogate evidence")


@pytest.mark.parametrize(
    ("payload", "pattern"),
    (
        ('{"value":NaN}', "non-standard constant"),
        ('{"value":1,"value":2}', "duplicate key"),
        ("[]", "root must be an object"),
        ("{", "not valid UTF-8 JSON"),
    ),
)
def test_strict_json_loader_rejects_ambiguous_or_non_object_payloads(
    tmp_path: Path,
    payload: str,
    pattern: str,
) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(backup.BackupVerificationError, match=pattern):
        backup._json_object(path, label="test manifest")


def test_manifest_primitive_readers_reject_implicit_coercion() -> None:
    with pytest.raises(backup.BackupVerificationError, match="must be an object"):
        backup._mapping([], label="value")
    with pytest.raises(backup.BackupVerificationError, match="must be an object"):
        backup._mapping({1: "value"}, label="value")
    with pytest.raises(backup.BackupVerificationError, match="must be text"):
        backup._text({"value": ""}, "value", label="value")
    assert backup._optional_text({"value": None}, "value", label="value") is None
    with pytest.raises(backup.BackupVerificationError, match="text or null"):
        backup._optional_text({"value": 1}, "value", label="value")
    for value in (True, "1"):
        with pytest.raises(backup.BackupVerificationError, match="must be integer"):
            backup._integer({"value": value}, "value", label="value")


def test_private_copy_helpers_resume_only_matching_prefixes(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"complete-payload")
    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    destination = tmp_path / "destination.bin"
    partial = tmp_path / ".destination.bin.copying"
    partial.write_bytes(b"complete-")

    backup._copy_verified_member(
        source,
        destination,
        expected_sha256=expected,
        label="evidence",
    )
    assert destination.read_bytes() == source.read_bytes()
    assert not partial.exists()

    conflicting = tmp_path / "conflicting.bin"
    conflicting.write_bytes(b"different")
    with pytest.raises(backup.BackupVerificationError, match="conflicts with source"):
        backup._copy_verified_member(
            source,
            conflicting,
            expected_sha256=expected,
            label="evidence",
        )

    linked = tmp_path / "linked.bin"
    _hardlink_or_skip(source, linked)
    with pytest.raises(backup.BackupVerificationError, match="not private regular evidence"):
        backup._copy_verified_member(
            source,
            linked,
            expected_sha256=expected,
            label="evidence",
        )

    bad_destination = tmp_path / "bad.bin"
    bad_partial = tmp_path / ".bad.bin.copying"
    bad_partial.write_bytes(b"wrong-prefix")
    with pytest.raises(backup.BackupVerificationError, match="partial copy conflicts"):
        backup._copy_verified_member(
            source,
            bad_destination,
            expected_sha256=expected,
            label="evidence",
        )


def test_private_content_helpers_resume_and_reject_collisions(tmp_path: Path) -> None:
    content = b"canonical-content"
    destination = tmp_path / "identity.json"
    partial = tmp_path / ".identity.json.copying"
    partial.write_bytes(b"canonical-")
    backup._ensure_fsynced_content(destination, content, label="identity")
    assert destination.read_bytes() == content
    backup._ensure_fsynced_content(destination, content, label="identity")

    destination.write_bytes(b"changed")
    with pytest.raises(backup.BackupVerificationError, match="staging member conflicts"):
        backup._ensure_fsynced_content(destination, content, label="identity")

    other = tmp_path / "other.json"
    other_partial = tmp_path / ".other.json.copying"
    other_partial.write_bytes(b"not-a-prefix")
    with pytest.raises(backup.BackupVerificationError, match="partial copy conflicts"):
        backup._ensure_fsynced_content(other, content, label="identity")

    symlinked = tmp_path / "symlinked.json"
    symlinked_partial = tmp_path / ".symlinked.json.copying"
    _symlink_or_skip(destination, symlinked_partial)
    with pytest.raises(backup.BackupVerificationError, match="not regular evidence"):
        backup._ensure_fsynced_content(symlinked, content, label="identity")


def test_restore_path_contract_rejects_aliases_containment_and_wrong_types(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    destination = tmp_path / "destination"
    source, target, parent = backup._validate_restore_paths(bundle, destination)
    assert source == bundle.resolve()
    assert target == destination.absolute()
    assert parent.resolved == tmp_path.resolve()
    backup._revalidate_parent(destination, parent)

    with pytest.raises(backup.BackupVerificationError, match="lexical alias"):
        backup._canonical_no_link_path(tmp_path / "child" / ".." / "bundle", label="source", must_exist=True)

    alias = tmp_path / "alias"
    _symlink_or_skip(bundle, alias, target_is_directory=True)
    with pytest.raises(backup.BackupVerificationError, match="symlink or reparse"):
        backup._canonical_no_link_path(alias / "child", label="source", must_exist=False)

    with pytest.raises(backup.BackupVerificationError, match="must not contain each other"):
        backup._validate_restore_paths(bundle, bundle / "restore")

    file_destination = tmp_path / "file-destination"
    file_destination.write_text("not a directory", encoding="utf-8")
    with pytest.raises(backup.BackupVerificationError, match="must be a directory"):
        backup._validate_restore_paths(bundle, file_destination)

    with pytest.raises(backup.BackupVerificationError, match="parent must exist"):
        backup._validate_restore_paths(bundle, tmp_path / "missing-parent" / "restore")

    wrong_parent = replace(parent, inode=parent.inode + 1)
    with pytest.raises(backup.BackupVerificationError, match="parent identity changed"):
        backup._revalidate_parent(destination, wrong_parent)


def test_restore_logical_values_and_identity_are_canonical(tmp_path: Path) -> None:
    assert backup._logical_value(b"payload") == {
        "blob_sha256": hashlib.sha256(b"payload").hexdigest(),
        "length": 7,
    }
    for value in (None, "text", 1, 1.5):
        assert backup._logical_value(value) == value
    with pytest.raises(backup.BackupVerificationError, match="unsupported SQLite value"):
        backup._logical_value(object())

    verification = backup.BackupVerification(
        backup_id="backup-test",
        database_sha256="1" * 64,
        account_state_sha256="2" * 64,
        manifest_sha256="3" * 64,
        audit_count=0,
        audit_head_hash="0" * 64,
        schema_version=3,
        operational_schema_version=6,
        production_authority=True,
    )
    first = backup._restore_identity(verification, tmp_path / "destination")
    second = backup._restore_identity(verification, tmp_path / "destination")
    assert first == second
    assert first[0].startswith("restore-")


def test_v3_source_receipt_requires_complete_identity_and_rejects_collision() -> None:
    verification = backup.BackupVerification(
        backup_id="backup-test",
        database_sha256="1" * 64,
        account_state_sha256=None,
        manifest_sha256="3" * 64,
        audit_count=0,
        audit_head_hash="0" * 64,
        schema_version=3,
        operational_schema_version=6,
        production_authority=True,
    )

    class MissingReceiptDatabase:
        def query_one(self, _query: str, _parameters: object) -> None:
            return None

    with pytest.raises(backup.BackupVerificationError, match="source identity is incomplete"):
        backup._ensure_source_backup_receipt(
            MissingReceiptDatabase(),  # type: ignore[arg-type]
            verification=verification,
            manifest={},
            now=NOW,
        )

    class ExistingReceiptDatabase:
        def __init__(self, row: tuple[object, ...]) -> None:
            self.row = row

        def query_one(self, _query: str, _parameters: object) -> tuple[object, ...]:
            return self.row

    expected = (verification.manifest_sha256, verification.database_sha256, "VERIFIED", 3)
    backup._ensure_source_backup_receipt(
        ExistingReceiptDatabase(expected),  # type: ignore[arg-type]
        verification=verification,
        manifest={},
        now=NOW,
    )
    with pytest.raises(backup.BackupVerificationError, match="collides with existing evidence"):
        backup._ensure_source_backup_receipt(
            ExistingReceiptDatabase(("wrong", *expected[1:])),  # type: ignore[arg-type]
            verification=verification,
            manifest={},
            now=NOW,
        )


def test_validated_config_rejects_path_parse_semantic_and_secret_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path / "inputs")
    missing = replace(inputs, config_path=tmp_path / "missing.toml")
    with pytest.raises(backup.BackupError, match="regular non-symlink"):
        backup._validated_config_bytes(missing)

    invalid_path = tmp_path / "invalid.toml"
    invalid_path.write_text("invalid = [", encoding="utf-8")
    invalid = replace(
        inputs,
        config_path=invalid_path,
        config_sha256=hashlib.sha256(invalid_path.read_bytes()).hexdigest(),
    )
    with pytest.raises(backup.BackupError, match="cannot be validated"):
        backup._validated_config_bytes(invalid)

    with monkeypatch.context() as context:
        context.setattr(backup, "load_settings", lambda _path: object())
        with pytest.raises(backup.BackupError, match="validated settings changed"):
            backup._validated_config_bytes(inputs)

    secret_path = tmp_path / "secret.toml"
    secret_path.write_text("# password must never be copied\n", encoding="utf-8")
    secret = replace(
        inputs,
        config_path=secret_path,
        config_sha256=hashlib.sha256(secret_path.read_bytes()).hexdigest(),
    )
    with pytest.raises(backup.BackupError, match="secret material"):
        backup._validated_config_bytes(secret)


def test_isolated_database_verification_rejects_missing_decision_schema_and_read_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class FakeDatabase:
        def __init__(self, *, integrity_error: bool = False) -> None:
            self.integrity_error = integrity_error
            self.closed = False

        def integrity_check(self) -> None:
            if self.integrity_error:
                raise backup.PersistenceError("integrity failure")

        def query_one(self, _query: str, _parameters: object) -> None:
            return None

        def close(self) -> None:
            self.closed = True

    class VerifiedAudit:
        def __init__(self, _database: object) -> None:
            pass

        def verify(self, **_expected: object) -> object:
            return type("AuditResult", (), {"count": 0, "head_hash": "0" * 64})()

    database = FakeDatabase()
    with monkeypatch.context() as context:
        context.setattr(backup.Database, "open_read_only", lambda *_args, **_kwargs: database)
        context.setattr(backup, "_database_schema_version", lambda _database: 6)
        context.setattr(backup, "_verify_migration_prefix", lambda *_args: None)
        context.setattr(backup, "AuditLedger", VerifiedAudit)
        with pytest.raises(backup.BackupVerificationError, match="read-only verification failed"):
            backup._verify_database(
                tmp_path / "database.sqlite3",
                expected_audit_count=0,
                expected_audit_head="0" * 64,
                expected_schema=6,
                required_decision_id="decision-required",
            )
    assert database.closed is True

    schema_database = FakeDatabase()
    with monkeypatch.context() as context:
        context.setattr(
            backup.Database,
            "open_read_only",
            lambda *_args, **_kwargs: schema_database,
        )
        context.setattr(backup, "_database_schema_version", lambda _database: 5)
        context.setattr(backup, "_verify_migration_prefix", lambda *_args: None)
        context.setattr(backup, "AuditLedger", VerifiedAudit)
        with pytest.raises(backup.BackupVerificationError, match="schema version mismatch"):
            backup._verify_database(
                tmp_path / "database.sqlite3",
                expected_audit_count=0,
                expected_audit_head="0" * 64,
                expected_schema=6,
            )

    failed_database = FakeDatabase(integrity_error=True)
    with monkeypatch.context() as context:
        context.setattr(
            backup.Database,
            "open_read_only",
            lambda *_args, **_kwargs: failed_database,
        )
        with pytest.raises(backup.BackupVerificationError, match="read-only verification failed"):
            backup._verify_database(
                tmp_path / "database.sqlite3",
                expected_audit_count=0,
                expected_audit_head="0" * 64,
                expected_schema=6,
            )
    assert failed_database.closed is True


def test_low_level_file_evidence_wraps_stat_read_copy_and_commit_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.bin"
    with pytest.raises(backup.BackupVerificationError, match="not a regular file"):
        backup._sha256_file(missing)
    with pytest.raises(backup.BackupVerificationError, match="cannot stat backup member"):
        backup._stable_file_identity(missing)
    with pytest.raises(backup.BackupVerificationError, match="identity is unavailable"):
        backup._private_regular_file_identity(missing, label="private member")

    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(backup.BackupVerificationError, match="not stable regular file"):
        backup._stable_file_identity(directory)

    source = tmp_path / "source.bin"
    source.write_bytes(b"source")
    with monkeypatch.context() as context:
        context.setattr(backup.shutil, "copyfile", lambda *_args: (_ for _ in ()).throw(OSError("copy")))
        with pytest.raises(backup.BackupError, match="cannot copy evidence"):
            backup._copy_fsynced(source, tmp_path / "copy.bin", label="evidence")

    destination = tmp_path / "destination.bin"
    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    with monkeypatch.context() as context:
        context.setattr(backup.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("replace")))
        with pytest.raises(backup.BackupError, match="cannot commit evidence private copy"):
            backup._copy_verified_member(
                source,
                destination,
                expected_sha256=expected,
                label="evidence",
            )

    content_destination = tmp_path / "content.json"
    with monkeypatch.context() as context:
        context.setattr(backup.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("replace")))
        with pytest.raises(backup.BackupError, match="cannot commit identity private content"):
            backup._ensure_fsynced_content(
                content_destination,
                b"content",
                label="identity",
            )


def test_private_v3_staging_requires_exact_receipt_digest_and_schema(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "staging"
    bundle.mkdir()
    manifest_path = bundle / "manifest.json"
    manifest_path.write_text('{"schema_version":2}', encoding="utf-8")
    observed = hashlib.sha256(manifest_path.read_bytes()).hexdigest()

    with pytest.raises(backup.BackupVerificationError, match="manifest SHA-256 changed"):
        backup._verify_private_v3_staging(
            bundle,
            expected_manifest_sha256="0" * 64,
            expected_backup_id="backup-test",
        )
    with pytest.raises(backup.BackupVerificationError, match="not schema-v3"):
        backup._verify_private_v3_staging(
            bundle,
            expected_manifest_sha256=observed,
            expected_backup_id="backup-test",
        )

    manifest_path.write_text('{"schema_version":3}', encoding="utf-8")
    observed = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    sentinel = backup.BackupVerification(
        backup_id="backup-test",
        database_sha256="1" * 64,
        account_state_sha256="2" * 64,
        manifest_sha256=observed,
        audit_count=0,
        audit_head_hash="0" * 64,
        schema_version=3,
        operational_schema_version=6,
    )
    with monkeypatch.context() as context:
        context.setattr(backup, "_verify_v3_bundle", lambda *_args, **_kwargs: sentinel)
        assert (
            backup._verify_private_v3_staging(
                bundle,
                expected_manifest_sha256=observed,
                expected_backup_id="backup-test",
            )
            == sentinel
        )


def test_directory_publication_rejects_cross_parent_collision_and_replace_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_parent = tmp_path / "source-parent"
    destination_parent = tmp_path / "destination-parent"
    source_parent.mkdir()
    destination_parent.mkdir()
    source = source_parent / "staging"
    source.mkdir()
    with pytest.raises(backup.BackupError, match="same parent and volume"):
        backup._publish_directory(source, destination_parent / "published", platform_name="posix")

    sibling_source = tmp_path / "sibling-staging"
    sibling_source.mkdir()
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(backup.BackupError, match="source or destination identity"):
        backup._publish_directory(sibling_source, existing, platform_name="posix")

    replace_source = tmp_path / "replace-staging"
    replace_source.mkdir()
    with monkeypatch.context() as context:
        context.setattr(backup.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("replace")))
        with pytest.raises(backup.BackupError, match="atomic directory publish failed"):
            backup._publish_directory(
                replace_source,
                tmp_path / "replace-published",
                platform_name="posix",
            )

    identity_source = tmp_path / "identity-staging"
    identity_source.mkdir()
    with monkeypatch.context() as context:
        context.setattr(
            backup,
            "_assert_directory_object_identity",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(backup.BackupVerificationError("changed")),
        )
        with pytest.raises(backup.BackupError, match="not the verified staging object"):
            backup._publish_directory(
                identity_source,
                tmp_path / "identity-published",
                platform_name="posix",
            )
