from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from firmquant.broker.gateway import BrokerFactUnavailable
from firmquant.broker.xtquant import BrokerSchemaMismatch
from firmquant.broker.xtquant_safety import (
    ManifestXtQuantSafetyProvider,
    XtQuantSafetyManifest,
)
from firmquant.domain.broker_facts import MarketSessionStatus, SecurityStatus, SecurityType
from firmquant.domain.values import Money, Shares, Symbol

NOW = datetime(2026, 8, 25, 1, 31, tzinfo=UTC)
SYMBOL = Symbol.parse("sh600519")


def manifest_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "source_name": "reviewed-local-probe",
        "source_sha256": "a" * 64,
        "probe_symbol": SYMBOL.canonical,
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


def write_manifest(path: Path) -> XtQuantSafetyManifest:
    path.write_text(json.dumps(manifest_payload()), encoding="utf-8")
    return XtQuantSafetyManifest.load(path)


def test_manifest_loads_exact_schema_and_has_stable_identity(tmp_path: Path) -> None:
    manifest = write_manifest(tmp_path / "safety.json")

    assert manifest.probe_symbol == SYMBOL
    assert manifest.trading_units["SH"] == 100
    assert manifest.sha256 == manifest.sha256
    assert len(manifest.sha256) == 64

    invalid = tmp_path / "invalid.json"
    payload = manifest_payload()
    payload["unexpected"] = True
    invalid.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="schema"):
        XtQuantSafetyManifest.load(invalid)


def test_manifest_provider_maps_reviewed_market_instrument_fee_and_volume_facts(tmp_path: Path) -> None:
    manifest = write_manifest(tmp_path / "safety.json")
    tick = {SYMBOL.xtquant: {"stockStatus": 3}}
    provider = ManifestXtQuantSafetyProvider(
        xtdata=SimpleNamespace(get_full_tick=lambda _symbols: tick),
        xtconstant=SimpleNamespace(STOCK_BUY=23, STOCK_SELL=24),
        manifest=manifest,
        clock=lambda: NOW,
    )

    assert provider.market_status() is MarketSessionStatus.OPEN
    instrument = provider.instrument_safety(
        SYMBOL,
        {"ProductType": 201, "InstrumentStatus": 0, "IsTrading": True},
    )
    assert instrument.security_type is SecurityType.EQUITY
    assert instrument.status is SecurityStatus.TRADING
    assert instrument.trading_unit == Shares(100)

    sell = SimpleNamespace(traded_price=10.0, traded_volume=100, order_type=24)
    fees = provider.fill_fees(sell)
    assert fees.commission == Money(Decimal("5.0000"))
    assert fees.stamp_duty == Money(Decimal("0.5000"))
    assert fees.transfer_fee == Money(Decimal("0.0100"))
    assert provider.quote_volume_shares(SYMBOL, {"volume": 1000}) == Shares(100_000)


def test_manifest_provider_fails_closed_on_unreviewed_or_malformed_facts(tmp_path: Path) -> None:
    manifest = write_manifest(tmp_path / "safety.json")
    tick: dict[str, object] = {SYMBOL.xtquant: {"stockStatus": 999}}
    provider = ManifestXtQuantSafetyProvider(
        xtdata=SimpleNamespace(get_full_tick=lambda _symbols: tick),
        xtconstant=SimpleNamespace(STOCK_BUY=23, STOCK_SELL=24),
        manifest=manifest,
        clock=lambda: NOW,
    )

    assert provider.market_status() is MarketSessionStatus.UNKNOWN
    unknown = provider.instrument_safety(
        SYMBOL,
        {"ProductType": 999, "InstrumentStatus": 9, "IsTrading": False},
    )
    assert unknown.security_type is SecurityType.UNKNOWN
    assert unknown.status is SecurityStatus.SUSPENDED

    with pytest.raises(BrokerSchemaMismatch):
        provider.quote_volume_shares(SYMBOL, {"volume": 1.5})
    with pytest.raises(BrokerSchemaMismatch):
        provider.fill_fees(SimpleNamespace(traded_price=10.0, traded_volume=100, order_type=999))

    missing = ManifestXtQuantSafetyProvider(
        xtdata=SimpleNamespace(),
        xtconstant=SimpleNamespace(STOCK_BUY=23, STOCK_SELL=24),
        manifest=manifest,
        clock=lambda: NOW,
    )
    with pytest.raises(BrokerFactUnavailable):
        missing.market_status()
