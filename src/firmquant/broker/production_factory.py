"""Fail-closed production XtQuant gateway construction from local deployment facts."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from importlib import import_module
from pathlib import Path
from typing import Protocol

from firmquant.config import BrokerAdapter, Mode, Settings
from firmquant.domain.broker_facts import (
    BrokerAccountFact,
    BrokerFillFact,
    BrokerOrderFact,
    BrokerPositionFact,
    InstrumentFact,
    MarketSessionStatus,
    QuoteFact,
)
from firmquant.domain.values import Symbol
from firmquant.persistence.database import Database

from .economic_identity import EconomicIdentityBroker
from .gateway import BrokerGateway, BrokerHealth
from .xtquant import BrokerDependencyMissing
from .xtquant_production import ProductionXtQuantBroker
from .xtquant_safety import ManifestXtQuantSafetyProvider, XtQuantSafetyManifest


class ModuleImporter(Protocol):
    def __call__(self, module_name: str) -> object: ...


class ProductionBrokerConfigurationError(RuntimeError):
    """Local XtQuant deployment prerequisites are missing or contradictory."""


def _default_importer(module_name: str) -> object:
    return import_module(module_name)


def _resolved_directory(path: Path, *, label: str) -> Path:
    candidate = Path(path)
    if candidate.is_symlink() or not candidate.is_dir():
        raise ProductionBrokerConfigurationError(f"{label} must be an existing non-symlink directory")
    try:
        return candidate.resolve(strict=True)
    except OSError as error:
        raise ProductionBrokerConfigurationError(f"{label} cannot be resolved") from error


def _build_transport(
    *,
    settings: Settings,
    clock: Callable[[], datetime],
    importer: ModuleImporter,
) -> ProductionXtQuantBroker:
    if not isinstance(settings, Settings):
        raise TypeError("production broker settings must be Settings")
    if not callable(clock) or not callable(importer):
        raise TypeError("production broker clock/importer must be callable")
    if settings.mode not in {Mode.SHADOW, Mode.CANARY, Mode.LIVE}:
        raise ProductionBrokerConfigurationError("XtQuant production gateway requires SHADOW/CANARY/LIVE")
    if settings.broker.adapter is not BrokerAdapter.XTQUANT:
        raise ProductionBrokerConfigurationError("production gateway requires XTQUANT adapter")
    account_id = settings.broker.account_alias
    userdata = settings.broker.xtquant_userdata_path
    session_id = settings.broker.session_id
    manifest_path = settings.broker.safety_manifest_path
    if account_id is None or userdata is None or session_id is None or manifest_path is None:
        raise ProductionBrokerConfigurationError("XtQuant broker prerequisites are incomplete")
    userdata = _resolved_directory(userdata, label="XtQuant userdata")
    try:
        xtdata = importer("xtquant.xtdata")
        xtconstant = importer("xtquant.xtconstant")
    except (ImportError, ModuleNotFoundError, KeyError) as error:
        raise BrokerDependencyMissing("XTQUANT_SDK_UNAVAILABLE") from error
    try:
        manifest = XtQuantSafetyManifest.load(manifest_path)
    except (OSError, ValueError) as error:
        raise ProductionBrokerConfigurationError("XtQuant safety manifest is invalid") from error
    safety = ManifestXtQuantSafetyProvider(
        xtdata=xtdata,
        xtconstant=xtconstant,
        manifest=manifest,
        clock=clock,
    )
    return ProductionXtQuantBroker.load_sdk(
        userdata_path=userdata,
        session_id=session_id,
        account_id=account_id,
        clock=clock,
        importer=importer,
        safety_facts=safety,
    )


class ReadOnlyXtQuantGateway:
    """Diagnostic XtQuant facade whose public type exposes no submit/cancel capability."""

    __slots__ = ("_transport",)

    def __init__(self, transport: ProductionXtQuantBroker) -> None:
        if not isinstance(transport, ProductionXtQuantBroker):
            raise TypeError("read-only XtQuant gateway requires official production transport")
        self._transport = transport

    def connect(self) -> None:
        self._transport.connect()

    def disconnect(self) -> None:
        self._transport.disconnect()

    def health(self) -> BrokerHealth:
        return self._transport.health()

    def query_account(self) -> BrokerAccountFact:
        return self._transport.query_account()

    def query_positions(self) -> tuple[BrokerPositionFact, ...]:
        return self._transport.query_positions()

    def query_orders(self) -> tuple[BrokerOrderFact, ...]:
        return self._transport.query_orders()

    def query_fills(self) -> tuple[BrokerFillFact, ...]:
        return self._transport.query_fills()

    def query_instrument(self, symbol: Symbol) -> InstrumentFact:
        return self._transport.query_instrument(symbol)

    def query_quote(self, symbol: Symbol) -> QuoteFact:
        return self._transport.query_quote(symbol)

    def query_market_status(self) -> MarketSessionStatus:
        return self._transport.query_market_status()


def build_readonly_xtquant_gateway(
    *,
    settings: Settings,
    clock: Callable[[], datetime],
    importer: ModuleImporter = _default_importer,
) -> ReadOnlyXtQuantGateway:
    """Build a fresh XtQuant read facade without a BrokerGateway/write interface."""

    return ReadOnlyXtQuantGateway(_build_transport(settings=settings, clock=clock, importer=importer))


def build_production_xtquant_gateway(
    *,
    settings: Settings,
    database: Database,
    clock: Callable[[], datetime],
    importer: ModuleImporter = _default_importer,
) -> BrokerGateway:
    """Construct the only production XtQuant gateway with stable economic identity."""

    if not isinstance(database, Database):
        raise TypeError("production broker database must be Database")
    transport = _build_transport(settings=settings, clock=clock, importer=importer)
    return EconomicIdentityBroker(gateway=transport, database=database)


__all__ = (
    "ProductionBrokerConfigurationError",
    "ReadOnlyXtQuantGateway",
    "build_production_xtquant_gateway",
    "build_readonly_xtquant_gateway",
)
