from __future__ import annotations

import dataclasses
from dataclasses import replace
from pathlib import Path

import pytest

from firmquant.build_identity import (
    SourceIdentityError,
    installed_uquant_identity,
    load_locked_source_identity,
    verify_uquant_identity,
)

EXPECTED_UQUANT_COMMIT = "105695aacd3d1c7e62705f64188da88d202db4cd"
EXPECTED_UQUANT_TREE = "e3e2832eb1321e6d45f103cab538aeb9c95852d3"
EXPECTED_WHEEL_SHA256 = "a5df13991b6696f22e8a1633b0dfb717d1a3647448462141318702844653137c"
EXPECTED_CODE_FINGERPRINT = "2209a539bacbc01d90b29b9f0bb78ace4991016bee0d41f9e86f38ccf5af545e"


def test_locked_source_identity_is_exact_and_immutable() -> None:
    identity = load_locked_source_identity()

    assert identity.uquant_commit == EXPECTED_UQUANT_COMMIT
    assert identity.uquant_tree == EXPECTED_UQUANT_TREE
    assert identity.wheel_sha256 == EXPECTED_WHEEL_SHA256
    assert identity.economic_code_fingerprint == EXPECTED_CODE_FINGERPRINT
    assert identity.relation_to_known_baseline == "identical"
    with pytest.raises(dataclasses.FrozenInstanceError):
        identity.uquant_commit = "0" * 40  # type: ignore[misc]


def test_installed_uquant_matches_locked_source_identity() -> None:
    assert installed_uquant_identity() == load_locked_source_identity()


def test_source_identity_rejects_wrong_commit() -> None:
    locked = load_locked_source_identity()
    bad = replace(locked, uquant_commit="0" * 40)

    with pytest.raises(SourceIdentityError, match="uquant commit"):
        verify_uquant_identity(bad)


def test_source_identity_rejects_wrong_wheel_bytes(tmp_path: Path) -> None:
    impostor = tmp_path / "uquant-1.1.0-py3-none-any.whl"
    impostor.write_bytes(b"not the reviewed wheel")

    with pytest.raises(SourceIdentityError, match="wheel SHA-256"):
        verify_uquant_identity(load_locked_source_identity(), wheel_path=impostor)


def test_source_identity_records_current_firmquant_lock() -> None:
    identity = load_locked_source_identity()
    repository_root = Path(__file__).resolve().parents[2]

    identity.verify_firmquant_files(repository_root)


def test_source_identity_rejects_unexpected_json_fields(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[2] / "src/firmquant/resources/source_identity.json"
    malformed = tmp_path / "source_identity.json"
    malformed.write_text(source.read_text(encoding="utf-8").replace("{", '{"extra":true,', 1))

    with pytest.raises(SourceIdentityError, match="schema fields"):
        load_locked_source_identity(malformed)
