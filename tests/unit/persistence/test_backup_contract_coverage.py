from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

import firmquant.persistence.backup as backup
from firmquant.application.production_identity import DeploymentIdentity, OperationalEvidenceIdentity
from firmquant.config import Mode
from firmquant.persistence.repositories import canonical_json
from tests.integration.test_backup_v3_restore import _v3_case

NOW = datetime(2026, 8, 25, 8, tzinfo=UTC)
ZERO = "0" * 64


def _write_canonical(path: Path, payload: dict[str, object]) -> None:
    path.write_text(canonical_json(payload), encoding="utf-8")


def _legacy_bundle(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    bundle = tmp_path / "legacy"
    bundle.mkdir()
    database = bundle / "firmquant.sqlite3"
    account = bundle / "account_state.json"
    database.write_bytes(b"database")
    account.write_bytes(b"account")
    manifest: dict[str, object] = {
        "schema_version": 1,
        "backup_id": "backup-legacy",
        "created_at": NOW.isoformat(),
        "database": {
            "filename": database.name,
            "sha256": hashlib.sha256(database.read_bytes()).hexdigest(),
        },
        "account_state": {
            "filename": account.name,
            "sha256": hashlib.sha256(account.read_bytes()).hexdigest(),
        },
        "operational_schema_version": 1,
        "audit": {"count": 2, "head_hash": "a" * 64},
    }
    (bundle / "manifest.json").write_text("{}", encoding="utf-8")
    return bundle, manifest


def test_legacy_bundle_accepts_exact_contract(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bundle, manifest = _legacy_bundle(tmp_path)
    monkeypatch.setattr(backup, "_verify_database", lambda *_args, **_kwargs: (1, 2, "a" * 64))
    result = backup._verify_legacy_bundle(bundle, manifest, manifest_sha256="b" * 64)
    assert result.backup_id == "backup-legacy"
    assert result.account_state_sha256 == hashlib.sha256(b"account").hexdigest()


@pytest.mark.parametrize(
    ("case", "message"),
    (
        ("account_fields", "account manifest fields"),
        ("account_filename", "account-state filename"),
        ("account_digest", "account-state SHA-256"),
        ("unexpected_member", "missing or unexpected members"),
        ("audit_fields", "audit manifest fields"),
        ("schema_type", "schema version must be integer"),
    ),
)
def test_legacy_bundle_rejects_tampered_optional_authority(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str, message: str
) -> None:
    bundle, manifest = _legacy_bundle(tmp_path)
    account = manifest["account_state"]
    audit = manifest["audit"]
    assert isinstance(account, dict) and isinstance(audit, dict)
    if case == "account_fields":
        account["extra"] = True
    elif case == "account_filename":
        account["filename"] = "renamed.json"
    elif case == "account_digest":
        account["sha256"] = ZERO
    elif case == "unexpected_member":
        (bundle / "extra").write_bytes(b"")
    elif case == "audit_fields":
        audit["extra"] = True
    elif case == "schema_type":
        manifest["operational_schema_version"] = True
    monkeypatch.setattr(backup, "_verify_database", lambda *_args, **_kwargs: (1, 2, "a" * 64))
    with pytest.raises(backup.BackupVerificationError, match=message):
        backup._verify_legacy_bundle(bundle, manifest, manifest_sha256="b" * 64)


def _complete_bundle(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    bundle = tmp_path / "complete"
    bundle.mkdir()
    deployment: dict[str, object] = {
        "decision_id": "decision-1",
        "account_sha256": hashlib.sha256(b"account").hexdigest(),
        "config_sha256": "",
        "calendar_sha256": "",
        "active_data_manifest_sha256": "",
        "strategy_data_manifest_sha256": "",
    }
    content = {
        "firmquant.sqlite3": b"database",
        "account_state.json": b"account",
        "production_config.toml": b"mode='PAPER'",
        "xtquant_safety_manifest.json": b"{}",
        "trading_calendar.json": b"{}",
        "active_data_source.json": b'{"source":"xtquant"}',
        "strategy_data_manifest.json": b'{"source":"xtquant"}',
    }
    for name, value in content.items():
        (bundle / name).write_bytes(value)
    deployment["config_sha256"] = hashlib.sha256(content["production_config.toml"]).hexdigest()
    deployment["calendar_sha256"] = hashlib.sha256(content["trading_calendar.json"]).hexdigest()
    deployment["active_data_manifest_sha256"] = hashlib.sha256(content["active_data_source.json"]).hexdigest()
    deployment["strategy_data_manifest_sha256"] = hashlib.sha256(
        content["strategy_data_manifest.json"]
    ).hexdigest()
    _write_canonical(bundle / "deployment_record.json", deployment)
    members = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in bundle.iterdir()}
    manifest: dict[str, object] = {
        "schema_version": 2,
        "backup_id": "backup-complete",
        "created_at": NOW.isoformat(),
        "members": members,
        "operational_schema_version": 6,
        "audit": {"count": 3, "head_hash": "c" * 64},
        "deployment": deployment,
    }
    (bundle / "manifest.json").write_text("{}", encoding="utf-8")
    return bundle, manifest


def _patch_complete_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backup, "load_settings", lambda _path: object())
    monkeypatch.setattr(backup, "load_trading_calendar_manifest", lambda _path: object())
    monkeypatch.setattr(backup.XtQuantSafetyManifest, "load", lambda _path: object())

    class Store:
        def hash_file(self, path: Path) -> str:
            return hashlib.sha256(path.read_bytes()).hexdigest()

    monkeypatch.setattr(backup, "UquantAccountStateStore", Store)
    monkeypatch.setattr(backup, "_verify_database", lambda *_args, **_kwargs: (6, 3, "c" * 64))


def _rewrite_deployment(bundle: Path, manifest: dict[str, object], **changes: object) -> None:
    deployment = manifest["deployment"]
    members = manifest["members"]
    assert isinstance(deployment, dict) and isinstance(members, dict)
    deployment.update(changes)
    path = bundle / "deployment_record.json"
    _write_canonical(path, deployment)
    members[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()


def test_complete_bundle_accepts_exact_read_only_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bundle, manifest = _complete_bundle(tmp_path)
    _patch_complete_dependencies(monkeypatch)
    result = backup._verify_complete_bundle(bundle, manifest, manifest_sha256="d" * 64)
    assert result.complete_bundle is True
    assert result.production_authority is False


@pytest.mark.parametrize(
    ("case", "message"),
    (
        ("unexpected_member", "missing or unexpected members"),
        ("digest_type", "member digest is invalid"),
        ("digest_mismatch", "member SHA-256 mismatch"),
        ("active_empty", "active data identity is empty"),
        ("strategy_empty", "strategy data identity is empty"),
        ("deployment_changed", "deployment identity changed"),
        ("account_mismatch", "account identity is inconsistent"),
        ("config_mismatch", "config identity is inconsistent"),
        ("calendar_mismatch", "calendar identity is inconsistent"),
        ("active_mismatch", "active-data identity is inconsistent"),
        ("strategy_mismatch", "strategy-data identity is inconsistent"),
        ("schema_type", "schema version must be integer"),
    ),
)
def test_complete_bundle_rejects_identity_or_member_tampering(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str, message: str
) -> None:
    bundle, manifest = _complete_bundle(tmp_path)
    members = manifest["members"]
    assert isinstance(members, dict)
    if case == "unexpected_member":
        (bundle / "extra").write_bytes(b"")
    elif case == "digest_type":
        members["firmquant.sqlite3"] = 1
    elif case == "digest_mismatch":
        members["firmquant.sqlite3"] = ZERO
    elif case == "active_empty":
        (bundle / "active_data_source.json").write_text("{}", encoding="utf-8")
        members["active_data_source.json"] = hashlib.sha256(b"{}").hexdigest()
        _rewrite_deployment(bundle, manifest, active_data_manifest_sha256=members["active_data_source.json"])
    elif case == "strategy_empty":
        (bundle / "strategy_data_manifest.json").write_text("{}", encoding="utf-8")
        members["strategy_data_manifest.json"] = hashlib.sha256(b"{}").hexdigest()
        _rewrite_deployment(
            bundle, manifest, strategy_data_manifest_sha256=members["strategy_data_manifest.json"]
        )
    elif case == "deployment_changed":
        deployment = manifest["deployment"]
        assert isinstance(deployment, dict)
        deployment["decision_id"] = "changed"
    elif case == "account_mismatch":
        _rewrite_deployment(bundle, manifest, account_sha256=ZERO)
    elif case == "config_mismatch":
        _rewrite_deployment(bundle, manifest, config_sha256=ZERO)
    elif case == "calendar_mismatch":
        _rewrite_deployment(bundle, manifest, calendar_sha256=ZERO)
    elif case == "active_mismatch":
        _rewrite_deployment(bundle, manifest, active_data_manifest_sha256=ZERO)
    elif case == "strategy_mismatch":
        _rewrite_deployment(bundle, manifest, strategy_data_manifest_sha256=ZERO)
    elif case == "schema_type":
        manifest["operational_schema_version"] = True
    _patch_complete_dependencies(monkeypatch)
    with pytest.raises(backup.BackupVerificationError, match=message):
        backup._verify_complete_bundle(bundle, manifest, manifest_sha256="d" * 64)


def _source_v3_bundle(tmp_path: Path) -> Path:
    database, account, inputs, root = _v3_case(tmp_path)
    try:
        return backup.backup_state(
            database,
            root,
            account_state_path=account,
            complete_inputs=inputs,
            created_at=NOW,
        ).bundle_path
    finally:
        database.close()


def _rewrite_v3_member(bundle: Path, manifest: dict[str, object], name: str, content: bytes) -> None:
    (bundle / name).write_bytes(content)
    members = manifest["members"]
    assert isinstance(members, dict)
    members[name] = hashlib.sha256(content).hexdigest()


@pytest.mark.parametrize(
    ("case", "message"),
    (
        ("fields", "manifest fields"),
        ("reason", "backup reason"),
        ("member_set", "member set"),
        ("unexpected_member", "unexpected members"),
        ("member_digest", "member SHA-256"),
        ("identity_kinds", "identity kinds"),
        ("account_state", "AccountState"),
        ("account_identity", "account state identity"),
        ("account_epoch", "account authority epoch"),
        ("mode_epoch", "mode epoch"),
        ("snapshot_id", "snapshot id"),
        ("snapshot_digest", "snapshot digest"),
        ("watermark", "broker watermark"),
        ("session", "strategy session"),
        ("decision", "decision identity"),
        ("config", "validated configuration evidence"),
        ("account_epoch_json", "account authority epoch is not canonical"),
        ("mode_epoch_json", "mode epoch is not canonical"),
        ("audit", "audit identity"),
        ("operational_schema", "current operational schema"),
    ),
)
def test_schema_v3_bundle_rejects_tampered_bound_identity(tmp_path: Path, case: str, message: str) -> None:
    bundle = _source_v3_bundle(tmp_path)
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert isinstance(manifest, dict)
    if case == "fields":
        manifest["extra"] = True
    elif case == "reason":
        manifest["reason"] = "UNKNOWN"
    elif case == "member_set":
        members = manifest["members"]
        assert isinstance(members, dict)
        members.pop("mode_epoch.json")
    elif case == "unexpected_member":
        (bundle / "unexpected").write_bytes(b"")
    elif case == "member_digest":
        (bundle / "mode_epoch.json").write_bytes(b"changed")
    elif case == "identity_kinds":
        evidence_bytes = (bundle / "operational_evidence_identity.json").read_bytes()
        _rewrite_v3_member(bundle, manifest, "deployment_identity.json", evidence_bytes)
        manifest["deployment_identity_sha256"] = hashlib.sha256(evidence_bytes).hexdigest()
    elif case == "account_state":
        _rewrite_v3_member(bundle, manifest, "account_state.json", b"{}")
        manifest["account_state_sha256"] = hashlib.sha256(b"{}").hexdigest()
    elif case == "account_identity":
        manifest["account_state_sha256"] = ZERO
    elif case == "account_epoch":
        manifest["account_authority_epoch"] = 999
    elif case == "mode_epoch":
        manifest["mode_epoch"] = 999
    elif case == "snapshot_id":
        manifest["broker_snapshot_id"] = "changed"
    elif case == "snapshot_digest":
        manifest["broker_snapshot_sha256"] = ZERO
    elif case == "watermark":
        manifest["broker_event_watermark"] = 999
    elif case == "session":
        manifest["strategy_session"] = "2026-08-23"
    elif case == "decision":
        manifest["decision_id"] = "changed"
    elif case == "config":
        _rewrite_v3_member(bundle, manifest, "production_config.toml", b"invalid = [")
    elif case in {"account_epoch_json", "mode_epoch_json"}:
        name = "account_authority_epoch.json" if case.startswith("account") else "mode_epoch.json"
        payload = json.loads((bundle / name).read_text(encoding="utf-8"))
        rendered = json.dumps(payload, indent=2).encode("utf-8")
        _rewrite_v3_member(bundle, manifest, name, rendered)
    elif case == "audit":
        audit = manifest["audit"]
        assert isinstance(audit, dict)
        audit["extra"] = True
    elif case == "operational_schema":
        manifest["operational_schema_version"] = 1
    _write_canonical(manifest_path, manifest)
    manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()

    with pytest.raises(backup.BackupVerificationError, match=message):
        backup._verify_v3_bundle(
            bundle,
            manifest,
            manifest_sha256=manifest_sha256,
            enforce_directory_name=False,
        )


@pytest.mark.parametrize(
    ("case", "message"),
    (
        ("raw_config", "raw config identity"),
        ("semantic_config", "semantic config identity"),
        ("caps", "deployment caps identity"),
        ("policy", "production policy identity"),
        ("mode", "deployment mode"),
        ("reason_facts", "reason-specific operational facts"),
        ("safety", "XtQuant safety identity"),
        ("calendar", "calendar identity"),
        ("active_data", "active data identity"),
        ("strategy_data", "strategy data identity"),
    ),
)
def test_schema_v3_bundle_rejects_coherently_rehashed_identity_drift(
    tmp_path: Path,
    case: str,
    message: str,
) -> None:
    bundle = _source_v3_bundle(tmp_path)
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert isinstance(manifest, dict)
    deployment = backup.parse_identity((bundle / "deployment_identity.json").read_bytes())
    evidence = backup.parse_identity((bundle / "operational_evidence_identity.json").read_bytes())
    assert isinstance(deployment, DeploymentIdentity)
    assert isinstance(evidence, OperationalEvidenceIdentity)

    if case == "raw_config":
        deployment = replace(deployment, raw_config_sha256=ZERO)
    elif case == "semantic_config":
        deployment = replace(deployment, semantic_config_sha256=ZERO)
    elif case == "caps":
        deployment = replace(deployment, caps_sha256=ZERO)
    elif case == "policy":
        deployment = replace(deployment, production_policy_sha256=ZERO)
    elif case == "mode":
        deployment = replace(deployment, mode=Mode.SHADOW)
    elif case == "reason_facts":
        evidence = replace(evidence, phase="READINESS")
    elif case == "safety":
        deployment = replace(deployment, xtquant_safety_manifest_sha256=ZERO)
    elif case == "calendar":
        evidence = replace(evidence, calendar_sha256=ZERO)
    elif case == "active_data":
        evidence = replace(evidence, active_data_generation_sha256=ZERO)
    elif case == "strategy_data":
        evidence = replace(evidence, strategy_data_manifest_sha256=ZERO)
    else:
        raise AssertionError(case)

    if evidence.deployment_identity != deployment:
        evidence = replace(evidence, deployment_identity=deployment)
    _rewrite_v3_member(
        bundle,
        manifest,
        "deployment_identity.json",
        deployment.canonical_json.encode("utf-8"),
    )
    _rewrite_v3_member(
        bundle,
        manifest,
        "operational_evidence_identity.json",
        evidence.canonical_json.encode("utf-8"),
    )
    manifest["deployment_identity_sha256"] = deployment.sha256
    manifest["operational_evidence_identity_sha256"] = evidence.sha256
    manifest["backup_id"] = backup._v3_backup_id_from_identities(
        backup.BackupReason(str(manifest["reason"])),
        deployment,
        evidence,
    )
    _write_canonical(manifest_path, manifest)
    manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()

    with pytest.raises(backup.BackupVerificationError, match=message):
        backup._verify_v3_bundle(
            bundle,
            manifest,
            manifest_sha256=manifest_sha256,
            enforce_directory_name=False,
        )


def test_logical_state_hash_is_order_stable_and_rejects_unsafe_values() -> None:
    class LogicalDatabase:
        def query_all(self, query: str) -> list[dict[str, object]]:
            if "sqlite_master" in query:
                return [{"name": "safe_table"}]
            if query.startswith("PRAGMA"):
                return [{"name": "value"}]
            return [{"value": 2}, {"value": b"blob"}, {"value": None}]

    observed = backup._logical_state_sha256(LogicalDatabase())  # type: ignore[arg-type]
    assert len(observed) == 64
    assert backup._logical_value(1.5) == 1.5

    class UnsafeTable(LogicalDatabase):
        def query_all(self, query: str) -> list[dict[str, object]]:
            if "sqlite_master" in query:
                return [{"name": 'unsafe"table'}]
            return []

    with pytest.raises(backup.BackupVerificationError, match="unsafe table name"):
        backup._logical_state_sha256(UnsafeTable())  # type: ignore[arg-type]
    with pytest.raises(backup.BackupVerificationError, match="unsupported SQLite value"):
        backup._logical_value(object())
