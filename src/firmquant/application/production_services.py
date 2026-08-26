"""Concrete production-service composition used by the long-running daemon."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from firmquant.application.production_runtime import ProductionRuntime
from firmquant.config import Mode, Settings
from firmquant.persistence.writer_lease import WriterLease


class ProductionServicesUnavailable(RuntimeError):
    """Required production authority facts or services cannot be proven safe."""


def build_production_runtime(
    *,
    config_path: Path,
    settings: Settings,
    writer: WriterLease,
    clock: Callable[[], datetime],
) -> ProductionRuntime:
    """Build only fully-authorized production modes; concrete services fail closed until composed."""

    if not isinstance(config_path, Path):
        raise TypeError("production services config path must be Path")
    if not isinstance(settings, Settings):
        raise TypeError("production services settings must be Settings")
    if settings.mode not in {Mode.SHADOW, Mode.CANARY, Mode.LIVE}:
        raise ProductionServicesUnavailable("production services require SHADOW/CANARY/LIVE")
    if not isinstance(writer, WriterLease):
        raise TypeError("production services require active WriterLease")
    if not callable(clock):
        raise TypeError("production services clock must be callable")
    writer.assert_current()
    raise ProductionServicesUnavailable("PRODUCTION_SERVICES_NOT_COMPOSED")


__all__ = (
    "ProductionServicesUnavailable",
    "build_production_runtime",
)
