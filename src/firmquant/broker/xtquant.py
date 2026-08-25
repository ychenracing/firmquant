"""Fail-closed MiniQMT/XtQuant boundary with lazy proprietary-SDK loading.

Only fields documented by the current official XtQuant interfaces are mapped here.
Facts not authoritatively exposed by that interface (notably share-volume conversion,
board lot, full fee breakdown, and market phase) must come from a locally verified
``XtQuantSdkFacade`` implementation. Missing facts stop the read or write operation.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, fields, replace
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from importlib import import_module
from pathlib import Path
from threading import RLock
from types import MappingProxyType
from typing import Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from firmquant.domain.broker_facts import (
    AccountType,
    BrokerAccountFact,
    BrokerFillFact,
    BrokerOrderFact,
    BrokerOrderStatus,
    BrokerPositionFact,
    FillStatus,
    InstrumentFact,
    MarketSessionStatus,
    PriceType,
    QuoteFact,
    SecurityStatus,
    SecurityType,
    Side,
)
from firmquant.domain.errors import DomainTypeError, DomainValidationError
from firmquant.domain.values import Money, Shares, Symbol

from .gateway import (
    BrokerDisconnected,
    BrokerEventSink,
    BrokerFactUnavailable,
    BrokerGatewayError,
    BrokerHealth,
    BrokerOrderCommand,
    BrokerWriteForbidden,
    _broker_write_is_authorized,
)
from .normalization import (
    canonical_raw_payload_sha256,
    normalize_account,
    normalize_fill,
    normalize_instrument,
    normalize_order,
    normalize_position,
    normalize_quote,
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_SDK_MODULES = (
    "xtquant.xttrader",
    "xtquant.xttype",
    "xtquant.xtdata",
    "xtquant.xtconstant",
)
_CONSTANT_NAMES = (
    "SECURITY_ACCOUNT",
    "STOCK_BUY",
    "STOCK_SELL",
    "FIX_PRICE",
    "ORDER_UNREPORTED",
    "ORDER_WAIT_REPORTING",
    "ORDER_REPORTED",
    "ORDER_REPORTED_CANCEL",
    "ORDER_PARTSUCC_CANCEL",
    "ORDER_PART_CANCEL",
    "ORDER_CANCELED",
    "ORDER_PART_SUCC",
    "ORDER_SUCCEEDED",
    "ORDER_JUNK",
    "ORDER_UNKNOWN",
)
_MONEY_PLACES = 4
_PRICE_PLACES = 8

type Importer = Callable[[str], object]
type XtQuantRawCallback = Callable[[str, object], None]


class BrokerDependencyMissing(BrokerGatewayError):
    """The legal local proprietary dependency is absent or incompatible."""


class BrokerSchemaMismatch(BrokerGatewayError):
    """An untrusted SDK object does not satisfy the pinned adapter contract."""


@dataclass(frozen=True, slots=True)
class XtQuantConstants:
    """Exact constants read from the installed official ``xtconstant`` module."""

    security_account: int
    stock_buy: int
    stock_sell: int
    fix_price: int
    order_unreported: int
    order_wait_reporting: int
    order_reported: int
    order_reported_cancel: int
    order_partsucc_cancel: int
    order_part_cancel: int
    order_canceled: int
    order_part_succ: int
    order_succeeded: int
    order_junk: int
    order_unknown: int

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise DomainTypeError(f"XtQuant constant {field.name} must be an integer")
        if self.stock_buy == self.stock_sell:
            raise DomainValidationError("XtQuant stock side constants must be distinct")


@dataclass(frozen=True, slots=True)
class XtQuantInstrumentSafety:
    """Broker-verified facts that the public instrument response may not expose."""

    security_type: SecurityType
    status: SecurityStatus
    trading_unit: Shares

    def __post_init__(self) -> None:
        if not isinstance(self.security_type, SecurityType):
            raise DomainTypeError("XtQuant security type must be SecurityType")
        if not isinstance(self.status, SecurityStatus):
            raise DomainTypeError("XtQuant security status must be SecurityStatus")
        if not isinstance(self.trading_unit, Shares) or not self.trading_unit.is_positive:
            raise DomainValidationError("XtQuant trading unit must be positive Shares")


@dataclass(frozen=True, slots=True)
class XtQuantFeeBreakdown:
    """Broker-confirmed fee components; absent components may never default to zero."""

    commission: Money
    stamp_duty: Money
    transfer_fee: Money

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, Money) for value in (self.commission, self.stamp_duty, self.transfer_fee)
        ):
            raise DomainTypeError("XtQuant fee components must be Money")


@dataclass(frozen=True, slots=True)
class XtQuantSdkDiagnosis:
    """Safe import/schema diagnostic; it never opens an account connection."""

    available: bool
    message: str
    checked_modules: tuple[str, ...]
    readonly_smoke_completed: bool = False
    real_order_calls: int = 0


@runtime_checkable
class XtQuantSdkFacade(Protocol):
    """Narrow SDK seam backed by an official local install or a contract fake."""

    constants: XtQuantConstants

    @property
    def write_api_available(self) -> bool: ...

    def register_callback(self, callback: XtQuantRawCallback) -> None: ...

    def start(self) -> None: ...

    def connect(self) -> int: ...

    def subscribe_account(self) -> int: ...

    def stop(self) -> None: ...

    def query_stock_asset(self) -> object | None: ...

    def query_stock_positions(self) -> object | None: ...

    def query_stock_orders(self) -> object | None: ...

    def query_stock_trades(self) -> object | None: ...

    def query_stock_order(self, order_id: int) -> object | None: ...

    def get_instrument_detail(self, stock_code: str) -> Mapping[str, object] | None: ...

    def get_full_tick(self, stock_codes: tuple[str, ...]) -> Mapping[str, object]: ...

    def market_status(self) -> MarketSessionStatus: ...

    def instrument_safety(self, symbol: Symbol, detail: Mapping[str, object]) -> XtQuantInstrumentSafety: ...

    def fill_fees(self, trade: object) -> XtQuantFeeBreakdown: ...

    def quote_volume_shares(self, symbol: Symbol, tick: Mapping[str, object]) -> Shares: ...

    def order_stock(
        self,
        stock_code: str,
        order_type: int,
        order_volume: int,
        price_type: int,
        price: float,
        strategy_name: str,
        order_remark: str,
    ) -> int: ...

    def cancel_order_stock(self, order_id: int) -> int: ...


@runtime_checkable
class XtQuantSafetyFactProvider(Protocol):
    """Deployment plug-in populated only from verified local broker facts."""

    def market_status(self) -> MarketSessionStatus: ...

    def instrument_safety(self, symbol: Symbol, detail: Mapping[str, object]) -> XtQuantInstrumentSafety: ...

    def fill_fees(self, trade: object) -> XtQuantFeeBreakdown: ...

    def quote_volume_shares(self, symbol: Symbol, tick: Mapping[str, object]) -> Shares: ...


def _int(value: object, *, label: str, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BrokerSchemaMismatch(f"{label} must be an integer")
    if value < 0 or (positive and value == 0):
        qualifier = "positive" if positive else "nonnegative"
        raise BrokerSchemaMismatch(f"{label} must be {qualifier}")
    return value


def _signed_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BrokerSchemaMismatch(f"{label} must be an integer")
    return value


def _text(value: object, *, label: str, maximum: int = 256) -> str:
    if not isinstance(value, str):
        raise BrokerSchemaMismatch(f"{label} must be text")
    if not value or value != value.strip() or len(value) > maximum:
        raise BrokerSchemaMismatch(f"{label} must be canonical non-empty text")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise BrokerSchemaMismatch(f"{label} contains control characters")
    return value


def _field(raw: object, name: str, *, label: str) -> object:
    if isinstance(raw, Mapping):
        if name not in raw:
            raise BrokerSchemaMismatch(f"{label} is missing required field {name}")
        return raw[name]
    try:
        return getattr(raw, name)
    except AttributeError as error:
        raise BrokerSchemaMismatch(f"{label} is missing required field {name}") from error


def _mapping(raw: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(raw, Mapping) or not all(isinstance(key, str) for key in raw):
        raise BrokerSchemaMismatch(f"{label} must be a text-keyed mapping")
    return raw


def _decimal(
    value: object,
    *,
    label: str,
    maximum_places: int,
    allow_zero: bool = True,
) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise BrokerSchemaMismatch(f"{label} must be a broker numeric value")
    if isinstance(value, float) and not math.isfinite(value):
        raise BrokerSchemaMismatch(f"{label} must be finite")
    try:
        observed = Decimal(str(value))
    except InvalidOperation as error:
        raise BrokerSchemaMismatch(f"{label} is not decimal-compatible") from error
    if not observed.is_finite():
        raise BrokerSchemaMismatch(f"{label} must be finite")
    if observed < 0 or (not allow_zero and observed == 0):
        qualifier = "nonnegative" if allow_zero else "positive"
        raise BrokerSchemaMismatch(f"{label} must be {qualifier}")
    quantum = Decimal(1).scaleb(-maximum_places)
    rounded = observed.quantize(quantum, rounding=ROUND_HALF_EVEN)
    tolerance = Decimal(1).scaleb(-(maximum_places + 4))
    if abs(observed - rounded) > tolerance:
        raise BrokerSchemaMismatch(f"{label} exceeds supported precision")
    return rounded


def _decimal_text(
    value: object,
    *,
    label: str,
    maximum_places: int,
    allow_zero: bool = True,
) -> str:
    decimal_value = _decimal(
        value,
        label=label,
        maximum_places=maximum_places,
        allow_zero=allow_zero,
    )
    rendered = format(decimal_value, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _optional_price_text(value: object, *, label: str) -> str | None:
    decimal_value = _decimal(
        value,
        label=label,
        maximum_places=_PRICE_PLACES,
        allow_zero=True,
    )
    if decimal_value == 0:
        return None
    rendered = format(decimal_value, "f")
    return rendered.rstrip("0").rstrip(".")


def _safe_sequence(raw: object, *, label: str) -> tuple[object, ...]:
    if raw is None:
        raise BrokerFactUnavailable(
            f"{label} returned None; official SDK cannot distinguish empty from query failure"
        )
    if isinstance(raw, (str, bytes, bytearray)) or not isinstance(raw, Sequence):
        raise BrokerSchemaMismatch(f"{label} must return a sequence or None")
    return tuple(raw)


def _module_attribute(module: object, name: str, *, module_name: str) -> object:
    try:
        return getattr(module, name)
    except AttributeError as error:
        raise BrokerDependencyMissing(
            f"official MiniQMT/XtQuant SDK is incompatible: {module_name} lacks required symbol {name}"
        ) from error


def _load_modules(importer: Importer) -> dict[str, object]:
    loaded: dict[str, object] = {}
    for module_name in _SDK_MODULES:
        try:
            loaded[module_name] = importer(module_name)
        except (ImportError, ModuleNotFoundError) as error:
            raise BrokerDependencyMissing(
                "official MiniQMT/XtQuant SDK is unavailable; install the SDK supplied "
                "with the legally authorized local MiniQMT client"
            ) from error
    _module_attribute(loaded["xtquant.xttrader"], "XtQuantTrader", module_name="xtquant.xttrader")
    _module_attribute(
        loaded["xtquant.xttrader"],
        "XtQuantTraderCallback",
        module_name="xtquant.xttrader",
    )
    _module_attribute(loaded["xtquant.xttype"], "StockAccount", module_name="xtquant.xttype")
    for name in ("get_instrument_detail", "get_full_tick"):
        _module_attribute(loaded["xtquant.xtdata"], name, module_name="xtquant.xtdata")
    for name in _CONSTANT_NAMES:
        _module_attribute(loaded["xtquant.xtconstant"], name, module_name="xtquant.xtconstant")
    return loaded


def _constants(module: object) -> XtQuantConstants:
    values = {
        name.lower(): _int(
            _module_attribute(module, name.upper(), module_name="xtquant.xtconstant"),
            label=f"XtQuant constant {name.upper()}",
        )
        for name in (
            "security_account",
            "stock_buy",
            "stock_sell",
            "fix_price",
            "order_unreported",
            "order_wait_reporting",
            "order_reported",
            "order_reported_cancel",
            "order_partsucc_cancel",
            "order_part_cancel",
            "order_canceled",
            "order_part_succ",
            "order_succeeded",
            "order_junk",
            "order_unknown",
        )
    }
    return XtQuantConstants(**values)


def diagnose_xtquant_sdk(*, importer: Importer = import_module) -> XtQuantSdkDiagnosis:
    """Validate import/schema only; never instantiate, connect, submit, or cancel."""

    try:
        _load_modules(importer)
    except BrokerDependencyMissing as error:
        return XtQuantSdkDiagnosis(
            available=False,
            message=str(error),
            checked_modules=_SDK_MODULES,
        )
    return XtQuantSdkDiagnosis(
        available=True,
        message="official MiniQMT/XtQuant SDK import and public schema are available",
        checked_modules=_SDK_MODULES,
    )


class _OfficialXtQuantFacade:
    """Thin calls into an installed SDK; policy and normalization stay outside."""

    def __init__(
        self,
        *,
        trader: object,
        account: object,
        xtdata: object,
        callback_base: object,
        constants: XtQuantConstants,
        safety_facts: XtQuantSafetyFactProvider | None,
    ) -> None:
        self._trader = trader
        self._account = account
        self._xtdata = xtdata
        self._callback_base = callback_base
        self.constants = constants
        self._safety_facts = safety_facts
        self._callback_object: object | None = None

    @property
    def write_api_available(self) -> bool:
        return callable(getattr(self._trader, "order_stock", None)) and callable(
            getattr(self._trader, "cancel_order_stock", None)
        )

    def _call(self, target: object, name: str, *args: object) -> object:
        method = _module_attribute(target, name, module_name=f"XtQuant runtime {name}")
        if not callable(method):
            raise BrokerDependencyMissing(f"official MiniQMT/XtQuant SDK symbol {name} is not callable")
        return method(*args)

    def register_callback(self, callback: XtQuantRawCallback) -> None:
        if not callable(callback):
            raise DomainTypeError("XtQuant callback must be callable")

        def on_disconnected(_callback_self: object) -> None:
            callback("DISCONNECTED", {})

        def on_stock_order(_callback_self: object, data: object) -> None:
            callback("ORDER", data)

        def on_stock_trade(_callback_self: object, data: object) -> None:
            callback("FILL", data)

        def on_order_error(_callback_self: object, data: object) -> None:
            callback("ORDER_ERROR", data)

        def on_cancel_error(_callback_self: object, data: object) -> None:
            callback("CANCEL_ERROR", data)

        if not isinstance(self._callback_base, type):
            raise BrokerDependencyMissing("official MiniQMT/XtQuant SDK callback base is not a class")
        callback_type = type(
            "FirmQuantXtQuantCallback",
            (self._callback_base,),
            {
                "on_disconnected": on_disconnected,
                "on_stock_order": on_stock_order,
                "on_stock_trade": on_stock_trade,
                "on_order_error": on_order_error,
                "on_cancel_error": on_cancel_error,
            },
        )
        self._callback_object = callback_type()
        self._call(self._trader, "register_callback", self._callback_object)

    def start(self) -> None:
        self._call(self._trader, "start")

    def connect(self) -> int:
        return _signed_int(self._call(self._trader, "connect"), label="XtQuant connect result")

    def subscribe_account(self) -> int:
        return _signed_int(
            self._call(self._trader, "subscribe", self._account),
            label="XtQuant subscribe result",
        )

    def stop(self) -> None:
        self._call(self._trader, "stop")

    def query_stock_asset(self) -> object | None:
        return self._call(self._trader, "query_stock_asset", self._account)

    def query_stock_positions(self) -> object | None:
        return self._call(self._trader, "query_stock_positions", self._account)

    def query_stock_orders(self) -> object | None:
        return self._call(self._trader, "query_stock_orders", self._account)

    def query_stock_trades(self) -> object | None:
        return self._call(self._trader, "query_stock_trades", self._account)

    def query_stock_order(self, order_id: int) -> object | None:
        return self._call(self._trader, "query_stock_order", self._account, order_id)

    def get_instrument_detail(self, stock_code: str) -> Mapping[str, object] | None:
        result = self._call(self._xtdata, "get_instrument_detail", stock_code)
        if result is None:
            return None
        return _mapping(result, label="XtQuant instrument detail")

    def get_full_tick(self, stock_codes: tuple[str, ...]) -> Mapping[str, object]:
        result = self._call(self._xtdata, "get_full_tick", list(stock_codes))
        return _mapping(result, label="XtQuant full tick result")

    def _safety(self) -> XtQuantSafetyFactProvider:
        if self._safety_facts is None:
            raise BrokerFactUnavailable(
                "XtQuant authoritative safety facts are not verified on this installation"
            )
        return self._safety_facts

    def market_status(self) -> MarketSessionStatus:
        return self._safety().market_status()

    def instrument_safety(self, symbol: Symbol, detail: Mapping[str, object]) -> XtQuantInstrumentSafety:
        return self._safety().instrument_safety(symbol, detail)

    def fill_fees(self, trade: object) -> XtQuantFeeBreakdown:
        return self._safety().fill_fees(trade)

    def quote_volume_shares(self, symbol: Symbol, tick: Mapping[str, object]) -> Shares:
        return self._safety().quote_volume_shares(symbol, tick)

    def order_stock(
        self,
        stock_code: str,
        order_type: int,
        order_volume: int,
        price_type: int,
        price: float,
        strategy_name: str,
        order_remark: str,
    ) -> int:
        return _signed_int(
            self._call(
                self._trader,
                "order_stock",
                self._account,
                stock_code,
                order_type,
                order_volume,
                price_type,
                price,
                strategy_name,
                order_remark,
            ),
            label="XtQuant order result",
        )

    def cancel_order_stock(self, order_id: int) -> int:
        return _signed_int(
            self._call(self._trader, "cancel_order_stock", self._account, order_id),
            label="XtQuant cancel result",
        )


def _official_facade(
    *,
    userdata_path: Path,
    session_id: int,
    account_id: str,
    importer: Importer,
    safety_facts: XtQuantSafetyFactProvider | None,
) -> XtQuantSdkFacade:
    modules = _load_modules(importer)
    trader_module = modules["xtquant.xttrader"]
    type_module = modules["xtquant.xttype"]
    trader_type = _module_attribute(trader_module, "XtQuantTrader", module_name="xtquant.xttrader")
    callback_base = _module_attribute(trader_module, "XtQuantTraderCallback", module_name="xtquant.xttrader")
    account_type = _module_attribute(type_module, "StockAccount", module_name="xtquant.xttype")
    if not callable(trader_type) or not callable(account_type):
        raise BrokerDependencyMissing("official MiniQMT/XtQuant SDK constructors are not callable")
    try:
        trader = trader_type(str(userdata_path), session_id)
        account = account_type(account_id, "STOCK")
    except Exception as error:
        raise BrokerDependencyMissing(
            "official MiniQMT/XtQuant SDK constructors rejected the configured schema"
        ) from error
    return _OfficialXtQuantFacade(
        trader=trader,
        account=account,
        xtdata=modules["xtquant.xtdata"],
        callback_base=callback_base,
        constants=_constants(modules["xtquant.xtconstant"]),
        safety_facts=safety_facts,
    )


class XtQuantBroker:
    """Single-account cash-equity BrokerGateway for a verified XtQuant facade."""

    def __init__(
        self,
        *,
        facade: XtQuantSdkFacade,
        account_id: str,
        clock: Callable[[], datetime],
    ) -> None:
        if not isinstance(facade, XtQuantSdkFacade):
            raise DomainTypeError("XtQuant facade does not satisfy XtQuantSdkFacade")
        self._account_id = _text(account_id, label="XtQuant account identity", maximum=128)
        if not callable(clock):
            raise DomainTypeError("XtQuant clock must be callable")
        self._facade = facade
        self._clock = clock
        self._connected = False
        self._callback_registered = False
        self._sink: BrokerEventSink | None = None
        self._diagnostic = "DISCONNECTED"
        self._lock = RLock()

    @classmethod
    def load_sdk(
        cls,
        *,
        userdata_path: Path,
        session_id: int,
        account_id: str,
        clock: Callable[[], datetime],
        importer: Importer = import_module,
        safety_facts: XtQuantSafetyFactProvider | None = None,
    ) -> XtQuantBroker:
        """Lazily load an official local SDK; importing this module never loads it."""

        if not isinstance(userdata_path, Path):
            raise DomainTypeError("XtQuant userdata path must be Path")
        if not userdata_path.exists() or not userdata_path.is_dir():
            raise BrokerDependencyMissing("official MiniQMT/XtQuant SDK userdata directory is unavailable")
        _int(session_id, label="XtQuant session id", positive=True)
        _text(account_id, label="XtQuant account identity", maximum=128)
        facade = _official_facade(
            userdata_path=userdata_path,
            session_id=session_id,
            account_id=account_id,
            importer=importer,
            safety_facts=safety_facts,
        )
        return cls(facade=facade, account_id=account_id, clock=clock)

    def __repr__(self) -> str:
        return "<XtQuantBroker account=redacted>"

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise BrokerSchemaMismatch("XtQuant observation clock must be timezone-aware")
        return value

    def _session_date(self, observed_at: datetime) -> str:
        return observed_at.astimezone(_SHANGHAI).date().isoformat()

    def _require_connected(self) -> None:
        with self._lock:
            if not self._connected:
                raise BrokerDisconnected("XtQuant broker is disconnected")

    def connect(self) -> None:
        with self._lock:
            if self._connected:
                return
            if not self._callback_registered:
                self._facade.register_callback(self._on_sdk_event)
                self._callback_registered = True
        try:
            self._facade.start()
            connect_result = self._facade.connect()
            if connect_result != 0:
                raise BrokerDisconnected("XtQuant connect failed")
            subscribe_result = self._facade.subscribe_account()
            if subscribe_result != 0:
                raise BrokerDisconnected("XtQuant account subscribe failed")
        except Exception:
            with self._lock:
                self._connected = False
                self._diagnostic = "CONNECT_FAILED"
            try:
                self._facade.stop()
            except Exception as cleanup_error:
                raise BrokerDisconnected(
                    "XtQuant connection failed and SDK cleanup was unsuccessful"
                ) from cleanup_error
            raise
        with self._lock:
            self._connected = True
            self._diagnostic = "CONNECTED"

    def disconnect(self) -> None:
        with self._lock:
            connected = self._connected
            self._connected = False
            self._diagnostic = "DISCONNECTED"
        if connected:
            self._facade.stop()

    def health(self) -> BrokerHealth:
        with self._lock:
            connected = self._connected
            diagnostic = self._diagnostic
        healthy = connected and diagnostic == "CONNECTED"
        return BrokerHealth(
            connected=connected,
            read_healthy=healthy,
            write_healthy=healthy and self._facade.write_api_available,
            observed_at=self._now(),
            diagnostic_code=diagnostic,
        )

    def _validate_identity(self, raw: object, *, label: str) -> None:
        account_id = _text(
            _field(raw, "account_id", label=label),
            label=f"{label} account identity",
            maximum=128,
        )
        if not hmac.compare_digest(account_id, self._account_id):
            raise BrokerSchemaMismatch(f"{label} account identity does not match binding")
        account_type = _int(
            _field(raw, "account_type", label=label),
            label=f"{label} account type",
        )
        if account_type != self._facade.constants.security_account:
            raise BrokerSchemaMismatch(f"{label} is not a cash securities account")

    def query_account(self) -> BrokerAccountFact:
        self._require_connected()
        raw = self._facade.query_stock_asset()
        if raw is None:
            raise BrokerFactUnavailable("XtQuant account query returned no fact")
        self._validate_identity(raw, label="XtQuant asset")
        payload: dict[str, object] = {
            "account_id_hash": hashlib.sha256(self._account_id.encode()).hexdigest(),
            "account_type": AccountType.CASH.value,
            "available_cash": _decimal_text(
                _field(raw, "cash", label="XtQuant asset"),
                label="XtQuant available cash",
                maximum_places=_MONEY_PLACES,
            ),
            "total_assets": _decimal_text(
                _field(raw, "total_asset", label="XtQuant asset"),
                label="XtQuant total assets",
                maximum_places=_MONEY_PLACES,
            ),
        }
        return normalize_account(payload)

    def _position(self, raw: object) -> BrokerPositionFact:
        self._validate_identity(raw, label="XtQuant position")
        average_cost = _optional_price_text(
            _field(raw, "avg_price", label="XtQuant position"),
            label="XtQuant average cost",
        )
        payload: dict[str, object] = {
            "symbol": _text(
                _field(raw, "stock_code", label="XtQuant position"),
                label="XtQuant position symbol",
            ),
            "total_shares": _int(
                _field(raw, "volume", label="XtQuant position"),
                label="XtQuant position volume",
            ),
            "sellable_shares": _int(
                _field(raw, "can_use_volume", label="XtQuant position"),
                label="XtQuant sellable volume",
            ),
            "average_cost": average_cost,
            "market_value": _decimal_text(
                _field(raw, "market_value", label="XtQuant position"),
                label="XtQuant position market value",
                maximum_places=_MONEY_PLACES,
            ),
        }
        return normalize_position(payload)

    def query_positions(self) -> tuple[BrokerPositionFact, ...]:
        self._require_connected()
        raw_values = _safe_sequence(self._facade.query_stock_positions(), label="XtQuant positions query")
        values = tuple(self._position(raw) for raw in raw_values)
        return tuple(sorted(values, key=lambda value: value.symbol.canonical))

    def _side(self, value: object, *, label: str) -> Side:
        code = _int(value, label=label)
        if code == self._facade.constants.stock_buy:
            return Side.BUY
        if code == self._facade.constants.stock_sell:
            return Side.SELL
        raise BrokerSchemaMismatch(f"{label} is not cash-equity buy or sell")

    def _order_status(self, value: object) -> BrokerOrderStatus:
        constants = self._facade.constants
        code = _int(value, label="XtQuant order status")
        mapping = {
            constants.order_unreported: BrokerOrderStatus.PENDING_NEW,
            constants.order_wait_reporting: BrokerOrderStatus.PENDING_NEW,
            constants.order_reported: BrokerOrderStatus.ACKNOWLEDGED,
            constants.order_reported_cancel: BrokerOrderStatus.PENDING_CANCEL,
            constants.order_partsucc_cancel: BrokerOrderStatus.PENDING_CANCEL,
            constants.order_part_cancel: BrokerOrderStatus.CANCELLED,
            constants.order_canceled: BrokerOrderStatus.CANCELLED,
            constants.order_part_succ: BrokerOrderStatus.PARTIALLY_FILLED,
            constants.order_succeeded: BrokerOrderStatus.FILLED,
            constants.order_junk: BrokerOrderStatus.REJECTED,
            constants.order_unknown: BrokerOrderStatus.UNKNOWN,
        }
        try:
            return mapping[code]
        except KeyError as error:
            raise BrokerSchemaMismatch("XtQuant returned an unknown order status") from error

    def _order_payload(self, raw: object, *, observed_at: datetime) -> dict[str, object]:
        self._validate_identity(raw, label="XtQuant order")
        price_type = _int(
            _field(raw, "price_type", label="XtQuant order"),
            label="XtQuant order price type",
        )
        if price_type != self._facade.constants.fix_price:
            raise BrokerSchemaMismatch("XtQuant order is not a verified protected limit order")
        return {
            "broker_order_id": str(
                _int(
                    _field(raw, "order_id", label="XtQuant order"),
                    label="XtQuant order id",
                    positive=True,
                )
            ),
            "client_order_id": None,
            "symbol": _text(
                _field(raw, "stock_code", label="XtQuant order"),
                label="XtQuant order symbol",
            ),
            "side": self._side(
                _field(raw, "order_type", label="XtQuant order"),
                label="XtQuant order type",
            ).value,
            "price_type": PriceType.LIMIT.value,
            "status": self._order_status(_field(raw, "order_status", label="XtQuant order")).value,
            "requested_shares": _int(
                _field(raw, "order_volume", label="XtQuant order"),
                label="XtQuant requested volume",
                positive=True,
            ),
            "filled_shares": _int(
                _field(raw, "traded_volume", label="XtQuant order"),
                label="XtQuant filled volume",
            ),
            "limit_price": _decimal_text(
                _field(raw, "price", label="XtQuant order"),
                label="XtQuant order price",
                maximum_places=_PRICE_PLACES,
                allow_zero=False,
            ),
            "session_date": self._session_date(observed_at),
            "event_time": observed_at.isoformat(),
            "event_sequence": _int(
                _field(raw, "order_time", label="XtQuant order"),
                label="XtQuant order time sequence",
            ),
        }

    def _order(self, raw: object, *, observed_at: datetime) -> BrokerOrderFact:
        return normalize_order(self._order_payload(raw, observed_at=observed_at), received_at=observed_at)

    def query_orders(self) -> tuple[BrokerOrderFact, ...]:
        self._require_connected()
        observed_at = self._now()
        raw_values = _safe_sequence(self._facade.query_stock_orders(), label="XtQuant orders query")
        values = tuple(self._order(raw, observed_at=observed_at) for raw in raw_values)
        return tuple(sorted(values, key=lambda value: value.broker_order_id))

    def _fill_payload(self, raw: object, *, observed_at: datetime) -> dict[str, object]:
        self._validate_identity(raw, label="XtQuant fill")
        fees = self._facade.fill_fees(raw)
        if not isinstance(fees, XtQuantFeeBreakdown):
            raise BrokerSchemaMismatch("XtQuant fee provider returned an invalid fact")
        return {
            "broker_fill_id": _text(
                _field(raw, "traded_id", label="XtQuant fill"),
                label="XtQuant fill id",
            ),
            "broker_order_id": str(
                _int(
                    _field(raw, "order_id", label="XtQuant fill"),
                    label="XtQuant fill order id",
                    positive=True,
                )
            ),
            "symbol": _text(
                _field(raw, "stock_code", label="XtQuant fill"),
                label="XtQuant fill symbol",
            ),
            "side": self._side(
                _field(raw, "order_type", label="XtQuant fill"),
                label="XtQuant fill order type",
            ).value,
            "status": FillStatus.CONFIRMED.value,
            "shares": _int(
                _field(raw, "traded_volume", label="XtQuant fill"),
                label="XtQuant fill volume",
                positive=True,
            ),
            "price": _decimal_text(
                _field(raw, "traded_price", label="XtQuant fill"),
                label="XtQuant fill price",
                maximum_places=_PRICE_PLACES,
                allow_zero=False,
            ),
            "commission": fees.commission.canonical,
            "stamp_duty": fees.stamp_duty.canonical,
            "transfer_fee": fees.transfer_fee.canonical,
            "session_date": self._session_date(observed_at),
            "event_time": observed_at.isoformat(),
            "event_sequence": _int(
                _field(raw, "traded_time", label="XtQuant fill"),
                label="XtQuant fill time sequence",
            ),
        }

    def _fill(self, raw: object, *, observed_at: datetime) -> BrokerFillFact:
        return normalize_fill(self._fill_payload(raw, observed_at=observed_at), received_at=observed_at)

    def query_fills(self) -> tuple[BrokerFillFact, ...]:
        self._require_connected()
        observed_at = self._now()
        raw_values = _safe_sequence(self._facade.query_stock_trades(), label="XtQuant fills query")
        values = tuple(self._fill(raw, observed_at=observed_at) for raw in raw_values)
        return tuple(sorted(values, key=lambda value: value.broker_fill_id))

    def _instrument_detail(self, symbol: Symbol) -> Mapping[str, object]:
        detail = self._facade.get_instrument_detail(symbol.xtquant)
        if detail is None:
            raise BrokerFactUnavailable("XtQuant instrument detail is unavailable")
        exchange = _text(
            _field(detail, "ExchangeID", label="XtQuant instrument"),
            label="XtQuant instrument exchange",
        ).upper()
        instrument_id = _text(
            _field(detail, "InstrumentID", label="XtQuant instrument"),
            label="XtQuant instrument id",
        )
        if exchange != symbol.market.value or instrument_id != symbol.code:
            raise BrokerSchemaMismatch("XtQuant instrument identity contradicts query")
        return detail

    def query_instrument(self, symbol: Symbol) -> InstrumentFact:
        self._require_connected()
        if not isinstance(symbol, Symbol):
            raise DomainTypeError("XtQuant instrument query symbol must be Symbol")
        observed_at = self._now()
        detail = self._instrument_detail(symbol)
        safety = self._facade.instrument_safety(symbol, detail)
        if not isinstance(safety, XtQuantInstrumentSafety):
            raise BrokerSchemaMismatch("XtQuant instrument safety fact is invalid")
        tick = _decimal(
            _field(detail, "PriceTick", label="XtQuant instrument"),
            label="XtQuant price tick",
            maximum_places=_PRICE_PLACES,
            allow_zero=False,
        )
        exponent = tick.normalize().as_tuple().exponent
        if not isinstance(exponent, int):
            raise BrokerSchemaMismatch("XtQuant price tick exponent is not finite")
        precision = max(0, -exponent)
        payload: dict[str, object] = {
            "symbol": symbol.xtquant,
            "security_type": safety.security_type.value,
            "status": safety.status.value,
            "trading_unit": safety.trading_unit.value,
            "price_tick": format(tick.normalize(), "f"),
            "price_precision": precision,
            "lower_limit": _optional_price_text(
                _field(detail, "DownStopPrice", label="XtQuant instrument"),
                label="XtQuant lower limit",
            ),
            "upper_limit": _optional_price_text(
                _field(detail, "UpStopPrice", label="XtQuant instrument"),
                label="XtQuant upper limit",
            ),
            "session_date": self._session_date(observed_at),
            "observed_at": observed_at.isoformat(),
        }
        return normalize_instrument(payload)

    @staticmethod
    def _event_time(tick: Mapping[str, object]) -> datetime:
        raw = tick.get("time", tick.get("timetag"))
        if isinstance(raw, bool):
            raise BrokerSchemaMismatch("XtQuant tick time is invalid")
        if isinstance(raw, int):
            try:
                return datetime.fromtimestamp(raw / 1000, tz=_SHANGHAI)
            except (OverflowError, OSError, ValueError) as error:
                raise BrokerSchemaMismatch("XtQuant tick epoch is invalid") from error
        if isinstance(raw, str):
            for pattern in (
                "%Y%m%d %H:%M:%S.%f",
                "%Y%m%d %H:%M:%S",
                "%Y%m%d%H%M%S.%f",
                "%Y%m%d%H%M%S",
            ):
                try:
                    return datetime.strptime(raw, pattern).replace(tzinfo=_SHANGHAI)
                except ValueError:
                    continue
        raise BrokerSchemaMismatch("XtQuant tick time format is unsupported")

    @staticmethod
    def _book_price(tick: Mapping[str, object], field: str) -> str | None:
        values = tick.get(field)
        if isinstance(values, (str, bytes, bytearray)) or not isinstance(values, Sequence):
            raise BrokerSchemaMismatch(f"XtQuant tick {field} must be a price sequence")
        if not values:
            return None
        return _optional_price_text(values[0], label=f"XtQuant tick {field}")

    def query_quote(self, symbol: Symbol) -> QuoteFact:
        self._require_connected()
        if not isinstance(symbol, Symbol):
            raise DomainTypeError("XtQuant quote query symbol must be Symbol")
        received_at = self._now()
        result = _mapping(
            self._facade.get_full_tick((symbol.xtquant,)),
            label="XtQuant full tick result",
        )
        if symbol.xtquant not in result:
            raise BrokerFactUnavailable("XtQuant quote is unavailable")
        tick = _mapping(result[symbol.xtquant], label="XtQuant tick")
        detail = self._instrument_detail(symbol)
        market_status = self._facade.market_status()
        if not isinstance(market_status, MarketSessionStatus):
            raise BrokerSchemaMismatch("XtQuant market status fact is invalid")
        share_volume = self._facade.quote_volume_shares(symbol, tick)
        if not isinstance(share_volume, Shares):
            raise BrokerSchemaMismatch("XtQuant quote volume fact is invalid")
        event_time = self._event_time(tick)
        sequence = int(event_time.timestamp() * 1000)
        payload: dict[str, object] = {
            "symbol": symbol.xtquant,
            "last_price": _optional_price_text(
                _field(tick, "lastPrice", label="XtQuant tick"),
                label="XtQuant last price",
            ),
            "previous_close": _optional_price_text(
                _field(tick, "lastClose", label="XtQuant tick"),
                label="XtQuant previous close",
            ),
            "bid_price": self._book_price(tick, "bidPrice"),
            "ask_price": self._book_price(tick, "askPrice"),
            "volume": share_volume.value,
            "turnover": _decimal_text(
                _field(tick, "amount", label="XtQuant tick"),
                label="XtQuant turnover",
                maximum_places=_MONEY_PLACES,
            ),
            "lower_limit": _optional_price_text(
                _field(detail, "DownStopPrice", label="XtQuant instrument"),
                label="XtQuant lower limit",
            ),
            "upper_limit": _optional_price_text(
                _field(detail, "UpStopPrice", label="XtQuant instrument"),
                label="XtQuant upper limit",
            ),
            "market_status": market_status.value,
            "sequence": sequence,
            "session_date": event_time.date().isoformat(),
            "event_time": event_time.isoformat(),
        }
        return normalize_quote(payload, received_at=received_at)

    def query_market_status(self) -> MarketSessionStatus:
        self._require_connected()
        status = self._facade.market_status()
        if not isinstance(status, MarketSessionStatus):
            raise BrokerSchemaMismatch("XtQuant market status fact is invalid")
        return status

    @staticmethod
    def _sdk_price(command: BrokerOrderCommand) -> float:
        sdk_value = float(command.limit_price.canonical)
        if not math.isfinite(sdk_value):
            raise BrokerSchemaMismatch("XtQuant SDK price conversion is not finite")
        if abs(Decimal(str(sdk_value)) - command.limit_price.value) > Decimal("0.00000001"):
            raise BrokerSchemaMismatch("XtQuant SDK price conversion exceeds tolerance")
        return sdk_value

    def submit_order(self, command: BrokerOrderCommand) -> BrokerOrderFact:
        self._require_connected()
        if not _broker_write_is_authorized():
            raise BrokerWriteForbidden("XtQuant submit requires a freshly authorized BrokerWriteCapability")
        if not isinstance(command, BrokerOrderCommand):
            raise DomainTypeError("XtQuant submit requires BrokerOrderCommand")
        constants = self._facade.constants
        order_type = constants.stock_buy if command.side is Side.BUY else constants.stock_sell
        order_remark = "fq" + command.idempotency_key[:22]
        order_id = self._facade.order_stock(
            command.symbol.xtquant,
            order_type,
            command.requested_shares.value,
            constants.fix_price,
            self._sdk_price(command),
            "firmquant",
            order_remark,
        )
        if isinstance(order_id, bool) or not isinstance(order_id, int) or order_id <= 0:
            raise BrokerGatewayError("XtQuant did not return a positive broker order id")
        raw = self._facade.query_stock_order(order_id)
        if raw is None:
            raise BrokerFactUnavailable("XtQuant accepted a submit id but the order cannot yet be confirmed")
        return replace(
            self._order(raw, observed_at=self._now()),
            client_order_id=command.client_order_id,
        )

    def cancel_order(self, broker_order_id: str) -> BrokerOrderFact:
        self._require_connected()
        if not _broker_write_is_authorized():
            raise BrokerWriteForbidden("XtQuant cancel requires a freshly authorized BrokerWriteCapability")
        canonical = _text(broker_order_id, label="XtQuant broker order id", maximum=32)
        if not canonical.isascii() or not canonical.isdecimal():
            raise BrokerSchemaMismatch("XtQuant broker order id must be a positive integer")
        order_id = int(canonical)
        if order_id <= 0:
            raise BrokerSchemaMismatch("XtQuant broker order id must be positive")
        result = self._facade.cancel_order_stock(order_id)
        if isinstance(result, bool) or not isinstance(result, int) or result != 0:
            raise BrokerGatewayError("XtQuant did not confirm the cancel request")
        raw = self._facade.query_stock_order(order_id)
        if raw is None:
            raise BrokerFactUnavailable("XtQuant accepted a cancel request but order state is unavailable")
        return self._order(raw, observed_at=self._now())

    def subscribe(self, callback_sink: BrokerEventSink) -> None:
        if not callable(callback_sink):
            raise DomainTypeError("XtQuant callback sink must be callable")
        with self._lock:
            if self._sink is not None and self._sink is not callback_sink:
                raise DomainValidationError("XtQuant callback sink is already registered")
            self._sink = callback_sink

    @staticmethod
    def _event_id(event_type: str, payload: Mapping[str, object]) -> str:
        encoded = json.dumps(
            dict(payload),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return f"xtquant-{event_type.casefold()}-{hashlib.sha256(encoded).hexdigest()}"

    def _on_sdk_event(self, event_type: str, raw: object) -> None:
        try:
            canonical_type = _text(event_type, label="XtQuant callback type", maximum=32).upper()
            if canonical_type == "DISCONNECTED":
                with self._lock:
                    self._connected = False
                    self._diagnostic = "DISCONNECTED"
                return
            observed_at = self._now()
            if canonical_type == "ORDER":
                payload = self._order_payload(raw, observed_at=observed_at)
            elif canonical_type == "FILL":
                payload = self._fill_payload(raw, observed_at=observed_at)
            else:
                raise BrokerSchemaMismatch("XtQuant callback type is unsupported")
            safe_payload = MappingProxyType(dict(payload))
            event: dict[str, object] = {
                "event_id": self._event_id(canonical_type, safe_payload),
                "event_type": canonical_type,
                "payload": dict(safe_payload),
            }
            canonical_raw_payload_sha256(event)
            with self._lock:
                sink = self._sink
            if sink is not None:
                sink(event)
        except Exception:
            with self._lock:
                self._diagnostic = "CALLBACK_SCHEMA_INVALID"


__all__ = (
    "BrokerDependencyMissing",
    "BrokerSchemaMismatch",
    "XtQuantBroker",
    "XtQuantConstants",
    "XtQuantFeeBreakdown",
    "XtQuantInstrumentSafety",
    "XtQuantSafetyFactProvider",
    "XtQuantSdkDiagnosis",
    "XtQuantSdkFacade",
    "diagnose_xtquant_sdk",
)
