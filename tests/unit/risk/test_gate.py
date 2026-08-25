from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from firmquant.config import Mode
from firmquant.domain.broker_facts import (
    AccountType,
    BrokerPositionFact,
    MarketSessionStatus,
    SecurityStatus,
    Side,
)
from firmquant.domain.states import RuntimeState
from firmquant.domain.values import Money, Price, Shares, Symbol
from firmquant.risk.gate import ExecutionRiskGate, GateAction
from tests.fixtures.risk_cases import (
    NOW,
    SYMBOL,
    instrument,
    quote,
    risk_command,
    risk_context,
    risk_limits,
)


def test_healthy_order_is_allowed_without_changing_quantity() -> None:
    decision = ExecutionRiskGate().evaluate(risk_command(), risk_context())

    assert decision.action is GateAction.ALLOW
    assert decision.authorized_shares == Shares(100)
    assert decision.reason_codes == ("ALL_CHECKS_PASSED",)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"kill_switch_tripped": True}, "KILL_SWITCH_TRIPPED"),
        ({"data_identity_matches": False}, "DATA_IDENTITY_DRIFT"),
        ({"config_identity_matches": False}, "CONFIG_IDENTITY_DRIFT"),
        ({"unresolved_order_count": 1}, "UNRESOLVED_ORDER_STATE"),
        ({"reconciliation_healthy": False}, "RECONCILIATION_UNHEALTHY"),
        ({"external_active_order_count": 1}, "EXTERNAL_ACTIVE_ORDER"),
        ({"unexplained_position_change": True}, "UNEXPLAINED_POSITION_CHANGE"),
        ({"corporate_action_suspected": True}, "CORPORATE_ACTION_SUSPECTED"),
        ({"consecutive_rejections": 3}, "CONSECUTIVE_REJECTION_LIMIT"),
        ({"equity_change_fraction": Decimal("0.11")}, "ACCOUNT_EQUITY_ANOMALY"),
        ({"intraday_loss_fraction": Decimal("0.06")}, "INTRADAY_LOSS_LIMIT"),
        ({"capital_drawdown_fraction": Decimal("0.16")}, "CAPITAL_DRAWDOWN_LIMIT"),
        ({"clock_drift": timedelta(seconds=3)}, "CLOCK_DRIFT_LIMIT"),
        ({"runtime_state": RuntimeState.HALTED}, "RUNTIME_NOT_WRITABLE"),
    ],
)
def test_systemic_anomalies_halt(changes: dict[str, object], reason: str) -> None:
    decision = ExecutionRiskGate().evaluate(
        risk_command(), replace(risk_context(), **changes)
    )

    assert decision.action is GateAction.HALT
    assert decision.authorized_shares == Shares(0)
    assert reason in decision.reason_codes


def test_disconnect_is_delayed_then_halted_after_bounded_duration() -> None:
    delayed = ExecutionRiskGate().evaluate(
        risk_command(),
        replace(
            risk_context(),
            broker_connected=False,
            disconnect_duration=timedelta(seconds=5),
        ),
    )
    halted = ExecutionRiskGate().evaluate(
        risk_command(),
        replace(
            risk_context(),
            broker_connected=False,
            disconnect_duration=timedelta(seconds=31),
        ),
    )

    assert delayed.action is GateAction.DELAY
    assert "BROKER_DISCONNECTED" in delayed.reason_codes
    assert halted.action is GateAction.HALT
    assert "BROKER_DISCONNECT_LIMIT" in halted.reason_codes


