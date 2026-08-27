"""Long-running single-writer production event loop for SHADOW/CANARY/LIVE."""

from __future__ import annotations

import re
import time as time_module
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from firmquant.application.control_channel import ControlBatch, ControlInbox
from firmquant.application.production_runtime import ProductionRuntime, ProductionRuntimeReceipt
from firmquant.application.runtime_control import RuntimeControlExecutor
from firmquant.broker.gateway import BrokerEventSink, BrokerGateway
from firmquant.config import Mode, Settings
from firmquant.domain.states import RuntimeState
from firmquant.persistence.writer_lease import WriterLease, WriterLeaseGuard, WriterLeaseLost
from firmquant.scheduling.clock import (
    RuntimeClockDiscontinuity,
    RuntimeClockMonitor,
    RuntimeClockObservation,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ProductionDaemonHalted(RuntimeError):
    """The production loop failed closed and requires explicit reconciliation/resume."""


@dataclass(frozen=True, slots=True)
class ProductionCycleResult:
    decisions: int
    executions: int
    eod: int

    def __post_init__(self) -> None:
        for label, value in (
            ("decision count", self.decisions),
            ("execution count", self.executions),
            ("EOD count", self.eod),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{label} must be a nonnegative integer")


@dataclass(frozen=True, slots=True)
class ProductionHeartbeat:
    """One lightweight process-liveness observation; persistence is owned by hooks."""

    mode: Mode
    runtime_state: RuntimeState
    observed_at: datetime
    host_hash: str
    process_id: int
    writer_generation: int
    broker_connected: bool
    broker_read_healthy: bool
    broker_write_healthy: bool
    pending_events: int
    last_broker_event: datetime | None
    last_quote: datetime | None
    last_reconciliation: datetime | None
    last_decision: datetime | None
    last_execution: datetime | None
    control_request_state: str
    processed_events: int
    decisions: int
    executions: int
    eod: int

    def __post_init__(self) -> None:
        if self.mode not in {Mode.SHADOW, Mode.CANARY, Mode.LIVE}:
            raise ValueError("heartbeat mode must be a production mode")
        if not isinstance(self.runtime_state, RuntimeState):
            raise TypeError("heartbeat runtime state must be RuntimeState")
        if (
            not isinstance(self.observed_at, datetime)
            or self.observed_at.tzinfo is None
            or self.observed_at.utcoffset() is None
        ):
            raise ValueError("heartbeat time must be timezone-aware")
        if not isinstance(self.host_hash, str) or _SHA256.fullmatch(self.host_hash) is None:
            raise ValueError("heartbeat host hash must be SHA-256")
        for label, value in (
            ("process id", self.process_id),
            ("writer generation", self.writer_generation),
            ("pending events", self.pending_events),
            ("processed events", self.processed_events),
            ("decisions", self.decisions),
            ("executions", self.executions),
            ("EOD", self.eod),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"heartbeat {label} must be a nonnegative integer")
        for label, value in (
            ("broker connected", self.broker_connected),
            ("broker read health", self.broker_read_healthy),
            ("broker write health", self.broker_write_healthy),
        ):
            if type(value) is not bool:
                raise TypeError(f"heartbeat {label} must be bool")
        if self.broker_write_healthy and not self.broker_read_healthy:
            raise ValueError("heartbeat write health requires read health")
        for label, value in (
            ("last broker event", self.last_broker_event),
            ("last quote", self.last_quote),
            ("last reconciliation", self.last_reconciliation),
            ("last decision", self.last_decision),
            ("last execution", self.last_execution),
        ):
            if value is not None and (
                not isinstance(value, datetime)
                or value.tzinfo is None
                or value.utcoffset() is None
            ):
                raise ValueError(f"heartbeat {label} must be timezone-aware or null")
        if (
            not isinstance(self.control_request_state, str)
            or not self.control_request_state
            or self.control_request_state != self.control_request_state.strip()
            or len(self.control_request_state) > 64
        ):
            raise ValueError("heartbeat control request state must be canonical text")


@runtime_checkable
class ProductionBrokerSession(Protocol):
    """Read/event process capability. Real write capability is intentionally absent."""

    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def subscribe(self, callback_sink: BrokerEventSink) -> None: ...


@runtime_checkable
class ProductionEventPump(Protocol):
    @property
    def sink(self) -> BrokerEventSink: ...

    @property
    def pending_count(self) -> int: ...

    @property
    def halt_required(self) -> bool: ...

    @property
    def halt_reason(self) -> str | None: ...

    def dispatch_one(self, writer: Callable[[object], None]) -> bool: ...


@runtime_checkable
class ProductionHooks(Protocol):
    """Production use cases executed by the event loop; economics remain outside the daemon."""

    def startup(self) -> str: ...

    def handle_event(self, event: object) -> None: ...

    def cycle(self, now: datetime) -> ProductionCycleResult: ...

    def heartbeat(self, heartbeat: ProductionHeartbeat) -> None: ...

    def halt(self, reason_code: str) -> None: ...

    def real_order_calls(self) -> int: ...


def _aware(value: datetime, *, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware datetime")
    return value


def _positive_duration(value: timedelta, *, label: str) -> timedelta:
    if not isinstance(value, timedelta) or value <= timedelta(0):
        raise ValueError(f"{label} must be a positive timedelta")
    return value


class ProductionDaemon(ProductionRuntime):
    """Own one writer lease for the full process and serialize every durable callback."""

    def __init__(
        self,
        *,
        mode: Mode,
        writer: WriterLease,
        broker: ProductionBrokerSession,
        pump: ProductionEventPump,
        hooks: ProductionHooks,
        clock: Callable[[], datetime],
        sleep: Callable[[float], None],
        stop_requested: Callable[[], bool],
        monotonic_clock: Callable[[], float] = time_module.monotonic,
        control_inbox: ControlInbox | None = None,
        poll_interval: timedelta = timedelta(seconds=1),
        renew_interval: timedelta = timedelta(seconds=10),
        heartbeat_interval: timedelta = timedelta(seconds=10),
        max_events_per_cycle: int = 1024,
    ) -> None:
        if mode not in {Mode.SHADOW, Mode.CANARY, Mode.LIVE}:
            raise ValueError("production mode must be SHADOW, CANARY, or LIVE")
        if not isinstance(writer, WriterLease):
            raise TypeError("production daemon requires WriterLease")
        if not isinstance(broker, ProductionBrokerSession):
            raise TypeError("production daemon broker does not satisfy read/event session")
        if not isinstance(pump, ProductionEventPump):
            raise TypeError("production daemon event pump does not satisfy its contract")
        if not isinstance(hooks, ProductionHooks):
            raise TypeError("production daemon hooks do not satisfy their contract")
        if not all(callable(item) for item in (clock, sleep, stop_requested, monotonic_clock)):
            raise TypeError("production daemon clock/sleep/stop ports must be callable")
        if control_inbox is not None and not isinstance(control_inbox, ControlInbox):
            raise TypeError("production daemon control inbox must be ControlInbox")
        self._mode = mode
        self._writer = writer
        self._broker = broker
        self._pump = pump
        self._hooks = hooks
        self._clock = clock
        self._monotonic_clock = monotonic_clock
        self._sleep = sleep
        self._stop_requested = stop_requested
        self._control_inbox = control_inbox or ControlInbox(writer.database.path.parent, clock=clock)
        cancel_broker = broker if isinstance(broker, BrokerGateway) else None
        self._control_executor = RuntimeControlExecutor(
            mode=mode,
            writer=writer,
            broker=cancel_broker,
            clock=clock,
        )
        self._risk_blocked = False
        self._poll_interval = _positive_duration(poll_interval, label="poll interval")
        self._renew_interval = _positive_duration(renew_interval, label="writer renewal interval")
        self._heartbeat_interval = _positive_duration(heartbeat_interval, label="heartbeat interval")
        if self._heartbeat_interval < self._poll_interval:
            raise ValueError("heartbeat interval cannot be shorter than poll interval")
        self._lease_guard = WriterLeaseGuard(
            writer,
            monotonic_clock=monotonic_clock,
            renew_interval=self._renew_interval,
        )
        self._clock_monitor = RuntimeClockMonitor(
            max_observation_gap=timedelta(seconds=30),
            max_wall_monotonic_divergence=timedelta(seconds=3),
        )
        if isinstance(max_events_per_cycle, bool) or not isinstance(max_events_per_cycle, int):
            raise TypeError("maximum events per cycle must be an integer")
        if max_events_per_cycle <= 0:
            raise ValueError("maximum events per cycle must be positive")
        self._max_events_per_cycle = max_events_per_cycle

    def _now(self) -> datetime:
        return _aware(self._clock(), label="production daemon clock")

    def _monotonic(self) -> float:
        value = self._monotonic_clock()
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ProductionDaemonHalted("MONOTONIC_CLOCK_INVALID")
        return float(value)

    @staticmethod
    def _reconciliation_id(value: str) -> str:
        if (
            not isinstance(value, str)
            or not value.startswith("recon_")
            or len(value) != 70
            or any(character not in "0123456789abcdef" for character in value[6:])
        ):
            raise ProductionDaemonHalted("startup reconciliation identity is invalid")
        return value

    def _drain_events(self) -> int:
        processed = 0
        while processed < self._max_events_per_cycle and self._pump.dispatch_one(self._hooks.handle_event):
            processed += 1
        return processed

    def _halt_if_required(self) -> None:
        if not self._pump.halt_required:
            return
        reason = self._pump.halt_reason or "BROKER_EVENT_PUMP_HALTED"
        with suppress(Exception):
            self._hooks.halt(reason)
        raise ProductionDaemonHalted(reason)

    def _process_controls(self) -> ControlBatch:
        batch = self._control_inbox.process_pending(self._control_executor.execute)
        if batch.halted:
            self._risk_blocked = True
        return batch

    @staticmethod
    def _control_state(batch: ControlBatch) -> str:
        if batch.stop:
            return "STOP_PENDING"
        if batch.halted:
            return "HALTED"
        if not batch.receipts:
            return "IDLE"
        return batch.receipts[-1].status.value

    def _runtime_state(self) -> RuntimeState:
        status = getattr(self._hooks, "status", None)
        state = getattr(status, "state", None)
        return state if isinstance(state, RuntimeState) else RuntimeState.HALTED

    def _heartbeat(
        self,
        *,
        now: datetime,
        controls: ControlBatch,
        event_count: int,
        decision_count: int,
        execution_count: int,
        eod_count: int,
    ) -> None:
        connected = False
        read_healthy = False
        write_healthy = False
        if isinstance(self._broker, BrokerGateway):
            health = self._broker.health()
            connected = health.connected
            read_healthy = health.read_healthy
            write_healthy = health.write_healthy
        self._hooks.heartbeat(
            ProductionHeartbeat(
                mode=self._mode,
                runtime_state=self._runtime_state(),
                observed_at=now,
                host_hash=self._writer.host_hash,
                process_id=self._writer.process_id,
                writer_generation=self._writer.generation,
                broker_connected=connected,
                broker_read_healthy=read_healthy,
                broker_write_healthy=write_healthy,
                pending_events=self._pump.pending_count,
                last_broker_event=None,
                last_quote=None,
                last_reconciliation=None,
                last_decision=None,
                last_execution=None,
                control_request_state=self._control_state(controls),
                processed_events=event_count,
                decisions=decision_count,
                executions=execution_count,
                eod=eod_count,
            )
        )

    def _observe_runtime_clock(self, *, wall_time: datetime, monotonic_time: float) -> None:
        try:
            self._clock_monitor.observe(
                RuntimeClockObservation(
                    wall_time=wall_time,
                    monotonic_time=monotonic_time,
                )
            )
        except RuntimeClockDiscontinuity as error:
            self._risk_blocked = True
            with suppress(Exception):
                self._control_executor.execute_internal_stop(source=error.code)
            with suppress(Exception):
                self._hooks.halt(error.code)
            raise ProductionDaemonHalted(error.code) from error

    def run(self) -> ProductionRuntimeReceipt:
        self._writer.assert_current()
        started_at = self._now()
        started_monotonic = self._monotonic()
        self._observe_runtime_clock(wall_time=started_at, monotonic_time=started_monotonic)
        last_heartbeat_monotonic = started_monotonic - self._heartbeat_interval.total_seconds()
        writer_renewals = 0
        event_count = 0
        decision_count = 0
        execution_count = 0
        eod_count = 0
        startup_reconciliation_id: str | None = None
        connected = False
        clean_stop = False
        disconnect_error: Exception | None = None
        controls = ControlBatch(receipts=(), halted=False, stop=False)
        try:
            self._broker.connect()
            connected = True
            self._broker.subscribe(self._pump.sink)

            controls = self._process_controls()
            if not controls.stop and not self._risk_blocked:
                startup_reconciliation_id = self._reconciliation_id(self._hooks.startup())

            while not controls.stop:
                now = self._now()
                monotonic_now = self._monotonic()
                self._observe_runtime_clock(wall_time=now, monotonic_time=monotonic_now)
                previous_expiry = self._writer.expires_at
                self._lease_guard.check()
                if self._writer.expires_at != previous_expiry:
                    writer_renewals += 1

                controls = self._process_controls()
                if controls.stop:
                    break
                if self._stop_requested():
                    self._control_executor.execute_internal_stop()
                    self._risk_blocked = True
                    break

                self._halt_if_required()
                event_count += self._drain_events()
                self._halt_if_required()

                if not self._risk_blocked:
                    cycle = self._hooks.cycle(now)
                    if not isinstance(cycle, ProductionCycleResult):
                        raise ProductionDaemonHalted("production cycle result is invalid")
                    decision_count += cycle.decisions
                    execution_count += cycle.executions
                    eod_count += cycle.eod
                monotonic_after_cycle = self._monotonic()
                if (
                    monotonic_after_cycle - last_heartbeat_monotonic
                    >= self._heartbeat_interval.total_seconds()
                ):
                    heartbeat_now = self._now()
                    self._heartbeat(
                        now=heartbeat_now,
                        controls=controls,
                        event_count=event_count,
                        decision_count=decision_count,
                        execution_count=execution_count,
                        eod_count=eod_count,
                    )
                    last_heartbeat_monotonic = monotonic_after_cycle
                self._sleep(self._poll_interval.total_seconds())
            clean_stop = True
        except WriterLeaseLost as error:
            # A lost lease may no longer persist HALT safely. Broker writes are already fenced by
            # the same guard; stale heartbeat forces recovery on the next local start.
            raise ProductionDaemonHalted("WRITER_LEASE_LOST") from error
        except ProductionDaemonHalted:
            raise
        except Exception as error:
            with suppress(Exception):
                self._hooks.halt("PRODUCTION_RUNTIME_EXCEPTION")
            raise ProductionDaemonHalted("PRODUCTION_RUNTIME_EXCEPTION") from error
        finally:
            if connected:
                try:
                    self._broker.disconnect()
                except Exception as error:
                    disconnect_error = error

        if disconnect_error is not None:
            with suppress(Exception):
                self._hooks.halt("BROKER_DISCONNECT_FAILED")
            raise ProductionDaemonHalted("BROKER_DISCONNECT_FAILED") from disconnect_error
        if self._control_executor.stop_pending:
            self._control_executor.finalize_stop()

        stopped_at = self._now()
        return ProductionRuntimeReceipt(
            mode=self._mode,
            started_at=started_at,
            stopped_at=stopped_at,
            startup_reconciliation_id=startup_reconciliation_id,
            event_count=event_count,
            decision_count=decision_count,
            execution_count=execution_count,
            eod_count=eod_count,
            writer_renewals=writer_renewals,
            real_order_calls=self._hooks.real_order_calls() + self._control_executor.cancel_calls,
            stopped_cleanly=clean_stop,
        )


def create_production_runtime(
    *,
    config_path: object,
    settings: Settings,
    writer: WriterLease,
    clock: Callable[[], datetime],
) -> ProductionRuntime:
    """Compose the concrete production services lazily to keep PAPER imports inert."""

    from pathlib import Path

    from firmquant.application.production_services import build_production_runtime

    if not isinstance(config_path, Path):
        raise TypeError("production runtime config path must be Path")
    return build_production_runtime(
        config_path=config_path,
        settings=settings,
        writer=writer,
        clock=clock,
    )


__all__ = (
    "ProductionCycleResult",
    "ProductionDaemon",
    "ProductionDaemonHalted",
    "ProductionHeartbeat",
    "ProductionHooks",
    "create_production_runtime",
)
