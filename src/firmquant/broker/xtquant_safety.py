"""Deployment-verified XtQuant facts that the public SDK does not authoritatively standardize."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

from firmquant.domain.broker_facts import MarketSessionStatus, SecurityStatus, SecurityType
from firmquant.domain.values import Money, Shares, Symbol

from .gateway import BrokerFactUnavailable
from .xtquant import (
    BrokerSchemaMismatch,
    XtQuantFeeBreakdown,
    XtQuantInstrumentSafety,
    XtQuantSafetyFactProvider,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _decimal(value: object, *, label: str) -> Decimal:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a canonical decimal string")
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{label} must be decimal-compatible") from error
    if not result.is_finite() or result < 0:
        raise ValueError(f"{label} must be finite and nonnegative")
    return result


def _int_set(value: object, *, label: str) -> frozenset[int]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} must be a non-empty integer array")
    result: set[int] = set()
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            raise ValueError(f"{label} must contain integers")
        result.add(item)
    return frozenset(result)


def _positive_mapping(value: object, *, label: str) -> Mapping[str, int]:
    if not isinstance(value, dict) or not value:
        raise ValueError(f"{label} must be a non-empty object")
    result: dict[str, int] = {}
    for key, item in value.items():
        if key not in {"SH", "SZ", "BJ"}:
            raise ValueError(f"{label} contains unsupported market")
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            raise ValueError(f"{label} values must be positive integers")
        result[key] = item
    return result


@dataclass(frozen=True, slots=True)
class XtQuantSafetyManifest:
    source_name: str
    source_sha256: str
    probe_symbol: Symbol
    equity_product_types: frozenset[int]
    trading_instrument_statuses: frozenset[int]
    open_stock_statuses: frozenset[int]
    auction_stock_statuses: frozenset[int]
    break_stock_statuses: frozenset[int]
    closed_stock_statuses: frozenset[int]
    trading_units: Mapping[str, int]
    volume_multipliers: Mapping[str, int]
    commission_rate: Decimal
    minimum_commission: Decimal
    stamp_duty_rate: Decimal
    transfer_fee_rate: Decimal

    @classmethod
    def load(cls, path: Path) -> XtQuantSafetyManifest:
        """Load an operator-reviewed local manifest; no deployment facts have defaults."""

        manifest_path = Path(path)
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise ValueError("XtQuant safety manifest is unavailable")
        raw = manifest_path.read_bytes()
        if len(raw) > 1024 * 1024:
            raise ValueError("XtQuant safety manifest exceeds one MiB")
        try:
            payload: object = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("XtQuant safety manifest is invalid UTF-8 JSON") from error
        if not isinstance(payload, dict):
            raise ValueError("XtQuant safety manifest root must be an object")
        expected = {
            "schema_version",
            "source_name",
            "source_sha256",
            "probe_symbol",
            "equity_product_types",
            "trading_instrument_statuses",
            "open_stock_statuses",
            "auction_stock_statuses",
            "break_stock_statuses",
            "closed_stock_statuses",
            "trading_units",
            "volume_multipliers",
            "commission_rate",
            "minimum_commission",
            "stamp_duty_rate",
            "transfer_fee_rate",
        }
        if set(payload) != expected or payload.get("schema_version") != 1:
            raise ValueError("XtQuant safety manifest schema is not the reviewed contract")
        source_name = payload["source_name"]
        source_sha256 = payload["source_sha256"]
        if not isinstance(source_name, str) or not source_name.strip():
            raise ValueError("XtQuant safety manifest requires source name")
        if not isinstance(source_sha256, str) or _SHA256.fullmatch(source_sha256) is None:
            raise ValueError("XtQuant safety manifest requires source SHA-256")
        probe_symbol = payload["probe_symbol"]
        if not isinstance(probe_symbol, str):
            raise ValueError("XtQuant safety probe symbol must be text")
        return cls(
            source_name=source_name,
            source_sha256=source_sha256,
            probe_symbol=Symbol.parse(probe_symbol),
            equity_product_types=_int_set(payload["equity_product_types"], label="equity product types"),
            trading_instrument_statuses=_int_set(
                payload["trading_instrument_statuses"],
                label="trading instrument statuses",
            ),
            open_stock_statuses=_int_set(payload["open_stock_statuses"], label="open stock statuses"),
            auction_stock_statuses=_int_set(
                payload["auction_stock_statuses"],
                label="auction stock statuses",
            ),
            break_stock_statuses=_int_set(payload["break_stock_statuses"], label="break stock statuses"),
            closed_stock_statuses=_int_set(
                payload["closed_stock_statuses"],
                label="closed stock statuses",
            ),
            trading_units=_positive_mapping(payload["trading_units"], label="trading units"),
            volume_multipliers=_positive_mapping(
                payload["volume_multipliers"],
                label="volume multipliers",
            ),
            commission_rate=_decimal(payload["commission_rate"], label="commission rate"),
            minimum_commission=_decimal(
                payload["minimum_commission"],
                label="minimum commission",
            ),
            stamp_duty_rate=_decimal(payload["stamp_duty_rate"], label="stamp duty rate"),
            transfer_fee_rate=_decimal(
                payload["transfer_fee_rate"],
                label="transfer fee rate",
            ),
        )

    @property
    def sha256(self) -> str:
        payload = {
            "source_name": self.source_name,
            "source_sha256": self.source_sha256,
            "probe_symbol": self.probe_symbol.canonical,
            "equity_product_types": sorted(self.equity_product_types),
            "trading_instrument_statuses": sorted(self.trading_instrument_statuses),
            "open_stock_statuses": sorted(self.open_stock_statuses),
            "auction_stock_statuses": sorted(self.auction_stock_statuses),
            "break_stock_statuses": sorted(self.break_stock_statuses),
            "closed_stock_statuses": sorted(self.closed_stock_statuses),
            "trading_units": dict(sorted(self.trading_units.items())),
            "volume_multipliers": dict(sorted(self.volume_multipliers.items())),
            "commission_rate": str(self.commission_rate),
            "minimum_commission": str(self.minimum_commission),
            "stamp_duty_rate": str(self.stamp_duty_rate),
            "transfer_fee_rate": str(self.transfer_fee_rate),
        }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
        return hashlib.sha256(encoded).hexdigest()


class ManifestXtQuantSafetyProvider(XtQuantSafetyFactProvider):
    """Read live market fields but interpret them only through a verified local manifest."""

    def __init__(
        self,
        *,
        xtdata: object,
        xtconstant: object,
        manifest: XtQuantSafetyManifest,
        clock: Callable[[], datetime],
    ) -> None:
        if not isinstance(manifest, XtQuantSafetyManifest):
            raise TypeError("XtQuant safety provider requires manifest")
        if not callable(clock):
            raise TypeError("XtQuant safety provider clock must be callable")
        self._xtdata = xtdata
        self._xtconstant = xtconstant
        self._manifest = manifest
        self._clock = clock

    @staticmethod
    def _field(raw: object, name: str, *, label: str) -> object:
        if isinstance(raw, Mapping):
            if name not in raw:
                raise BrokerFactUnavailable(f"{label} is missing {name}")
            return raw[name]
        try:
            return getattr(raw, name)
        except AttributeError as error:
            raise BrokerFactUnavailable(f"{label} is missing {name}") from error

    @staticmethod
    def _integer(value: object, *, label: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise BrokerSchemaMismatch(f"{label} must be integer")
        return value

    def market_status(self) -> MarketSessionStatus:
        getter = getattr(self._xtdata, "get_full_tick", None)
        if not callable(getter):
            raise BrokerFactUnavailable("XtQuant full tick API is unavailable")
        result = getter([self._manifest.probe_symbol.xtquant])
        if not isinstance(result, Mapping) or self._manifest.probe_symbol.xtquant not in result:
            raise BrokerFactUnavailable("XtQuant safety probe quote is unavailable")
        tick = result[self._manifest.probe_symbol.xtquant]
        if not isinstance(tick, Mapping):
            raise BrokerSchemaMismatch("XtQuant safety probe tick is malformed")
        status = self._integer(tick.get("stockStatus"), label="XtQuant stockStatus")
        if status in self._manifest.open_stock_statuses:
            return MarketSessionStatus.OPEN
        if status in self._manifest.auction_stock_statuses:
            return MarketSessionStatus.AUCTION
        if status in self._manifest.break_stock_statuses:
            return MarketSessionStatus.BREAK
        if status in self._manifest.closed_stock_statuses:
            return MarketSessionStatus.CLOSED
        return MarketSessionStatus.UNKNOWN

    def instrument_safety(
        self,
        symbol: Symbol,
        detail: Mapping[str, object],
    ) -> XtQuantInstrumentSafety:
        product = self._integer(detail.get("ProductType"), label="XtQuant ProductType")
        status_code = self._integer(
            detail.get("InstrumentStatus"),
            label="XtQuant InstrumentStatus",
        )
        is_trading = detail.get("IsTrading")
        if not isinstance(is_trading, bool):
            raise BrokerSchemaMismatch("XtQuant IsTrading must be bool")
        exchange = symbol.market.value
        unit = self._manifest.trading_units.get(exchange)
        if unit is None:
            raise BrokerFactUnavailable("trading unit is not verified for this market")
        if product not in self._manifest.equity_product_types:
            security_type = SecurityType.UNKNOWN
        else:
            security_type = SecurityType.EQUITY
        status = (
            SecurityStatus.TRADING
            if is_trading and status_code in self._manifest.trading_instrument_statuses
            else SecurityStatus.SUSPENDED
        )
        return XtQuantInstrumentSafety(
            security_type=security_type,
            status=status,
            trading_unit=Shares(unit),
        )

    def fill_fees(self, trade: object) -> XtQuantFeeBreakdown:
        price_raw = self._field(trade, "traded_price", label="XtQuant trade")
        volume = self._integer(
            self._field(trade, "traded_volume", label="XtQuant trade"),
            label="XtQuant traded volume",
        )
        if volume <= 0 or isinstance(price_raw, bool) or not isinstance(price_raw, (int, float)):
            raise BrokerSchemaMismatch("XtQuant trade price or volume is invalid")
        if isinstance(price_raw, float) and not math.isfinite(price_raw):
            raise BrokerSchemaMismatch("XtQuant traded price must be finite")
        price = Decimal(str(price_raw))
        if not price.is_finite() or price <= 0:
            raise BrokerSchemaMismatch("XtQuant traded price must be positive")
        order_type = self._integer(
            self._field(trade, "order_type", label="XtQuant trade"),
            label="XtQuant trade order type",
        )
        stock_sell = getattr(self._xtconstant, "STOCK_SELL", None)
        stock_buy = getattr(self._xtconstant, "STOCK_BUY", None)
        if order_type not in {stock_buy, stock_sell}:
            raise BrokerSchemaMismatch("XtQuant trade side is not verified cash equity")
        gross = price * volume

        def rounded(value: Decimal) -> Money:
            return Money(value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))

        commission = rounded(max(self._manifest.minimum_commission, gross * self._manifest.commission_rate))
        stamp = rounded(
            gross * self._manifest.stamp_duty_rate
            if order_type == stock_sell
            else Decimal(0)
        )
        transfer = rounded(gross * self._manifest.transfer_fee_rate)
        return XtQuantFeeBreakdown(
            commission=commission,
            stamp_duty=stamp,
            transfer_fee=transfer,
        )

    def quote_volume_shares(self, symbol: Symbol, tick: Mapping[str, object]) -> Shares:
        raw = tick.get("volume")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise BrokerSchemaMismatch("XtQuant quote volume is invalid")
        if isinstance(raw, float) and (not math.isfinite(raw) or not raw.is_integer()):
            raise BrokerSchemaMismatch("XtQuant quote volume is not integral")
        value = int(raw)
        if value < 0:
            raise BrokerSchemaMismatch("XtQuant quote volume must be nonnegative")
        multiplier = self._manifest.volume_multipliers.get(symbol.market.value)
        if multiplier is None:
            raise BrokerFactUnavailable("quote volume multiplier is not verified for this market")
        return Shares(value * multiplier)


__all__ = (
    "ManifestXtQuantSafetyProvider",
    "XtQuantSafetyManifest",
)
