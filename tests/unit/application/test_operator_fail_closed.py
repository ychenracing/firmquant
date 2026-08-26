from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

import pytest

from firmquant.application.operations import (
    LocalOperatorService,
    OperatorCommand,
    OperatorCommandDenied,
    OperatorInteraction,
    OperatorReconciliation,
    OperatorRequest,
    OperatorResult,
    _ci_detected,
    _clean_git_commit,
    _clock_value,
)
from firmquant.config import Mode
from tests.integration.test_cli_operations import (
    FIRMQUANT_COMMIT,
    NOW,
    initialize,
    interaction,
    paper_config,
    request,
    service,
)


@pytest.mark.parametrize("value", ["", "lowercase", " BAD", "BAD-CODE", 1, None])
def test_operator_denial_requires_safe_canonical_reason_code(value: object) -> None:
    with pytest.raises(ValueError, match="canonical"):
        OperatorCommandDenied(value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("change", "exception"),
    [
        ({"command": "status"}, TypeError),
        ({"output_json": 1}, TypeError),
        ({"mode": "PAPER"}, TypeError),
        ({"session": datetime(2026, 8, 25)}, TypeError),
        ({"events_path": "events.jsonl"}, TypeError),
        ({"bundle_path": "backup"}, TypeError),
        ({"account_state_path": "account.json"}, TypeError),
        ({"ttl_seconds": True}, ValueError),
        ({"ttl_seconds": 0}, ValueError),
        ({"ttl_seconds": 901}, ValueError),
        ({"limit": True}, ValueError),
        ({"limit": 0}, ValueError),
        ({"limit": 1001}, ValueError),
        ({"reason": ""}, ValueError),
        ({"reason": " bad"}, ValueError),
        ({"reason": "x" * 257}, ValueError),
        ({"reason": "bad\n"}, ValueError),
    ],
)
def test_operator_request_rejects_ambiguous_cli_values(
    change: dict[str, object], exception: type[Exception]
) -> None:
    with pytest.raises(exception):
        replace(OperatorRequest(command=OperatorCommand.STATUS), **change)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: OperatorInteraction(1, lambda _prompt: "", {}),
        lambda: OperatorInteraction(True, object(), {}),
        lambda: OperatorInteraction(True, lambda _prompt: "", []),
        lambda: OperatorInteraction(True, lambda _prompt: "", {1: "value"}),
        lambda: OperatorInteraction(True, lambda _prompt: "", {"KEY": 1}),
    ],
)
def test_operator_interaction_rejects_nonterminal_or_secret_boundary_confusion(
    factory: Callable[[], object],
) -> None:
    with pytest.raises(TypeError):
        factory()


@pytest.mark.parametrize(
    ("factory", "exception"),
    [
        (lambda: OperatorResult("", {}), ValueError),
        (lambda: OperatorResult(" bad", {}), ValueError),
        (lambda: OperatorResult("bad\n", {}), ValueError),
        (lambda: OperatorResult("ok", {}, True), TypeError),
        (lambda: OperatorResult("ok", {}, -1), ValueError),
        (lambda: OperatorResult("ok", {}, 256), ValueError),
        (lambda: OperatorResult("ok", []), TypeError),
        (lambda: OperatorResult("ok", {1: "value"}), TypeError),
        (lambda: OperatorResult("ok", {"value": math.nan}), ValueError),
    ],
)
def test_operator_result_is_json_safe_and_process_safe(
    factory: Callable[[], object], exception: type[Exception]
) -> None:
    with pytest.raises(exception):
        factory()


@pytest.mark.parametrize(
    ("change", "exception"),
    [
        ({"reconciliation_id": "bad"}, ValueError),
        ({"passed": 1}, TypeError),
        ({"blockers": ["MISMATCH"]}, ValueError),
        ({"blockers": ("Z", "A"), "passed": False}, ValueError),
        ({"blockers": (" bad",), "passed": False}, ValueError),
        ({"blockers": ("MISMATCH",)}, ValueError),
    ],
)
def test_operator_reconciliation_requires_consistent_evidence(
    change: dict[str, object], exception: type[Exception]
) -> None:
    valid = OperatorReconciliation("recon_" + "a" * 64, True, ())
    with pytest.raises(exception):
        replace(valid, **change)


def test_operator_interaction_repr_and_environment_are_redacted_and_immutable() -> None:
    value = OperatorInteraction(True, lambda _prompt: "", {"ARM_MAC_KEY": "sensitive"})
    assert repr(value) == "<OperatorInteraction redacted>"
    with pytest.raises(TypeError):
        value.environment["NEW"] = "value"  # type: ignore[index]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"config_path": "firmquant.toml"},
        {"config_path": Path("firmquant.toml"), "clock": object()},
        {"config_path": Path("firmquant.toml"), "firmquant_commit_provider": object()},
        {"config_path": Path("firmquant.toml"), "runner": object()},
        {"config_path": Path("firmquant.toml"), "reconciler": object()},
        {"config_path": Path("firmquant.toml"), "reporter": object()},
        {"config_path": Path("firmquant.toml"), "doctor_broker_provider": object()},
        {"config_path": Path("firmquant.toml"), "system_order_canceller": object()},
    ],
)
def test_operator_service_rejects_invalid_dependency_composition(kwargs: dict[str, object]) -> None:
    with pytest.raises(TypeError):
        LocalOperatorService(**kwargs)  # type: ignore[arg-type]


