"""Atomic daily-data updates with authoritative suspension and bounded-retry semantics."""

from __future__ import annotations

import csv
import hashlib
import importlib
import json
import os
import shutil
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast, runtime_checkable

from firmquant.market_data.generations import DataGenerationStore


class SourceEpochResealRequired(RuntimeError):
    """Adjusted history changed and requires an explicit reviewed source epoch."""

    def __init__(self, message: str, *, candidate_id: str | None = None) -> None:
        self.candidate_id = candidate_id
        super().__init__(message)


class DailyDataUpdateError(RuntimeError):
    """Daily market data could not be proven safe for a strategy decision."""


class DailyDataDeadlineExceeded(DailyDataUpdateError):
    """The bounded close-data deadline elapsed before a safe update was available."""


class DailyDataRetriesExhausted(DailyDataUpdateError):
    """The bounded close-data attempt budget was exhausted."""


class InstrumentSessionState(StrEnum):
    TRADING = "TRADING"
    SUSPENDED = "SUSPENDED"
    NON_TRADING = "NON_TRADING"


@dataclass(frozen=True, slots=True)
class InstrumentSessionStatus:
    symbol: str
    session: date
    state: InstrumentSessionState
    observed_at: datetime
    source: str
    raw_payload_sha256: str

    def __post_init__(self) -> None:
        _canonical_symbol(self.symbol)
        if type(self.session) is not date:
            raise TypeError("instrument status session must be a calendar date")
        if not isinstance(self.state, InstrumentSessionState):
            raise TypeError("instrument status state is invalid")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise TypeError("instrument status observed_at must be timezone-aware")
        if not isinstance(self.source, str) or not self.source or self.source != self.source.strip():
            raise ValueError("instrument status source must be canonical text")
        if (
            not isinstance(self.raw_payload_sha256, str)
            or len(self.raw_payload_sha256) != 64
            or any(ch not in "0123456789abcdef" for ch in self.raw_payload_sha256)
        ):
            raise ValueError("instrument status raw payload digest must be lowercase SHA-256")

    @property
    def evidence_sha256(self) -> str:
        payload = {
            "symbol": _canonical_symbol(self.symbol),
            "session": self.session.isoformat(),
            "state": self.state.value,
            "observed_at": self.observed_at.astimezone(UTC).isoformat(),
            "source": self.source,
            "raw_payload_sha256": self.raw_payload_sha256,
        }
        return hashlib.sha256(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()).hexdigest()


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


@runtime_checkable
class InstrumentStatusProvider(Protocol):
    def fetch_status(
        self,
        symbols: tuple[str, ...],
        *,
        session: date,
    ) -> Mapping[str, InstrumentSessionStatus]: ...


@dataclass(frozen=True, slots=True)
class DailyFetchPolicy:
    max_attempts: int = 3
    retry_interval_seconds: float = 5.0
    total_deadline_seconds: float = 60.0

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or self.max_attempts < 1
        ):
            raise ValueError("daily fetch max_attempts must be a positive integer")
        for label, value in (
            ("retry interval", self.retry_interval_seconds),
            ("total deadline", self.total_deadline_seconds),
        ):
            if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
                raise ValueError(f"daily fetch {label} must be positive")
        if self.retry_interval_seconds > self.total_deadline_seconds:
            raise ValueError("daily fetch retry interval cannot exceed total deadline")


@dataclass(frozen=True, slots=True)
class DailySeriesObservation:
    symbol: str
    latest_observed_session: date
    suspension_evidence_sha256: str | None


@dataclass(frozen=True, slots=True)
class DailyDataUpdateReceipt:
    latest_common_session: date
    manifest_sha256: str
    appended_rows: int
    symbols: tuple[str, ...]
    observations: tuple[DailySeriesObservation, ...] = ()
    fetch_attempts: int = 1
    governance_manifest_sha256: str | None = None
    data_generation_id: str | None = None


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


