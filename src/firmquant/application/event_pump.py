"""Bounded callback queue keeping broker threads away from durable state mutation."""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable, Mapping
from datetime import datetime

from firmquant.broker.gateway import BrokerEventSink
from firmquant.broker.normalization import BrokerEventEnvelope, normalize_broker_event
from firmquant.domain.errors import DomainTypeError, DomainValidationError


class BrokerEventQueueOverflow(RuntimeError):
    """Raised after retaining overflow evidence and marking the runtime for HALT."""


class BrokerEventPumpHalted(RuntimeError):
    """Raised when a callback arrives after the event pump has failed closed."""


class BrokerEventWriterViolation(RuntimeError):
    """Raised if more than one thread attempts to consume the callback queue."""


class _QueueSink:
    def __init__(self, pump: DomainEventPump) -> None:
        self._pump = pump

    def __call__(self, untrusted_event: Mapping[str, object]) -> None:
        self._pump._accept(untrusted_event)


class DomainEventPump:
    """Validate callbacks, enqueue them, and expose one serial writer dispatch path."""

    def __init__(self, *, capacity: int, clock: Callable[[], datetime]) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int):
            raise DomainTypeError("broker event queue capacity must be an integer")
        if capacity <= 0:
            raise DomainValidationError("broker event queue capacity must be positive")
        if not callable(clock):
            raise DomainTypeError("broker event clock must be callable")
        self._queue: queue.Queue[BrokerEventEnvelope] = queue.Queue(maxsize=capacity)
        self._clock = clock
        self._sink: BrokerEventSink = _QueueSink(self)
        self._halt = threading.Event()
        self._state_lock = threading.Lock()
        self._halt_reason: str | None = None
        self._overflow_envelope: BrokerEventEnvelope | None = None
        self._failed_envelope: BrokerEventEnvelope | None = None
        self._writer_thread_id: int | None = None

    @property
    def sink(self) -> BrokerEventSink:
        return self._sink

    @property
    def pending_count(self) -> int:
        return self._queue.qsize()

    @property
    def halt_required(self) -> bool:
        return self._halt.is_set()

    @property
    def halt_reason(self) -> str | None:
        with self._state_lock:
            return self._halt_reason

    @property
    def overflow_envelope(self) -> BrokerEventEnvelope | None:
        with self._state_lock:
            return self._overflow_envelope

    @property
    def failed_envelope(self) -> BrokerEventEnvelope | None:
        with self._state_lock:
            return self._failed_envelope

    def _mark_halt(self, reason: str) -> None:
        with self._state_lock:
            if self._halt_reason is None:
                self._halt_reason = reason
            self._halt.set()

    def _accept(self, untrusted_event: Mapping[str, object]) -> None:
        if self._halt.is_set():
            raise BrokerEventPumpHalted("broker event pump is halted; reconciliation required")
        try:
            envelope = normalize_broker_event(untrusted_event, received_at=self._clock())
        except (DomainTypeError, DomainValidationError):
            self._mark_halt("BROKER_EVENT_NORMALIZATION_FAILED")
            raise
        try:
            self._queue.put_nowait(envelope)
        except queue.Full as error:
            with self._state_lock:
                self._overflow_envelope = envelope
            self._mark_halt("BROKER_EVENT_QUEUE_OVERFLOW")
            raise BrokerEventQueueOverflow(
                "broker callback queue is full; halt required before reconciliation"
            ) from error

    def _bind_writer(self) -> None:
        current = threading.get_ident()
        with self._state_lock:
            if self._writer_thread_id is None:
                self._writer_thread_id = current
            elif self._writer_thread_id != current:
                self._halt_reason = self._halt_reason or "BROKER_EVENT_MULTIPLE_WRITERS"
                self._halt.set()
                raise BrokerEventWriterViolation("broker events must be dispatched by one writer thread")

    def dispatch_one(self, writer: Callable[[BrokerEventEnvelope], None]) -> bool:
        """Dispatch one queued event; callback threads never invoke ``writer``."""

        if not callable(writer):
            raise DomainTypeError("broker event writer must be callable")
        self._bind_writer()
        try:
            envelope = self._queue.get_nowait()
        except queue.Empty:
            return False
        try:
            writer(envelope)
        except Exception:
            with self._state_lock:
                self._failed_envelope = envelope
            self._mark_halt("BROKER_EVENT_WRITER_FAILED")
            raise
        else:
            self._queue.task_done()
            return True


__all__ = (
    "BrokerEventPumpHalted",
    "BrokerEventQueueOverflow",
    "BrokerEventWriterViolation",
    "DomainEventPump",
)
