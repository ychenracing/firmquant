"""Point-in-time daily data seals and execution-fact freshness checks."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum

from firmquant.domain.broker_facts import MarketSessionStatus
from firmquant.execution.planner import ExecutionBrokerSnapshot
from firmquant.persistence.repositories import canonical_sha256

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class DataValidationError(RuntimeError):
    """Raised when strategy or execution data cannot safely authorize work."""


class DataKind(StrEnum):
    EQUITY = "EQUITY"
    INDEX = "INDEX"


class Adjustment(StrEnum):
    FORWARD_ADJUSTED = "FORWARD_ADJUSTED"
    UNADJUSTED = "UNADJUSTED"


def _digest(value: str, *, label: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise DataValidationError(f"{label} must be lowercase SHA-256")


def _aware(value: datetime, *, label: str) -> None:
    if not isinstance(value, datetime):
        raise DataValidationError(f"{label} must be datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise DataValidationError(f"{label} must be timezone-aware")


def _date(value: date, *, label: str) -> None:
    if type(value) is not date:
        raise DataValidationError(f"{label} must be a date")


@dataclass(frozen=True, slots=True)
class SeriesSeal:
    """Hashes required to prove that an updated series only appended rows."""

    series_id: str
    kind: DataKind
    adjustment: Adjustment
    first_session: date
    last_session: date
    row_count: int
    full_sha256: str
    verified_prefix_row_count: int
    verified_prefix_sha256: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.series_id, str)
            or not self.series_id
            or self.series_id != self.series_id.strip()
        ):
            raise DataValidationError("series id must be canonical text")
        if not isinstance(self.kind, DataKind):
            raise DataValidationError("series kind must be typed")
        if not isinstance(self.adjustment, Adjustment):
            raise DataValidationError("series adjustment must be typed")
        _date(self.first_session, label="series first session")
        _date(self.last_session, label="series last session")
        if self.last_session < self.first_session:
            raise DataValidationError("series last session precedes first session")
        for value, label in (
            (self.row_count, "series row count"),
            (self.verified_prefix_row_count, "series prefix row count"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise DataValidationError(f"{label} must be a nonnegative integer")
        if self.row_count <= 0:
            raise DataValidationError("series row count must be positive")
        if self.verified_prefix_row_count >= self.row_count:
            raise DataValidationError("verified prefix must be shorter than the full series")
        _digest(self.full_sha256, label="series full digest")
        _digest(self.verified_prefix_sha256, label="series prefix digest")


@dataclass(frozen=True, slots=True)
class DataManifest:
    """Immutable manifest for the exact daily data consumed by uquant."""

    latest_common_session: date
    captured_at: datetime
    provider: str
    series: tuple[SeriesSeal, ...]

    def __post_init__(self) -> None:
        _date(self.latest_common_session, label="latest common session")
        _aware(self.captured_at, label="data manifest captured_at")
        if not isinstance(self.provider, str) or not self.provider or self.provider != self.provider.strip():
            raise DataValidationError("data provider must be canonical text")
        if not isinstance(self.series, tuple) or not all(
            isinstance(item, SeriesSeal) for item in self.series
        ):
            raise DataValidationError("data manifest series must be a typed tuple")
        if not self.series:
            raise DataValidationError("data manifest must contain series")
        identities = [item.series_id for item in self.series]
        if len(identities) != len(set(identities)):
            raise DataValidationError("data manifest contains duplicate series")
        if any(item.last_session != self.latest_common_session for item in self.series):
            raise DataValidationError("series do not share the declared latest common session")

    @property
    def sha256(self) -> str:
        return canonical_sha256(
            {
                "schema": "firmquant.strategy-data-manifest.v1",
                "latest_common_session": self.latest_common_session,
                "captured_at": self.captured_at,
                "provider": self.provider,
                "series": [
                    {
                        "series_id": item.series_id,
                        "kind": item.kind,
                        "adjustment": item.adjustment,
                        "first_session": item.first_session,
                        "last_session": item.last_session,
                        "row_count": item.row_count,
                        "full_sha256": item.full_sha256,
                        "verified_prefix_row_count": item.verified_prefix_row_count,
                        "verified_prefix_sha256": item.verified_prefix_sha256,
                    }
                    for item in sorted(self.series, key=lambda value: value.series_id)
                ],
            }
        )


@dataclass(frozen=True, slots=True)
class DataValidationReceipt:
    latest_common_session: date
    previous_manifest_sha256: str
    current_manifest_sha256: str


@dataclass(frozen=True, slots=True)
class ExecutionFactsReceipt:
    execution_session: date
    broker_snapshot_sha256: str
    quote_count: int


class StrategyDataValidator:
    """Preserve uquant data semantics and reject every historical rewrite."""

    def __init__(self, *, max_manifest_age: timedelta) -> None:
        if not isinstance(max_manifest_age, timedelta):
            raise TypeError("maximum manifest age must be timedelta")
        if max_manifest_age <= timedelta(0):
            raise ValueError("maximum manifest age must be positive")
        self._max_manifest_age = max_manifest_age

    def validate(
        self,
        *,
        previous: DataManifest,
        current: DataManifest,
        target_session: date,
        now: datetime,
    ) -> DataValidationReceipt:
        if not isinstance(previous, DataManifest) or not isinstance(current, DataManifest):
            raise DataValidationError("strategy data manifests must be typed")
        _date(target_session, label="target session")
        _aware(now, label="data validation time")
        if current.latest_common_session != target_session:
            raise DataValidationError("latest common session does not equal target session")
        age = now - current.captured_at
        if age < timedelta(0):
            raise DataValidationError("data manifest timestamp is in the future")
        if age > self._max_manifest_age:
            raise DataValidationError("strategy data manifest is stale")
        previous_by_id = {item.series_id: item for item in previous.series}
        current_by_id = {item.series_id: item for item in current.series}
        if current.provider != previous.provider:
            raise DataValidationError("strategy data provider changed")
        if previous_by_id.keys() != current_by_id.keys():
            raise DataValidationError("strategy data series set changed")
        for series_id, current_series in current_by_id.items():
            prior = previous_by_id[series_id]
            if current_series.kind is DataKind.EQUITY:
                expected_adjustment = Adjustment.FORWARD_ADJUSTED
            else:
                expected_adjustment = Adjustment.UNADJUSTED
            if current_series.adjustment is not expected_adjustment:
                raise DataValidationError("strategy data adjustment contract changed")
            if current_series.kind is not prior.kind or current_series.adjustment is not prior.adjustment:
                raise DataValidationError("strategy data series semantics changed")
            if current_series.first_session != prior.first_session:
                raise DataValidationError("strategy data historical start drifted")
            if current_series.row_count <= prior.row_count:
                raise DataValidationError("strategy data update did not append rows")
            if current_series.verified_prefix_row_count != prior.row_count:
                raise DataValidationError("strategy data prefix row count is not the prior history")
            if current_series.verified_prefix_sha256 != prior.full_sha256:
                raise DataValidationError("strategy data history prefix drift detected")
        return DataValidationReceipt(
            latest_common_session=current.latest_common_session,
            previous_manifest_sha256=previous.sha256,
            current_manifest_sha256=current.sha256,
        )


def validate_execution_facts(
    facts: ExecutionBrokerSnapshot,
    *,
    execution_session: date,
    now: datetime,
    max_age: timedelta,
) -> ExecutionFactsReceipt:
    """Require fresh broker facts and continuous trading; never infer limits locally."""

    if not isinstance(facts, ExecutionBrokerSnapshot):
        raise DataValidationError("execution facts must be a typed broker snapshot")
    _date(execution_session, label="execution session")
    _aware(now, label="execution validation time")
    if not isinstance(max_age, timedelta) or max_age <= timedelta(0):
        raise DataValidationError("maximum execution fact age must be positive")
    if facts.market_status is not MarketSessionStatus.OPEN:
        raise DataValidationError("execution facts require continuous OPEN market status")
    if facts.broker_snapshot.session_date != execution_session:
        raise DataValidationError("broker snapshot session differs from execution session")
    snapshot_age = now - facts.broker_snapshot.captured_at
    if snapshot_age < timedelta(0) or snapshot_age > max_age:
        raise DataValidationError("broker snapshot is stale or future-dated")
    if not facts.quotes:
        raise DataValidationError("execution facts contain no quotes")
    instrument_symbols = {item.symbol for item in facts.instruments}
    quote_symbols = {item.symbol for item in facts.quotes}
    if instrument_symbols != quote_symbols:
        raise DataValidationError("execution instrument and quote sets differ")
    for instrument in facts.instruments:
        age = now - instrument.observed_at
        if age < timedelta(0) or age > max_age:
            raise DataValidationError("execution instrument metadata is stale or future-dated")
    for quote in facts.quotes:
        for observed in (quote.event_time, quote.received_at):
            age = now - observed
            if age < timedelta(0) or age > max_age:
                raise DataValidationError("execution quote is stale or future-dated")
    return ExecutionFactsReceipt(
        execution_session=execution_session,
        broker_snapshot_sha256=facts.sha256,
        quote_count=len(facts.quotes),
    )


__all__ = (
    "Adjustment",
    "DataKind",
    "DataManifest",
    "DataValidationError",
    "DataValidationReceipt",
    "ExecutionFactsReceipt",
    "SeriesSeal",
    "StrategyDataValidator",
    "validate_execution_facts",
)