def _merge(
    existing: tuple[DailyBar, ...], incoming: tuple[DailyBar, ...], *, symbol: str
) -> tuple[tuple[DailyBar, ...], int]:
    _validate_series(incoming, label=symbol)
    incoming_by_session = {item.session: item for item in incoming}
    for prior in existing:
        observed = incoming_by_session.get(prior.session)
        if observed is None or observed != prior:
            raise SourceEpochResealRequired(
                f"SOURCE_EPOCH_RESEAL_REQUIRED:{symbol}:{prior.session.isoformat()}"
            )
    last_existing = existing[-1].session if existing else None
    appended = tuple(item for item in incoming if last_existing is None or item.session > last_existing)
    return (*existing, *appended), len(appended)


def _data_store(root: Path) -> _UquantDataStore:
    try:
        module = importlib.import_module("uquant.data")
        factory = module.DataStore
        if not callable(factory):
            raise TypeError
        return cast(_UquantDataStore, factory(root))
    except (AttributeError, ImportError, TypeError) as error:
        raise DailyDataUpdateError("locked uquant DataStore contract is unavailable") from error


class XtQuantDailyDataUpdater:
    """Stage all symbols, prove history/status completeness, then publish atomically."""

    def __init__(
        self,
        *,
        root: Path,
        provider: DailyHistoryProvider,
        state_root: Path | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
        fetch_policy: DailyFetchPolicy | None = None,
        required_complete_symbols: frozenset[str] = frozenset(),
        max_status_age: timedelta = timedelta(minutes=15),
        generation_store: DataGenerationStore | None = None,
    ) -> None:
        self._root = Path(root)
        if self._root.exists() and (self._root.is_symlink() or not self._root.is_dir()):
            raise DailyDataUpdateError("daily data root must be a regular directory")
        self._root.mkdir(parents=True, exist_ok=True)
        if not isinstance(provider, DailyHistoryProvider):
            raise TypeError("daily history provider does not satisfy its contract")
        if generation_store is not None:
            active = generation_store.active()
            if active.path.resolve() != self._root.resolve():
                raise DailyDataUpdateError("daily data root is not the active data generation")
        self._provider = provider
        self._generation_store = generation_store
        self._state_root = Path(state_root) if state_root is not None else self._root / ".firmquant"
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep or time.sleep
        self._fetch_policy = fetch_policy or DailyFetchPolicy()
        self._required_complete_symbols = frozenset(
            _canonical_symbol(item) for item in required_complete_symbols
        )
        if max_status_age <= timedelta(0):
            raise ValueError("instrument status maximum age must be positive")
        self._max_status_age = max_status_age

    def _path(self, symbol: str) -> Path:
        canonical = _canonical_symbol(symbol)
        prefixed = self._root / f"{canonical}.csv"
        bare = self._root / f"{canonical[2:]}.csv"
        if prefixed.exists():
            return prefixed
        if bare.exists():
            return bare
        return prefixed

    def _record_attempt(self, *, session: date, attempt: int, error: Exception | None) -> None:
        directory = self._state_root / "attempts" / session.isoformat()
        directory.mkdir(parents=True, exist_ok=True)
        payload: dict[str, object] = {
            "schema": "firmquant.daily-data-attempt.v1",
            "session": session.isoformat(),
            "attempt": attempt,
            "success": error is None,
        }
        if error is not None:
            summary = f"{type(error).__name__}:{error}"
            payload["error_type"] = type(error).__name__
            payload["error_sha256"] = hashlib.sha256(summary.encode()).hexdigest()
        target = directory / f"attempt-{attempt:03d}.json"
        temporary = target.with_suffix(".json.new")
        temporary.write_text(
            json.dumps(payload, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, target)

    def _status_observations(
        self,
        raw: Mapping[str, tuple[DailyBar, ...]],
        *,
        canonical: tuple[str, ...],
        through: date,
    ) -> tuple[DailySeriesObservation, ...]:
        lagging = tuple(symbol for symbol in canonical if raw[symbol][-1].session < through)
        for symbol in lagging:
            if symbol in self._required_complete_symbols:
                raise DailyDataUpdateError(
                    f"required complete symbol {symbol} does not reach target trading session"
                )
        statuses: Mapping[str, InstrumentSessionStatus] = {}
        if lagging:
            if not isinstance(self._provider, InstrumentStatusProvider):
                raise DailyDataUpdateError(
                    "authoritative instrument status is unavailable for missing target bar"
                )
            statuses = self._provider.fetch_status(lagging, session=through)
            if not isinstance(statuses, Mapping) or set(statuses) != set(lagging):
                raise DailyDataUpdateError("authoritative instrument status set is incomplete")
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise DailyDataUpdateError("daily data clock must be timezone-aware")
        observations: list[DailySeriesObservation] = []
        for symbol in canonical:
            latest = raw[symbol][-1].session
            if latest > through:
                raise DailyDataUpdateError(f"{symbol} history exceeds target trading session")
            evidence: str | None = None
            if latest < through:
                fact = statuses[symbol]
                if _canonical_symbol(fact.symbol) != symbol or fact.session != through:
                    raise DailyDataUpdateError(f"{symbol} authoritative status does not match target session")
                age = now.astimezone(UTC) - fact.observed_at.astimezone(UTC)
                if age < timedelta(0) or age > self._max_status_age:
                    raise DailyDataUpdateError(f"{symbol} authoritative instrument status is stale")
                if fact.state not in {
                    InstrumentSessionState.SUSPENDED,
                    InstrumentSessionState.NON_TRADING,
                }:
                    raise DailyDataUpdateError(
                        f"{symbol} does not reach target session while security is trading"
                    )
                evidence = fact.evidence_sha256
            observations.append(
                DailySeriesObservation(
                    symbol=symbol,
                    latest_observed_session=latest,
                    suspension_evidence_sha256=evidence,
                )
            )
        return tuple(observations)

    def _acquire(
        self,
        canonical: tuple[str, ...],
        *,
        through: date,
    ) -> tuple[Mapping[str, tuple[DailyBar, ...]], tuple[DailySeriesObservation, ...], int]:
        started = self._monotonic()
        last_error: Exception | None = None
        for attempt in range(1, self._fetch_policy.max_attempts + 1):
            if attempt > 1 and self._monotonic() - started >= self._fetch_policy.total_deadline_seconds:
                raise DailyDataDeadlineExceeded("daily data total deadline exceeded") from last_error
            try:
                raw = self._provider.fetch(canonical, through=through)
                if not isinstance(raw, Mapping) or set(raw) != set(canonical):
                    raise DailyDataUpdateError("daily history provider returned incomplete symbol set")
                for symbol in canonical:
                    _validate_series(raw[symbol], label=symbol)
                observations = self._status_observations(
                    raw,
                    canonical=canonical,
                    through=through,
                )
            except Exception as error:  # provider/status boundary is deliberately fail-closed
                last_error = error
                self._record_attempt(session=through, attempt=attempt, error=error)
                if attempt >= self._fetch_policy.max_attempts:
                    raise DailyDataRetriesExhausted("daily data attempt budget exhausted") from error
                elapsed = self._monotonic() - started
                if (
                    elapsed + self._fetch_policy.retry_interval_seconds
                    > self._fetch_policy.total_deadline_seconds
                ):
                    raise DailyDataDeadlineExceeded("daily data total deadline exceeded") from error
                self._sleep(self._fetch_policy.retry_interval_seconds)
                continue
            self._record_attempt(session=through, attempt=attempt, error=None)
            return raw, observations, attempt
        raise DailyDataRetriesExhausted("daily data attempt budget exhausted") from last_error

    def _rewrite_candidate(
        self,
        *,
        canonical: tuple[str, ...],
        raw: Mapping[str, tuple[DailyBar, ...]],
        cause: SourceEpochResealRequired,
    ) -> None:
        if self._generation_store is None:
            raise cause
        active = self._generation_store.active()
        if active.path.resolve() != self._root.resolve():
            raise DailyDataUpdateError("active data generation changed during daily update")
        observed_at = self._clock()
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise DailyDataUpdateError("daily data clock must be timezone-aware")
        candidate = self._generation_store.create_candidate(
            active_generation_id=active.generation_id,
            replacement_rows={symbol: _render(raw[symbol]).encode() for symbol in canonical},
            source="xtquant",
            generated_at=observed_at,
        )
        raise SourceEpochResealRequired(
            f"SOURCE_EPOCH_RESEAL_REQUIRED:{candidate.candidate_id}",
            candidate_id=candidate.candidate_id,
        ) from cause

    def update(self, symbols: tuple[str, ...], *, through: date) -> DailyDataUpdateReceipt:
        if type(through) is not date:
            raise TypeError("daily update through must be a calendar date")
        canonical = tuple(sorted({_canonical_symbol(item) for item in symbols}))
        if not canonical:
            raise DailyDataUpdateError("daily update requires at least one symbol")
        if self._generation_store is not None:
            active = self._generation_store.active()
            if active.path.resolve() != self._root.resolve():
                raise DailyDataUpdateError("active data generation changed while daemon is running")
        raw, observations, fetch_attempts = self._acquire(canonical, through=through)

        candidates: dict[str, tuple[DailyBar, ...]] = {}
        destinations: dict[str, Path] = {}
        appended_rows = 0
        try:
            for symbol in canonical:
                destination = self._path(symbol)
                existing = _read_existing(destination)
                merged, appended = _merge(existing, raw[symbol], symbol=symbol)
                candidates[symbol] = merged
                destinations[symbol] = destination
                appended_rows += appended
        except SourceEpochResealRequired as error:
            self._rewrite_candidate(canonical=canonical, raw=raw, cause=error)
            raise AssertionError("rewrite candidate path must raise") from error

        with tempfile.TemporaryDirectory(prefix="firmquant-data-", dir=self._root.parent) as temporary:
            staging = Path(temporary)
            for symbol, bars in candidates.items():
                (staging / f"{symbol}.csv").write_text(
                    _render(bars),
                    encoding="utf-8",
                    newline="\n",
                )
            manifest = _data_store(staging).manifest(
                canonical,
                source="xtquant",
                as_of=through.isoformat(),
            )
            if manifest.symbols != canonical:
                raise DailyDataUpdateError("uquant manifest symbol identity does not match updated data")
            observed_common = min(item.latest_observed_session for item in observations)
            if date.fromisoformat(manifest.end) != observed_common:
                raise DailyDataUpdateError("uquant manifest does not match observed strategy-data coverage")
            for symbol, destination in destinations.items():
                source = staging / f"{symbol}.csv"
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary_target = destination.with_suffix(destination.suffix + ".new")
                shutil.copyfile(source, temporary_target)
                os.replace(temporary_target, destination)

        generation_id: str | None = None
        if self._generation_store is not None:
            refreshed = self._generation_store.refresh_active_manifest()
            generation_id = refreshed.generation_id

        governance = {
            "schema": "firmquant.daily-data-manifest.v2",
            "target_session": through.isoformat(),
            "source": "xtquant",
            "uquant_manifest_sha256": manifest.digest,
            "data_generation_id": generation_id,
            "observations": [
                {
                    "symbol": item.symbol,
                    "latest_observed_session": item.latest_observed_session.isoformat(),
                    "suspension_evidence_sha256": item.suspension_evidence_sha256,
                }
                for item in observations
            ],
        }
        rendered = json.dumps(governance, separators=(",", ":"), sort_keys=True).encode()
        governance_manifest_sha256 = hashlib.sha256(rendered).hexdigest()
        manifest_path = self._root / ".firmquant-data-manifest.json"
        temporary_manifest = manifest_path.with_suffix(".json.new")
        temporary_manifest.write_bytes(rendered)
        os.replace(temporary_manifest, manifest_path)
        archive = self._state_root / "data-manifests" / f"{through.isoformat()}.json"
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive_temp = archive.with_suffix(".json.new")
        archive_temp.write_bytes(rendered)
        os.replace(archive_temp, archive)

        return DailyDataUpdateReceipt(
            latest_common_session=through,
            manifest_sha256=manifest.digest,
            appended_rows=appended_rows,
            symbols=canonical,
            observations=observations,
            fetch_attempts=fetch_attempts,
            governance_manifest_sha256=governance_manifest_sha256,
            data_generation_id=generation_id,
        )


__all__ = (
    "DailyBar",
    "DailyDataDeadlineExceeded",
    "DailyDataRetriesExhausted",
    "DailyDataUpdateError",
    "DailyDataUpdateReceipt",
    "DailyFetchPolicy",
    "DailyHistoryProvider",
    "DailySeriesObservation",
    "InstrumentSessionState",
    "InstrumentSessionStatus",
    "InstrumentStatusProvider",
    "SourceEpochResealRequired",
    "XtQuantDailyDataUpdater",
)
