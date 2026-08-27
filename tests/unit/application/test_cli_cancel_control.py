from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from firmquant.application.control_channel import ControlInbox, ControlStatus
from firmquant.broker.fake import BrokerOperation, ScriptedOutcome
from firmquant.cli import main
from firmquant.domain.broker_facts import BrokerOrderStatus
from firmquant.persistence.account_authority import AccountBinding, AccountBindingRepository
from firmquant.persistence.writer_lease import WriterLease
from tests.fixtures.recovery_cases import (
    NOW,
    acknowledge_locally,
    broker_order,
    create_submitting_case,
    fake_recovery_broker,
)


def _write_canary_config(path: Path, state: Path) -> None:
    path.write_text(
        "\n".join(
            (
                "schema_version = 1",
                'mode = "CANARY"',
                "live_trading_enabled = true",
                'timezone = "Asia/Shanghai"',
                "",
                "[broker]",
                'adapter = "XTQUANT"',
                'account_alias = "test-account"',
                f'xtquant_userdata_path = "{(state.parent / "userdata").as_posix()}"',
                "session_id = 1",
                f'safety_manifest_path = "{(state.parent / "safety.json").as_posix()}"',
                "",
                "[paths]",
                f'state_directory = "{state.as_posix()}"',
                f'data_directory = "{(state.parent / "data").as_posix()}"',
                f'report_directory = "{(state.parent / "reports").as_posix()}"',
                f'backup_directory = "{(state.parent / "backups").as_posix()}"',
                f'uquant_source_checkout = "{(state.parent / "uquant").as_posix()}"',
                "",
                "[compliance]",
                "program_trading_report_confirmed = true",
                "broker_api_authorized = true",
                "",
                "[canary_caps]",
                'max_order_notional = "10000"',
                'max_daily_submitted_notional = "30000"',
                'max_daily_filled_notional = "30000"',
                'max_symbol_notional = "20000"',
                'max_total_gross_notional = "50000"',
                "",
            )
        ),
        encoding="utf-8",
    )


def _write_paper_config(path: Path, state: Path) -> None:
    path.write_text(
        "\n".join(
            (
                "schema_version = 1",
                'mode = "PAPER"',
                "live_trading_enabled = false",
                'timezone = "Asia/Shanghai"',
                "",
                "[broker]",
                'adapter = "PAPER"',
                "",
                "[paths]",
                f'state_directory = "{state.as_posix()}"',
                f'data_directory = "{(state.parent / "data").as_posix()}"',
                f'report_directory = "{(state.parent / "reports").as_posix()}"',
                f'backup_directory = "{(state.parent / "backups").as_posix()}"',
                "",
                "[compliance]",
                "program_trading_report_confirmed = false",
                "broker_api_authorized = false",
                "",
            )
        ),
        encoding="utf-8",
    )


def _bind_account(writer: WriterLease, account_hash: str) -> None:
    account_type = fake_recovery_broker().query_account().account_type
    AccountBindingRepository(writer.database).bind(
        AccountBinding.create(
            account_id_hash=account_hash,
            account_type=account_type,
            broker_snapshot_sha256="b" * 64,
            account_state_sha256="c" * 64,
            uquant_commit="1" * 40,
            uquant_code_fingerprint="d" * 64,
            data_hash="e" * 64,
            data_as_of="2026-08-25",
            data_symbols=("600519.SH",),
            created_at=NOW,
        )
    )


def test_cli_direct_cancel_uses_ledger_derived_cancel_only_capability(tmp_path: Path, capsys) -> None:
    state = tmp_path / "state"
    state.mkdir()
    config = tmp_path / "firmquant.toml"
    _write_canary_config(config, state)

    with WriterLease.acquire(state / "firmquant.sqlite3", owner="seed") as writer:
        case = create_submitting_case(writer.database)
        acknowledged_fact = broker_order(case.command)
        acknowledged = acknowledge_locally(case, acknowledged_fact)
        broker = fake_recovery_broker(orders=(acknowledged_fact,))
        _bind_account(writer, broker.query_account().account_id_hash)
    cancelled = replace(
        acknowledged_fact,
        status=BrokerOrderStatus.CANCELLED,
        event_sequence=acknowledged_fact.event_sequence + 1,
    )
    broker.script((ScriptedOutcome(BrokerOperation.CANCEL, response=cancelled),))
    broker.disconnect()

    calls = 0

    def broker_factory(_settings, _database, _clock):
        nonlocal calls
        calls += 1
        return broker

    exit_code = main(
        ["--config", str(config), "cancel-system-orders", "--json"],
        control_broker_factory=broker_factory,
        interactive_terminal=False,
        environment={},
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["control_status"] == "COMPLETED"
    assert payload["command"] == "CANCEL_SYSTEM_ORDERS"
    assert payload["outcome"]["cancelled_order_ids"] == [acknowledged.broker_order_id]
    assert payload["outcome"]["cancel_calls"] == 1
    assert calls == 1
    assert broker.cancelled_order_ids == (acknowledged.broker_order_id,)
    assert broker.health().connected is False
    assert ControlInbox(state).status(payload["request_id"]).status is ControlStatus.COMPLETED


def test_cli_direct_cancel_is_write_free_in_paper(tmp_path: Path, capsys) -> None:
    state = tmp_path / "state"
    state.mkdir()
    config = tmp_path / "firmquant.toml"
    _write_paper_config(config, state)
    with WriterLease.acquire(state / "firmquant.sqlite3", owner="seed"):
        pass

    calls = 0

    def broker_factory(_settings, _database, _clock):
        nonlocal calls
        calls += 1
        raise AssertionError("PAPER cancel must never construct a production broker")

    exit_code = main(
        ["--config", str(config), "cancel-system-orders", "--json"],
        control_broker_factory=broker_factory,
        interactive_terminal=False,
        environment={},
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["control_status"] == "COMPLETED"
    assert payload["outcome"]["mode_write_forbidden"] is True
    assert payload["outcome"]["cancel_calls"] == 0
    assert calls == 0


def test_cli_cancel_queues_when_daemon_owns_writer_without_constructing_broker(tmp_path: Path, capsys) -> None:
    state = tmp_path / "state"
    state.mkdir()
    config = tmp_path / "firmquant.toml"
    _write_canary_config(config, state)
    calls = 0

    def broker_factory(_settings, _database, _clock):
        nonlocal calls
        calls += 1
        raise AssertionError("busy writer path must only enqueue")

    with WriterLease.acquire(state / "firmquant.sqlite3", owner="production-runtime"):
        exit_code = main(
            ["--config", str(config), "cancel-system-orders", "--json", "--reason", "risk reduction"],
            control_broker_factory=broker_factory,
            interactive_terminal=False,
            environment={},
        )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["control_status"] == "QUEUED"
    assert payload["command"] == "CANCEL_SYSTEM_ORDERS"
    assert calls == 0
    assert ControlInbox(state).status(payload["request_id"]).status is ControlStatus.QUEUED
