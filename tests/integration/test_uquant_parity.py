from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

from tests.fixtures.uquant_parity import write_synthetic_market_data

EXPECTED_DECISION_DIGEST = "d4fc41aee48209f2d14677500986e7da9347b4b3baabca1d2629c5ae0041d04c"
EXPECTED_CODE_FINGERPRINT = "d1ef7977ae482e46a920381e6af58791199ec8e1a02586dbe8df451e7d4696c9"
EXPECTED_PUBLIC_CONTRACT_SHA256 = "b485932a5eb10b0528c2d01008c6495f8f2e1e74ead04c737cafd9c665efa6b5"
EXPECTED_TRACE_ACCOUNT_SHA256 = "42477aa44a51be02193dd14a12a9d1628c612d02357fbe7d9f5027279d73487d"
EXPECTED_TRACE_ECONOMIC_SHA256 = "85e3b3472d409499519bed40d869c554886d77ddc384f9e93cfac8cb4be91eed"


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


def _assert_complete_contract_trace(result: dict[str, object], source_checkout: Path) -> None:
    contract = json.loads(
        (source_checkout / "benchmarks/public_api_contract.json").read_text(encoding="utf-8")
    )
    assert result["contract"] == {
        "contract_id": "uquant-public-api-v1",
        "contract_sha256": EXPECTED_PUBLIC_CONTRACT_SHA256,
        "schema_version": 1,
    }
    assert result["public_surface"] == {
        "uquant.account": ["economic_state_sha256", "load_account", "save_account"],
        "uquant.config": ["DEFAULT_CONFIG", "config_fingerprint"],
        "uquant.data": ["DataStore", "DataStore.load"],
        "uquant.engine": ["ProductionEngine", "ProductionEngine.decide", "code_fingerprint"],
        "uquant.execution": ["ExecutionPlanner", "ExecutionPlanner.execute_open"],
        "uquant.types": [
            "AccountState",
            "AccountState.empty",
            "AccountState.pending_orders",
            "AccountState.to_dict",
            "Decision.canonical_payload",
            "Decision.pending_orders",
            "Fill",
        ],
    }
    assert result["trace"] == contract["contract"]["decision_fill_account_trace"]
    trace = cast(dict[str, object], result["trace"])
    account_after = cast(dict[str, object], trace["account_after"])
    fills = cast(list[dict[str, object]], trace["fills"])
    positions = cast(dict[str, dict[str, object]], account_after["positions"])
    assert trace["inputs"] == {
        "fill_date": "2023-01-05",
        "initial_cash": 2_000_000.0,
        "signal_date": "2023-01-04",
        "symbols": ["sz300308", "sz300502", "sz300394"],
        "warmup_decision_date": "2023-01-03",
    }
    assert trace["initial_account_sha256"] == (
        "94582775e008dc4c57a962c3573f7446b1c74d95e4dca075a2655cbcddbfe540"
    )
    assert trace["account_after_sha256"] == EXPECTED_TRACE_ACCOUNT_SHA256
    assert len(fills) == 1
    assert {
        key: fills[0][key]
        for key in (
            "fill_date",
            "gross_value",
            "order_id",
            "price",
            "shares",
            "side",
            "symbol",
        )
    } == {
        "fill_date": "2023-01-05",
        "gross_value": 610_786.176,
        "order_id": "O000000001",
        "price": 18.17816,
        "shares": 33_600,
        "side": "BUY",
        "symbol": "sz300308",
    }
    assert account_after["cash"] == 1_389_055.01959424
    assert account_after["schema_version"] == 8
    assert positions["sz300308"]["shares"] == 33_600
    assert result["persistence"] == {
        "account_payload_equal": True,
        "economic_sha256": EXPECTED_TRACE_ECONOMIC_SHA256,
        "reloaded_economic_sha256": EXPECTED_TRACE_ECONOMIC_SHA256,
        "reloaded_schema_version": 8,
    }


def test_source_public_contract_fill_and_account_trace_is_canonical(tmp_path: Path) -> None:
    configured_source = os.environ.get("FIRMQUANT_UQUANT_SOURCE_CHECKOUT")
    if configured_source is None:
        pytest.skip("set FIRMQUANT_UQUANT_SOURCE_CHECKOUT to run source contract trace")
    source_checkout = Path(configured_source).resolve()
    repository_root = Path(__file__).resolve().parents[2]
    result = _public_trace(
        repository_root=repository_root,
        source_checkout=source_checkout,
        data_directory=source_checkout / "data/frozen",
        pythonpath=os.pathsep.join((str(source_checkout), str(repository_root / "src"))),
        database=tmp_path / "source-account.json",
    )

    _assert_complete_contract_trace(result, source_checkout)


def test_source_and_installed_wheel_public_traces_are_exactly_equal(tmp_path: Path) -> None:
    configured_source = os.environ.get("FIRMQUANT_UQUANT_SOURCE_CHECKOUT")
    if configured_source is None:
        pytest.skip("set FIRMQUANT_UQUANT_SOURCE_CHECKOUT to run source/wheel parity")
    source_checkout = Path(configured_source).resolve()
    repository_root = Path(__file__).resolve().parents[2]
    data_directory = source_checkout / "data/frozen"

    source = _public_trace(
        repository_root=repository_root,
        source_checkout=source_checkout,
        data_directory=data_directory,
        pythonpath=os.pathsep.join((str(source_checkout), str(repository_root / "src"))),
        database=tmp_path / "source-account.json",
    )
    _assert_complete_contract_trace(source, source_checkout)

    try:
        installed = _public_trace(
            repository_root=repository_root,
            source_checkout=source_checkout,
            data_directory=data_directory,
            pythonpath=str(repository_root / "src"),
            database=tmp_path / "wheel-account.json",
        )
    except PublicTraceError as exc:
        expected = (
            r"source surface registry is missing or unsafe: "
            r"benchmarks/source_surface_registry\.json"
        )
        if re.search(expected, str(exc)) is None:
            raise
        pytest.xfail(
            "target uquant wheel omits benchmarks/source_surface_registry.json required by "
            "public code_fingerprint()"
        )

    assert source == installed
    trace = cast(dict[str, object], source["trace"])
    account_after = cast(dict[str, object], trace["account_after"])
    assert account_after["code_hash"] == EXPECTED_CODE_FINGERPRINT


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
