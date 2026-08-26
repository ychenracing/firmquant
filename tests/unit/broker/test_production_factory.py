from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from firmquant.broker.economic_identity import EconomicIdentityBroker
from firmquant.broker.production_factory import (
    ProductionBrokerConfigurationError,
    build_production_xtquant_gateway,
)
from firmquant.config import BrokerAdapter, BrokerSettings, Mode, Settings
from firmquant.persistence.database import Database
from tests.fixtures.xtquant_sdk_fake import OfficialSdkModules

NOW = datetime(2026, 8, 25, 1, 31, tzinfo=UTC)


def safety_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "source_name": "reviewed-local-probe",
        "source_sha256": "a" * 64,
        "probe_symbol": "sh600519",
        "equity_product_types": [201],
        "trading_instrument_statuses": [0],
        "open_stock_statuses": [3],
        "auction_stock_statuses": [1, 2],
        "break_stock_statuses": [4],
        "closed_stock_statuses": [0, 5],
        "trading_units": {"SH": 100, "SZ": 100, "BJ": 100},
        "volume_multipliers": {"SH": 100, "SZ": 100, "BJ": 100},
        "commission_rate": "0.0003",
        "minimum_commission": "5",
        "stamp_duty_rate": "0.0005",
        "transfer_fee_rate": "0.00001",
    }


def shadow_settings(tmp_path: Path) -> Settings:
    userdata = tmp_path / "userdata"
    userdata.mkdir()
    manifest = tmp_path / "safety.json"
    manifest.write_text(json.dumps(safety_payload()), encoding="utf-8")
    return Settings(
        mode=Mode.SHADOW,
        live_trading_enabled=False,
        broker=BrokerSettings(
            adapter=BrokerAdapter.XTQUANT,
            account_alias="account-001",
            xtquant_userdata_path=userdata,
            session_id=123456,
            safety_manifest_path=manifest,
        ),
    )


def test_factory_builds_identity_wrapped_official_sdk_gateway(tmp_path: Path) -> None:
    modules = OfficialSdkModules()
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        gateway = build_production_xtquant_gateway(
            settings=shadow_settings(tmp_path),
            database=database,
            clock=lambda: NOW,
            importer=modules.importer,
        )
        assert isinstance(gateway, EconomicIdentityBroker)
        gateway.connect()
        try:
            assert gateway.query_account().account_id_hash
            assert gateway.health().connected is True
        finally:
            gateway.disconnect()
        assert set(modules.imported) >= {
            "xtquant.xtdata",
            "xtquant.xtconstant",
            "xtquant.xttrader",
            "xtquant.xttype",
        }
    finally:
        database.close()


def test_factory_rejects_nonproduction_mode_and_missing_runtime_paths(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        with pytest.raises(ProductionBrokerConfigurationError, match="SHADOW/CANARY/LIVE"):
            build_production_xtquant_gateway(
                settings=Settings(),
                database=database,
                clock=lambda: NOW,
            )

        settings = Settings(
            mode=Mode.SHADOW,
            broker=BrokerSettings(adapter=BrokerAdapter.XTQUANT),
        )
        with pytest.raises(ProductionBrokerConfigurationError, match="incomplete"):
            build_production_xtquant_gateway(
                settings=settings,
                database=database,
                clock=lambda: NOW,
            )
    finally:
        database.close()
