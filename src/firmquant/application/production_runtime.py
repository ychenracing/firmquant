"""Long-running production runtime boundary and durable completion receipt."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from firmquant.config import Mode, Settings
from firmquant.persistence.repositories import canonical_sha256
from firmquant.persistence.writer_lease import WriterLease


def _aware(value: datetime, *, label: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware datetime")


def _count(value: int, *, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")


@dataclass(frozen=True, slots=True)
class ProductionRuntimeReceipt:
    """Safe, deterministic summary returned only after a production process stops."""

    mode: Mode
    started_at: datetime
    stopped_at: datetime
    startup_reconciliation_id: str
    event_count: int
    decision_count: int
    execution_count: int
    eod_count: int
    writer_renewals: int
    real_order_calls: int
    stopped_cleanly: bool

    def __post_init__(self) -> None:
        if self.mode not in {Mode.SHADOW, Mode.CANARY, Mode.LIVE}:
            raise ValueError("production runtime mode must be SHADOW, CANARY, or LIVE")
        _aware(self.started_at, label="production runtime start")
        _aware(self.stopped_at, label="production runtime stop")
        if self.stopped_at < self.started_at:
            raise ValueError("production runtime stopped before it started")
        if (
            not isinstance(self.startup_reconciliation_id, str)
            or not self.startup_reconciliation_id.startswith("recon_")
            or len(self.startup_reconciliation_id) != 70
        ):
            raise ValueError("production runtime reconciliation id is not canonical")
        for label, value in (
            ("event count", self.event_count),
            ("decision count", self.decision_count),
            ("execution count", self.execution_count),
            ("EOD count", self.eod_count),
            ("writer renewal count", self.writer_renewals),
            ("real order call count", self.real_order_calls),
        ):
            _count(value, label=label)
        if type(self.stopped_cleanly) is not bool:
            raise TypeError("production runtime clean-stop flag must be bool")
        if self.mode is Mode.SHADOW and self.real_order_calls != 0:
            raise ValueError("SHADOW runtime cannot report real broker writes")

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.payload())

    def payload(self) -> Mapping[str, object]:
        return {
            "schema": "firmquant.production-runtime-receipt.v1",
            "mode": self.mode.value,
            "started_at": self.started_at.isoformat(),
            "stopped_at": self.stopped_at.isoformat(),
            "startup_reconciliation_id": self.startup_reconciliation_id,
            "event_count": self.event_count,
            "decision_count": self.decision_count,
            "execution_count": self.execution_count,
            "eod_count": self.eod_count,
            "writer_renewals": self.writer_renewals,
            "real_order_calls": self.real_order_calls,
            "stopped_cleanly": self.stopped_cleanly,
        }


@runtime_checkable
class ProductionRuntime(Protocol):
    def run(self) -> ProductionRuntimeReceipt: ...


ProductionRuntimeFactory = Callable[[Settings, WriterLease], ProductionRuntime]


__all__ = (
    "ProductionRuntime",
    "ProductionRuntimeFactory",
    "ProductionRuntimeReceipt",
)
