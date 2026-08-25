from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from firmquant.domain.broker_facts import BrokerOrderStatus
from firmquant.domain.values import Money, Shares
from firmquant.persistence.database import Database
from firmquant.reconciliation.models import ReconciliationKind
from firmquant.reconciliation.service import ReconciliationService
from tests.fixtures.reconciliation_cases import NOW, healthy_reconciliation_facts


def test_external_order_forces_halt_without_adopting_it(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        facts = healthy_reconciliation_facts()
        external = replace(
            facts.broker_snapshot.orders[0],
            broker_order_id="manual-broker-order",
            client_order_id="manual-client-order",
            status=BrokerOrderStatus.ACKNOWLEDGED,
            requested_shares=Shares(200),
            filled_shares=Shares(0),
            event_sequence=3,
            raw_payload_sha256="6" * 64,
        )
        facts = replace(
            facts,
            broker_snapshot=replace(
                facts.broker_snapshot,
                orders=(*facts.broker_snapshot.orders, external),
                raw_payload_sha256="5" * 64,
            ),
        )
        service = ReconciliationService(
            database=database,
            cash_tolerance=Money(Decimal("0.01")),
            clock=lambda: NOW,
        )

        receipt = service.run(ReconciliationKind.STARTUP, facts)

        assert receipt.passed is False
        assert receipt.halt_required is True
        assert "EXTERNAL_ACTIVE_ORDER" in receipt.blockers
        assert "REVIEW_EXTERNAL_BROKER_ACTIVITY" in receipt.operator_actions
        assert len(facts.operational_ledger.orders) == 1
        assert database.scalar("SELECT count(*) FROM broker_orders") == 0
    finally:
        database.close()
