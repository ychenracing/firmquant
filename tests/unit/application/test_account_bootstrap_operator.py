from __future__ import annotations

from pathlib import Path

import pytest

from firmquant.application.operations import (
    LocalOperatorService,
    OperatorCommand,
    OperatorCommandDenied,
    OperatorInteraction,
    OperatorRequest,
)
from tests.integration.test_cli_operations import NOW, paper_config


def _interaction() -> OperatorInteraction:
    return OperatorInteraction(
        interactive_terminal=False,
        confirmation_reader=lambda _prompt: "",
        environment={},
    )


def test_bootstrap_account_requires_composed_business_port(tmp_path: Path) -> None:
    config = tmp_path / "firmquant.toml"
    paper_config(config)
    service = LocalOperatorService(config_path=config, clock=lambda: NOW)

    with pytest.raises(OperatorCommandDenied, match="ACCOUNT_BOOTSTRAP_PORT_UNAVAILABLE"):
        service.execute(
            OperatorRequest(command=OperatorCommand.BOOTSTRAP_ACCOUNT),
            _interaction(),
        )


def test_bootstrap_account_delegates_reviewed_seed_without_leaking_path(tmp_path: Path) -> None:
    config = tmp_path / "firmquant.toml"
    paper_config(config)
    seed = tmp_path / "reviewed-account.json"
    calls: list[Path | None] = []

    def bootstrapper(seed_path: Path | None):
        calls.append(seed_path)
        return {
            "binding_id": "binding_" + "a" * 64,
            "account_state_sha256": "b" * 64,
            "broker_snapshot_sha256": "c" * 64,
        }

    service = LocalOperatorService(
        config_path=config,
        clock=lambda: NOW,
        account_bootstrapper=bootstrapper,
    )
    result = service.execute(
        OperatorRequest(
            command=OperatorCommand.BOOTSTRAP_ACCOUNT,
            account_state_path=seed,
        ),
        _interaction(),
    )

    assert calls == [seed]
    assert result.exit_code == 0
    assert result.payload["binding_id"] == "binding_" + "a" * 64
    assert "reviewed-account.json" not in result.message
    assert "reviewed-account.json" not in str(dict(result.payload))
