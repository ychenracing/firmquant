"""Strict adapter from official XtQuant daily history APIs to canonical daily bars."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol
from zoneinfo import ZoneInfo

from firmquant.domain.values import Symbol
from firmquant.market_data.xtquant_daily import DailyBar, DailyDataUpdateError, DailyHistoryProvider

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_RAW_INDEX_SYMBOLS = frozenset({"sh000300", "sh000682"})
_FIELDS = ["time", "open", "high", "low", "close", "volume", "amount"]


class _Frame(Protocol):
    def reset_index(self) -> object: ...

    def to_dict(self, orient: str) -> object: ...


def _decimal(value: object, *, label: str) -> Decimal:
    if isinstance(value, bool):
        raise DailyDataUpdateError(f"XtQuant {label} cannot be bool")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise DailyDataUpdateError(f"XtQuant {label} is not decimal-compatible") from error
    if not result.is_finite():
        raise DailyDataUpdateError(f"XtQuant {label} must be finite")
    return result


def _session(value: object) -> date:
    if isinstance(value, bool):
        raise DailyDataUpdateError("XtQuant history time is invalid")
    if isinstance(value, int):
        try:
            return datetime.fromtimestamp(value / 1000, tz=_SHANGHAI).date()
        except (OSError, OverflowError, ValueError) as error:
            raise DailyDataUpdateError("XtQuant history epoch is invalid") from error
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=_SHANGHAI).date()
        return value.astimezone(_SHANGHAI).date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise DailyDataUpdateError("XtQuant history time has unsupported type")
    text = value.strip()
    for pattern in ("%Y%m%d", "%Y-%m-%d", "%Y%m%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    raise DailyDataUpdateError("XtQuant history time has unsupported format")


def _records(frame: object) -> tuple[Mapping[str, object], ...]:
    reset = getattr(frame, "reset_index", None)
    if callable(reset):
        frame = reset()
    converter = getattr(frame, "to_dict", None)
    if not callable(converter):
        raise DailyDataUpdateError("XtQuant history frame does not expose record conversion")
    raw = converter("records")
    if not isinstance(raw, list) or not all(isinstance(item, Mapping) for item in raw):
        raise DailyDataUpdateError("XtQuant history frame records are malformed")
    return tuple(raw)


def _time_value(row: Mapping[str, object]) -> object:
    for key in ("time", "date", "index", "timetag"):
        if key in row:
            return row[key]
    raise DailyDataUpdateError("XtQuant history row is missing time")


class OfficialXtQuantDailyHistoryProvider(DailyHistoryProvider):
    """Download/read official local MiniQMT history; all unit multipliers are reviewed inputs."""

    def __init__(self, *, xtdata: object, volume_multipliers: Mapping[str, int]) -> None:
        downloader = getattr(xtdata, "download_history_data", None)
        reader = getattr(xtdata, "get_market_data_ex", None)
        if not callable(downloader) or not callable(reader):
            raise TypeError("official XtQuant history APIs are unavailable")
        if not isinstance(volume_multipliers, Mapping):
            raise TypeError("XtQuant history volume multipliers must be a mapping")
        normalized: dict[str, int] = {}
        for market in ("SH", "SZ", "BJ"):
            value = volume_multipliers.get(market)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("XtQuant history volume multipliers must cover SH/SZ/BJ")
            normalized[market] = value
        self._xtdata = xtdata
        self._multipliers = normalized

    def _fetch_symbol(self, symbol: str, *, through: date) -> tuple[DailyBar, ...]:
        parsed = Symbol.parse(symbol)
        code = parsed.xtquant
        end = through.strftime("%Y%m%d")
        downloader = getattr(self._xtdata, "download_history_data")
        reader = getattr(self._xtdata, "get_market_data_ex")
        downloader(
            stock_code=code,
            period="1d",
            start_time="",
            end_time=end,
            incrementally=True,
        )
        adjustment = "none" if symbol in _RAW_INDEX_SYMBOLS else "front"
        result = reader(
            field_list=_FIELDS,
            stock_list=[code],
            period="1d",
            start_time="",
            end_time=end,
            count=-1,
            dividend_type=adjustment,
            fill_data=False,
        )
        if not isinstance(result, Mapping) or code not in result:
            raise DailyDataUpdateError(f"XtQuant daily history is unavailable for {symbol}")
        multiplier = self._multipliers[parsed.market.value]
        bars: list[DailyBar] = []
        for row in _records(result[code]):
            session = _session(_time_value(row))
            if session > through:
                continue
            volume = _decimal(row.get("volume"), label="history volume") * multiplier
            if volume != volume.to_integral_value():
                raise DailyDataUpdateError("XtQuant history volume cannot be converted to whole shares")
            bars.append(
                DailyBar(
                    session=session,
                    open=_decimal(row.get("open"), label="history open"),
                    high=_decimal(row.get("high"), label="history high"),
                    low=_decimal(row.get("low"), label="history low"),
                    close=_decimal(row.get("close"), label="history close"),
                    volume=int(volume),
                    amount=_decimal(row.get("amount"), label="history amount"),
                )
            )
        values = tuple(sorted(bars, key=lambda item: item.session))
        if not values or values[-1].session != through:
            raise DailyDataUpdateError(f"XtQuant daily history does not reach {through}: {symbol}")
        if len({item.session for item in values}) != len(values):
            raise DailyDataUpdateError(f"XtQuant daily history contains duplicate sessions: {symbol}")
        return values

    def fetch(
        self,
        symbols: tuple[str, ...],
        *,
        through: date,
    ) -> Mapping[str, tuple[DailyBar, ...]]:
        if type(through) is not date:
            raise TypeError("XtQuant history target must be calendar date")
        if not isinstance(symbols, tuple) or not symbols:
            raise ValueError("XtQuant history provider requires symbols")
        normalized = tuple(sorted({Symbol.parse(item).canonical for item in symbols}))
        return {symbol: self._fetch_symbol(symbol, through=through) for symbol in normalized}


__all__ = ("OfficialXtQuantDailyHistoryProvider",)
