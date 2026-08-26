from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from firmquant.broker.gateway import BrokerFactUnavailable
from firmquant.broker.paper import (
    PaperBroker,
    PaperCallbackDeliveryError,
    _money,
    _typed_tuple,
)
from firmquant.domain.broker_facts import (
    AccountType,
    BrokerPositionFact,
    MarketSessionStatus,
    SecurityType,
    Side,
)
from firmquant.domain.errors import DomainTypeError, DomainValidationError
from firmquant.domain.values import Money, Price, Shares, Symbol
from firmquant.execution.policy import ExecutionPolicy
from tests.contract.test_paper_broker import _paper, _policy
from tests.fixtures.broker_contract import gateway_facts, order_command

DEFAULT_AVERAGE_COST = Price(Decimal("10"))


def _custom_paper(
    *,
    account: object | None = None,
    positions: object | None = None,
    instruments: object | None = None,
    quotes: object | None = None,
    market_status: object = MarketSessionStatus.OPEN,
    policy: object | None = None,
    clock: object | None = None,
) -> PaperBroker:
    facts = gateway_facts()
    return PaperBroker(
        account=facts.account if account is None else account,  # type: ignore[arg-type]
        positions=() if positions is None else positions,  # type: ignore[arg-type]
        instruments=(facts.instrument,) if instruments is None else instruments,  # type: ignore[arg-type]
        quotes=(facts.quote,) if quotes is None else quotes,  # type: ignore[arg-type]
        market_status=market_status,  # type: ignore[arg-type]
        policy=_policy() if policy is None else policy,  # type: ignore[arg-type]
        clock=(lambda: facts.quote.received_at) if clock is None else clock,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("factory", "exception"),
    [
        (lambda: _typed_tuple([], object, label="values"), DomainTypeError),
        (lambda: _typed_tuple(("bad",), int, label="values"), DomainTypeError),
        (lambda: _money(Decimal("NaN"), label="money"), DomainValidationError),
        (lambda: _custom_paper(account=object()), DomainTypeError),
        (
            lambda: _custom_paper(account=replace(gateway_facts().account, account_type=AccountType.MARGIN)),
            DomainValidationError,
        ),
        (lambda: _custom_paper(policy=object()), DomainTypeError),
        (lambda: _custom_paper(market_status="OPEN"), DomainTypeError),
        (lambda: _custom_paper(clock=object()), DomainTypeError),
        (lambda: _custom_paper(positions=[]), DomainTypeError),
        (lambda: _custom_paper(instruments=[]), DomainTypeError),
        (lambda: _custom_paper(quotes=[]), DomainTypeError),
        (
            lambda: _custom_paper(
                positions=(
                    BrokerPositionFact(
                        symbol=gateway_facts().instrument.symbol,
                        total_shares=Shares(1),
                        sellable_shares=Shares(1),
                        average_cost=Price(Decimal("10")),
                        market_value=Money(Decimal("10")),
                    ),
                )
                * 2
            ),
            DomainValidationError,
        ),
        (
            lambda: _custom_paper(instruments=(gateway_facts().instrument,) * 2),
            DomainValidationError,
        ),
        (lambda: _custom_paper(quotes=(gateway_facts().quote,) * 2), DomainValidationError),
    ],
)
def test_paper_broker_constructor_rejects_ambiguous_state(
    factory: Callable[[], object], exception: type[Exception]
) -> None:
    with pytest.raises(exception):
        factory()


def _position(*, average_cost: Price | None = DEFAULT_AVERAGE_COST) -> BrokerPositionFact:
    facts = gateway_facts()
    return BrokerPositionFact(
        symbol=facts.instrument.symbol,
        total_shares=Shares(100),
        sellable_shares=Shares(100),
        average_cost=average_cost,
        market_value=Money(Decimal("1010")),
    )


def test_position_requires_instrument_quote_cost_and_valuation() -> None:
    facts = gateway_facts()
    with pytest.raises(DomainValidationError, match="instrument metadata"):
        _custom_paper(positions=(_position(),), instruments=())
    with pytest.raises(DomainValidationError, match="quote metadata"):
        _custom_paper(positions=(_position(),), quotes=())
    with pytest.raises(DomainValidationError, match="average cost"):
        _custom_paper(positions=(_position(average_cost=None),))
    no_price = replace(facts.quote, last_price=None, previous_close=None)
    with pytest.raises(DomainValidationError, match="valuation price"):
        _custom_paper(positions=(_position(),), quotes=(no_price,))


