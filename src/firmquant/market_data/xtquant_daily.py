"""Atomic daily-data updates that reject silent historical rewrites."""

from __future__ import annotations

import csv
import importlib
import os
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol, cast, runtime_checkable


class SourceEpochResealRequired(RuntimeError):
    """Adjusted history changed and requires an explicit reviewed source epoch."""


class DailyDataUpdateError(RuntimeError):
    """Daily market data could not be proven safe for a strategy decision."""


@dataclass(frozen=True, slots=True)
class DailyBar:
    session: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    amount: Decimal

    def __post_init__(self) -> None:
        if type(self.session) is not date:
            raise TypeError("daily bar session must be a calendar date")
        for label, value in (
            ("open", self.open),
            ("high", self.high),
            ("low", self.low),
            ("close", self.close),
            ("amount", self.amount),
        ):
            if not isinstance(value, Decimal) or not value.is_finite():
                raise TypeError(f"daily bar {label} must be finite Decimal")
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("daily bar prices must be positive")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("daily bar high is inconsistent")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("daily bar low is inconsistent")
        if isinstance(self.volume, bool) or not isinstance(self.volume, int) or self.volume < 0:
            raise ValueError("daily bar volume must be a nonnegative integer")
        if self.amount < 0:
            raise ValueError("daily bar amount must be nonnegative")


@runtime_checkable
class DailyHistoryProvider(Protocol):
    def fetch(
        self,
        symbols: tuple[str, ...],
        *,
        through: date,
    ) -> Mapping[str, tuple[DailyBar, ...]]: ...


@dataclass(frozen=True, slots=True)
class DailyDataUpdateReceipt:
    latest_common_session: date
    manifest_sha256: str
    appended_rows: int
    symbols: tuple[str, ...]


class _UquantManifest(Protocol):
    digest: str
    end: str
    symbols: tuple[str, ...]


class _UquantDataStore(Protocol):
    def manifest(
        self,
        symbols: tuple[str, ...],
        *,
        source: str = "frozen",
        as_of: str | None = None,
    ) -> _UquantManifest: ...


def _canonical_symbol(raw: str) -> str:
    if not isinstance(raw, str):
        raise TypeError("daily data symbol must be text")
    value = raw.strip().lower().replace(".", "")
    if len(value) == 8 and value[:2] in {"sh", "sz", "bj"} and value[2:].isdigit():
        return value
    raise ValueError(f"invalid canonical daily data symbol: {raw!r}")


def _decimal(raw: object, *, label: str) -> Decimal:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError) as error:
        raise DailyDataUpdateError(f"{label} is not decimal-compatible") from error
    if not value.is_finite():
        raise DailyDataUpdateError(f"{label} must be finite")
    return value


def _read_existing(path: Path) -> tuple[DailyBar, ...]:
    if not path.exists():
        return ()
    if path.is_symlink() or not path.is_file():
        raise DailyDataUpdateError("market data path must be a regular file")
    try:
        with path.open("r", encoding="utf-8", newline="") as stream:
            rows = tuple(csv.DictReader(stream))
    except (OSError, UnicodeDecodeError, csv.Error) as error:
        raise DailyDataUpdateError(f"cannot read market data file: {path.name}") from error
    bars: list[DailyBar] = []
    for row in rows:
        try:
            session = date.fromisoformat(str(row["date"]))
            volume_value = _decimal(row["volume"], label="existing volume")
            if volume_value != volume_value.to_integral_value():
                raise DailyDataUpdateError("existing volume must be whole shares")
            bars.append(
                DailyBar(
                    session=session,
                    open=_decimal(row["open"], label="existing open"),
                    high=_decimal(row["high"], label="existing high"),
                    low=_decimal(row["low"], label="existing low"),
                    close=_decimal(row["close"], label="existing close"),
                    volume=int(volume_value),
                    amount=_decimal(
                        row.get("amount") or _decimal(row["close"], label="existing close") * volume_value,
                        label="existing amount",
                    ),
                )
            )
        except (KeyError, ValueError) as error:
            raise DailyDataUpdateError(f"market data row is invalid: {path.name}") from error
    _validate_series(tuple(bars), label=path.name, allow_empty=True)
    return tuple(bars)


def _validate_series(bars: tuple[DailyBar, ...], *, label: str, allow_empty: bool = False) -> None:
    if not isinstance(bars, tuple) or not all(isinstance(item, DailyBar) for item in bars):
        raise TypeError(f"{label} daily bars must be tuple[DailyBar, ...]")
    if not bars and not allow_empty:
        raise DailyDataUpdateError(f"{label} daily history is empty")
    sessions = tuple(item.session for item in bars)
    if sessions != tuple(sorted(set(sessions))):
        raise DailyDataUpdateError(f"{label} sessions must be sorted and unique")


