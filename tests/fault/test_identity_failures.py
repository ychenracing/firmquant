from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from firmquant.domain.broker_facts import BrokerOrderStatus
from firmquant.domain.values import Money, Shares
from firmquant.market_data.calendar import AuthoritativeTradingCalendar
from firmquant.persistence.database import Database
from firmquant.reconciliation.models import ReconciliationKind
from firmquant.reconciliation.service import ReconciliationService
from firmquant.risk.gate import ExecutionRiskGate, GateAction
from tests.fixtures.reconciliation_cases import NOW, healthy_reconciliation_facts
from tests.fixtures.risk_cases import risk_command, risk_context


@pytest.mark.parametrize(
    ("change", "blocker"),
    [
        pytest.param(
            {"uquant_code_identity_matches": False},
            "UQUANT_CODE_IDENTITY_DRIFT",
            id="uquant-code-change",
        ),
        pytest.param(
            {"data_identity_matches": False},
            "DATA_HISTORY_REWRITE",
            id="data-history-rewrite",
        ),
        pytest.param(
            {"config_identity_matches": False},
            "CONFIG_IDENTITY_DRIFT",
            id="config-change-after-arm",
        ),
    ],
)
def test_identity_drift_is_durable_halt_evidence(
    change: dict[str, object],
    blocker: str,
    tmp_path: Path,
) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        receipt = ReconciliationService(
            database=database,
            cash_tolerance=Money(Decimal("0.01")),
            clock=lambda: NOW,
        ).run(
            ReconciliationKind.RECOVERY,
            replace(healthy_reconciliation_facts(), **change),
        )

        assert receipt.halt_required is True
        assert blocker in receipt.blockers
        persisted = database.query_one(
            "SELECT passed, blockers_json FROM reconciliation_runs WHERE reconciliation_id = ?",
            (receipt.reconciliation_id,),
        )
        assert persisted is not None
        assert persisted["passed"] == 0
        assert blocker in str(persisted["blockers_json"])
    finally:
        database.close()


def test_external_order_unexplained_position_and_company_action_all_halt(
    tmp_path: Path,
) -> None:
    facts = healthy_reconciliation_facts()
    external = replace(
        facts.broker_snapshot.orders[0],
        broker_order_id="external-order",
        client_order_id="MANUAL-ORDER",
        status=BrokerOrderStatus.ACKNOWLEDGED,
        filled_shares=Shares(0),
        raw_payload_sha256="7" * 64,
    )
    broker_position = facts.broker_snapshot.positions[0]
    changed_position = replace(
        broker_position,
        total_shares=Shares(broker_position.total_shares.value + 100),
        sellable_shares=Shares(broker_position.sellable_shares.value + 100),
    )
    changed = replace(
        facts,
        broker_snapshot=replace(
            facts.broker_snapshot,
            positions=(changed_position,),
            orders=(*facts.broker_snapshot.orders, external),
            raw_payload_sha256="8" * 64,
        ),
        company_action_suspected_symbols=frozenset({broker_position.symbol}),
    )
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        receipt = ReconciliationService(
            database=database,
            cash_tolerance=Money(Decimal("0.01")),
            clock=lambda: NOW,
        ).run(ReconciliationKind.INTRADAY, changed)

        assert {
            "EXTERNAL_ACTIVE_ORDER",
            "UNEXPLAINED_POSITION_CHANGE",
            "CORPORATE_ACTION_SUSPECTED",
        }.issubset(receipt.blockers)
        assert receipt.halt_required is True
    finally:
        database.close()


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        pytest.param(
            {"clock_drift": timedelta(seconds=3)},
            "CLOCK_DRIFT_LIMIT",
            id="clock-skew",
        ),
        pytest.param(
            {"kill_switch_tripped": True},
            "KILL_SWITCH_TRIPPED",
            id="kill-switch",
        ),
        pytest.param(
            {"data_identity_matches": False},
            "DATA_IDENTITY_DRIFT",
            id="data-drift-risk-gate",
        ),
        pytest.param(
            {"config_identity_matches": False},
            "CONFIG_IDENTITY_DRIFT",
            id="config-drift-risk-gate",
        ),
    ],
)
def test_runtime_identity_and_kill_switch_remove_all_submit_authority(
    change: dict[str, object],
    reason: str,
) -> None:
    decision = ExecutionRiskGate().evaluate(
        risk_command(),
        replace(risk_context(), **change),
    )

    assert decision.authorized_shares == Shares(0)
    assert decision.action is GateAction.HALT
    assert reason in decision.reason_codes


def test_weekday_holiday_is_never_inferred_as_tradable() -> None:
    calendar = AuthoritativeTradingCalendar(
        source="broker-calendar",
        source_sha256="a" * 64,
        covered_from=date(2026, 10, 1),
        covered_through=date(2026, 10, 9),
        trading_sessions=(date(2026, 10, 9),),
    )

    assert date(2026, 10, 2).weekday() < 5
    assert calendar.is_trading_session(date(2026, 10, 2)) is False