@pytest.mark.parametrize(
    ("context", "reason"),
    [
        (
            replace(risk_context(), deployment_allowlist=frozenset()),
            "SYMBOL_NOT_DEPLOYMENT_ALLOWED",
        ),
        (
            replace(
                risk_context(),
                canonical_universe=frozenset(),
                deployment_allowlist=frozenset(),
            ),
            "SYMBOL_NOT_CANONICAL_AI_UNIVERSE",
        ),
        (replace(risk_context(), account_type=AccountType.MARGIN), "ACCOUNT_NOT_CASH"),
        (replace(risk_context(), instrument=None), "INSTRUMENT_FACT_MISSING"),
        (replace(risk_context(), quote=None), "QUOTE_FACT_MISSING"),
        (
            replace(
                risk_context(),
                instrument=replace(instrument(), status=SecurityStatus.SUSPENDED),
            ),
            "INSTRUMENT_NOT_TRADING",
        ),
        (
            replace(
                risk_context(),
                instrument=replace(instrument(), status=SecurityStatus.RISK_WARNING),
            ),
            "INSTRUMENT_RISK_STATUS",
        ),
        (
            replace(
                risk_context(),
                instrument=replace(instrument(), lower_limit=None, upper_limit=None),
            ),
            "PRICE_LIMIT_FACT_MISSING",
        ),
    ],
)
def test_static_order_and_security_violations_block(
    context: object, reason: str
) -> None:
    decision = ExecutionRiskGate().evaluate(risk_command(), context)  # type: ignore[arg-type]

    assert decision.action is GateAction.BLOCK
    assert reason in decision.reason_codes


def test_freeze_new_risk_blocks_buy_but_not_risk_reducing_sell() -> None:
    blocked = ExecutionRiskGate().evaluate(
        risk_command(), replace(risk_context(), freeze_new_risk=True)
    )
    position = BrokerPositionFact(
        symbol=SYMBOL,
        total_shares=Shares(500),
        sellable_shares=Shares(500),
        average_cost=Price(Decimal("9")),
        market_value=Money(Decimal("5000")),
    )
    sell_context = replace(
        risk_context(),
        freeze_new_risk=True,
        position=position,
        uquant_target_shares=Shares(0),
        uquant_target_weight=Decimal("0"),
        actual_symbol_notional=position.market_value,
        actual_gross_notional=position.market_value,
    )
    sell = ExecutionRiskGate().evaluate(
        risk_command(side=Side.SELL), sell_context
    )

    assert blocked.action is GateAction.BLOCK
    assert "FREEZE_NEW_RISK" in blocked.reason_codes
    assert sell.action is GateAction.ALLOW


def test_sell_is_shrunk_to_sellable_t_plus_one_quantity_and_lot() -> None:
    position = BrokerPositionFact(
        symbol=SYMBOL,
        total_shares=Shares(500),
        sellable_shares=Shares(250),
        average_cost=Price(Decimal("9")),
        market_value=Money(Decimal("5000")),
    )
    context = replace(
        risk_context(),
        position=position,
        uquant_target_shares=Shares(0),
        uquant_target_weight=Decimal("0"),
        actual_symbol_notional=position.market_value,
        actual_gross_notional=position.market_value,
    )

    decision = ExecutionRiskGate().evaluate(
        risk_command(side=Side.SELL, requested=500, authorized=500), context
    )

    assert decision.action is GateAction.SHRINK
    assert decision.authorized_shares == Shares(200)
    assert "SELLABLE_QUANTITY_SHRINK" in decision.reason_codes


@pytest.mark.parametrize(
    ("changes", "action", "reason"),
    [
        (
            {"market_status": MarketSessionStatus.CLOSED},
            GateAction.DELAY,
            "MARKET_NOT_TRADABLE",
        ),
        (
            {"quote": replace(quote(), received_at=NOW - timedelta(seconds=6))},
            GateAction.DELAY,
            "QUOTE_STALE",
        ),
        (
            {"open_order_count": 10},
            GateAction.DELAY,
            "OPEN_ORDER_LIMIT",
        ),
        (
            {"submit_count_window": 10},
            GateAction.DELAY,
            "SUBMIT_RATE_LIMIT",
        ),
        (
            {"cancel_count_window": 10},
            GateAction.DELAY,
            "CANCEL_RATE_LIMIT",
        ),
        (
            {"existing_order_age": timedelta(minutes=11)},
            GateAction.BLOCK,
            "ORDER_LIFETIME_LIMIT",
        ),
        (
            {"replacement_count": 2},
            GateAction.BLOCK,
            "REPLACEMENT_LIMIT",
        ),
    ],
)
def test_transient_and_lifecycle_limits_have_explicit_actions(
    changes: dict[str, object], action: GateAction, reason: str
) -> None:
    decision = ExecutionRiskGate().evaluate(
        risk_command(), replace(risk_context(), **changes)
    )

    assert decision.action is action
    assert reason in decision.reason_codes


