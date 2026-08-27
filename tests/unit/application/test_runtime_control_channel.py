from __future__ import annotations

import json
import os
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from firmquant.application.control_channel import (
    MAX_CONTROL_REQUEST_BYTES,
    ControlCommand,
    ControlExecution,
    ControlInbox,
    ControlRequest,
    ControlStatus,
)
from firmquant.application.production_daemon import ProductionCycleResult, ProductionDaemon
from firmquant.cli import main
from firmquant.config import Mode
from firmquant.persistence.writer_lease import WriterLease


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 27, 5, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)


class Broker:
    def __init__(self, sequence: list[str]) -> None:
        self.connected = False
        self.sink = None
        self.sequence = sequence

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.sequence.append("disconnect")
        self.connected = False

    def subscribe(self, sink: object) -> None:
        self.sink = sink


class Pump:
    def __init__(self) -> None:
        self.sink = lambda _event: None
        self.pending = ["event-1"]
        self.halt_required = False
        self.halt_reason = None

    @property
    def pending_count(self) -> int:
        return len(self.pending)

    def dispatch_one(self, writer) -> bool:
        if not self.pending:
            return False
        writer(self.pending.pop(0))
        return True


@dataclass
class Hooks:
    sequence: list[str]
    startup_calls: int = 0
    cycles: int = 0
    halted: list[str] = field(default_factory=list)

    def startup(self) -> str:
        self.startup_calls += 1
        self.sequence.append("startup")
        return "recon_" + "a" * 64

    def handle_event(self, _event: object) -> None:
        self.sequence.append("event")

    def cycle(self, _now: datetime) -> ProductionCycleResult:
        self.cycles += 1
        self.sequence.append("cycle")
        return ProductionCycleResult(0, 0, 0)

    def heartbeat(self, _heartbeat: object) -> None:
        self.sequence.append("heartbeat")

    def halt(self, reason_code: str) -> None:
        self.halted.append(reason_code)
        self.sequence.append("halt")

    def real_order_calls(self) -> int:
        return 0


def _write_config(path: Path, state: Path) -> None:
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


def _seed_arm(writer: WriterLease, clock: Clock) -> None:
    with writer.database.transaction():
        writer.database.write(
            """
            INSERT INTO arm_leases(
                lease_id, mode, host_hash, account_hash, firmquant_commit,
                uquant_commit, config_sha256, identity_payload_sha256,
                issued_at, expires_at, revoked_at, revoke_reason, lease_mac
            ) VALUES (?, 'CANARY', ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
            """,
            (
                "arm_" + "a" * 32,
                writer.host_hash,
                "b" * 64,
                "c" * 40,
                "d" * 40,
                "e" * 64,
                "f" * 64,
                clock().isoformat(),
                (clock() + timedelta(minutes=5)).isoformat(),
                "0" * 64,
            ),
        )


def _private(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o600)


def _write_request(path: Path, request: ControlRequest) -> None:
    path.write_bytes(request.canonical_bytes())
    _private(path)


def test_control_inbox_prioritizes_halt_and_duplicate_request_is_idempotent(tmp_path: Path) -> None:
    clock = Clock()
    ids = iter(("ctrl_" + "1" * 64, "ctrl_" + "2" * 64))
    inbox = ControlInbox(tmp_path, clock=clock, request_id_factory=lambda: next(ids))
    stop = inbox.enqueue(ControlCommand.STOP, reason="maintenance")
    halt = inbox.enqueue(ControlCommand.HALT, reason="operator stop")
    observed: list[ControlCommand] = []

    def handle(request: ControlRequest) -> ControlExecution:
        observed.append(request.command)
        return ControlExecution(
            outcome={"command": request.command.value},
            halted=request.command is ControlCommand.HALT,
            stop=request.command is ControlCommand.STOP,
        )

    batch = inbox.process_pending(handle)
    assert observed == [ControlCommand.HALT, ControlCommand.STOP]
    assert batch.halted is True
    assert batch.stop is True
    assert inbox.status(halt.request_id).status is ControlStatus.COMPLETED
    assert inbox.status(stop.request_id).status is ControlStatus.COMPLETED

    duplicate_path = inbox.inbox_directory / f"{halt.request_id}.json"
    _write_request(duplicate_path, halt)
    second = inbox.process_pending(handle)
    assert second.receipts == ()
    assert observed == [ControlCommand.HALT, ControlCommand.STOP]


