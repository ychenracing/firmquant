from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace

from firmquant.broker.gateway import BrokerFactUnavailable
from firmquant.broker.xtquant import (
    XtQuantConstants,
    XtQuantFeeBreakdown,
    XtQuantInstrumentSafety,
)
from firmquant.domain.broker_facts import (
    MarketSessionStatus,
    SecurityStatus,
    SecurityType,
)
from firmquant.domain.values import Money, Shares, Symbol


@dataclass(slots=True)
class SdkObject:
    account_type: int = 2
    account_id: str = "account-001"
    cash: float = 100_000.0
    frozen_cash: float = 0.0
    market_value: float = 0.0
    total_asset: float = 100_000.0
    stock_code: str = "600519.SH"
    volume: int = 100
    can_use_volume: int = 80
    open_price: float = 10.0
    avg_price: float = 10.0
    order_id: int = 9001
    order_sysid: str = "sys-9001"
    order_time: int = 93_001
    order_type: int = 23
    order_volume: int = 100
    price_type: int = 11
    price: float = 10.1
    traded_volume: int = 0
    traded_price: float = 0.0
    order_status: int = 50
    status_msg: str = ""
    strategy_name: str = "firmquant"
    order_remark: str = "fq0123456789012345678901"
    traded_id: str = "fill-1"
    traded_time: int = 93_101
    traded_amount: float = 1_010.0


def instrument_detail() -> dict[str, object]:
    return {
        "ExchangeID": "SH",
        "InstrumentID": "600519",
        "ProductType": 201,
        "PreClose": 10.0,
        "UpStopPrice": 11.0,
        "DownStopPrice": 9.0,
        "PriceTick": 0.01,
        "InstrumentStatus": 0,
        "IsTrading": True,
    }


def full_tick() -> dict[str, object]:
    return {
        "timetag": "20260825 09:31:00.000",
        "lastPrice": 10.1,
        "lastClose": 10.0,
        "amount": 1_010_000.0,
        "volume": 1_000,
        "pvolume": 1_000,
        "stockStatus": 3,
        "askPrice": [10.1, 10.11, 10.12, 10.13, 10.14],
        "bidPrice": [10.09, 10.08, 10.07, 10.06, 10.05],
        "askVol": [20, 30, 40, 50, 60],
        "bidVol": [10, 20, 30, 40, 50],
    }


class ContractXtQuantSdkFacade:
    """Programmable public-SDK-shaped fake; it never imports proprietary code."""

    def __init__(self) -> None:
        self.constants = XtQuantConstants(
            security_account=2,
            stock_buy=23,
            stock_sell=24,
            fix_price=11,
            order_unreported=48,
            order_wait_reporting=49,
            order_reported=50,
            order_reported_cancel=51,
            order_partsucc_cancel=52,
            order_part_cancel=53,
            order_canceled=54,
            order_part_succ=55,
            order_succeeded=56,
            order_junk=57,
            order_unknown=255,
        )
        self.asset: object | None = SdkObject()
        self.positions: object | None = []
        self.orders: object | None = []
        self.trades: object | None = []
        self.instruments: dict[str, Mapping[str, object]] = {"600519.SH": instrument_detail()}
        self.ticks: dict[str, Mapping[str, object]] = {"600519.SH": full_tick()}
        self._callback: Callable[[str, object], None] | None = None
        self.started = False
        self.connected = False
        self.stopped = False
        self.subscribe_result = 0
        self.connect_result = 0
        self.order_calls: list[tuple[object, ...]] = []
        self.cancel_calls: list[int] = []
        self.market_status_value = MarketSessionStatus.OPEN
        self.safety = XtQuantInstrumentSafety(
            security_type=SecurityType.EQUITY,
            status=SecurityStatus.TRADING,
            trading_unit=Shares(100),
        )
        self.fees = XtQuantFeeBreakdown(
            commission=Money(Decimal("1.00")),
            stamp_duty=Money(Decimal("0.50")),
            transfer_fee=Money(Decimal("0.02")),
        )
        self.quote_share_volume = Shares(100_000)

    @property
    def write_api_available(self) -> bool:
        return True

    def register_callback(self, callback: Callable[[str, object], None]) -> None:
        self._callback = callback

    def start(self) -> None:
        self.started = True

    def connect(self) -> int:
        self.connected = self.connect_result == 0
        return self.connect_result

    def subscribe_account(self) -> int:
        return self.subscribe_result

    def stop(self) -> None:
        self.connected = False
        self.stopped = True

    def query_stock_asset(self) -> object | None:
        return self.asset

    def query_stock_positions(self) -> object | None:
        return self.positions

    def query_stock_orders(self) -> object | None:
        return self.orders

    def query_stock_trades(self) -> object | None:
        return self.trades

    def query_stock_order(self, order_id: int) -> object | None:
        values = self.orders if isinstance(self.orders, list) else []
        return next(
            (value for value in values if getattr(value, "order_id", None) == order_id),
            None,
        )

    def get_instrument_detail(self, stock_code: str) -> Mapping[str, object] | None:
        return self.instruments.get(stock_code)

    def get_full_tick(self, stock_codes: tuple[str, ...]) -> Mapping[str, object]:
        return {code: self.ticks[code] for code in stock_codes if code in self.ticks}

    def market_status(self) -> MarketSessionStatus:
        return self.market_status_value

    def instrument_safety(self, symbol: Symbol, detail: Mapping[str, object]) -> XtQuantInstrumentSafety:
        del symbol, detail
        return self.safety

    def fill_fees(self, trade: object) -> XtQuantFeeBreakdown:
        del trade
        return self.fees

    def quote_volume_shares(self, symbol: Symbol, tick: Mapping[str, object]) -> Shares:
        del symbol, tick
        return self.quote_share_volume

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
        self.order_calls.append(
            (
                stock_code,
                order_type,
                order_volume,
                price_type,
                price,
                strategy_name,
                order_remark,
            )
        )
        return 9001

    def cancel_order_stock(self, order_id: int) -> int:
        self.cancel_calls.append(order_id)
        return 0

    def emit(self, event_type: str, raw: object) -> None:
        if self._callback is None:
            raise BrokerFactUnavailable("contract fake callback is not registered")
        self._callback(event_type, raw)