def test_queries_updates_and_reason_lookup_validate_types_and_presence() -> None:
    broker = _paper()
    broker.connect()
    unknown = Symbol.parse("000001.SZ")
    with pytest.raises(DomainTypeError):
        broker.query_instrument("600519.SH")  # type: ignore[arg-type]
    with pytest.raises(BrokerFactUnavailable):
        broker.query_instrument(unknown)
    with pytest.raises(DomainTypeError):
        broker.query_quote("600519.SH")  # type: ignore[arg-type]
    with pytest.raises(BrokerFactUnavailable):
        broker.query_quote(unknown)
    with pytest.raises(DomainTypeError):
        broker.subscribe(object())  # type: ignore[arg-type]
    with pytest.raises(DomainTypeError):
        broker.set_quote(object())  # type: ignore[arg-type]
    with pytest.raises(DomainTypeError):
        broker.set_market_status("OPEN")  # type: ignore[arg-type]
    with pytest.raises(DomainValidationError):
        broker.reason_for("")
    with pytest.raises(DomainValidationError):
        broker.reason_for(1)  # type: ignore[arg-type]


def test_quote_update_rejects_unknown_symbol_session_regression_and_collision() -> None:
    facts = gateway_facts()
    broker = _paper()
    unknown = replace(facts.quote, symbol=Symbol.parse("000001.SZ"))
    with pytest.raises(BrokerFactUnavailable):
        broker.set_quote(unknown)
    with pytest.raises(DomainValidationError, match="session dates"):
        broker.set_quote(replace(facts.quote, session_date=date(2026, 8, 26)))
    with pytest.raises(DomainValidationError, match="cannot regress"):
        broker.set_quote(replace(facts.quote, sequence=facts.quote.sequence - 1))
    with pytest.raises(DomainValidationError, match="identity collision"):
        broker.set_quote(replace(facts.quote, last_price=Price(Decimal("10.20"))))


def test_pending_callback_recovery_failure_preserves_degraded_state() -> None:
    broker = _paper(policy=_policy(participation="1"))
    broker.connect()
    broker.submit_order(order_command(identity="pending-callback"))

    def fail(_event: Mapping[str, object]) -> None:
        raise RuntimeError("injected queue failure")

    with pytest.raises(PaperCallbackDeliveryError, match="recovery failed"):
        broker.subscribe(fail)
    with pytest.raises(PaperCallbackDeliveryError, match="degraded"):
        broker.submit_order(order_command(identity="blocked"))


def test_callback_degraded_without_retained_exception_still_blocks_writes() -> None:
    broker = _paper()
    broker.connect()
    broker._callback_delivery_failed = True  # type: ignore[attr-defined]
    broker._callback_error = None  # type: ignore[attr-defined]
    with pytest.raises(PaperCallbackDeliveryError, match="degraded"):
        broker.submit_order(order_command(identity="degraded"))


def _submit_reason(
    *,
    instrument_change: dict[str, object] | None = None,
    quote_change: dict[str, object] | None = None,
    market_status: MarketSessionStatus = MarketSessionStatus.OPEN,
    policy: ExecutionPolicy | None = None,
    command_change: dict[str, object] | None = None,
) -> str:
    facts = gateway_facts()
    broker = _custom_paper(
        instruments=(replace(facts.instrument, **(instrument_change or {})),),
        quotes=(replace(facts.quote, **(quote_change or {})),),
        market_status=market_status,
        policy=policy,
    )
    broker.connect()
    command = order_command(identity="reason")
    if command_change:
        command = replace(command, **command_change)
    order = broker.submit_order(command)
    reason = broker.reason_for(order.broker_order_id)
    assert reason is not None
    return reason


