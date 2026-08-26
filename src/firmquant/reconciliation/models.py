"""Typed views that preserve broker, strategy, and operational authority boundaries."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final

from firmquant.domain.broker_facts import (
    AccountType,
    BrokerSnapshot,
    Side,
)
from firmquant.domain.errors import DomainTypeError, DomainValidationError
from firmquant.domain.orders import OrderState
from firmquant.domain.values import Money, Shares, Symbol

_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
_RECONCILIATION_ID: Final = re.compile(r"^recon_[0-9a-f]{64}$")


class ReconciliationKind(StrEnum):
    STARTUP = "STARTUP"
    INTRADAY = "INTRADAY"
    EOD = "EOD"
    MANUAL = "MANUAL"
    RECOVERY = "RECOVERY"


def _canonical_text(value: str, *, label: str, maximum: int = 256) -> None:
    if not isinstance(value, str):
        raise DomainTypeError(f"{label} must be text")
    if not value or value != value.strip() or len(value) > maximum:
        raise DomainValidationError(f"{label} must be canonical non-empty text")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise DomainValidationError(f"{label} contains control characters")


def _digest(value: str, *, label: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise DomainValidationError(f"{label} must be lowercase SHA-256")


def _aware(value: datetime, *, label: str) -> None:
    if not isinstance(value, datetime):
        raise DomainTypeError(f"{label} must be datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise DomainValidationError(f"{label} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ExpectedPosition:
    """Strategy account's post-sync economic view; lifecycle remains owned by uquant."""

    symbol: Symbol
    total_shares: Shares
    sellable_shares: Shares

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, Symbol):
            raise DomainTypeError("expected position symbol must be Symbol")
        if not isinstance(self.total_shares, Shares) or not isinstance(self.sellable_shares, Shares):
            raise DomainTypeError("expected position quantities must be Shares")
        if not self.total_shares.is_positive:
            raise DomainValidationError("expected position must contain positive shares")
        if self.sellable_shares.value > self.total_shares.value:
            raise DomainValidationError("expected sellable shares exceed position shares")


@dataclass(frozen=True, slots=True)
class StrategyAccountView:
    """Only reconciliation fields projected from uquant AccountState after broker sync."""

    available_cash: Money
    total_assets: Money
    positions: tuple[ExpectedPosition, ...]
    known_uquant_order_ids: frozenset[str]
    economic_state_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.available_cash, Money) or not isinstance(self.total_assets, Money):
            raise DomainTypeError("strategy account cash and assets must be Money")
        if self.available_cash.value > self.total_assets.value:
            raise DomainValidationError("strategy available cash cannot exceed total assets")
        if not isinstance(self.positions, tuple) or not all(
            isinstance(position, ExpectedPosition) for position in self.positions
        ):
            raise DomainTypeError("strategy positions must be typed tuple")
        symbols = tuple(position.symbol for position in self.positions)
        if len(symbols) != len(set(symbols)):
            raise DomainValidationError("strategy positions contain duplicate symbols")
        if not isinstance(self.known_uquant_order_ids, frozenset):
            raise DomainTypeError("known uquant order ids must be frozenset")
        for order_id in self.known_uquant_order_ids:
            _canonical_text(order_id, label="known uquant order id")
        _digest(self.economic_state_sha256, label="strategy economic state digest")


@dataclass(frozen=True, slots=True)
class OperationalOrderView:
    """firmquant-owned broker mapping and durable local lifecycle evidence."""

    broker_order_id: str
    uquant_order_id: str
    symbol: Symbol
    side: Side
    requested_shares: Shares
    filled_shares: Shares
    local_state: OrderState

    def __post_init__(self) -> None:
        _canonical_text(self.broker_order_id, label="operational broker order id")
        _canonical_text(self.uquant_order_id, label="operational uquant order id")
        if not isinstance(self.symbol, Symbol):
            raise DomainTypeError("operational order symbol must be Symbol")
        if not isinstance(self.side, Side):
            raise DomainTypeError("operational order side must be Side")
        if not isinstance(self.requested_shares, Shares) or not isinstance(self.filled_shares, Shares):
            raise DomainTypeError("operational order quantities must be Shares")
        if not self.requested_shares.is_positive:
            raise DomainValidationError("operational requested shares must be positive")
        if self.filled_shares.value > self.requested_shares.value:
            raise DomainValidationError("operational filled shares exceed request")
        if not isinstance(self.local_state, OrderState):
            raise DomainTypeError("operational local state must be OrderState")


