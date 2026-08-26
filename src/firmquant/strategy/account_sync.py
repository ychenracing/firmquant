"""Translate normalized broker facts into uquant's public account-sync contract."""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from decimal import Decimal
from typing import Any, Protocol, cast

from firmquant.domain.broker_facts import (
    AccountType,
    BrokerFillFact,
    BrokerOrderFact,
    BrokerOrderStatus,
    BrokerPositionFact,
    BrokerSnapshot,
    FillStatus,
)
from firmquant.domain.values import Money, Price

from .identity import StrategyIdentity, StrategyIdentityViolation


class StrategySyncError(RuntimeError):
    """Raised without mutating the caller when broker facts cannot enter uquant."""


class AccountStateContract(Protocol):
    """Narrow structural view of the public uquant AccountState boundary."""

    def to_dict(self) -> dict[str, object]: ...


@dataclass(frozen=True, slots=True)
class AccountSyncReceipt:
    snapshot_id: str
    as_of: str
    payload_sha256: str
    account_before_sha256: str
    account_after_sha256: str
    fills_imported: int
    positions_reconciled: int
    pending_orders: int


def _decimal_float(value: Decimal, *, label: str, tolerance: Decimal) -> float:
    converted = float(value)
    if not math.isfinite(converted):
        raise StrategySyncError(f"{label} cannot cross uquant float boundary")
    round_trip = Decimal(str(converted))
    if abs(round_trip - value) > tolerance:
        raise StrategySyncError(f"{label} cannot cross uquant float boundary within tolerance")
    return converted


def _money_float(value: Money, *, label: str) -> float:
    return _decimal_float(value.value, label=label, tolerance=Decimal("0.0001"))


def _price_float(value: Price, *, label: str) -> float:
    return _decimal_float(value.value, label=label, tolerance=Decimal("0.00000001"))


def _order_identity_map(snapshot: BrokerSnapshot) -> dict[str, BrokerOrderFact]:
    mapping: dict[str, BrokerOrderFact] = {}
    economic_ids: set[str] = set()
    for order in snapshot.orders:
        economic_id = order.client_order_id
        if economic_id is None:
            raise StrategySyncError(f"broker order {order.broker_order_id!r} has no uquant order identity")
        if economic_id in economic_ids:
            raise StrategySyncError("multiple broker orders claim one uquant order identity")
        if order.status is BrokerOrderStatus.UNKNOWN:
            raise StrategySyncError("UNKNOWN broker order cannot enter uquant account sync")
        if order.status in {BrokerOrderStatus.REJECTED, BrokerOrderStatus.EXPIRED}:
            raise StrategySyncError(
                f"broker terminal status {order.status.value} has no uquant sync representation"
            )
        mapping[order.broker_order_id] = order
        economic_ids.add(economic_id)
    return mapping


def _position_payload(position: BrokerPositionFact) -> dict[str, object]:
    if not position.total_shares.is_positive or position.average_cost is None:
        raise StrategySyncError("broker position requires positive shares and average cost")
    return {
        "symbol": position.symbol.canonical,
        "shares": position.total_shares.value,
        "sellable_shares": position.sellable_shares.value,
        "avg_cost": _price_float(position.average_cost, label="position average cost"),
    }


