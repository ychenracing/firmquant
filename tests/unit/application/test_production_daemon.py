from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from firmquant.application.production_daemon import (
    ProductionCycleResult,
    ProductionDaemon,
    ProductionDaemonHalted,
)
from firmquant.config import Mode
from firmquant.persistence.writer_lease import WriterLease


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 25, 1, 30, tzinfo=UTC)
        self.elapsed = 0.0

    def __call__(self) -> datetime:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)
        self.elapsed += seconds

    def monotonic(self) -> float:
        return self.elapsed


class Broker:
    def __init__(self) -> None:
        self.connected = False
        self.sink = None

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def subscribe(self, sink: object) -> None:
        self.sink = sink


class Pump:
    def __init__(self) -> None:
        self.sink = lambda _event: None
        self.pending = ["e1", "e2"]
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
    startup_id: str = "recon_" + "a" * 64
    cycles: int = 0
    handled: int = 0
    heartbeats: int = 0
    halted: list[str] | None = None

    def startup(self) -> str:
        return self.startup_id

    def handle_event(self, _event: object) -> None:
        self.handled += 1

    def cycle(self, _now: datetime) -> ProductionCycleResult:
        self.cycles += 1
        return ProductionCycleResult(
            decisions=1 if self.cycles == 1 else 0,
            executions=0,
            eod=0,
        )

    def heartbeat(self, _heartbeat: object) -> None:
        self.heartbeats += 1

    def halt(self, reason_code: str) -> None:
        if self.halted is None:
            self.halted = []
        self.halted.append(reason_code)

    def real_order_calls(self) -> int:
        return 0


def test_daemon_drains_callbacks_renews_writer_and_stops_cleanly(tmp_path: Path) -> None:
    clock = Clock()
    database_path = tmp_path / "firmquant.sqlite3"
    hooks = Hooks()
    broker = Broker()
    pump = Pump()
    loops = 0

    def stop_requested() -> bool:
        nonlocal loops
        loops += 1
        return loops >= 4

    with WriterLease.acquire(
        database_path,
        owner="production-test",
        ttl=timedelta(seconds=5),
        clock=clock,
    ) as writer:
        daemon = ProductionDaemon(
            mode=Mode.SHADOW,
            writer=writer,
            broker=broker,
            pump=pump,
            hooks=hooks,
            clock=clock,
            monotonic_clock=clock.monotonic,
            sleep=clock.sleep,
            stop_requested=stop_requested,
            poll_interval=timedelta(seconds=2),
            renew_interval=timedelta(seconds=2),
        )
        receipt = daemon.run()

    assert broker.connected is False
    assert hooks.handled == 2
    assert hooks.cycles == 3
    assert hooks.heartbeats == 1
    assert receipt.event_count == 2
    assert receipt.decision_count == 1
    assert receipt.writer_renewals >= 2
    assert receipt.real_order_calls == 0
    assert receipt.stopped_cleanly is True


def test_daemon_halts_before_cycle_when_event_pump_requires_halt(tmp_path: Path) -> None:
    clock = Clock()
    hooks = Hooks()
    broker = Broker()
    pump = Pump()
    pump.halt_required = True
    pump.halt_reason = "BROKER_EVENT_QUEUE_OVERFLOW"

    with WriterLease.acquire(
        tmp_path / "firmquant.sqlite3",
        owner="production-test",
        clock=clock,
    ) as writer:
        daemon = ProductionDaemon(
            mode=Mode.SHADOW,
            writer=writer,
            broker=broker,
            pump=pump,
            hooks=hooks,
            clock=clock,
            monotonic_clock=clock.monotonic,
            sleep=clock.sleep,
            stop_requested=lambda: False,
        )
        with pytest.raises(ProductionDaemonHalted, match="BROKER_EVENT_QUEUE_OVERFLOW"):
            daemon.run()

    assert hooks.cycles == 0
    assert hooks.halted == ["BROKER_EVENT_QUEUE_OVERFLOW"]
    assert broker.connected is False


def test_daemon_rejects_nonproduction_mode(tmp_path: Path) -> None:
    clock = Clock()
    with (
        WriterLease.acquire(
            tmp_path / "firmquant.sqlite3",
            owner="production-test",
            clock=clock,
        ) as writer,
        pytest.raises(ValueError, match="production mode"),
    ):
        ProductionDaemon(
            mode=Mode.PAPER,
            writer=writer,
            broker=Broker(),
            pump=Pump(),
            hooks=Hooks(),
            clock=clock,
            monotonic_clock=clock.monotonic,
            sleep=clock.sleep,
            stop_requested=lambda: True,
        )
