from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

import pytest

from firmquant.cli import main

EXPECTED_COMMANDS = {
    "arm-live",
    "backup",
    "cancel-system-orders",
    "decisions",
    "disarm",
    "doctor",
    "fills",
    "halt",
    "init",
    "orders",
    "reconcile",
    "replay",
    "report",
    "resume",
    "run",
    "status",
    "verify-backup",
}


def test_help_registers_complete_operational_command_surface(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])

    assert exit_info.value.code == 0
    output = capsys.readouterr().out
    assert set(output.replace("{", "").replace("}", "").replace(",", " ").split()) >= EXPECTED_COMMANDS


def test_module_help_is_import_safe_and_successful() -> None:
    repository_root = Path(__file__).resolve().parents[2]

    completed = subprocess.run(
        [sys.executable, "-m", "firmquant", "--help"],
        cwd=repository_root,
        check=False,
        capture_output=True,
        encoding="utf-8",
        text=True,
    )

    assert completed.returncode == 0
    assert "实盘默认关闭" in completed.stdout
    assert "Traceback" not in completed.stderr


def test_help_reconfigures_redirected_legacy_stream_to_utf8(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    buffer = io.BytesIO()
    legacy_stdout = io.TextIOWrapper(buffer, encoding="cp1252", write_through=True)
    monkeypatch.setattr(sys, "stdout", legacy_stdout)

    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])

    assert exit_info.value.code == 0
    assert "实盘默认关闭" in buffer.getvalue().decode("utf-8")


@pytest.mark.parametrize("mode", ["paper", "shadow", "canary", "live"])
def test_unimplemented_run_modes_fail_closed(mode: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run", "--mode", mode]) == 2

    assert "尚未启用" in capsys.readouterr().err
