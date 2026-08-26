from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from firmquant.application.operations import (
    OperatorCommand,
    OperatorInteraction,
    OperatorRequest,
    OperatorResult,
    _clean_git_commit,
)
from firmquant.cli import main

REQUIRED_STATUS_FIELDS = {
    "mode",
    "runtime_state",
    "armed",
    "arm_expires_at",
    "firmquant_commit",
    "uquant_commit",
    "strategy_session",
    "broker_connection",
    "last_quote",
    "last_reconciliation",
    "unresolved_orders",
    "current_cash",
    "actual_gross",
    "target_gross",
    "kill_switch",
    "blockers",
}


@dataclass
class RecordingOperator:
    payload: Mapping[str, object] = field(default_factory=dict)
    calls: list[tuple[OperatorRequest, OperatorInteraction]] = field(default_factory=list)

    def execute(
        self,
        request: OperatorRequest,
        interaction: OperatorInteraction,
    ) -> OperatorResult:
        self.calls.append((request, interaction))
        return OperatorResult(message="完成", payload=self.payload)


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (["init"], OperatorCommand.INIT),
        (["doctor"], OperatorCommand.DOCTOR),
        (["run", "--mode", "paper"], OperatorCommand.RUN),
        (["status"], OperatorCommand.STATUS),
        (["arm-live"], OperatorCommand.ARM_LIVE),
        (["disarm"], OperatorCommand.DISARM),
        (["halt"], OperatorCommand.HALT),
        (["resume"], OperatorCommand.RESUME),
        (["reconcile"], OperatorCommand.RECONCILE),
        (["decisions"], OperatorCommand.DECISIONS),
        (["orders"], OperatorCommand.ORDERS),
        (["fills"], OperatorCommand.FILLS),
        (["report"], OperatorCommand.REPORT),
        (["replay", "--events", "recording.jsonl"], OperatorCommand.REPLAY),
        (["backup"], OperatorCommand.BACKUP),
        (["verify-backup", "--bundle", "backup-1"], OperatorCommand.VERIFY_BACKUP),
        (["cancel-system-orders"], OperatorCommand.CANCEL_SYSTEM_ORDERS),
    ],
)
def test_every_command_delegates_to_the_application_service(
    arguments: list[str],
    expected: OperatorCommand,
    tmp_path: Path,
) -> None:
    operator = RecordingOperator()
    config = tmp_path / "firmquant.toml"

    exit_code = main(
        ["--config", str(config), *arguments],
        service_factory=lambda observed: operator,
        interactive_terminal=False,
        confirmation_reader=lambda _prompt: "unused",
        environment={},
    )

    assert exit_code == 0
    assert len(operator.calls) == 1
    request, interaction = operator.calls[0]
    assert request.command is expected
    assert interaction.interactive_terminal is False
    assert interaction.environment == {}


def test_status_json_contains_the_complete_stable_contract(
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = {name: None for name in REQUIRED_STATUS_FIELDS}
    payload.update(mode="PAPER", runtime_state="DISARMED", armed=False, blockers=[])
    operator = RecordingOperator(payload=payload)

    assert main(["status", "--json"], service_factory=lambda _path: operator) == 0

    observed = json.loads(capsys.readouterr().out)
    assert observed.keys() >= REQUIRED_STATUS_FIELDS
    assert observed["armed"] is False


def test_cli_parses_typed_command_arguments_without_business_logic(tmp_path: Path) -> None:
    operator = RecordingOperator()
    bundle = tmp_path / "backup"

    assert (
        main(
            [
                "arm-live",
                "--ttl-seconds",
                "90",
                "--json",
            ],
            service_factory=lambda _path: operator,
        )
        == 0
    )
    request, _ = operator.calls[-1]
    assert request.ttl_seconds == 90
    assert request.output_json is True

    assert (
        main(
            ["verify-backup", "--bundle", str(bundle)],
            service_factory=lambda _path: operator,
        )
        == 0
    )
    request, _ = operator.calls[-1]
    assert request.bundle_path == bundle


@pytest.mark.parametrize(
    "arguments",
    [
        ["arm-live", "--ttl-seconds", "0"],
        ["arm-live", "--ttl-seconds", "901"],
        ["decisions", "--limit", "0"],
        ["orders", "--limit", "1001"],
        ["report", "--session", "2026-02-30"],
        ["replay"],
        ["verify-backup"],
    ],
)
def test_invalid_cli_arguments_are_rejected_before_service_dispatch(
    arguments: list[str],
) -> None:
    operator = RecordingOperator()

    with pytest.raises(SystemExit) as error:
        main(arguments, service_factory=lambda _path: operator)

    assert error.value.code == 2
    assert operator.calls == []


def test_service_failure_is_rendered_without_exception_details(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FailingOperator:
        def execute(
            self,
            request: OperatorRequest,
            interaction: OperatorInteraction,
        ) -> OperatorResult:
            del request, interaction
            raise RuntimeError("account-123 secret-value")

    assert main(["status"], service_factory=lambda _path: FailingOperator()) == 2

    error = capsys.readouterr().err
    assert "OPERATION_FAILED" in error
    assert "account-123" not in error
    assert "secret-value" not in error


def test_unexpected_failure_preserves_json_output_contract(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FailingOperator:
        def execute(
            self,
            request: OperatorRequest,
            interaction: OperatorInteraction,
        ) -> OperatorResult:
            del request, interaction
            raise RuntimeError("account-123 secret-value")

    assert main(["status", "--json"], service_factory=lambda _path: FailingOperator()) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload == {
        "error": "OPERATION_FAILED",
        "error_type": "RuntimeError",
        "success": False,
    }
    assert "account-123" not in captured.err
    assert "secret-value" not in captured.err


def test_firmquant_git_identity_requires_a_clean_checkout(tmp_path: Path) -> None:
    git = shutil.which("git")
    if git is None:
        pytest.skip("git executable unavailable")
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run([git, "init", "-q"], cwd=repository, check=True)
    subprocess.run(
        [git, "config", "user.email", "firmquant-test@example.invalid"],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        [git, "config", "user.name", "firmquant test"],
        cwd=repository,
        check=True,
    )
    tracked = repository / "tracked.txt"
    tracked.write_text("reviewed\n", encoding="utf-8")
    subprocess.run([git, "add", "tracked.txt"], cwd=repository, check=True)
    subprocess.run([git, "commit", "-q", "-m", "reviewed"], cwd=repository, check=True)

    clean_commit = _clean_git_commit(repository)
    assert len(clean_commit) == 40

    tracked.write_text("modified\n", encoding="utf-8")
    assert _clean_git_commit(repository) == "UNKNOWN"
