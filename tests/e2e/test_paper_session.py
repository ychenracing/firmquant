from __future__ import annotations

from pathlib import Path

import pytest

from firmquant.domain.broker_facts import Side
from firmquant.domain.orders import OrderState
from firmquant.domain.values import Shares
from tests.fixtures.session_cases import SessionCase


@pytest.fixture
def session_case(tmp_path: Path) -> SessionCase:
    return SessionCase(tmp_path)


def test_buy_uses_realized_cash_not_expected_sale(session_case: SessionCase) -> None:
    result = session_case.run_with_partial_sell()
    sell, buy = result.outcomes

    assert sell.side is Side.SELL
    assert sell.uquant_authorized_shares == Shares(1000)
    assert sell.filled_shares == Shares(100)
    assert sell.final_state is OrderState.CANCELLED
    assert buy.side is Side.BUY
    assert buy.uquant_authorized_shares == Shares(800)
    assert buy.submitted_shares == Shares(100)
    assert buy.submitted_value.value <= result.cash_after_sells.value
    assert result.ending_cash.value >= 0
    assert result.negative_cash is False


def test_submit_and_cancel_are_durable_before_broker_writes(
    session_case: SessionCase,
) -> None:
    session_case.run_with_partial_sell()

    assert session_case.persistence_checks == (
        "SUBMITTING_BEFORE_SUBMIT",
        "CANCEL_REQUESTED_BEFORE_CANCEL",
        "SUBMITTING_BEFORE_SUBMIT",
    )
    assert session_case.last_intent_states == ("CANCELLED", "FILLED")


def test_deadline_cancels_resting_order_without_reprice_loop(
    session_case: SessionCase,
) -> None:
    result = session_case.run_with_deadline_cancel()
    outcome = result.outcomes[0]

    assert outcome.submitted_shares == Shares(100)
    assert outcome.filled_shares == Shares(0)
    assert outcome.final_state is OrderState.CANCELLED
    assert outcome.submit_attempts == 1
    assert outcome.cancel_requests == 1
    assert result.submit_calls == 1
    assert result.cancel_calls == 1


def test_submit_timeout_becomes_unknown_and_never_blindly_resubmits(
    session_case: SessionCase,
) -> None:
    first, second, submit_calls, persisted_state = session_case.run_submit_timeout_twice()

    assert first.unresolved_unknown is True
    assert first.outcomes[0].final_state is OrderState.UNKNOWN
    assert second.outcomes[0].reason_code == "UNRESOLVED_UNKNOWN"
    assert submit_calls == 1
    assert persisted_state == "UNKNOWN"


def test_execution_requires_reconciliation_before_reporting(
    session_case: SessionCase,
) -> None:
    result = session_case.run_with_partial_sell()

    assert result.reconciliation_required is True
    assert result.ready_for_report is False