def test_execute_rejects_values_that_bypass_cli_parser(tmp_path: Path) -> None:
    operator = service(tmp_path / "firmquant.toml")
    with pytest.raises(TypeError, match="request"):
        operator.execute(object(), interaction())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="interaction"):
        operator.execute(request(OperatorCommand.STATUS), object())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "clock",
    [lambda: "now", lambda: datetime(2026, 8, 25)],
)
def test_operator_clock_requires_timezone_aware_datetime(clock: Callable[[], object]) -> None:
    with pytest.raises(OperatorCommandDenied, match="CLOCK_UNAVAILABLE"):
        _clock_value(clock)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("environment", "detected"),
    [
        ({}, False),
        ({"CI": ""}, False),
        ({"CI": "0"}, False),
        ({"GITHUB_ACTIONS": "false"}, False),
        ({"GITLAB_CI": "no"}, False),
        ({"CI": "1"}, True),
        ({"GITHUB_ACTIONS": "TRUE"}, True),
    ],
)
def test_ci_detection_cannot_be_bypassed_by_common_false_spellings(
    environment: dict[str, str], detected: bool
) -> None:
    assert _ci_detected(environment) is detected


def test_clean_git_commit_fails_closed_for_nonrepository_and_dirty_repository(tmp_path: Path) -> None:
    assert _clean_git_commit(tmp_path) == "UNKNOWN"

    repository = tmp_path / "repository"
    repository.mkdir()
    assert _clean_git_commit(repository) == "UNKNOWN"


def test_settings_and_identity_fail_closed(tmp_path: Path) -> None:
    missing = LocalOperatorService(config_path=tmp_path / "missing.toml", clock=lambda: NOW)
    with pytest.raises(OperatorCommandDenied, match="CONFIGURATION_UNAVAILABLE"):
        missing.execute(request(OperatorCommand.STATUS), interaction())

    config = tmp_path / "firmquant.toml"
    config.write_text("not valid toml =", encoding="utf-8")
    invalid = LocalOperatorService(config_path=config, clock=lambda: NOW)
    with pytest.raises(OperatorCommandDenied, match="CONFIGURATION_INVALID"):
        invalid.execute(request(OperatorCommand.STATUS), interaction())

    paper_config(config)
    throwing = LocalOperatorService(
        config_path=config,
        clock=lambda: NOW,
        firmquant_commit_provider=lambda: (_ for _ in ()).throw(RuntimeError("injected")),
    )
    with pytest.raises(OperatorCommandDenied, match="FIRMQUANT_IDENTITY_UNAVAILABLE"):
        throwing._firmquant_commit()  # type: ignore[attr-defined]

    malformed = LocalOperatorService(
        config_path=config,
        clock=lambda: NOW,
        firmquant_commit_provider=lambda: "not-a-commit",
    )
    with pytest.raises(OperatorCommandDenied, match="FIRMQUANT_IDENTITY_UNAVAILABLE"):
        malformed._firmquant_commit()  # type: ignore[attr-defined]


def test_state_directory_symlink_and_file_are_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    symlink = tmp_path / "linked"
    symlink.symlink_to(real, target_is_directory=True)
    with pytest.raises(OperatorCommandDenied, match="STATE_PATH_INVALID"):
        LocalOperatorService._ensure_directory(symlink)  # type: ignore[attr-defined]

    file_path = tmp_path / "file"
    file_path.write_text("not a directory", encoding="utf-8")
    with pytest.raises(OperatorCommandDenied, match="STATE_PATH_UNAVAILABLE"):
        LocalOperatorService._ensure_directory(file_path)  # type: ignore[attr-defined]


def test_run_requires_exact_configured_mode_and_runtime_port(tmp_path: Path) -> None:
    config = tmp_path / "firmquant.toml"
    paper_config(config)
    operator = service(config)
    with pytest.raises(OperatorCommandDenied, match="RUN_MODE_CONFIG_MISMATCH"):
        operator.execute(request(OperatorCommand.RUN, mode=Mode.SHADOW), interaction())
    with pytest.raises(OperatorCommandDenied, match="RUNTIME_COMPOSITION_UNAVAILABLE"):
        operator.execute(request(OperatorCommand.RUN), interaction())


@pytest.mark.parametrize("value", [None, True, -1, "1"])
def test_database_count_rejects_corrupt_scalar_values(value: object) -> None:
    with pytest.raises(OperatorCommandDenied, match="DATABASE_STATE_INVALID"):
        LocalOperatorService._count(value)  # type: ignore[arg-type]


def test_required_path_arguments_and_uninitialized_reads_fail_closed(tmp_path: Path) -> None:
    config = tmp_path / "firmquant.toml"
    paper_config(config)
    operator = service(config)
    with pytest.raises(OperatorCommandDenied, match="INITIALIZATION_REQUIRED"):
        operator.execute(request(OperatorCommand.DECISIONS), interaction())

    initialize(operator)
    with pytest.raises(OperatorCommandDenied, match="REPLAY_EVENTS_REQUIRED"):
        operator.execute(request(OperatorCommand.REPLAY), interaction())
    with pytest.raises(OperatorCommandDenied, match="BACKUP_BUNDLE_REQUIRED"):
        operator.execute(request(OperatorCommand.VERIFY_BACKUP), interaction())
    with pytest.raises(OperatorCommandDenied, match="REPORT_UNAVAILABLE"):
        operator.execute(request(OperatorCommand.REPORT, session=date(2026, 8, 25)), interaction())


def test_configuration_hash_read_failure_is_safe(tmp_path: Path) -> None:
    config = tmp_path / "firmquant.toml"
    paper_config(config)
    operator = LocalOperatorService(
        config_path=config,
        clock=lambda: NOW,
        firmquant_commit_provider=lambda: FIRMQUANT_COMMIT,
    )
    config.unlink()
    with pytest.raises(OperatorCommandDenied, match="CONFIGURATION_UNAVAILABLE"):
        operator._configuration_sha256()  # type: ignore[attr-defined]