def test_submission_requires_all_authoritative_market_facts() -> None:
    broker = _paper()
    broker._instruments.clear()  # type: ignore[attr-defined]
    broker.connect()
    order = broker.submit_order(order_command(identity="missing-instrument"))
    assert broker.reason_for(order.broker_order_id) == "INSTRUMENT_FACT_MISSING"

    broker = _paper()
    broker._quotes.clear()  # type: ignore[attr-defined]
    broker.connect()
    order = broker.submit_order(order_command(identity="missing-quote"))
    assert broker.reason_for(order.broker_order_id) == "QUOTE_FACT_MISSING"

    broker = _paper()
    broker._instruments.clear()  # type: ignore[attr-defined]
    broker._quotes.clear()  # type: ignore[attr-defined]
    broker.connect()
    missing_facts_command = order_command(identity="missing-all-market-facts")
    state_before = broker.state_sha256
    sequence_before = broker._event_sequence  # type: ignore[attr-defined]
    for _ in range(2):
        with pytest.raises(BrokerFactUnavailable, match="without market facts"):
            broker.submit_order(missing_facts_command)
    assert broker.query_orders() == ()
    assert broker.state_sha256 == state_before
    assert broker._event_sequence == sequence_before  # type: ignore[attr-defined]
    assert broker._idempotency_orders == {}  # type: ignore[attr-defined]
    assert broker._commands == {}  # type: ignore[attr-defined]
    assert broker._command_fingerprints == {}  # type: ignore[attr-defined]
    assert broker._match_counts == {}  # type: ignore[attr-defined]

    assert (
        _submit_reason(
            quote_change={"market_status": MarketSessionStatus.CLOSED},
            market_status=MarketSessionStatus.CLOSED,
        )
        == "MARKET_NOT_OPEN"
    )
    auction_policy = replace(_policy(), allow_auction=True)
    assert (
        _submit_reason(
            quote_change={"market_status": MarketSessionStatus.AUCTION},
            market_status=MarketSessionStatus.OPEN,
            policy=auction_policy,
        )
        == "MARKET_STATUS_MISMATCH"
    )
    assert (
        _submit_reason(instrument_change={"security_type": SecurityType.FUND}) == "SECURITY_TYPE_NOT_EQUITY"
    )
    assert (
        _submit_reason(
            quote_change={
                "lower_limit": Price(Decimal("8")),
                "upper_limit": Price(Decimal("12")),
            }
        )
        == "PRICE_LIMIT_FACT_MISMATCH"
    )
    assert _submit_reason(quote_change={"session_date": date(2026, 8, 26)}) == "MARKET_FACT_SESSION_MISMATCH"
    assert (
        _submit_reason(command_change={"limit_price": Price(Decimal("10.101"))})
        == "LIMIT_PRICE_PRECISION_INVALID"
    )
    assert (
        _submit_reason(command_change={"limit_price": Price(Decimal("10.105"))})
        == "LIMIT_PRICE_PRECISION_INVALID"
    )


def test_sell_requires_real_position_and_actual_sellable_shares() -> None:
    broker = _paper()
    broker.connect()
    order = broker.submit_order(order_command(side=Side.SELL, identity="no-position"))
    assert broker.reason_for(order.broker_order_id) == "POSITION_INSUFFICIENT"


def test_missing_liquidity_nonmarketable_and_slippage_limits_are_deterministic() -> None:
    assert _submit_reason(quote_change={"ask_price": None}) == "ASK_LIQUIDITY_MISSING"
    assert _submit_reason(command_change={"limit_price": Price(Decimal("9.50"))}) == "ORDER_NOT_MARKETABLE"
    assert (
        _submit_reason(
            policy=_policy(participation="1", slippage_bps="100"),
            command_change={"limit_price": Price(Decimal("10.10"))},
        )
        == "SLIPPAGE_EXCEEDS_LIMIT"
    )


def test_cash_insufficient_resting_order_never_creates_negative_cash() -> None:
    facts = gateway_facts()
    account = replace(
        facts.account,
        available_cash=Money(Decimal("1")),
        total_assets=Money(Decimal("1")),
    )
    broker = _paper(account=account, policy=_policy(participation="1"))
    broker.connect()
    order = broker.submit_order(order_command(identity="cash"))
    assert broker.reason_for(order.broker_order_id) == "CASH_INSUFFICIENT"
    assert broker.query_account().available_cash == Money(Decimal("1"))


def test_idempotency_collision_and_invalid_order_ids_fail_closed() -> None:
    broker = _paper()
    broker.connect()
    command = order_command(identity="idempotent")
    first = broker.submit_order(command)
    assert broker.submit_order(command) == first
    with pytest.raises(DomainValidationError, match="identity collision"):
        broker.submit_order(replace(command, requested_shares=Shares(200)))
    with pytest.raises(DomainTypeError):
        broker.submit_order(object())  # type: ignore[arg-type]
    with pytest.raises(DomainValidationError):
        broker.match("")
    with pytest.raises(BrokerFactUnavailable):
        broker.match("missing")
    with pytest.raises(DomainValidationError):
        broker.cancel_order("")
    with pytest.raises(BrokerFactUnavailable):
        broker.cancel_order("missing")


def test_match_quote_symbol_must_equal_order_symbol() -> None:
    broker = _paper()
    broker.connect()
    order = broker.submit_order(order_command(limit_price="9.50", identity="resting"))
    conflicting = replace(gateway_facts().quote, symbol=Symbol.parse("000001.SZ"))
    with pytest.raises(DomainValidationError, match="contradicts"):
        broker.match(order.broker_order_id, quote=conflicting)
