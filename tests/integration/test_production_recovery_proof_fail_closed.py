from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from firmquant.broker.fake import FakeBroker
from firmquant.broker.gateway import BrokerOrderAbsenceProof, BrokerOrderCommand
from firmquant.domain.broker_facts import MarketSessionStatus
from firmquant.domain.orders import OrderState
from firmquant.persistence.database import Database
from firmquant.persistence.production_recovery import ProductionRecoveryService
from tests.fixtures.broker_contract import gateway_facts
from tests.fixtures.recovery_cases import NOW, create_submitting_case


@pytest.fixture
def database(tmp_path: Path):
    opened = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        yield opened
    finally:
        opened.close()


class ProofBehaviorBroker(FakeBroker):
    def __init__(self, behavior: Callable[[BrokerOrderCommand], object]) -> None:
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
        self._behavior = behavior
        self.proof_calls = 0
        self.connect()

    def prove_order_not_accepted(self, command: BrokerOrderCommand) -> object:
        self.proof_calls += 1
        return self._behavior(command)


def _proof(
    command: BrokerOrderCommand,
    *,
    captured_offset: int = 1,
    replace_command: bool = False,
) -> BrokerOrderAbsenceProof:
    proof_command = (
        replace(command, client_order_id=command.client_order_id + "-different")
        if replace_command
        else command
    )
    return BrokerOrderAbsenceProof(
        command=proof_command,
        snapshot_id="authoritative-absence-proof",
        session_date=proof_command.strategy_session,
        captured_at=NOW + timedelta(seconds=captured_offset),
        broker_event_watermark=100,
        evidence_sha256="a" * 64,
    )


def _raises(_: BrokerOrderCommand) -> object:
    raise RuntimeError("proof query unavailable")


@pytest.mark.parametrize(
    "behavior",
    [
        lambda command: _proof(command, captured_offset=-1),
        lambda command: _proof(command, captured_offset=3),
        lambda command: _proof(command, replace_command=True),
        lambda command: {"not_accepted": True},
        _raises,
    ],
)
def test_invalid_or_unavailable_not_accepted_proof_keeps_submit_unknown_and_halted(
    database: Database,
    behavior: Callable[[BrokerOrderCommand], object],
) -> None:
    case = create_submitting_case(database)
    broker = ProofBehaviorBroker(behavior)

    report = ProductionRecoveryService(
        database=database,
        account_store=None,
        account_path=None,
        gateway=broker,
        clock=lambda: NOW + timedelta(seconds=2),
    ).recover()
    recovered = case.repository.load(case.aggregate.intent.execution_id)

    assert recovered is not None and recovered.state is OrderState.UNKNOWN
    assert report.halt_required is True
    assert report.unresolved_order_ids == (case.aggregate.intent.execution_id,)
    assert broker.proof_calls == 1
    assert broker.submitted_commands == ()
    assert broker.cancelled_order_ids == ()
