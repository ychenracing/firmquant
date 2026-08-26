"""Ports for uquant-compatible daily data and execution-only market facts."""

from __future__ import annotations

from datetime import date
from typing import Protocol

from firmquant.domain.broker_facts import InstrumentFact, MarketSessionStatus, QuoteFact
from firmquant.domain.values import Symbol

from .validation import DataManifest


class StrategyDailyDataGateway(Protocol):
    """Refresh only the data contract consumed by locked uquant."""

    def refresh(self, target_session: date) -> DataManifest: ...

    def previous_manifest(self, target_session: date) -> DataManifest: ...


class ExecutionMarketDataGateway(Protocol):
    """Read intraday facts for execution and safety, never strategy selection."""

    def query_instrument(self, symbol: Symbol) -> InstrumentFact: ...

    def query_quote(self, symbol: Symbol) -> QuoteFact: ...

    def query_market_status(self) -> MarketSessionStatus: ...


__all__ = ("ExecutionMarketDataGateway", "StrategyDailyDataGateway")
