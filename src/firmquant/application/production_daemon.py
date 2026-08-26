"""Long-running single-writer production event loop for SHADOW/CANARY/LIVE."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from firmquant.application.production_runtime import ProductionRuntime, ProductionRuntimeReceipt
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

    def subscribe(self, callback_sink: object) -> None: ...


@runtime_checkable
class ProductionEventPump(Protocol):
    @property
    def sink(self) -> object: ...

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
        self._mode = mode
        self._writer = writer
        self._broker = broker
        self._pump = pump
        self._hooks = hooks
        self._clock = clock
        self._sleep = sleep
        self._stop_requested = stop_requested
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

    def run(self) -> ProductionRuntimeReceipt:
        self._writer.assert_current()
        started_at = self._now()
        last_renewed_at = started_at
        writer_renewals = 0
        event_count = 0
        decision_count = 0
        execution_count = 0
        eod_count = 0
        startup_reconciliation_id = ""
        connected = False
        try:
            self._broker.connect()
            connected = True
            self._broker.subscribe(self._pump.sink)
            startup_reconciliation_id = self._reconciliation_id(self._hooks.startup())
            while True:
                now = self._now()
                if now - last_renewed_at >= self._renew_interval:
                    self._writer.renew()
                    writer_renewals += 1
                    last_renewed_at = now
                else:
                    self._writer.assert_current()

                self._halt_if_required()
                event_count += self._drain_events()
                self._halt_if_required()

                cycle = self._hooks.cycle(now)
                if not isinstance(cycle, ProductionCycleResult):
                    raise ProductionDaemonHalted("production cycle result is invalid")
                decision_count += cycle.decisions
                execution_count += cycle.executions
                eod_count += cycle.eod
                heartbeat = ProductionHeartbeat(
                    mode=self._mode,
                    observed_at=now,
                    writer_generation=self._writer.generation,
                    pending_events=self._pump.pending_count,
                    processed_events=event_count,
                    decisions=decision_count,
                    executions=execution_count,
                    eod=eod_count,
                )
                self._hooks.heartbeat(heartbeat)
                if self._stop_requested():
                    break
                self._sleep(self._poll_interval.total_seconds())
        except ProductionDaemonHalted:
            raise
        except Exception as error:
            with suppress(Exception):
                self._hooks.halt("PRODUCTION_RUNTIME_EXCEPTION")
            raise ProductionDaemonHalted("PRODUCTION_RUNTIME_EXCEPTION") from error
        finally:
            if connected:
                with suppress(Exception):
                    self._broker.disconnect()

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
            real_order_calls=self._hooks.real_order_calls(),
            stopped_cleanly=True,
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