def _render(bars: tuple[DailyBar, ...]) -> str:
    lines = ["date,open,high,low,close,volume,amount\n"]
    lines.extend(
        ",".join(
            (
                item.session.isoformat(),
                format(item.open, "f"),
                format(item.high, "f"),
                format(item.low, "f"),
                format(item.close, "f"),
                str(item.volume),
                format(item.amount, "f"),
            )
        )
        + "\n"
        for item in bars
    )
    return "".join(lines)


def _merge(existing: tuple[DailyBar, ...], incoming: tuple[DailyBar, ...], *, symbol: str) -> tuple[tuple[DailyBar, ...], int]:
    _validate_series(incoming, label=symbol)
    incoming_by_session = {item.session: item for item in incoming}
    for prior in existing:
        observed = incoming_by_session.get(prior.session)
        if observed is None or observed != prior:
            raise SourceEpochResealRequired(
                f"SOURCE_EPOCH_RESEAL_REQUIRED:{symbol}:{prior.session.isoformat()}"
            )
    last_existing = existing[-1].session if existing else None
    appended = tuple(
        item for item in incoming if last_existing is None or item.session > last_existing
    )
    return (*existing, *appended), len(appended)


def _data_store(root: Path) -> _UquantDataStore:
    try:
        module = importlib.import_module("uquant.data")
        factory = module.DataStore  # type: ignore[attr-defined]
        if not callable(factory):
            raise TypeError
        return cast(_UquantDataStore, factory(root))
    except (AttributeError, ImportError, TypeError) as error:
        raise DailyDataUpdateError("locked uquant DataStore contract is unavailable") from error


class XtQuantDailyDataUpdater:
    """Stage all symbols, prove append-only history, then atomically publish every file."""

    def __init__(self, *, root: Path, provider: DailyHistoryProvider) -> None:
        self._root = Path(root)
        if self._root.is_symlink() or not self._root.is_dir():
            raise DailyDataUpdateError("daily data root must be an existing non-symlink directory")
        if not isinstance(provider, DailyHistoryProvider):
            raise TypeError("daily history provider does not satisfy its contract")
        self._provider = provider

    def _path(self, symbol: str) -> Path:
        canonical = _canonical_symbol(symbol)
        prefixed = self._root / f"{canonical}.csv"
        bare = self._root / f"{canonical[2:]}.csv"
        if prefixed.exists():
            return prefixed
        if bare.exists():
            return bare
        return prefixed

    def update(self, symbols: tuple[str, ...], *, through: date) -> DailyDataUpdateReceipt:
        if type(through) is not date:
            raise TypeError("daily update through must be a calendar date")
        canonical = tuple(sorted({_canonical_symbol(item) for item in symbols}))
        if not canonical:
            raise DailyDataUpdateError("daily update requires at least one symbol")
        raw = self._provider.fetch(canonical, through=through)
        if not isinstance(raw, Mapping) or set(raw) != set(canonical):
            raise DailyDataUpdateError("daily history provider returned incomplete symbol set")

        candidates: dict[str, tuple[DailyBar, ...]] = {}
        destinations: dict[str, Path] = {}
        appended_rows = 0
        for symbol in canonical:
            incoming = raw[symbol]
            _validate_series(incoming, label=symbol)
            if incoming[-1].session != through:
                raise DailyDataUpdateError(f"{symbol} does not reach target trading session")
            destination = self._path(symbol)
            existing = _read_existing(destination)
            merged, appended = _merge(existing, incoming, symbol=symbol)
            candidates[symbol] = merged
            destinations[symbol] = destination
            appended_rows += appended

        with tempfile.TemporaryDirectory(prefix="firmquant-data-", dir=self._root.parent) as temporary:
            staging = Path(temporary)
            for symbol, bars in candidates.items():
                (staging / f"{symbol}.csv").write_text(_render(bars), encoding="utf-8", newline="\n")
            manifest = _data_store(staging).manifest(
                canonical,
                source="xtquant",
                as_of=through.isoformat(),
            )
            if manifest.end != through.isoformat() or manifest.symbols != canonical:
                raise DailyDataUpdateError("uquant manifest does not match updated trading session")
            for symbol, destination in destinations.items():
                source = staging / f"{symbol}.csv"
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary_target = destination.with_suffix(destination.suffix + ".new")
                shutil.copyfile(source, temporary_target)
                os.replace(temporary_target, destination)

        return DailyDataUpdateReceipt(
            latest_common_session=through,
            manifest_sha256=manifest.digest,
            appended_rows=appended_rows,
            symbols=canonical,
        )


__all__ = (
    "DailyBar",
    "DailyDataUpdateError",
    "DailyDataUpdateReceipt",
    "DailyHistoryProvider",
    "SourceEpochResealRequired",
    "XtQuantDailyDataUpdater",
)