class OfficialSdkModules:
    """In-memory modules matching only the currently documented public signatures."""

    def __init__(self) -> None:
        self.contract = ContractXtQuantSdkFacade()
        self.imported: list[str] = []
        self.traders: list[object] = []
        owner = self

        class VendorCallback:
            pass

        class VendorAccount:
            def __init__(self, account_id: str, account_type: str) -> None:
                self.account_id = account_id
                self.account_type = account_type

        class VendorTrader:
            def __init__(self, userdata_path: str, session_id: int) -> None:
                self.userdata_path = userdata_path
                self.session_id = session_id
                self.callback: object | None = None
                owner.traders.append(self)

            def register_callback(self, callback: object) -> None:
                self.callback = callback

            def start(self) -> None:
                owner.contract.start()

            def connect(self) -> int:
                return owner.contract.connect()

            def subscribe(self, account: VendorAccount) -> int:
                assert account.account_type == "STOCK"
                return owner.contract.subscribe_account()

            def stop(self) -> None:
                owner.contract.stop()

            def query_stock_asset(self, account: VendorAccount) -> object | None:
                del account
                return owner.contract.query_stock_asset()

            def query_stock_positions(self, account: VendorAccount) -> object | None:
                del account
                return owner.contract.query_stock_positions()

            def query_stock_orders(self, account: VendorAccount) -> object | None:
                del account
                return owner.contract.query_stock_orders()

            def query_stock_trades(self, account: VendorAccount) -> object | None:
                del account
                return owner.contract.query_stock_trades()

            def query_stock_order(self, account: VendorAccount, order_id: int) -> object | None:
                del account
                return owner.contract.query_stock_order(order_id)

            def order_stock(
                self,
                account: VendorAccount,
                stock_code: str,
                order_type: int,
                order_volume: int,
                price_type: int,
                price: float,
                strategy_name: str,
                order_remark: str,
            ) -> int:
                del account
                return owner.contract.order_stock(
                    stock_code,
                    order_type,
                    order_volume,
                    price_type,
                    price,
                    strategy_name,
                    order_remark,
                )

            def cancel_order_stock(self, account: VendorAccount, order_id: int) -> int:
                del account
                return owner.contract.cancel_order_stock(order_id)

        constants = self.contract.constants
        constant_module = SimpleNamespace(
            **{
                field: value
                for field, value in (
                    ("SECURITY_ACCOUNT", constants.security_account),
                    ("STOCK_BUY", constants.stock_buy),
                    ("STOCK_SELL", constants.stock_sell),
                    ("FIX_PRICE", constants.fix_price),
                    ("ORDER_UNREPORTED", constants.order_unreported),
                    ("ORDER_WAIT_REPORTING", constants.order_wait_reporting),
                    ("ORDER_REPORTED", constants.order_reported),
                    ("ORDER_REPORTED_CANCEL", constants.order_reported_cancel),
                    ("ORDER_PARTSUCC_CANCEL", constants.order_partsucc_cancel),
                    ("ORDER_PART_CANCEL", constants.order_part_cancel),
                    ("ORDER_CANCELED", constants.order_canceled),
                    ("ORDER_PART_SUCC", constants.order_part_succ),
                    ("ORDER_SUCCEEDED", constants.order_succeeded),
                    ("ORDER_JUNK", constants.order_junk),
                    ("ORDER_UNKNOWN", constants.order_unknown),
                )
            }
        )
        self.modules: dict[str, object] = {
            "xtquant.xttrader": SimpleNamespace(
                XtQuantTrader=VendorTrader,
                XtQuantTraderCallback=VendorCallback,
            ),
            "xtquant.xttype": SimpleNamespace(StockAccount=VendorAccount),
            "xtquant.xtdata": SimpleNamespace(
                get_instrument_detail=self.contract.get_instrument_detail,
                get_full_tick=lambda stock_codes: self.contract.get_full_tick(tuple(stock_codes)),
            ),
            "xtquant.xtconstant": constant_module,
        }

    def importer(self, module_name: str) -> object:
        self.imported.append(module_name)
        return self.modules[module_name]


__all__ = (
    "ContractXtQuantSdkFacade",
    "OfficialSdkModules",
    "SdkObject",
    "full_tick",
    "instrument_detail",
)
