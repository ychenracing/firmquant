from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

from tests.fixtures.uquant_parity import write_synthetic_market_data

EXPECTED_DECISION_DIGEST = "d4fc41aee48209f2d14677500986e7da9347b4b3baabca1d2629c5ae0041d04c"
EXPECTED_CODE_FINGERPRINT = "d1ef7977ae482e46a920381e6af58791199ec8e1a02586dbe8df451e7d4696c9"


class PublicTraceError(RuntimeError):
    pass


def _public_trace(
    *,
    repository_root: Path,
    source_checkout: Path,
    data_directory: Path,
    pythonpath: str,
    database: Path,
) -> dict[str, object]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = pythonpath
    completed = subprocess.run(
        [
            sys.executable,
            str(repository_root / "tests/fixtures/uquant_parity.py"),
            "--source-checkout",
            str(source_checkout),
            "--data-directory",
            str(data_directory),
            "--database",
            str(database),
            "--firmquant-commit",
            "1" * 40,
            "--public-trace-only",
        ],
        cwd=data_directory.parent,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if completed.returncode != 0:
        raise PublicTraceError(completed.stderr)
    return cast(dict[str, object], json.loads(completed.stdout))


@pytest.mark.xfail(
    strict=True,
    reason=(
        "target uquant wheel omits benchmarks/source_surface_registry.json required by "
        "public code_fingerprint()"
    ),
)
def test_source_and_installed_wheel_public_traces_are_exactly_equal(tmp_path: Path) -> None:
    configured_source = os.environ.get("FIRMQUANT_UQUANT_SOURCE_CHECKOUT")
    if configured_source is None:
        pytest.skip("set FIRMQUANT_UQUANT_SOURCE_CHECKOUT to run source/wheel parity")
    source_checkout = Path(configured_source).resolve()
    repository_root = Path(__file__).resolve().parents[2]
    data_directory = tmp_path / "market-data"
    write_synthetic_market_data(source_checkout, data_directory)

    source = _public_trace(
        repository_root=repository_root,
        source_checkout=source_checkout,
        data_directory=data_directory,
        pythonpath=os.pathsep.join((str(source_checkout), str(repository_root / "src"))),
        database=tmp_path / "source-unused.sqlite3",
    )
    installed = _public_trace(
        repository_root=repository_root,
        source_checkout=source_checkout,
        data_directory=data_directory,
        pythonpath=str(repository_root / "src"),
        database=tmp_path / "wheel-unused.sqlite3",
    )

    assert source == installed
    assert source["code_fingerprint"] == EXPECTED_CODE_FINGERPRINT


def test_installed_wheel_public_trace_fails_closed_without_source_registry(tmp_path: Path) -> None:
    configured_source = os.environ.get("FIRMQUANT_UQUANT_SOURCE_CHECKOUT")
    if configured_source is None:
        pytest.skip("set FIRMQUANT_UQUANT_SOURCE_CHECKOUT to run installed-wheel contract test")
    source_checkout = Path(configured_source).resolve()
    repository_root = Path(__file__).resolve().parents[2]
    data_directory = tmp_path / "market-data"
    write_synthetic_market_data(source_checkout, data_directory)

    with pytest.raises(
        PublicTraceError,
        match=r"source surface registry is missing or unsafe: benchmarks/source_surface_registry\.json",
    ):
        _public_trace(
            repository_root=repository_root,
            source_checkout=source_checkout,
            data_directory=data_directory,
            pythonpath=str(repository_root / "src"),
            database=tmp_path / "wheel-unused.sqlite3",
        )


def test_adapter_matches_direct_engine_exactly_in_verified_source_checkout(
    tmp_path: Path,
) -> None:
    configured_source = os.environ.get("FIRMQUANT_UQUANT_SOURCE_CHECKOUT")
    if configured_source is None:
        pytest.skip("set FIRMQUANT_UQUANT_SOURCE_CHECKOUT to run exact source parity")
    source_checkout = Path(configured_source).resolve()
    repository_root = Path(__file__).resolve().parents[2]
    data_directory = tmp_path / "market-data"
    write_synthetic_market_data(source_checkout, data_directory)
    firmquant_commit = subprocess.run(
        ["git", "rev-parse", "HEAD^{commit}"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join((str(source_checkout), str(repository_root / "src")))
    completed = subprocess.run(
        [
            sys.executable,
            str(repository_root / "tests/fixtures/uquant_parity.py"),
            "--source-checkout",
            str(source_checkout),
            "--data-directory",
            str(data_directory),
            "--database",
            str(tmp_path / "parity.sqlite3"),
            "--firmquant-commit",
            firmquant_commit,
        ],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)

    assert result == {
        "account_code_hash": EXPECTED_CODE_FINGERPRINT,
        "account_equal": True,
        "adapter_public_engine_surface": True,
        "conflict_recorded": True,
        "decision_digest": EXPECTED_DECISION_DIGEST,
        "opportunity": "CHOPPY",
        "orders": 0,
        "recovered_unapplied_account": True,
        "repeated_account_unchanged": True,
        "repeated_decision_id_equal": True,
        "recovery_required_for_unapplied_account": True,
        "risk": "NORMAL",
        "stored_payload_equal": True,
        "targets": 0,
        "uquant_payload_equal": True,
    }