def _fill_payloads(
    snapshot: BrokerSnapshot,
    orders: Mapping[str, BrokerOrderFact],
) -> list[dict[str, object]]:
    grouped: dict[str, list[BrokerFillFact]] = {}
    for fill in snapshot.fills:
        if fill.status is not FillStatus.CONFIRMED:
            raise StrategySyncError("only confirmed broker fills can enter uquant account sync")
        order = orders.get(fill.broker_order_id)
        if order is None:
            raise StrategySyncError(f"broker fill {fill.broker_fill_id!r} has no mapped broker order")
        if fill.symbol != order.symbol or fill.side is not order.side:
            raise StrategySyncError("broker fill symbol or side differs from mapped order")
        if fill.event_sequence <= 0:
            raise StrategySyncError("broker fill requires a positive execution sequence")
        grouped.setdefault(fill.broker_order_id, []).append(fill)

    translated: list[dict[str, object]] = []
    for broker_order_id in sorted(grouped):
        order = orders[broker_order_id]
        fills = sorted(
            grouped[broker_order_id],
            key=lambda item: (
                item.session_date,
                item.event_sequence,
                item.broker_fill_id,
            ),
        )
        reported_fill_shares = sum(fill.shares.value for fill in fills)
        imported_before = order.filled_shares.value - reported_fill_shares
        if imported_before < 0:
            raise StrategySyncError("broker fills exceed the mapped order cumulative fill")
        cumulative = imported_before
        for fill in fills:
            cumulative += fill.shares.value
            remaining = order.requested_shares.value - cumulative
            if remaining < 0:
                raise StrategySyncError("broker fill exceeds requested uquant order shares")
            gross = fill.price.value * fill.shares.value
            economic_id = order.client_order_id
            if economic_id is None:
                raise StrategySyncError("broker fill lost its uquant order identity")
            translated.append(
                {
                    "fill_id": fill.broker_fill_id,
                    "order_id": economic_id,
                    "fill_date": fill.session_date.isoformat(),
                    "symbol": fill.symbol.canonical,
                    "side": fill.side.value,
                    "shares": fill.shares.value,
                    "price": _price_float(fill.price, label="fill price"),
                    "gross_value": _decimal_float(
                        gross,
                        label="fill gross value",
                        tolerance=Decimal("0.00000001"),
                    ),
                    "commission": _money_float(fill.commission, label="fill commission"),
                    "stamp_duty": _money_float(fill.stamp_duty, label="fill stamp duty"),
                    "transfer_fee": _money_float(fill.transfer_fee, label="fill transfer fee"),
                    "slippage_cost": 0.0,
                    "final": remaining == 0,
                    "remaining_shares": remaining,
                    "execution_sequence": fill.event_sequence,
                }
            )
    translated.sort(
        key=lambda item: (
            str(item["fill_date"]),
            int(cast(int, item["execution_sequence"])),
            str(item["order_id"]),
            str(item["fill_id"]),
        )
    )
    return translated


def to_uquant_broker_payload(snapshot: BrokerSnapshot) -> dict[str, object]:
    """Convert one complete normalized snapshot without inventing missing broker facts."""

    if not isinstance(snapshot, BrokerSnapshot):
        raise StrategySyncError("account sync requires a complete typed BrokerSnapshot")
    if snapshot.account.account_type is not AccountType.CASH:
        raise StrategySyncError("uquant production sync requires a cash account")
    orders = _order_identity_map(snapshot)
    cancellation_updates = [
        {
            "order_id": order.client_order_id,
            "status": "CANCELLED",
            "remaining_shares": 0,
        }
        for order in sorted(snapshot.orders, key=lambda item: item.broker_order_id)
        if order.status is BrokerOrderStatus.CANCELLED
    ]
    return {
        "as_of": snapshot.session_date.isoformat(),
        "cash": _money_float(snapshot.account.available_cash, label="cash"),
        "positions": [
            _position_payload(position)
            for position in sorted(
                snapshot.positions,
                key=lambda item: item.symbol.canonical,
            )
        ],
        "orders": cancellation_updates,
        "fills": _fill_payloads(snapshot, orders),
    }


