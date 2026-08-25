from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from firmquant.broker.fake import FakeBroker
from firmquant.broker.gateway import BrokerOrderCommand
from firmquant.broker.normalization import normalize_fill, normalize_order
from firmquant.domain.broker_facts import (
    BrokerFillFact,
    BrokerOrderFact,
    BrokerOrderStatus,
    FillStatus,
    MarketSessionStatus,
    PriceType,
    Side,
)
from firmquant.domain.orders import ExecutionIntent, OrderAggregate
from firmquant.domain.values import Price, Shares
from firmquant.persistence.database import Database
from firmquant.persistence.recovery import AccountStateStore
from firmquant.persistence.repositories import (
    BrokerAttempt,
    DecisionSnapshotRepository,
    ExecutionLedgerRepository,
)
from tests.fixtures.broker_contract import gateway_facts
from tests.fixtures.session_cases import decision_snapshot

NOW = datetime(2026, 8, 25, 1, 32, tzinfo=UTC)


class JsonAccountStateStore:
    """Deterministic crash-test store with the production store protocol."""

    def hash_state(self, state: object) -> str:
        encoded = json.dumps(
            state,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def hash_file(self, path: Path) -> str:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError("test account file is corrupt") from error
        return self.hash_state(payload)

    def save(self, state: object, path: Path) -> None:
        encoded = json.dumps(
            state,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        temporary = path.with_name(path.name + ".temporary")
        temporary.write_bytes(encoded)
        os.replace(temporary, path)


assert isinstance(JsonAccountStateStore(), AccountStateStore)


def write_account(path: Path, state: object, store: JsonAccountStateStore) -> None:
    store.save(state, path)


@dataclass(frozen=True, slots=True)
class SubmittingCase:
    repository: ExecutionLedgerRepository
    aggregate: OrderAggregate
    attempt: BrokerAttempt
    command: BrokerOrderCommand


def create_submitting_case(database: Database) -> SubmittingCase:
    snapshot = decision_snapshot(include_sell=False, include_buy=True)
    repository = ExecutionLedgerRepository(database)
    symbol = gateway_facts().instrument.symbol
    intent = ExecutionIntent.create(
        decision_id=snapshot.decision_id,
        uquant_order_id="O-RECOVERY-1",
        symbol=symbol,
        side=Side.BUY,
        requested_shares=Shares(100),
        strategy_session=snapshot.strategy_session,
        uquant_source_sha="1" * 40,
    )
    command = BrokerOrderCommand(
        execution_id=intent.execution_id,
        idempotency_key=intent.idempotency_key,
        client_order_id=intent.uquant_order_id,
        symbol=intent.symbol,
        side=intent.side,
        price_type=PriceType.LIMIT,
        requested_shares=intent.requested_shares,
        limit_price=Price(Decimal("10.10")),
        strategy_session=intent.strategy_session,
    )
    with database.transaction():
        DecisionSnapshotRepository(database).append(snapshot)
        aggregate = repository.append_intent(intent, created_at=NOW)
        aggregate = repository.validate_and_arm(aggregate, occurred_at=NOW)
        aggregate, attempt = repository.begin_submit(
            aggregate,
            command,
            started_at=NOW,
        )
    return SubmittingCase(repository, aggregate, attempt, command)


def broker_order(
    command: BrokerOrderCommand,
    *,
    status: BrokerOrderStatus = BrokerOrderStatus.ACKNOWLEDGED,
    filled_shares: int = 0,
    sequence: int = 20,
) -> BrokerOrderFact:
    return normalize_order(
        {
            "broker_order_id": "broker-recovery-order",
            "client_order_id": command.client_order_id,
            "symbol": command.symbol.canonical,
            "side": command.side.value,
            "price_type": command.price_type.value,
            "status": status.value,
            "requested_shares": command.requested_shares.value,
            "filled_shares": filled_shares,
            "limit_price": command.limit_price.canonical,
            "session_date": command.strategy_session.isoformat(),
            "event_time": NOW.isoformat(),
            "event_sequence": sequence,
        },
        received_at=NOW,
    )


def broker_fill(
    command: BrokerOrderCommand,
    *,
    shares: int = 50,
    sequence: int = 21,
    fill_id: str = "broker-recovery-fill",
) -> BrokerFillFact:
    return normalize_fill(
        {
            "broker_fill_id": fill_id,
            "broker_order_id": "broker-recovery-order",
            "symbol": command.symbol.canonical,
            "side": command.side.value,
            "status": FillStatus.CONFIRMED.value,
            "shares": shares,
            "price": command.limit_price.canonical,
            "commission": "5.00",
            "stamp_duty": "0",
            "transfer_fee": "0.01",
            "session_date": command.strategy_session.isoformat(),
            "event_time": NOW.isoformat(),
            "event_sequence": sequence,
        },
        received_at=NOW,
    )


def fake_recovery_broker(
    *,
    orders: tuple[BrokerOrderFact, ...] = (),
    fills: tuple[BrokerFillFact, ...] = (),
    connected: bool = True,
) -> FakeBroker:
    facts = gateway_facts()
    broker = FakeBroker(
        account=facts.account,
        positions=(),
        orders=orders,
        fills=fills,
        instruments=(facts.instrument,),
        quotes=(facts.quote,),
        market_status=MarketSessionStatus.OPEN,
        clock=lambda: NOW,
    )
    if connected:
        broker.connect()
    return broker


def acknowledge_locally(
    case: SubmittingCase,
    order: BrokerOrderFact,
    fills: tuple[BrokerFillFact, ...] = (),
) -> OrderAggregate:
    with case.repository.database.transaction():
        return case.repository.record_submit_result(
            case.aggregate,
            case.attempt,
            order,
            fills,
            received_at=NOW,
        )


def cancelled_locally(
    case: SubmittingCase,
) -> tuple[OrderAggregate, BrokerOrderFact]:
    acknowledged_fact = broker_order(case.command)
    acknowledged = acknowledge_locally(case, acknowledged_fact)
    with case.repository.database.transaction():
        cancelling, attempt = case.repository.begin_cancel(
            acknowledged,
            started_at=NOW,
        )
        cancelled_fact = replace(
            acknowledged_fact,
            status=BrokerOrderStatus.CANCELLED,
            event_sequence=21,
        )
        cancelled = case.repository.record_cancel_result(
            cancelling,
            attempt,
            cancelled_fact,
            (),
            received_at=NOW,
        )
    return cancelled, cancelled_fact
