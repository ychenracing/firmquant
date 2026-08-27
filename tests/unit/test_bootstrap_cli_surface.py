from __future__ import annotations

from pathlib import Path

from firmquant.application.operations import OperatorCommand
from firmquant.cli import build_parser


def test_bootstrap_account_cli_accepts_optional_reviewed_seed_path() -> None:
    parser = build_parser()

    empty = parser.parse_args(["bootstrap-account"])
    seeded = parser.parse_args(["bootstrap-account", "--account-state", "reviewed.json"])

    assert OperatorCommand(empty.command) is OperatorCommand.BOOTSTRAP_ACCOUNT
    assert empty.account_state is None
    assert OperatorCommand(seeded.command) is OperatorCommand.BOOTSTRAP_ACCOUNT
    assert seeded.account_state == Path("reviewed.json")