@dataclass(frozen=True, slots=True)
class OperationalLedgerView:
    """Online facts owned by firmquant, never a second strategy account."""

    expected_account_id_hash: str
    expected_account_type: AccountType
    orders: tuple[OperationalOrderView, ...]
    known_broker_fill_ids: frozenset[str]
    unresolved_execution_ids: tuple[str, ...]
    submitting_unresolved_execution_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _digest(self.expected_account_id_hash, label="expected account identity hash")
        if not isinstance(self.expected_account_type, AccountType):
            raise DomainTypeError("expected account type must be AccountType")
        if not isinstance(self.orders, tuple) or not all(
            isinstance(order, OperationalOrderView) for order in self.orders
        ):
            raise DomainTypeError("operational orders must be typed tuple")
        broker_ids = tuple(order.broker_order_id for order in self.orders)
        if len(broker_ids) != len(set(broker_ids)):
            raise DomainValidationError("operational orders contain duplicate broker ids")
        if not isinstance(self.known_broker_fill_ids, frozenset):
            raise DomainTypeError("known broker fill ids must be frozenset")
        for fill_id in self.known_broker_fill_ids:
            _canonical_text(fill_id, label="known broker fill id")
        for identity_label, identities in (
            ("unresolved execution id", self.unresolved_execution_ids),
            (
                "submitting unresolved execution id",
                self.submitting_unresolved_execution_ids,
            ),
        ):
            if not isinstance(identities, tuple):
                raise DomainTypeError(f"{identity_label} collection must be tuple")
            if len(identities) != len(set(identities)):
                raise DomainValidationError(f"{identity_label} collection contains duplicates")
            for identity in identities:
                _canonical_text(identity, label=identity_label)


@dataclass(frozen=True, slots=True)
class ReconciliationFacts:
    broker_snapshot: BrokerSnapshot
    strategy_account: StrategyAccountView
    operational_ledger: OperationalLedgerView
    company_action_suspected_symbols: frozenset[Symbol]
    uquant_code_identity_matches: bool
    data_identity_matches: bool
    config_identity_matches: bool

    def __post_init__(self) -> None:
        if not isinstance(self.broker_snapshot, BrokerSnapshot):
            raise DomainTypeError("reconciliation broker snapshot must be BrokerSnapshot")
        if not isinstance(self.strategy_account, StrategyAccountView):
            raise DomainTypeError("reconciliation strategy account must be StrategyAccountView")
        if not isinstance(self.operational_ledger, OperationalLedgerView):
            raise DomainTypeError("reconciliation ledger must be OperationalLedgerView")
        if not isinstance(self.company_action_suspected_symbols, frozenset) or not all(
            isinstance(symbol, Symbol) for symbol in self.company_action_suspected_symbols
        ):
            raise DomainTypeError("company action suspicions must be frozenset[Symbol]")
        identities = (
            self.uquant_code_identity_matches,
            self.data_identity_matches,
            self.config_identity_matches,
        )
        if not all(isinstance(value, bool) for value in identities):
            raise DomainTypeError("reconciliation identity flags must be bool")


@dataclass(frozen=True, slots=True)
class ReconciliationReceipt:
    reconciliation_id: str
    kind: ReconciliationKind
    snapshot_id: str
    started_at: datetime
    completed_at: datetime
    passed: bool
    blockers: tuple[str, ...]
    operator_actions: tuple[str, ...]
    details_json: str
    details_sha256: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.reconciliation_id, str)
            or _RECONCILIATION_ID.fullmatch(self.reconciliation_id) is None
        ):
            raise DomainValidationError("reconciliation id is not canonical")
        if not isinstance(self.kind, ReconciliationKind):
            raise DomainTypeError("reconciliation kind must be ReconciliationKind")
        _canonical_text(self.snapshot_id, label="reconciliation snapshot id")
        _aware(self.started_at, label="reconciliation started_at")
        _aware(self.completed_at, label="reconciliation completed_at")
        if self.completed_at < self.started_at:
            raise DomainValidationError("reconciliation completed before it started")
        if not isinstance(self.passed, bool):
            raise DomainTypeError("reconciliation passed must be bool")
        for collection_label, values in (
            ("blockers", self.blockers),
            ("operator actions", self.operator_actions),
        ):
            if not isinstance(values, tuple):
                raise DomainTypeError(f"reconciliation {collection_label} must be tuple")
            if tuple(sorted(set(values))) != values:
                raise DomainValidationError(f"reconciliation {collection_label} must be sorted and unique")
            for value in values:
                _canonical_text(value, label=f"reconciliation {collection_label}")
        if self.passed != (not self.blockers):
            raise DomainValidationError("reconciliation pass result contradicts blockers")
        if not isinstance(self.details_json, str) or not self.details_json:
            raise DomainValidationError("reconciliation details JSON must not be empty")
        _digest(self.details_sha256, label="reconciliation details digest")

    @property
    def halt_required(self) -> bool:
        return not self.passed


__all__ = (
    "ExpectedPosition",
    "OperationalLedgerView",
    "OperationalOrderView",
    "ReconciliationFacts",
    "ReconciliationKind",
    "ReconciliationReceipt",
    "StrategyAccountView",
)
