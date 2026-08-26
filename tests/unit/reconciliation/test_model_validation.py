from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from firmquant.domain.broker_facts import AccountType, Side
from firmquant.domain.errors import DomainTypeError, DomainValidationError
from firmquant.domain.orders import OrderState
from firmquant.domain.values import Money, Shares, Symbol
from firmquant.reconciliation.models import (
    ExpectedPosition,
    OperationalOrderView,
    ReconciliationKind,
    ReconciliationReceipt,
)
from tests.fixtures.reconciliation_cases import healthy_reconciliation_facts

NOW = datetime(2026, 8, 25, 1, tzinfo=UTC)


def _receipt() -> ReconciliationReceipt:
    return ReconciliationReceipt(
        reconciliation_id="recon_" + "a" * 64,
        kind=ReconciliationKind.STARTUP,
        snapshot_id="snapshot-1",
        started_at=NOW,
        completed_at=NOW,
        passed=True,
        blockers=(),
        operator_actions=(),
        details_json="{}",
        details_sha256="b" * 64,
    )


@pytest.mark.parametrize(
    ("factory", "exception"),
    [
        (lambda: ExpectedPosition("bad", Shares(1), Shares(1)), DomainTypeError),
        (lambda: ExpectedPosition(Symbol.parse("600519.SH"), 1, Shares(1)), DomainTypeError),
        (
            lambda: ExpectedPosition(Symbol.parse("600519.SH"), Shares(0), Shares(0)),
            DomainValidationError,
        ),
        (
            lambda: ExpectedPosition(Symbol.parse("600519.SH"), Shares(1), Shares(2)),
            DomainValidationError,
        ),
    ],
)
def test_expected_position_rejects_invalid_strategy_projection(
    factory: Callable[[], object], exception: type[Exception]
) -> None:
    with pytest.raises(exception):
        factory()


@pytest.mark.parametrize(
    ("change", "exception"),
    [
        ({"available_cash": "1"}, DomainTypeError),
        ({"available_cash": Money(Decimal("2")), "total_assets": Money(Decimal("1"))}, DomainValidationError),
        ({"positions": []}, DomainTypeError),
        (
            {"positions": (ExpectedPosition(Symbol.parse("600519.SH"), Shares(1), Shares(1)),) * 2},
            DomainValidationError,
        ),
        ({"known_uquant_order_ids": ("order-1",)}, DomainTypeError),
        ({"known_uquant_order_ids": frozenset({" bad"})}, DomainValidationError),
        ({"economic_state_sha256": "bad"}, DomainValidationError),
    ],
)
def test_strategy_account_view_rejects_ambiguous_economic_state(
    change: dict[str, object], exception: type[Exception]
) -> None:
    valid = healthy_reconciliation_facts().strategy_account
    with pytest.raises(exception):
        replace(valid, **change)


@pytest.mark.parametrize(
    ("change", "exception"),
    [
        ({"broker_order_id": ""}, DomainValidationError),
        ({"uquant_order_id": "bad\n"}, DomainValidationError),
        ({"symbol": "bad"}, DomainTypeError),
        ({"side": "BUY"}, DomainTypeError),
        ({"requested_shares": 1}, DomainTypeError),
        ({"requested_shares": Shares(0)}, DomainValidationError),
        ({"filled_shares": Shares(101)}, DomainValidationError),
        ({"local_state": "FILLED"}, DomainTypeError),
    ],
)
def test_operational_order_view_rejects_invalid_mapping(
    change: dict[str, object], exception: type[Exception]
) -> None:
    valid = healthy_reconciliation_facts().operational_ledger.orders[0]
    with pytest.raises(exception):
        replace(valid, **change)


def _operational_order(identity: str) -> OperationalOrderView:
    return OperationalOrderView(
        broker_order_id=f"broker-{identity}",
        uquant_order_id=f"uquant-{identity}",
        symbol=Symbol.parse("600519.SH"),
        side=Side.BUY,
        requested_shares=Shares(100),
        filled_shares=Shares(0),
        local_state=OrderState.ACKNOWLEDGED,
    )


