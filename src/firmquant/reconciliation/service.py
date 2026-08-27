"""Transactional reconciliation that never adopts unexplained broker activity."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from contextlib import nullcontext
from datetime import datetime

from firmquant.domain.broker_facts import (
    BrokerOrderStatus,
    FillStatus,
)
from firmquant.domain.errors import DomainTypeError, DomainValidationError
from firmquant.domain.orders import OrderState
from firmquant.domain.values import Money
from firmquant.persistence.audit import AuditLedger
from firmquant.persistence.database import Database
from firmquant.persistence.repositories import (
    PersistenceConflict,
    canonical_json,
)

from .models import (
    OperationalOrderView,
    ReconciliationFacts,
    ReconciliationKind,
    ReconciliationReceipt,
)

_BROKER_ACTIVE = frozenset(
    {
        BrokerOrderStatus.PENDING_NEW,
        BrokerOrderStatus.ACKNOWLEDGED,
        BrokerOrderStatus.PARTIALLY_FILLED,
        BrokerOrderStatus.PENDING_CANCEL,
        BrokerOrderStatus.UNKNOWN,
    }
)
_LOCAL_TERMINAL = frozenset(
    {
        OrderState.FILLED,
        OrderState.CANCELLED,
        OrderState.REJECTED,
        OrderState.EXPIRED,
    }
)
_BROKER_FOR_LOCAL_TERMINAL = {
    OrderState.FILLED: BrokerOrderStatus.FILLED,
    OrderState.CANCELLED: BrokerOrderStatus.CANCELLED,
    OrderState.REJECTED: BrokerOrderStatus.REJECTED,
    OrderState.EXPIRED: BrokerOrderStatus.EXPIRED,
}


def _aware(value: datetime, *, label: str) -> None:
    if not isinstance(value, datetime):
        raise DomainTypeError(f"{label} must be datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise DomainValidationError(f"{label} must be timezone-aware")


def _evidence_hash(*values: object) -> str:
    return hashlib.sha256(canonical_json(values).encode("utf-8")).hexdigest()


class ReconciliationService:
    """Compare authority-specific views and append one immutable receipt atomically."""

    def __init__(
        self,
        *,
        database: Database,
        cash_tolerance: Money,
        clock: Callable[[], datetime],
    ) -> None:
        if not isinstance(database, Database):
            raise DomainTypeError("reconciliation database must be Database")
        if not isinstance(cash_tolerance, Money):
            raise DomainTypeError("reconciliation cash tolerance must be Money")
        if not callable(clock):
            raise DomainTypeError("reconciliation clock must be callable")
        self._database = database
        self._cash_tolerance = cash_tolerance
        self._clock = clock
        self._audit = AuditLedger(database)

    def evaluate(
        self,
        kind: ReconciliationKind,
        facts: ReconciliationFacts,
    ) -> ReconciliationReceipt:
        if not isinstance(kind, ReconciliationKind):
            raise DomainTypeError("reconciliation kind must be ReconciliationKind")
        if not isinstance(facts, ReconciliationFacts):
            raise DomainTypeError("reconciliation facts must be ReconciliationFacts")
        started_at = self._clock()
        _aware(started_at, label="reconciliation start time")
        blockers: set[str] = set()
        evidence: list[str] = []

        self._compare_identity(facts, blockers)
        self._compare_cash(facts, blockers, evidence)
        self._compare_positions(facts, blockers, evidence)
        self._compare_orders(facts, blockers, evidence)
        self._compare_fills(facts, blockers, evidence)
        self._compare_operational_health(facts, blockers)

        operator_actions = self._operator_actions(blockers)
        completed_at = self._clock()
        _aware(completed_at, label="reconciliation completion time")
        blocker_values = tuple(sorted(blockers))
        action_values = tuple(sorted(operator_actions))
        details = {
            "schema": "firmquant.reconciliation-details.v1",
            "kind": kind,
            "snapshot_id": facts.broker_snapshot.snapshot_id,
            "broker_snapshot_sha256": facts.broker_snapshot.raw_payload_sha256,
            "strategy_economic_state_sha256": (facts.strategy_account.economic_state_sha256),
            "expected_account_identity_hash": (facts.operational_ledger.expected_account_id_hash),
            "broker_event_watermark": facts.broker_snapshot.broker_event_watermark,
            "broker_order_count": len(facts.broker_snapshot.orders),
            "broker_fill_count": len(facts.broker_snapshot.fills),
            "broker_position_count": len(facts.broker_snapshot.positions),
            "blockers": blocker_values,
            "operator_actions": action_values,
            "evidence_sha256": tuple(sorted(set(evidence))),
        }
        details_json = canonical_json(details)
        details_sha256 = hashlib.sha256(details_json.encode("utf-8")).hexdigest()
        reconciliation_id = (
            "recon_"
            + hashlib.sha256(
                canonical_json(
                    {
                        "kind": kind,
                        "details_sha256": details_sha256,
                        "started_at": started_at,
                        "completed_at": completed_at,
                    }
                ).encode("utf-8")
            ).hexdigest()
        )
        receipt = ReconciliationReceipt(
            reconciliation_id=reconciliation_id,
            kind=kind,
            snapshot_id=facts.broker_snapshot.snapshot_id,
            started_at=started_at,
            completed_at=completed_at,
            passed=not blocker_values,
            blockers=blocker_values,
            operator_actions=action_values,
            details_json=details_json,
            details_sha256=details_sha256,
        )
        return receipt

    def commit(
        self,
        receipt: ReconciliationReceipt,
        *,
        broker_snapshot_sha256: str,
    ) -> None:
        if not isinstance(receipt, ReconciliationReceipt):
            raise DomainTypeError("reconciliation commit requires ReconciliationReceipt")
        if (
            not isinstance(broker_snapshot_sha256, str)
            or len(broker_snapshot_sha256) != 64
            or any(character not in "0123456789abcdef" for character in broker_snapshot_sha256)
        ):
            raise DomainValidationError("reconciliation broker snapshot hash must be SHA-256")
        self._append(receipt, broker_snapshot_sha256=broker_snapshot_sha256)

    def run(
        self,
        kind: ReconciliationKind,
        facts: ReconciliationFacts,
    ) -> ReconciliationReceipt:
        receipt = self.evaluate(kind, facts)
        self.commit(
            receipt,
            broker_snapshot_sha256=facts.broker_snapshot.raw_payload_sha256,
        )
        return receipt

    def _compare_identity(
        self,
        facts: ReconciliationFacts,
        blockers: set[str],
    ) -> None:
        broker_account = facts.broker_snapshot.account
        ledger = facts.operational_ledger
        if broker_account.account_id_hash != ledger.expected_account_id_hash:
            blockers.add("ACCOUNT_IDENTITY_CHANGED")
        if broker_account.account_type is not ledger.expected_account_type:
            blockers.add("ACCOUNT_TYPE_CHANGED")
        if not facts.uquant_code_identity_matches:
            blockers.add("UQUANT_CODE_IDENTITY_DRIFT")
        if not facts.data_identity_matches:
            blockers.add("DATA_HISTORY_REWRITE")
        if not facts.config_identity_matches:
            blockers.add("CONFIG_IDENTITY_DRIFT")
        if facts.company_action_suspected_symbols:
            blockers.add("CORPORATE_ACTION_SUSPECTED")

    def _compare_cash(
        self,
        facts: ReconciliationFacts,
        blockers: set[str],
        evidence: list[str],
    ) -> None:
        broker = facts.broker_snapshot.account
        strategy = facts.strategy_account
        cash_difference = abs(broker.available_cash.value - strategy.available_cash.value)
        asset_difference = abs(broker.total_assets.value - strategy.total_assets.value)
        if cash_difference > self._cash_tolerance.value:
            blockers.add("AVAILABLE_CASH_MISMATCH")
            evidence.append(_evidence_hash("available_cash", cash_difference))
        if asset_difference > self._cash_tolerance.value:
            blockers.add("TOTAL_ASSETS_MISMATCH")
            evidence.append(_evidence_hash("total_assets", asset_difference))

    def _compare_positions(
        self,
        facts: ReconciliationFacts,
        blockers: set[str],
        evidence: list[str],
    ) -> None:
        broker = {position.symbol: position for position in facts.broker_snapshot.positions}
        strategy = {position.symbol: position for position in facts.strategy_account.positions}
        for symbol in sorted(set(broker) | set(strategy), key=lambda item: item.canonical):
            broker_position = broker.get(symbol)
            strategy_position = strategy.get(symbol)
            broker_total = 0 if broker_position is None else broker_position.total_shares.value
            expected_total = 0 if strategy_position is None else strategy_position.total_shares.value
            broker_sellable = 0 if broker_position is None else broker_position.sellable_shares.value
            expected_sellable = 0 if strategy_position is None else strategy_position.sellable_shares.value
            if broker_total != expected_total:
                blockers.update({"POSITION_SHARE_MISMATCH", "UNEXPLAINED_POSITION_CHANGE"})
                evidence.append(
                    _evidence_hash(
                        "position_total",
                        symbol,
                        broker_total,
                        expected_total,
                    )
                )
            if broker_sellable != expected_sellable:
                blockers.update({"SELLABLE_SHARE_MISMATCH", "UNEXPLAINED_POSITION_CHANGE"})
                evidence.append(
                    _evidence_hash(
                        "position_sellable",
                        symbol,
                        broker_sellable,
                        expected_sellable,
                    )
                )

    def _compare_orders(
        self,
        facts: ReconciliationFacts,
        blockers: set[str],
        evidence: list[str],
    ) -> None:
        broker_orders = {order.broker_order_id: order for order in facts.broker_snapshot.orders}
        local_orders = {order.broker_order_id: order for order in facts.operational_ledger.orders}
        known_uquant_ids = facts.strategy_account.known_uquant_order_ids
        for broker_id, broker_order in broker_orders.items():
            local = local_orders.get(broker_id)
            if local is None:
                blockers.add("EXTERNAL_BROKER_ORDER")
                if broker_order.status in _BROKER_ACTIVE:
                    blockers.add("EXTERNAL_ACTIVE_ORDER")
                evidence.append(_evidence_hash("external_order", broker_id))
                continue
            self._compare_mapped_order(
                local,
                broker_order,
                known_uquant_ids,
                blockers,
                evidence,
            )
        for broker_id, local in local_orders.items():
            if broker_id in broker_orders:
                continue
            if local.local_state not in {
                OrderState.PLANNED,
                OrderState.VALIDATED,
                OrderState.ARMED,
            }:
                blockers.add("SYSTEM_ORDER_MISSING_AT_BROKER")
                evidence.append(_evidence_hash("missing_system_order", broker_id))

    @staticmethod
    def _compare_mapped_order(
        local: OperationalOrderView,
        broker_order: object,
        known_uquant_ids: frozenset[str],
        blockers: set[str],
        evidence: list[str],
    ) -> None:
        from firmquant.domain.broker_facts import BrokerOrderFact

        if not isinstance(broker_order, BrokerOrderFact):
            raise DomainTypeError("mapped broker order must be BrokerOrderFact")
        identity_matches = (
            broker_order.client_order_id == local.uquant_order_id
            and broker_order.symbol == local.symbol
            and broker_order.side is local.side
            and broker_order.requested_shares == local.requested_shares
        )
        if not identity_matches:
            blockers.add("BROKER_ORDER_IDENTITY_MISMATCH")
            evidence.append(
                _evidence_hash(
                    "order_identity",
                    local.broker_order_id,
                    broker_order.raw_payload_sha256,
                )
            )
        if local.uquant_order_id not in known_uquant_ids:
            blockers.add("BROKER_ORDER_WITHOUT_UQUANT_INTENT")
        if broker_order.status is BrokerOrderStatus.UNKNOWN:
            blockers.add("BROKER_ORDER_UNKNOWN")
        if broker_order.filled_shares != local.filled_shares:
            blockers.add("ORDER_FILLED_SHARES_MISMATCH")
        if local.local_state is OrderState.UNKNOWN:
            blockers.add("UNRESOLVED_LOCAL_ORDER")
        expected_terminal = _BROKER_FOR_LOCAL_TERMINAL.get(local.local_state)
        if expected_terminal is not None and broker_order.status is not expected_terminal:
            if broker_order.status in _BROKER_ACTIVE:
                blockers.add("LOCAL_TERMINAL_BROKER_ACTIVE")
            else:
                blockers.add("ORDER_TERMINAL_STATE_MISMATCH")
        elif local.local_state not in _LOCAL_TERMINAL and broker_order.status not in _BROKER_ACTIVE:
            blockers.add("ORDER_STATE_MISMATCH")

    def _compare_fills(
        self,
        facts: ReconciliationFacts,
        blockers: set[str],
        evidence: list[str],
    ) -> None:
        known_orders = {order.broker_order_id: order for order in facts.operational_ledger.orders}
        known_uquant = facts.strategy_account.known_uquant_order_ids
        broker_fill_ids: set[str] = set()
        for fill in facts.broker_snapshot.fills:
            broker_fill_ids.add(fill.broker_fill_id)
            order = known_orders.get(fill.broker_order_id)
            if order is None or order.uquant_order_id not in known_uquant:
                blockers.add("UNMAPPED_BROKER_FILL")
                evidence.append(_evidence_hash("unmapped_fill", fill.broker_fill_id))
            if fill.status is not FillStatus.CONFIRMED:
                blockers.add("BROKER_FILL_NOT_CONFIRMED")
            if fill.broker_fill_id not in facts.operational_ledger.known_broker_fill_ids:
                blockers.add("BROKER_FILL_NOT_INGESTED")
        if not facts.operational_ledger.known_broker_fill_ids.issubset(broker_fill_ids):
            blockers.add("LOCAL_FILL_MISSING_AT_BROKER")

    @staticmethod
    def _compare_operational_health(
        facts: ReconciliationFacts,
        blockers: set[str],
    ) -> None:
        if facts.operational_ledger.unresolved_execution_ids:
            blockers.add("UNRESOLVED_LOCAL_ORDER")
        if facts.operational_ledger.submitting_unresolved_execution_ids:
            blockers.add("SUBMITTING_UNRESOLVED")

    @staticmethod
    def _operator_actions(blockers: set[str]) -> set[str]:
        actions: set[str] = set()
        if blockers & {"EXTERNAL_BROKER_ORDER", "EXTERNAL_ACTIVE_ORDER"}:
            actions.add("REVIEW_EXTERNAL_BROKER_ACTIVITY")
        if blockers & {
            "UNMAPPED_BROKER_FILL",
            "BROKER_FILL_NOT_INGESTED",
            "BROKER_ORDER_UNKNOWN",
            "SYSTEM_ORDER_MISSING_AT_BROKER",
        }:
            actions.add("INVESTIGATE_UNKNOWN_BROKER_ACTIVITY")
        if "CORPORATE_ACTION_SUSPECTED" in blockers:
            actions.add("VERIFY_COMPANY_ACTION_WITH_BROKER")
        if blockers & {
            "AVAILABLE_CASH_MISMATCH",
            "TOTAL_ASSETS_MISMATCH",
            "POSITION_SHARE_MISMATCH",
            "SELLABLE_SHARE_MISMATCH",
            "UNEXPLAINED_POSITION_CHANGE",
        }:
            actions.add("RECONCILE_ACCOUNT_STATE_EXPLICITLY")
        if blockers & {
            "UQUANT_CODE_IDENTITY_DRIFT",
            "DATA_HISTORY_REWRITE",
            "CONFIG_IDENTITY_DRIFT",
        }:
            actions.add("RESTORE_AND_VERIFY_LOCKED_IDENTITY")
        return actions

    def _append(
        self,
        receipt: ReconciliationReceipt,
        *,
        broker_snapshot_sha256: str,
    ) -> None:
        stable = (
            receipt.kind.value,
            receipt.started_at.isoformat(),
            receipt.completed_at.isoformat(),
            int(receipt.passed),
            canonical_json(receipt.blockers),
            receipt.details_json,
            receipt.details_sha256,
        )
        transaction = nullcontext() if self._database.in_transaction else self._database.transaction()
        with transaction:
            existing = self._database.query_one(
                "SELECT kind, started_at, completed_at, passed, blockers_json, "
                "details_json, details_sha256 FROM reconciliation_runs "
                "WHERE reconciliation_id = ?",
                (receipt.reconciliation_id,),
            )
            if existing is None:
                self._database.write(
                    """
                    INSERT INTO reconciliation_runs(
                        reconciliation_id, kind, strategy_session, started_at,
                        completed_at, passed, blockers_json, details_json, details_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        receipt.reconciliation_id,
                        receipt.kind.value,
                        None,
                        *stable[1:],
                    ),
                )
            elif tuple(existing) != stable:
                raise PersistenceConflict("reconciliation receipt identity collision")
            self._audit.append(
                audit_event_id="reconciliation." + receipt.reconciliation_id.removeprefix("recon_"),
                category="reconciliation.receipt",
                actor="firmquant",
                payload={
                    "reconciliation_id": receipt.reconciliation_id,
                    "kind": receipt.kind,
                    "passed": receipt.passed,
                    "blockers": receipt.blockers,
                    "details_sha256": receipt.details_sha256,
                    "broker_snapshot_sha256": broker_snapshot_sha256,
                },
                created_at=receipt.completed_at,
            )


__all__ = ("ReconciliationService",)
