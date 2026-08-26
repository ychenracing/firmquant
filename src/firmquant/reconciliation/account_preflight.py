"""Fail-closed pre-sync account authority checks.

This module compares the broker snapshot with the current uquant AccountState
*before* broker facts are allowed to mutate the strategy account. Only
firmquant-owned, durably mapped and ingested broker fills may explain an
account delta. Exact reviewed cash differences may be adopted; reviewed
position differences still require an explicit reviewed AccountState so that
firmquant never invents strategy lifecycle or lot semantics.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol, cast

from firmquant.domain.broker_facts import BrokerOrderFact, BrokerSnapshot, FillStatus, Side
from firmquant.domain.errors import DomainTypeError, DomainValidationError
from firmquant.domain.values import Money, Symbol
from firmquant.persistence.account_authority import (
    AccountBinding,
    AdjustmentCoverage,
    ReviewedAccountAdjustmentRepository,
)
from firmquant.persistence.repositories import canonical_sha256

from .models import OperationalLedgerView, OperationalOrderView


class _AccountOrder(Protocol):
    order_id: str
    symbol: str
    side: str


class _AccountFill(Protocol):
    fill_id: str


class _AccountPosition(Protocol):
    shares: int

    def sellable_shares(self, date: str) -> int: ...


class _AccountState(Protocol):
    cash: float
    positions: dict[str, _AccountPosition]
    order_ledger: list[_AccountOrder]
    fills: list[_AccountFill]


@dataclass(frozen=True, slots=True)
class AccountPreflightResult:
    """Pure pre-sync verdict; reviewed evidence never invents account semantics."""

    passed: bool
    blockers: tuple[str, ...]
    explained_fill_ids: tuple[str, ...]
    reviewed_adjustment_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.passed, bool):
            raise DomainTypeError("account preflight passed must be bool")
        if tuple(sorted(set(self.blockers))) != self.blockers:
            raise DomainValidationError("account preflight blockers must be sorted and unique")
        if tuple(sorted(set(self.explained_fill_ids))) != self.explained_fill_ids:
            raise DomainValidationError("account preflight fill ids must be sorted and unique")
        if tuple(sorted(set(self.reviewed_adjustment_ids))) != self.reviewed_adjustment_ids:
            raise DomainValidationError("account preflight reviewed adjustment ids must be sorted and unique")
        if self.passed != (not self.blockers):
            raise DomainValidationError("account preflight pass result contradicts blockers")


def _sha256(value: str, *, label: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise DomainValidationError(f"{label} must be lowercase SHA-256")


def _difference_value(value: Decimal | int, *, label: str) -> Decimal | int:
    if isinstance(value, bool) or not isinstance(value, (Decimal, int)):
        raise DomainTypeError(f"{label} must be Decimal or int")
    if isinstance(value, Decimal) and not value.is_finite():
        raise DomainValidationError(f"{label} must be finite")
    if value < 0:
        raise DomainValidationError(f"{label} must be nonnegative")
    return value


def account_difference_sha256(
    *,
    account_id_hash: str,
    symbol: Symbol | None,
    session: date,
    coverage: AdjustmentCoverage,
    broker_snapshot_sha256: str,
    expected: Decimal | int,
    observed: Decimal | int,
) -> str:
    """Return the exact deterministic identity of one reviewed account delta."""

    _sha256(account_id_hash, label="account difference account identity")
    _sha256(broker_snapshot_sha256, label="account difference broker snapshot")
    if type(session) is not date:
        raise DomainTypeError("account difference session must be date")
    if not isinstance(coverage, AdjustmentCoverage):
        raise DomainTypeError("account difference coverage must be typed")
    if symbol is not None and not isinstance(symbol, Symbol):
        raise DomainTypeError("account difference symbol must be Symbol or None")
    if (
        coverage
        in {
            AdjustmentCoverage.POSITION_TOTAL_SHARES,
            AdjustmentCoverage.POSITION_SELLABLE_SHARES,
        }
        and symbol is None
    ):
        raise DomainValidationError("position account difference requires symbol")
    if (
        coverage in {AdjustmentCoverage.AVAILABLE_CASH, AdjustmentCoverage.TOTAL_ASSETS}
        and symbol is not None
    ):
        raise DomainValidationError("account-level difference must not carry a symbol")
    expected_value = _difference_value(expected, label="account difference expected value")
    observed_value = _difference_value(observed, label="account difference observed value")
    return canonical_sha256(
        {
            "schema": "firmquant.account-difference.v1",
            "account_id_hash": account_id_hash,
            "symbol": symbol,
            "session": session,
            "coverage": coverage,
            "broker_snapshot_sha256": broker_snapshot_sha256,
            "expected": expected_value,
            "observed": observed_value,
        }
    )


def _account_cash(account: _AccountState) -> Decimal:
    value = account.cash
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DomainTypeError("uquant account cash must be numeric")
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0:
        raise DomainValidationError("uquant account cash must be finite and nonnegative")
    return Decimal(str(numeric))


def _current_positions(account: _AccountState, *, session: str) -> dict[str, list[int]]:
    result: dict[str, list[int]] = {}
    for symbol, position in account.positions.items():
        if not isinstance(symbol, str) or not symbol:
            raise DomainValidationError("uquant account position symbol is invalid")
        shares = position.shares
        sellable = position.sellable_shares(session)
        if (
            isinstance(shares, bool)
            or not isinstance(shares, int)
            or isinstance(sellable, bool)
            or not isinstance(sellable, int)
            or shares <= 0
            or sellable < 0
            or sellable > shares
        ):
            raise DomainValidationError("uquant account position quantities are invalid")
        result[symbol] = [shares, sellable]
    return result


def _known_account_orders(account: _AccountState) -> dict[str, _AccountOrder]:
    result: dict[str, _AccountOrder] = {}
    for order in account.order_ledger:
        if not isinstance(order.order_id, str) or not order.order_id or order.order_id in result:
            raise DomainValidationError("uquant account order identity is invalid")
        result[order.order_id] = order
    return result


def _known_account_fill_ids(account: _AccountState) -> frozenset[str]:
    result: set[str] = set()
    for fill in account.fills:
        fill_id = fill.fill_id
        if not isinstance(fill_id, str) or not fill_id:
            continue
        if fill_id in result:
            raise DomainValidationError("uquant account contains duplicate fill identity")
        result.add(fill_id)
    return frozenset(result)


def _compare_account_identity(
    snapshot: BrokerSnapshot,
    operational_ledger: OperationalLedgerView,
    binding: AccountBinding,
    blockers: set[str],
) -> None:
    broker = snapshot.account
    if (
        broker.account_id_hash != binding.account_id_hash
        or broker.account_id_hash != operational_ledger.expected_account_id_hash
    ):
        blockers.add("ACCOUNT_IDENTITY_CHANGED")
    if broker.account_type is not binding.account_type or (
        broker.account_type is not operational_ledger.expected_account_type
    ):
        blockers.add("ACCOUNT_TYPE_CHANGED")


def _mapped_orders(
    snapshot: BrokerSnapshot,
    operational_ledger: OperationalLedgerView,
    blockers: set[str],
) -> tuple[dict[str, OperationalOrderView], dict[str, BrokerOrderFact]]:
    local_by_broker = {order.broker_order_id: order for order in operational_ledger.orders}
    broker_by_id = {order.broker_order_id: order for order in snapshot.orders}
    for broker_order in snapshot.orders:
        local = local_by_broker.get(broker_order.broker_order_id)
        if local is None:
            blockers.add("EXTERNAL_BROKER_ORDER")
            continue
        if (
            broker_order.client_order_id != local.uquant_order_id
            or broker_order.symbol != local.symbol
            or broker_order.side is not local.side
            or broker_order.requested_shares != local.requested_shares
        ):
            blockers.add("BROKER_ORDER_IDENTITY_MISMATCH")
    return local_by_broker, broker_by_id


def _matching_review_ids(
    repository: ReviewedAccountAdjustmentRepository | None,
    *,
    account_id_hash: str,
    symbol: Symbol | None,
    session: date,
    coverage: AdjustmentCoverage,
    broker_snapshot_sha256: str,
    expected: Decimal | int,
    observed: Decimal | int,
) -> tuple[str, ...]:
    if repository is None:
        return ()
    difference_sha256 = account_difference_sha256(
        account_id_hash=account_id_hash,
        symbol=symbol,
        session=session,
        coverage=coverage,
        broker_snapshot_sha256=broker_snapshot_sha256,
        expected=expected,
        observed=observed,
    )
    return repository.matching_ids(
        account_id_hash=account_id_hash,
        symbol=symbol,
        session=session,
        coverage=coverage,
        broker_snapshot_sha256=broker_snapshot_sha256,
        difference_sha256=difference_sha256,
    )


def evaluate_account_preflight(
    *,
    snapshot: BrokerSnapshot,
    account: object,
    operational_ledger: OperationalLedgerView,
    binding: AccountBinding,
    cash_tolerance: Money,
    reviewed_adjustments: ReviewedAccountAdjustmentRepository | None = None,
) -> AccountPreflightResult:
    """Explain broker deltas only from owned execution facts or exact reviewed evidence.

    The function is intentionally pure: it never invokes uquant broker sync and
    never mutates the supplied AccountState. Cash may be authorized by an exact
    reviewed difference. Position/sellability reviews are recorded as evidence
    but still require an explicit reviewed AccountState before adoption.
    """

    if not isinstance(snapshot, BrokerSnapshot):
        raise DomainTypeError("account preflight snapshot must be BrokerSnapshot")
    if not isinstance(operational_ledger, OperationalLedgerView):
        raise DomainTypeError("account preflight ledger must be OperationalLedgerView")
    if not isinstance(binding, AccountBinding):
        raise DomainTypeError("account preflight binding must be AccountBinding")
    if not isinstance(cash_tolerance, Money):
        raise DomainTypeError("account preflight cash tolerance must be Money")
    if reviewed_adjustments is not None and not isinstance(
        reviewed_adjustments, ReviewedAccountAdjustmentRepository
    ):
        raise DomainTypeError("account preflight reviewed adjustments must be repository or None")

    state = cast(_AccountState, account)
    blockers: set[str] = set()
    explained_fill_ids: set[str] = set()
    reviewed_adjustment_ids: set[str] = set()
    _compare_account_identity(snapshot, operational_ledger, binding, blockers)

    local_by_broker, broker_by_id = _mapped_orders(snapshot, operational_ledger, blockers)
    account_orders = _known_account_orders(state)
    account_fill_ids = _known_account_fill_ids(state)
    session_text = snapshot.session_date.isoformat()
    expected_positions = _current_positions(state, session=session_text)
    expected_cash = _account_cash(state)

    ordered_fills = sorted(
        snapshot.fills,
        key=lambda fill: (fill.session_date, fill.event_sequence, fill.broker_fill_id),
    )
    for fill in ordered_fills:
        local = local_by_broker.get(fill.broker_order_id)
        broker_order = broker_by_id.get(fill.broker_order_id)
        if (
            local is None
            or broker_order is None
            or fill.broker_fill_id not in operational_ledger.known_broker_fill_ids
            or fill.status is not FillStatus.CONFIRMED
        ):
            blockers.add("UNMAPPED_BROKER_FILL")
            continue
        if (
            broker_order.client_order_id != local.uquant_order_id
            or fill.symbol != local.symbol
            or fill.side is not local.side
        ):
            blockers.add("BROKER_FILL_IDENTITY_MISMATCH")
            continue
        uquant_order = account_orders.get(local.uquant_order_id)
        if (
            uquant_order is None
            or uquant_order.symbol != fill.symbol.canonical
            or (uquant_order.side != fill.side.value)
        ):
            blockers.add("BROKER_FILL_WITHOUT_UQUANT_INTENT")
            continue

        explained_fill_ids.add(fill.broker_fill_id)
        if fill.broker_fill_id in account_fill_ids:
            continue

        gross = fill.price.value * fill.shares.value
        fees = fill.total_fees.value
        position = expected_positions.setdefault(fill.symbol.canonical, [0, 0])
        if fill.side is Side.BUY:
            expected_cash -= gross + fees
            position[0] += fill.shares.value
        else:
            expected_cash += gross - fees
            if position[0] < fill.shares.value or position[1] < fill.shares.value:
                blockers.add("SYSTEM_FILL_CONTRADICTS_ACCOUNT_STATE")
            else:
                position[0] -= fill.shares.value
                position[1] -= fill.shares.value
                if position[0] == 0:
                    expected_positions.pop(fill.symbol.canonical, None)

    observed_cash = snapshot.account.available_cash.value
    if expected_cash < 0:
        blockers.add("SYSTEM_FILL_CONTRADICTS_ACCOUNT_STATE")
    elif abs(observed_cash - expected_cash) > cash_tolerance.value:
        cash_reviews = _matching_review_ids(
            reviewed_adjustments,
            account_id_hash=snapshot.account.account_id_hash,
            symbol=None,
            session=snapshot.session_date,
            coverage=AdjustmentCoverage.AVAILABLE_CASH,
            broker_snapshot_sha256=snapshot.raw_payload_sha256,
            expected=expected_cash,
            observed=observed_cash,
        )
        if cash_reviews:
            reviewed_adjustment_ids.update(cash_reviews)
        else:
            blockers.add("UNEXPLAINED_CASH_CHANGE")

    broker_positions = {
        position.symbol.canonical: (position.total_shares.value, position.sellable_shares.value)
        for position in snapshot.positions
    }
    expected_position_values = {
        symbol: (values[0], values[1]) for symbol, values in expected_positions.items() if values[0] > 0
    }
    for raw_symbol in sorted(set(expected_position_values) | set(broker_positions)):
        expected_total, expected_sellable = expected_position_values.get(raw_symbol, (0, 0))
        observed_total, observed_sellable = broker_positions.get(raw_symbol, (0, 0))
        symbol = Symbol.parse(raw_symbol)
        if expected_total != observed_total:
            position_reviews = _matching_review_ids(
                reviewed_adjustments,
                account_id_hash=snapshot.account.account_id_hash,
                symbol=symbol,
                session=snapshot.session_date,
                coverage=AdjustmentCoverage.POSITION_TOTAL_SHARES,
                broker_snapshot_sha256=snapshot.raw_payload_sha256,
                expected=expected_total,
                observed=observed_total,
            )
            if position_reviews:
                reviewed_adjustment_ids.update(position_reviews)
                blockers.add("REVIEWED_ACCOUNT_STATE_REQUIRED")
            blockers.update({"CORPORATE_ACTION_SUSPECTED", "UNEXPLAINED_POSITION_CHANGE"})
            continue
        if expected_sellable != observed_sellable:
            sellable_reviews = _matching_review_ids(
                reviewed_adjustments,
                account_id_hash=snapshot.account.account_id_hash,
                symbol=symbol,
                session=snapshot.session_date,
                coverage=AdjustmentCoverage.POSITION_SELLABLE_SHARES,
                broker_snapshot_sha256=snapshot.raw_payload_sha256,
                expected=expected_sellable,
                observed=observed_sellable,
            )
            if sellable_reviews:
                reviewed_adjustment_ids.update(sellable_reviews)
                blockers.add("REVIEWED_ACCOUNT_STATE_REQUIRED")
            blockers.update({"CORPORATE_ACTION_SUSPECTED", "UNEXPLAINED_POSITION_CHANGE"})

    blocker_values = tuple(sorted(blockers))
    return AccountPreflightResult(
        passed=not blocker_values,
        blockers=blocker_values,
        explained_fill_ids=tuple(sorted(explained_fill_ids)),
        reviewed_adjustment_ids=tuple(sorted(reviewed_adjustment_ids)),
    )


__all__ = (
    "AccountPreflightResult",
    "account_difference_sha256",
    "evaluate_account_preflight",
)
