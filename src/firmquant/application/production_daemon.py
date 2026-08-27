"""Long-running single-writer production event loop for SHADOW/CANARY/LIVE."""

from __future__ import annotations

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
from firmquant.persistence.writer_lease import WriterLease


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
    mode: Mode
    observed_at: datetime
    writer_generation: int
    pending_events: int
    processed_events: int
    decisions: int
    executions: int
    eod: int

    def __post_init__(self) -> None:
        if self.mode not in {Mode.SHADOW, Mode.CANARY, Mode.LIVE}:
            raise ValueError("heartbeat mode must be a production mode")
        if (
            not isinstance(self.observed_at, datetime)
            or self.observed_at.tzinfo is None
            or self.observed_at.utcoffset() is None
        ):
            raise ValueError("heartbeat time must be timezone-aware")
        for label, value in (
            ("writer generation", self.writer_generation),
            ("pending events", self.pending_events),
            ("processed events", self.processed_events),
            ("decisions", self.decisions),
            ("executions", self.executions),
            ("EOD", self.eod),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"heartbeat {label} must be a nonnegative integer")


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
        control_inbox: ControlInbox | None = None,
        poll_interval: timedelta = timedelta(seconds=1),
        renew_interval: timedelta = timedelta(seconds=10),
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
        if not all(callable(item) for item in (clock, sleep, stop_requested)):
            raise TypeError("production daemon clock/sleep/stop ports must be callable")
        if control_inbox is not None and not isinstance(control_inbox, ControlInbox):
            raise TypeError("production daemon control inbox must be ControlInbox")
        self._mode = mode
        self._writer = writer
        self._broker = broker
        self._pump = pump
        self._hooks = hooks
        self._clock = clock
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
        if isinstance(max_events_per_cycle, bool) or not isinstance(max_events_per_cycle, int):
            raise TypeError("maximum events per cycle must be an integer")
        if max_events_per_cycle <= 0:
            raise ValueError("maximum events per cycle must be positive")
        self._max_events_per_cycle = max_events_per_cycle

    def _now(self) -> datetime:
        return _aware(self._clock(), label="production daemon clock")

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

    def _heartbeat(
        self,
        *,
        now: datetime,
        event_count: int,
        decision_count: int,
        execution_count: int,
        eod_count: int,
    ) -> None:
        self._hooks.heartbeat(
            ProductionHeartbeat(
                mode=self._mode,
                observed_at=now,
                writer_generation=self._writer.generation,
                pending_events=self._pump.pending_count,
                processed_events=event_count,
                decisions=decision_count,
                executions=execution_count,
                eod=eod_count,
            )
        )

    def run(self) -> ProductionRuntimeReceipt:
        self._writer.assert_current()
        started_at = self._now()
        last_renewed_at = started_at
        writer_renewals = 0
        event_count = 0
        decision_count = 0
        execution_count = 0
        eod_count = 0
        startup_reconciliation_id: str | None = None
        connected = False
        clean_stop = False
        disconnect_error: Exception | None = None
        try:
            self._broker.connect()
            connected = True
            self._broker.subscribe(self._pump.sink)

            initial_controls = self._process_controls()
            if not initial_controls.stop and not self._risk_blocked:
                startup_reconciliation_id = self._reconciliation_id(self._hooks.startup())

            while not initial_controls.stop:
                now = self._now()
                if now - last_renewed_at >= self._renew_interval:
                    self._writer.renew()
                    writer_renewals += 1
                    last_renewed_at = now
                else:
                    self._writer.assert_current()

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
                self._heartbeat(
                    now=now,
                    event_count=event_count,
                    decision_count=decision_count,
                    execution_count=execution_count,
                    eod_count=eod_count,
                )
                self._sleep(self._poll_interval.total_seconds())
            clean_stop = True
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
