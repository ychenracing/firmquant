"""Fail-closed production XtQuant gateway construction from local deployment facts."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from importlib import import_module
from pathlib import Path
from typing import Protocol

from firmquant.config import BrokerAdapter, Mode, Settings
from firmquant.persistence.database import Database

from .economic_identity import EconomicIdentityBroker
from .gateway import BrokerGateway
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


def build_production_xtquant_gateway(
    *,
    settings: Settings,
    database: Database,
    clock: Callable[[], datetime],
    importer: ModuleImporter = _default_importer,
) -> BrokerGateway:
    """Construct the only production XtQuant gateway with stable economic identity."""

    if not isinstance(settings, Settings):
        raise TypeError("production broker settings must be Settings")
    if not isinstance(database, Database):
        raise TypeError("production broker database must be Database")
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
        raise BrokerDependencyMissing("official XtQuant SDK modules are unavailable") from error
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
    transport = ProductionXtQuantBroker.load_sdk(
        userdata_path=userdata,
        session_id=session_id,
        account_id=account_id,
        clock=clock,
        importer=importer,
        safety_facts=safety,
    )
    return EconomicIdentityBroker(gateway=transport, database=database)


__all__ = (
    "ProductionBrokerConfigurationError",
    "build_production_xtquant_gateway",
)
