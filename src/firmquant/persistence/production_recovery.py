"""Production recovery service using monotonic broker-order persistence."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from firmquant.broker.gateway import (
    BrokerGateway,
    BrokerOrderAbsenceVerifier,
    BrokerOrderCommand,
)
from firmquant.domain.broker_facts import PriceType, Side
from firmquant.domain.values import Price, Shares, Symbol

from .database import Database
from .production_repository import MonotonicExecutionLedgerRepository
from .recovery import (
    AccountStateStore,
    OrderRecoveryClassification,
    OrderRecoveryReceipt,
    RecoveryService,
)
from .repositories import BrokerAttempt, PersistenceConflict


class ProductionRecoveryService(RecoveryService):
    """Fail-closed recovery that never treats an empty ordinary query as non-acceptance."""

    def __init__(
        self,
        *,
        database: Database,
        account_store: AccountStateStore | None,
        account_path: Path | None,
        gateway: BrokerGateway | None,
        clock: Callable[[], datetime],
    ) -> None:
        super().__init__(
            database=database,
            account_store=account_store,
            account_path=account_path,
            gateway=gateway,
            clock=clock,
        )
        self._orders = MonotonicExecutionLedgerRepository(database)

    def _durable_submit_command(
        self, attempt: BrokerAttempt
    ) -> tuple[BrokerOrderCommand, datetime] | None:
        row = self._database.query_one(
            """
            SELECT oc.payload_json, boa.started_at
            FROM order_commands oc
            JOIN broker_order_attempts boa ON boa.attempt_id = oc.attempt_id
            WHERE oc.attempt_id = ? AND oc.command_kind = 'SUBMIT'
            """,
            (attempt.attempt_id,),
        )
        if row is None:
            return None
        try:
            payload = json.loads(str(row["payload_json"]))
            if not isinstance(payload, dict):
                return None
            command = BrokerOrderCommand(
                execution_id=str(payload["execution_id"]),
                idempotency_key=str(payload["idempotency_key"]),
                client_order_id=str(payload["client_order_id"]),
                symbol=Symbol.parse(str(payload["symbol"])),
                side=Side(str(payload["side"])),
                price_type=PriceType(str(payload["price_type"])),
                requested_shares=Shares(int(payload["requested_shares"])),
                limit_price=Price(Decimal(str(payload["limit_price"]))),
                strategy_session=date.fromisoformat(str(payload["strategy_session"])),
            )
            started_at = datetime.fromisoformat(str(row["started_at"]))
        except (KeyError, TypeError, ValueError):
            return None
        if started_at.tzinfo is None or started_at.utcoffset() is None:
            return None
        return command, started_at

    def _prove_submit_not_accepted(
        self,
        attempt: BrokerAttempt,
        *,
        now: datetime,
    ) -> OrderRecoveryReceipt | None:
        gateway = self._gateway
        if attempt.command_kind != "SUBMIT" or not isinstance(
            gateway, BrokerOrderAbsenceVerifier
        ):
            return None
        durable = self._durable_submit_command(attempt)
        if durable is None:
            return None
        command, started_at = durable
        try:
            proof = gateway.prove_order_not_accepted(command)
        except Exception:
            return None
        if (
            proof is None
            or proof.command != command
            or proof.captured_at < started_at
            or proof.captured_at > now
        ):
            return None
        aggregate = self._orders.load(attempt.execution_id)
        if aggregate is None:
            return OrderRecoveryReceipt(
                execution_id=attempt.execution_id,
                classification=OrderRecoveryClassification.CONTRADICTION,
                reason_code="RECOVERY_AGGREGATE_MISSING",
            )
        try:
            with self._database.transaction():
                self._orders.resolve_submit_not_accepted(
                    aggregate,
                    attempt,
                    evidence_sha256=proof.evidence_sha256,
                    occurred_at=now,
                )
        except (PersistenceConflict, RuntimeError, ValueError):
            return None
        return OrderRecoveryReceipt(
            execution_id=attempt.execution_id,
            classification=OrderRecoveryClassification.RESOLVED_FROM_BROKER,
            reason_code="BROKER_PROVED_NOT_ACCEPTED",
        )

    def _mark_attempts_unknown(
        self,
        attempts: tuple[BrokerAttempt, ...],
        *,
        now: datetime,
        reason: str,
    ) -> tuple[OrderRecoveryReceipt, ...]:
        if reason != "BROKER_ACCEPTANCE_UNPROVEN":
            return super()._mark_attempts_unknown(attempts, now=now, reason=reason)
        receipts: list[OrderRecoveryReceipt] = []
        unresolved: list[BrokerAttempt] = []
        for attempt in attempts:
            receipt = self._prove_submit_not_accepted(attempt, now=now)
            if receipt is None:
                unresolved.append(attempt)
            else:
                receipts.append(receipt)
        receipts.extend(
            super()._mark_attempts_unknown(tuple(unresolved), now=now, reason=reason)
        )
        return tuple(receipts)


__all__ = ("ProductionRecoveryService",)
