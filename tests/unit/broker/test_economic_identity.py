from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from firmquant.broker.client_identity import client_order_tag
from firmquant.broker.economic_identity import EconomicIdentityBroker
from firmquant.broker.gateway import BrokerFactUnavailable
from firmquant.domain.orders import OrderState
from firmquant.persistence.database import Database
from firmquant.persistence.recovery import RecoveryService
from tests.fixtures.recovery_cases import (
    NOW,
    broker_order,
    create_submitting_case,
    fake_recovery_broker,
)


def test_query_and_recovery_resolve_miniqmt_tag_back_to_uquant_order_id(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        case = create_submitting_case(database)
        tagged = replace(
            broker_order(case.command),
            client_order_id=client_order_tag(case.command.client_order_id),
        )
        raw_gateway = fake_recovery_broker(orders=(tagged,))
        gateway = EconomicIdentityBroker(gateway=raw_gateway, database=database)

        assert gateway.query_orders()[0].client_order_id == case.command.client_order_id

        report = RecoveryService(
            database=database,
            account_store=None,
            account_path=None,
            gateway=gateway,
            clock=lambda: NOW,
        ).recover()
        recovered = case.repository.load(case.aggregate.intent.execution_id)

        assert recovered is not None and recovered.state is OrderState.ACKNOWLEDGED
        assert report.unresolved_order_ids == ()
        assert raw_gateway.submitted_commands == ()
    finally:
        database.close()


def test_unknown_firmquant_tag_fails_closed_instead_of_becoming_external(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        case = create_submitting_case(database)
        tagged = replace(
            broker_order(case.command),
            client_order_id=client_order_tag("O-UNKNOWN-FIRMQUANT-ORDER"),
        )
        gateway = EconomicIdentityBroker(
            gateway=fake_recovery_broker(orders=(tagged,)),
            database=database,
        )

        with pytest.raises(BrokerFactUnavailable, match="economic identity"):
            gateway.query_orders()
    finally:
        database.close()