def test_control_inbox_rejects_untrusted_request_shapes(tmp_path: Path) -> None:
    clock = Clock()
    inbox = ControlInbox(
        tmp_path,
        clock=clock,
        request_id_factory=lambda: "ctrl_" + "3" * 64,
    )
    expired = inbox.enqueue(ControlCommand.DISARM, ttl=timedelta(seconds=1))
    clock.advance(2)

    oversize_id = "ctrl_" + "4" * 64
    oversize = inbox.inbox_directory / f"{oversize_id}.json"
    oversize.write_bytes(b"x" * (MAX_CONTROL_REQUEST_BYTES + 1))
    _private(oversize)

    malformed_id = "ctrl_" + "5" * 64
    malformed = inbox.inbox_directory / f"{malformed_id}.json"
    malformed.write_text('{"command":"HALT","command":"STOP"}', encoding="utf-8")
    _private(malformed)

    traversal_id = "ctrl_" + "6" * 64
    traversal = inbox.inbox_directory / f"{traversal_id}.json"
    traversal.write_text(
        json.dumps(
            {
                "command": "HALT",
                "created_at": clock().isoformat(),
                "expires_at": (clock() + timedelta(minutes=1)).isoformat(),
                "host_hash": inbox.host_hash,
                "request_id": "../escape",
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    _private(traversal)

    future_id = "ctrl_" + "7" * 64
    future = ControlRequest(
        request_id=future_id,
        command=ControlCommand.HALT,
        created_at=clock() + timedelta(seconds=1),
        expires_at=clock() + timedelta(minutes=1),
        host_hash=inbox.host_hash,
    )
    _write_request(inbox.inbox_directory / f"{future_id}.json", future)

    extra_id = "ctrl_" + "8" * 64
    extra_path = inbox.inbox_directory / f"{extra_id}.json"
    extra_path.write_text(
        json.dumps(
            {
                "command": "HALT",
                "created_at": clock().isoformat(),
                "expires_at": (clock() + timedelta(minutes=1)).isoformat(),
                "extra": "forbidden",
                "host_hash": inbox.host_hash,
                "request_id": extra_id,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    _private(extra_path)

    symlink_created = False
    target = tmp_path / "outside-control-target"
    target.write_text("do-not-read", encoding="utf-8")
    symlink_id = "ctrl_" + "9" * 64
    with suppress(OSError, NotImplementedError):
        inbox.inbox_directory.joinpath(symlink_id + ".json").symlink_to(target)
        symlink_created = True

    permissive_id: str | None = None
    if os.name != "nt":
        permissive_id = "ctrl_" + "a" * 64
        permissive = ControlRequest(
            request_id=permissive_id,
            command=ControlCommand.DISARM,
            created_at=clock(),
            expires_at=clock() + timedelta(minutes=1),
            host_hash=inbox.host_hash,
        )
        permissive_path = inbox.inbox_directory / f"{permissive_id}.json"
        permissive_path.write_bytes(permissive.canonical_bytes())
        permissive_path.chmod(0o644)

    called = 0

    def handle(_request: ControlRequest) -> ControlExecution:
        nonlocal called
        called += 1
        return ControlExecution(outcome={})

    batch = inbox.process_pending(handle)
    assert called == 0
    assert all(receipt.status is ControlStatus.REJECTED for receipt in batch.receipts)
    for request_id in (expired.request_id, oversize_id, malformed_id, traversal_id, future_id, extra_id):
        assert inbox.status(request_id).status is ControlStatus.REJECTED
    if symlink_created:
        assert inbox.status(symlink_id).status is ControlStatus.REJECTED
        assert target.read_text(encoding="utf-8") == "do-not-read"
    if permissive_id is not None:
        assert inbox.status(permissive_id).status is ControlStatus.REJECTED
    assert list(inbox.inbox_directory.iterdir()) == []


def test_cli_halt_queues_under_active_writer_and_daemon_consumes_it(tmp_path: Path, capsys) -> None:
    state = tmp_path / "state"
    state.mkdir()
    config = tmp_path / "firmquant.toml"
    _write_config(config, state)
    database_path = state / "firmquant.sqlite3"
    sequence: list[str] = []

    with WriterLease.acquire(database_path, owner="production-runtime") as writer:
        exit_code = main(
            ["--config", str(config), "halt", "--json", "--reason", "operator emergency"],
            interactive_terminal=False,
            environment={},
        )
        assert exit_code == 0
        payload = json.loads(capsys.readouterr().out)
        request_id = payload["request_id"]
        assert payload["control_status"] == "QUEUED"
        assert payload["command"] == "HALT"
        queued_path = state / "control" / "inbox" / f"{request_id}.json"
        assert b"operator emergency" not in queued_path.read_bytes()
        inbox = ControlInbox(state)
        assert inbox.status(request_id).status is ControlStatus.QUEUED
        inbox.enqueue(ControlCommand.STOP)

        hooks = Hooks(sequence)
        daemon = ProductionDaemon(
            mode=Mode.SHADOW,
            writer=writer,
            broker=Broker(sequence),
            pump=Pump(),
            hooks=hooks,
            clock=lambda: datetime.now(UTC),
            sleep=lambda _seconds: None,
            stop_requested=lambda: False,
            control_inbox=inbox,
        )
        receipt = daemon.run()
        assert inbox.status(request_id).status is ControlStatus.COMPLETED
        assert hooks.cycles == 0
        assert receipt.real_order_calls == 0

    status_exit = main(
        ["--config", str(config), "status", "--request-id", request_id, "--json"],
        interactive_terminal=False,
        environment={},
    )
    assert status_exit == 0
    status_payload = json.loads(capsys.readouterr().out)
    assert status_payload["control_status"] == "COMPLETED"
    assert status_payload["command"] == "HALT"


def test_daemon_applies_halt_before_broker_events_or_cycle_and_revokes_arm(tmp_path: Path) -> None:
    clock = Clock()
    sequence: list[str] = []
    inbox = ControlInbox(
        tmp_path,
        clock=clock,
        request_id_factory=lambda: "ctrl_" + "b" * 64,
    )
    halt = inbox.enqueue(ControlCommand.HALT)
    hooks = Hooks(sequence)
    broker = Broker(sequence)
    pump = Pump()
    observed_states: list[str] = []

    with WriterLease.acquire(
        tmp_path / "firmquant.sqlite3", owner="production-runtime", clock=clock
    ) as writer:
        _seed_arm(writer, clock)

        def stop_requested() -> bool:
            row = writer.database.query_one("SELECT state FROM runtime_state WHERE singleton_id = 1")
            if row is not None:
                observed_states.append(str(row["state"]))
                return str(row["state"]) == "HALTED"
            return False

        daemon = ProductionDaemon(
            mode=Mode.CANARY,
            writer=writer,
            broker=broker,
            pump=pump,
            hooks=hooks,
            clock=clock,
            sleep=clock.sleep,
            stop_requested=stop_requested,
            control_inbox=inbox,
            poll_interval=timedelta(seconds=1),
        )
        receipt = daemon.run()
        revoked = writer.database.scalar("SELECT count(*) FROM arm_leases WHERE revoked_at IS NOT NULL")
        kill_events = writer.database.scalar(
            "SELECT count(*) FROM risk_events WHERE code = 'KILL_SWITCH_TRIPPED'"
        )

    assert observed_states[0] == "HALTED"
    assert revoked == 1
    assert kill_events == 1
    assert hooks.cycles == 0
    assert pump.pending_count == 1
    assert hooks.halted == []
    assert inbox.status(halt.request_id).status is ControlStatus.COMPLETED
    halt_outcome = inbox.status(halt.request_id).outcome
    assert halt_outcome is not None and halt_outcome["auto_liquidation"] is False
    assert receipt.real_order_calls == 0
    assert broker.connected is False


def test_daemon_stop_request_disconnects_cleanly_without_starting_new_work(tmp_path: Path) -> None:
    clock = Clock()
    sequence: list[str] = []
    inbox = ControlInbox(
        tmp_path,
        clock=clock,
        request_id_factory=lambda: "ctrl_" + "c" * 64,
    )
    stop = inbox.enqueue(ControlCommand.STOP)
    hooks = Hooks(sequence)
    broker = Broker(sequence)

    with WriterLease.acquire(
        tmp_path / "firmquant.sqlite3", owner="production-runtime", clock=clock
    ) as writer:
        daemon = ProductionDaemon(
            mode=Mode.SHADOW,
            writer=writer,
            broker=broker,
            pump=Pump(),
            hooks=hooks,
            clock=clock,
            sleep=clock.sleep,
            stop_requested=lambda: False,
            control_inbox=inbox,
        )
        receipt = daemon.run()
        state = writer.database.scalar("SELECT state FROM runtime_state WHERE singleton_id = 1")

    assert hooks.startup_calls == 0
    assert hooks.cycles == 0
    assert sequence[-1] == "disconnect"
    assert state == "DISARMED"
    assert inbox.status(stop.request_id).status is ControlStatus.COMPLETED
    assert receipt.stopped_cleanly is True
    assert receipt.real_order_calls == 0


def test_control_channel_uses_fixed_local_directories_and_no_background_threads(tmp_path: Path) -> None:
    inbox = ControlInbox(tmp_path)
    assert inbox.inbox_directory == tmp_path / "control" / "inbox"
    assert inbox.receipt_directory == tmp_path / "control" / "receipts"
    assert not inbox.inbox_directory.is_symlink()
    assert not inbox.receipt_directory.is_symlink()
    if os.name != "nt":
        assert inbox.inbox_directory.stat().st_mode & 0o077 == 0
        assert inbox.receipt_directory.stat().st_mode & 0o077 == 0
