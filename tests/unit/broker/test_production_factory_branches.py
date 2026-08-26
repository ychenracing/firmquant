from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import firmquant.broker.production_factory as factory
import tests.unit.application.test_production_services_acceptance as base
from firmquant.broker.xtquant import BrokerDependencyMissing
from firmquant.config import BrokerAdapter, Mode, Settings
from firmquant.persistence.database import Database
from tests.fixtures.session_cases import NOW


def test_resolved_directory_rejects_missing_path_and_resolution_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(factory.ProductionBrokerConfigurationError, match="existing"):
        factory._resolved_directory(missing, label="userdata")

    directory = tmp_path / "userdata"
    directory.mkdir()
    original = Path.resolve

    def fail_resolve(path: Path, *args, **kwargs):
        if path == directory:
            raise OSError("resolve failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fail_resolve)
    with pytest.raises(factory.ProductionBrokerConfigurationError, match="cannot be resolved"):
        factory._resolved_directory(directory, label="userdata")


def test_factory_rejects_invalid_types_modes_adapter_and_prerequisites(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    settings, _config = base.settings_for(tmp_path / "prod", Mode.SHADOW)
    try:
        with pytest.raises(TypeError, match="settings must be Settings"):
            factory.build_production_xtquant_gateway(
                settings=object(),  # type: ignore[arg-type]
                database=database,
                clock=lambda: NOW,
            )
        with pytest.raises(TypeError, match="database must be Database"):
            factory.build_production_xtquant_gateway(
                settings=settings,
                database=object(),  # type: ignore[arg-type]
                clock=lambda: NOW,
            )
        with pytest.raises(TypeError, match="clock/importer"):
            factory.build_production_xtquant_gateway(
                settings=settings,
                database=database,
                clock=None,  # type: ignore[arg-type]
            )
        with pytest.raises(factory.ProductionBrokerConfigurationError, match="SHADOW/CANARY/LIVE"):
            factory.build_production_xtquant_gateway(
                settings=Settings(),
                database=database,
                clock=lambda: NOW,
            )

        paper_broker = settings.broker.model_copy(update={"adapter": BrokerAdapter.PAPER})
        paper_settings = settings.model_copy(update={"broker": paper_broker})
        with pytest.raises(factory.ProductionBrokerConfigurationError, match="XTQUANT adapter"):
            factory.build_production_xtquant_gateway(
                settings=paper_settings,
                database=database,
                clock=lambda: NOW,
            )

        incomplete_broker = settings.broker.model_copy(update={"account_alias": None})
        incomplete = settings.model_copy(update={"broker": incomplete_broker})
        with pytest.raises(factory.ProductionBrokerConfigurationError, match="prerequisites"):
            factory.build_production_xtquant_gateway(
                settings=incomplete,
                database=database,
                clock=lambda: NOW,
            )
    finally:
        database.close()


def test_factory_classifies_sdk_manifest_failures_and_composes_identity_wrapper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, _config = base.settings_for(tmp_path / "prod", Mode.SHADOW)
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        def missing_import(_name: str) -> object:
            raise ModuleNotFoundError("xtquant")

        with pytest.raises(BrokerDependencyMissing, match="SDK modules"):
            factory.build_production_xtquant_gateway(
                settings=settings,
                database=database,
                clock=lambda: NOW,
                importer=missing_import,
            )

        importer = lambda name: SimpleNamespace(name=name)
        monkeypatch.setattr(
            factory.XtQuantSafetyManifest,
            "load",
            lambda _path: (_ for _ in ()).throw(ValueError("bad manifest")),
        )
        with pytest.raises(factory.ProductionBrokerConfigurationError, match="manifest is invalid"):
            factory.build_production_xtquant_gateway(
                settings=settings,
                database=database,
                clock=lambda: NOW,
                importer=importer,
            )

        manifest = base.safety_manifest()
        safety = object()
        transport = object()
        wrapped = object()
        monkeypatch.setattr(factory.XtQuantSafetyManifest, "load", lambda _path: manifest)
        monkeypatch.setattr(factory, "ManifestXtQuantSafetyProvider", lambda **_kwargs: safety)
        monkeypatch.setattr(
            factory.ProductionXtQuantBroker,
            "load_sdk",
            lambda **kwargs: transport if kwargs["safety_facts"] is safety else None,
        )
        monkeypatch.setattr(
            factory,
            "EconomicIdentityBroker",
            lambda *, gateway, database: wrapped
            if gateway is transport and isinstance(database, Database)
            else None,
        )
        result = factory.build_production_xtquant_gateway(
            settings=settings,
            database=database,
            clock=lambda: NOW,
            importer=importer,
        )
        assert result is wrapped
    finally:
        database.close()