@pytest.mark.parametrize(
    ("change", "exception"),
    [
        ({"expected_account_id_hash": "bad"}, DomainValidationError),
        ({"expected_account_type": "CASH"}, DomainTypeError),
        ({"orders": []}, DomainTypeError),
        ({"orders": (_operational_order("1"), _operational_order("1"))}, DomainValidationError),
        ({"known_broker_fill_ids": ("fill-1",)}, DomainTypeError),
        ({"known_broker_fill_ids": frozenset({""})}, DomainValidationError),
        ({"unresolved_execution_ids": ["exec-1"]}, DomainTypeError),
        ({"unresolved_execution_ids": ("exec-1", "exec-1")}, DomainValidationError),
        ({"unresolved_execution_ids": (" bad",)}, DomainValidationError),
        ({"submitting_unresolved_execution_ids": ["exec-1"]}, DomainTypeError),
        ({"submitting_unresolved_execution_ids": ("exec-1", "exec-1")}, DomainValidationError),
    ],
)
def test_operational_ledger_view_rejects_invalid_online_state(
    change: dict[str, object], exception: type[Exception]
) -> None:
    valid = healthy_reconciliation_facts().operational_ledger
    with pytest.raises(exception):
        replace(valid, **change)


@pytest.mark.parametrize(
    ("change", "exception"),
    [
        ({"broker_snapshot": object()}, DomainTypeError),
        ({"strategy_account": object()}, DomainTypeError),
        ({"operational_ledger": object()}, DomainTypeError),
        ({"company_action_suspected_symbols": {Symbol.parse("600519.SH")}}, DomainTypeError),
        ({"company_action_suspected_symbols": frozenset({"600519.SH"})}, DomainTypeError),
        ({"uquant_code_identity_matches": 1}, DomainTypeError),
        ({"data_identity_matches": 1}, DomainTypeError),
        ({"config_identity_matches": 1}, DomainTypeError),
    ],
)
def test_reconciliation_facts_reject_authority_boundary_confusion(
    change: dict[str, object], exception: type[Exception]
) -> None:
    with pytest.raises(exception):
        replace(healthy_reconciliation_facts(), **change)


@pytest.mark.parametrize(
    ("change", "exception"),
    [
        ({"reconciliation_id": "bad"}, DomainValidationError),
        ({"kind": "STARTUP"}, DomainTypeError),
        ({"snapshot_id": ""}, DomainValidationError),
        ({"started_at": "now"}, DomainTypeError),
        ({"started_at": datetime(2026, 8, 25)}, DomainValidationError),
        ({"completed_at": NOW - timedelta(seconds=1)}, DomainValidationError),
        ({"passed": 1}, DomainTypeError),
        ({"blockers": ["MISMATCH"]}, DomainTypeError),
        ({"blockers": ("Z", "A")}, DomainValidationError),
        ({"operator_actions": ("ACTION", "ACTION")}, DomainValidationError),
        ({"blockers": (" bad",), "passed": False}, DomainValidationError),
        ({"blockers": ("MISMATCH",)}, DomainValidationError),
        ({"details_json": ""}, DomainValidationError),
        ({"details_sha256": "bad"}, DomainValidationError),
    ],
)
def test_reconciliation_receipt_rejects_inconsistent_evidence(
    change: dict[str, object], exception: type[Exception]
) -> None:
    with pytest.raises(exception):
        replace(_receipt(), **change)


def test_failed_reconciliation_receipt_requires_halt() -> None:
    receipt = replace(_receipt(), passed=False, blockers=("MISMATCH",))
    assert receipt.halt_required is True


def test_account_type_is_explicit_in_operational_view() -> None:
    ledger = replace(
        healthy_reconciliation_facts().operational_ledger,
        expected_account_type=AccountType.CASH,
    )
    assert ledger.expected_account_type is AccountType.CASH
