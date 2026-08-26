from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.fixtures.uquant_parity import write_synthetic_market_data

EXPECTED_DECISION_DIGEST = "d4fc41aee48209f2d14677500986e7da9347b4b3baabca1d2629c5ae0041d04c"
EXPECTED_CODE_FINGERPRINT = "2209a539bacbc01d90b29b9f0bb78ace4991016bee0d41f9e86f38ccf5af545e"


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
        "conflict_recorded": True,
        "decision_digest": EXPECTED_DECISION_DIGEST,
        "opportunity": "CHOPPY",
        "orders": 0,
        "repeated_account_unchanged": True,
        "repeated_decision_id_equal": True,
        "recovery_required_for_unapplied_account": True,
        "risk": "NORMAL",
        "stored_payload_equal": True,
        "targets": 0,
        "uquant_payload_equal": True,
    }
