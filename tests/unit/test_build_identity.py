from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from firmquant.build_identity import (
    SourceIdentityError,
    installed_uquant_identity,
    load_locked_source_identity,
    verify_uquant_identity,
)

EXPECTED_UQUANT_COMMIT = "a17322f6330953a27c77f70d463a713c9a48ebc9"
EXPECTED_UQUANT_TREE = "846566bb6317ddbdcff729aa9fff7950fa5baa58"
EXPECTED_WHEEL_SHA256 = "13ef26d5d34a86d8ee45641ef63bb1c8a01d381156cff323fcdc582b599189d8"
EXPECTED_CODE_FINGERPRINT = "d1ef7977ae482e46a920381e6af58791199ec8e1a02586dbe8df451e7d4696c9"
EXPECTED_PUBLIC_API_CONTRACT_SHA256 = "b485932a5eb10b0528c2d01008c6495f8f2e1e74ead04c737cafd9c665efa6b5"


def test_locked_source_identity_is_exact_and_immutable() -> None:
    identity = load_locked_source_identity()

    assert identity.uquant_commit == EXPECTED_UQUANT_COMMIT
    assert identity.uquant_tree == EXPECTED_UQUANT_TREE
    assert identity.wheel_sha256 == EXPECTED_WHEEL_SHA256
    assert identity.economic_code_fingerprint == EXPECTED_CODE_FINGERPRINT
    assert identity.public_api_contract_sha256 == EXPECTED_PUBLIC_API_CONTRACT_SHA256
    assert identity.relation_to_known_baseline == "descendant"
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


def test_source_baseline_cli_reports_public_contract_identity() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [sys.executable, "scripts/verify_source_baseline.py"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    )

    payload = json.loads(completed.stdout)
    assert payload["public_api_contract_sha256"] == EXPECTED_PUBLIC_API_CONTRACT_SHA256


def test_source_baseline_cli_accepts_source_root_option() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [sys.executable, "scripts/verify_source_baseline.py", "--help"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "--source-root" in completed.stdout
