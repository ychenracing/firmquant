from __future__ import annotations

import json
from pathlib import Path

from firmquant.security.scanning import scan_paths


def test_secret_scan_accepts_placeholders_and_rejects_high_confidence_credentials(
    tmp_path: Path,
) -> None:
    safe = tmp_path / "firmquant.example.toml"
    safe.write_text('account_alias = "<SET_LOCALLY>"\n', encoding="utf-8")
    leaked = tmp_path / "leaked.txt"
    leaked.write_text("ghp_" + "A" * 36, encoding="utf-8")

    violations = scan_paths(tmp_path, (safe, leaked))

    assert [(item.path.name, item.code) for item in violations] == [
        ("leaked.txt", "GITHUB_TOKEN"),
    ]


def test_secret_scan_rejects_sensitive_files_and_real_account_snapshots(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("FIRMQUANT_SECRET_ARM_MAC_KEY=value\n", encoding="utf-8")
    snapshot = tmp_path / "account-snapshot.json"
    account_number = "".join(("1234", "5678", "9012"))
    snapshot.write_text(
        json.dumps({"account_id": account_number, "cash": "1000"}),
        encoding="utf-8",
    )

    violations = scan_paths(tmp_path, (env_file, snapshot))

    assert {(item.path.name, item.code) for item in violations} == {
        (".env", "SENSITIVE_FILENAME"),
        ("account-snapshot.json", "REAL_ACCOUNT_SNAPSHOT"),
    }


def test_secret_scan_rejects_miniqmt_userdata_files(tmp_path: Path) -> None:
    userdata = tmp_path / "userdata_mini"
    userdata.mkdir()
    broker_state = userdata / "xt_trader.cfg"
    broker_state.write_text("locally-managed broker client state", encoding="utf-8")

    violations = scan_paths(tmp_path, (broker_state,))

    assert [(item.path.as_posix(), item.code) for item in violations] == [
        ("userdata_mini/xt_trader.cfg", "MINIQMT_USERDATA_FILE"),
    ]


def test_secret_scan_rejects_numeric_account_identifier_in_config(tmp_path: Path) -> None:
    config = tmp_path / "firmquant.local.toml"
    account_number = "".join(("9988", "7766", "5544"))
    config.write_text(f'account_number = "{account_number}"\n', encoding="utf-8")

    violations = scan_paths(tmp_path, (config,))

    assert [(item.path.name, item.code) for item in violations] == [
        ("firmquant.local.toml", "ACCOUNT_IDENTIFIER"),
    ]
