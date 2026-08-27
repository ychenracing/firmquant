from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from firmquant.broker.fake import FakeBroker
from firmquant.broker.gateway import BrokerOrderAbsenceProof, BrokerOrderCommand
from firmquant.domain.broker_facts import BrokerOrderStatus, MarketSessionStatus
from firmquant.domain.orders import OrderState
from firmquant.persistence.database import Database
from firmquant.persistence.production_recovery import ProductionRecoveryService
from tests.fixtures.broker_contract import gateway_facts
from tests.fixtures.recovery_cases import (
    NOW,
    broker_fill,
    broker_order,
    create_submitting_case,
    fake_recovery_broker,
)


@pytest.fixture
def database(tmp_path: Path):
    opened = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        yield opened
    finally:
        opened.close()


class AbsenceProofFakeBroker(FakeBroker):
    def __init__(self) -> None:
        facts = gateway_facts()
        super().__init__(
            account=facts.account,
            positions=(),
            orders=(),
            fills=(),
            instruments=(facts.instrument,),
            quotes=(facts.quote,),
            market_status=MarketSessionStatus.OPEN,
            clock=lambda: NOW,
        )
        self.proof_commands: list[BrokerOrderCommand] = []
        self.connect()

    def prove_order_not_accepted(
        self, command: BrokerOrderCommand
    ) -> BrokerOrderAbsenceProof | None:
        self.proof_commands.append(command)
        return BrokerOrderAbsenceProof(
            command=command,
            snapshot_id="authoritative-absence-1",
            session_date=command.strategy_session,
            captured_at=NOW + timedelta(seconds=1),
            broker_event_watermark=100,
            evidence_sha256="a" * 64,
        )


def _recover(database: Database, broker: FakeBroker, *, offset: int = 0):
    return ProductionRecoveryService(
        database=database,
        account_store=None,
        account_path=None,
        gateway=broker,
        clock=lambda: NOW + timedelta(seconds=offset),
    ).recover()


@pytest.mark.parametrize(
    ("status", "filled", "expected"),
    [
        (BrokerOrderStatus.ACKNOWLEDGED, 0, OrderState.ACKNOWLEDGED),
        (BrokerOrderStatus.FILLED, 100, OrderState.FILLED),
        (BrokerOrderStatus.CANCELLED, 0, OrderState.CANCELLED),
    ],
)
def test_unknown_submit_restart_resolves_from_authoritative_broker_truth_without_resubmit(
    database: Database,
    status: BrokerOrderStatus,
    filled: int,
    expected: OrderState,
) -> None:
    case = create_submitting_case(database)
    first = _recover(database, fake_recovery_broker())
    assert first.halt_required is True
    assert case.repository.load(case.aggregate.intent.execution_id).state is OrderState.UNKNOWN  # type: ignore[union-attr]

    fills = (
        (broker_fill(case.command, shares=100, sequence=21, fill_id="full-fill"),)
        if filled
        else ()
    )
    fact = broker_order(case.command, status=status, filled_shares=filled, sequence=22)
    broker = fake_recovery_broker(orders=(fact,), fills=fills)
    second = _recover(database, broker, offset=2)
    recovered = case.repository.load(case.aggregate.intent.execution_id)

    assert recovered is not None and recovered.state is expected
    assert recovered.filled_shares.value == filled
    assert second.unresolved_order_ids == ()
    assert broker.submitted_commands == ()
    assert broker.cancelled_order_ids == ()


def test_empty_authoritative_queries_do_not_prove_submit_not_accepted(database: Database) -> None:
    case = create_submitting_case(database)
    broker = fake_recovery_broker()

    first = _recover(database, broker)
    second = _recover(database, broker, offset=1)
    recovered = case.repository.load(case.aggregate.intent.execution_id)

    assert recovered is not None and recovered.state is OrderState.UNKNOWN
    assert first.halt_required is True
    assert second.halt_required is True
    assert second.unresolved_order_ids == first.unresolved_order_ids
    assert broker.submitted_commands == ()


def test_explicit_authoritative_absence_proof_resolves_unknown_submit_without_write(
    database: Database,
) -> None:
    case = create_submitting_case(database)
    broker = AbsenceProofFakeBroker()

    report = _recover(database, broker, offset=2)
    recovered = case.repository.load(case.aggregate.intent.execution_id)

    assert recovered is not None and recovered.state is OrderState.ARMED
    assert report.unresolved_order_ids == ()
    assert report.halt_required is False
    assert broker.proof_commands == [case.command]
    assert broker.submitted_commands == ()
    assert broker.cancelled_order_ids == ()
    assert database.scalar(
        "SELECT state FROM broker_order_attempts WHERE attempt_id = ?",
        (case.attempt.attempt_id,),
    ) == "FAILED_LOCAL"


def test_absence_proof_recovery_is_repeatable_and_never_resubmits(database: Database) -> None:
    case = create_submitting_case(database)
    broker = AbsenceProofFakeBroker()

    first = _recover(database, broker, offset=2)
    second = _recover(database, broker, offset=3)
    recovered = case.repository.load(case.aggregate.intent.execution_id)

    assert recovered is not None and recovered.state is OrderState.ARMED
    assert first.halt_required is False
    assert second.halt_required is False
    assert broker.submitted_commands == ()
    assert database.scalar(
        "SELECT count(*) FROM broker_order_attempts WHERE execution_id = ?",
        (case.aggregate.intent.execution_id,),
    ) == 1