def test_price_checks_use_broker_facts_not_hardcoded_board_percentages() -> None:
    outside = ExecutionRiskGate().evaluate(risk_command(price="11.01"), risk_context())
    deviation = ExecutionRiskGate().evaluate(
        risk_command(price="10.50"),
        replace(
            risk_context(),
            limits=replace(risk_limits(), max_price_deviation_bps=Decimal("100")),
        ),
    )

    assert outside.action is GateAction.BLOCK
    assert "LIMIT_PRICE_OUT_OF_BOUNDS" in outside.reason_codes
    assert deviation.action is GateAction.BLOCK
    assert "PRICE_DEVIATION_LIMIT" in deviation.reason_codes


def test_quantity_caps_compose_by_taking_the_strictest_floor() -> None:
    limits = replace(
        risk_limits(),
        max_order_notional=Money(Decimal("2500")),
        max_daily_submitted_notional=Money(Decimal("2200")),
    )
    decision = ExecutionRiskGate().evaluate(
        risk_command(requested=500, authorized=500),
        replace(
            risk_context(),
            available_cash=Money(Decimal("3500")),
            daily_submitted_notional=Money(Decimal("200")),
            limits=limits,
        ),
    )

    assert decision.action is GateAction.SHRINK
    assert decision.authorized_shares == Shares(200)
    assert "DAILY_SUBMITTED_CAP_SHRINK" in decision.reason_codes
    assert decision.authorized_shares.value <= 500


def test_volume_participation_comes_from_uquant_context_and_shrinks_by_lot() -> None:
    decision = ExecutionRiskGate().evaluate(
        risk_command(requested=500, authorized=500),
        replace(
            risk_context(),
            quote=quote(volume=25_000),
            uquant_max_volume_participation=Decimal("0.005"),
        ),
    )

    assert decision.action is GateAction.SHRINK
    assert decision.authorized_shares == Shares(100)
    assert "VOLUME_PARTICIPATION_SHRINK" in decision.reason_codes


def test_command_exceeding_uquant_authorization_halts_instead_of_normalizing_it() -> None:
    decision = ExecutionRiskGate().evaluate(
        risk_command(requested=500, authorized=100), risk_context()
    )

    assert decision.action is GateAction.HALT
    assert decision.authorized_shares == Shares(0)
    assert "COMMAND_EXCEEDS_UQUANT_AUTHORIZATION" in decision.reason_codes


def test_non_ai_symbol_cannot_pass_even_if_deployment_allowlist_is_wrong() -> None:
    outsider = Symbol.parse("sh600519")
    base = risk_command()
    command = replace(base.command, symbol=outsider)
    candidate = replace(base, command=command)
    context = replace(
        risk_context(),
        deployment_allowlist=frozenset({outsider}),
        canonical_universe=frozenset({SYMBOL}),
    )

    decision = ExecutionRiskGate().evaluate(candidate, context)

    assert decision.action is GateAction.HALT
    assert "DEPLOYMENT_ALLOWLIST_EXPANDS_UNIVERSE" in decision.reason_codes


def test_canary_total_gross_cap_can_only_shrink_buy() -> None:
    decision = ExecutionRiskGate().evaluate(
        risk_command(requested=500, authorized=500),
        replace(
            risk_context(),
            mode=Mode.CANARY,
            total_assets=Money(Decimal("1000000")),
            actual_gross_notional=Money(Decimal("798500")),
            uquant_target_weight=Decimal("0.01"),
            uquant_target_gross=Decimal("1"),
        ),
    )

    assert decision.action is GateAction.SHRINK
    assert decision.authorized_shares == Shares(100)
    assert "TOTAL_GROSS_CAP_SHRINK" in decision.reason_codes