def _canonical_payload_sha256(payload: Mapping[str, object]) -> str:
    try:
        encoded = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise StrategySyncError("uquant broker payload is not canonical JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


def _uquant_account_type() -> type[object]:
    module = importlib.import_module("uquant.types")
    account_type = module.AccountState
    if not isinstance(account_type, type):
        raise StrategySyncError("uquant AccountState contract is unavailable")
    return account_type


def _economic_state_sha256(account: object) -> str:
    module = importlib.import_module("uquant.account")
    function = cast(Callable[[object], str], module.economic_state_sha256)
    try:
        return function(account)
    except (TypeError, ValueError) as exc:
        raise StrategySyncError("uquant economic account hash failed") from exc


def _sync_broker_snapshot(account: object, payload: dict[str, object]) -> dict[str, int | str]:
    module = importlib.import_module("uquant.broker")
    function = cast(
        Callable[[object, dict[str, object]], dict[str, int | str]],
        module.sync_broker_snapshot,
    )
    return function(account, payload)


def _known_order_ids(account: object) -> frozenset[str]:
    ledger = getattr(account, "order_ledger", None)
    if not isinstance(ledger, list):
        raise StrategySyncError("uquant account order ledger is unavailable")
    result: set[str] = set()
    for order in ledger:
        order_id = getattr(order, "order_id", None)
        if not isinstance(order_id, str) or not order_id:
            raise StrategySyncError("uquant account contains an invalid order identity")
        result.add(order_id)
    return frozenset(result)


def commit_prepared_account(
    target: object,
    source: object,
    *,
    expected_sha256: str,
) -> None:
    """Replace a uquant AccountState with a fully prepared and hashed copy."""

    try:
        descriptors = fields(cast(Any, source))
        original = {item.name: copy.deepcopy(getattr(target, item.name)) for item in descriptors}
        replacement = {item.name: copy.deepcopy(getattr(source, item.name)) for item in descriptors}
    except (AttributeError, TypeError) as exc:
        raise StrategySyncError("uquant AccountState is not the reviewed dataclass contract") from exc
    try:
        for name, value in replacement.items():
            setattr(target, name, value)
        if _economic_state_sha256(target) != expected_sha256:
            raise StrategySyncError("committed uquant account hash differs from prepared state")
    except BaseException:
        for name, value in original.items():
            setattr(target, name, value)
        raise


def _summary_integer(summary: Mapping[str, int | str], key: str) -> int:
    value = summary.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StrategySyncError(f"uquant account sync returned invalid {key}")
    return value


def sync_account(account: AccountStateContract, snapshot: BrokerSnapshot) -> AccountSyncReceipt:
    """Prepare on a deep copy and replace caller state only after exact uquant validation."""

    try:
        StrategyIdentity.locked().verify()
    except StrategyIdentityViolation as exc:
        raise StrategySyncError("uquant strategy identity is not verified") from exc
    if not isinstance(account, _uquant_account_type()):
        raise StrategySyncError("account sync requires uquant AccountState")
    payload = to_uquant_broker_payload(snapshot)
    known_order_ids = _known_order_ids(account)
    mapped_order_ids = {
        order.client_order_id for order in snapshot.orders if order.client_order_id is not None
    }
    unknown = sorted(mapped_order_ids - known_order_ids)
    if unknown:
        raise StrategySyncError(f"broker snapshot references unknown uquant order {unknown[0]!r}")

    before_sha256 = _economic_state_sha256(account)
    try:
        working = copy.deepcopy(account)
        summary = _sync_broker_snapshot(working, payload)
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        raise StrategySyncError("uquant rejected the broker snapshot atomically") from exc
    if set(summary) != {"as_of", "fills_imported", "positions_reconciled", "pending_orders"}:
        raise StrategySyncError("uquant account sync returned an unexpected receipt schema")
    as_of = summary["as_of"]
    if not isinstance(as_of, str) or as_of != snapshot.session_date.isoformat():
        raise StrategySyncError("uquant account sync returned an unexpected as_of")
    after_sha256 = _economic_state_sha256(working)
    receipt = AccountSyncReceipt(
        snapshot_id=snapshot.snapshot_id,
        as_of=as_of,
        payload_sha256=_canonical_payload_sha256(payload),
        account_before_sha256=before_sha256,
        account_after_sha256=after_sha256,
        fills_imported=_summary_integer(summary, "fills_imported"),
        positions_reconciled=_summary_integer(summary, "positions_reconciled"),
        pending_orders=_summary_integer(summary, "pending_orders"),
    )
    commit_prepared_account(account, working, expected_sha256=after_sha256)
    return receipt


__all__ = (
    "AccountStateContract",
    "AccountSyncReceipt",
    "StrategySyncError",
    "commit_prepared_account",
    "sync_account",
    "to_uquant_broker_payload",
)
