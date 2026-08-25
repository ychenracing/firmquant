from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from firmquant.domain.broker_facts import (
    AccountType,
    BrokerAccountFact,
    BrokerPositionFact,
    BrokerSnapshot,
    InstrumentFact,
    MarketSessionStatus,
    QuoteFact,
    SecurityStatus,
    SecurityType,
)
from firmquant.domain.errors import DomainValidationError
from firmquant.domain.values import Market, Money, Price, Shares, Symbol


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("300308.SZ", "sz300308"),
        ("SH600000", "sh600000"),
        ("600000.SH", "sh600000"),
        ("bj.830799", "bj830799"),
        ("000001", "sz000001"),
        ("000300", "sh000300"),
    ],
)
def test_symbol_normalization(raw: str, expected: str) -> None:
    assert Symbol.parse(raw).canonical == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "60000",
        "600000ABC",
        "US.AAPL",
        "sh-600000",
        "600000.SZ.extra",
        "\uff11\uff12\uff13\uff14\uff15\uff16",
    ],
)
def test_symbol_rejects_malformed_or_non_ascii_input(raw: str) -> None:
    with pytest.raises(DomainValidationError, match="A-share symbol"):
        Symbol.parse(raw)


def test_price_rejects_float_and_money_rejects_negative() -> None:
    with pytest.raises(TypeError, match="Decimal"):
        Price(10.1)  # type: ignore[arg-type]
    with pytest.raises(DomainValidationError, match="nonnegative"):
        Money(Decimal("-0.01"))


def test_values_reject_unbounded_precision_and_bool_shares() -> None:
    with pytest.raises(DomainValidationError, match="decimal places"):
        Price(Decimal("1.123456789"))
    with pytest.raises(TypeError, match="integer"):
        Shares(True)  # type: ignore[arg-type]


def test_values_are_frozen_and_have_canonical_text() -> None:
    price = Price(Decimal("10.1200"))

    assert price.canonical == "10.12"
    assert Money(Decimal("0.0000")).canonical == "0"
    with pytest.raises(FrozenInstanceError):
        price.value = Decimal("11")  # type: ignore[misc]


def test_instrument_uses_broker_facts_without_hardcoded_price_limits() -> None:
    instrument = InstrumentFact(
        symbol=Symbol.parse("688256.SH"),
        security_type=SecurityType.EQUITY,
        status=SecurityStatus.TRADING,
        trading_unit=Shares(200),
        price_tick=Price(Decimal("0.001")),
        price_precision=3,
        lower_limit=Price(Decimal("87.321")),
        upper_limit=Price(Decimal("145.535")),
        session_date=date(2026, 8, 25),
        observed_at=datetime(2026, 8, 25, 1, tzinfo=UTC),
    )

    assert instrument.market is Market.SH
    assert instrument.trading_unit == Shares(200)
    assert instrument.upper_limit == Price(Decimal("145.535"))


def test_quote_rejects_naive_event_time() -> None:
    with pytest.raises(DomainValidationError, match="timezone-aware"):
        QuoteFact(
            symbol=Symbol.parse("sz300308"),
            last_price=Price(Decimal("100")),
            previous_close=Price(Decimal("99")),
            bid_price=Price(Decimal("99.99")),
            ask_price=Price(Decimal("100.01")),
            volume=Shares(10_000),
            turnover=Money(Decimal("1000000")),
            lower_limit=Price(Decimal("80")),
            upper_limit=Price(Decimal("120")),
            market_status=MarketSessionStatus.OPEN,
            sequence=1,
            session_date=date(2026, 8, 25),
            event_time=datetime(2026, 8, 25, 9, 31),
            received_at=datetime(2026, 8, 25, 1, 31, tzinfo=UTC),
        )


def test_snapshot_rejects_duplicate_positions_and_invalid_sellable_shares() -> None:
    position = BrokerPositionFact(
        symbol=Symbol.parse("sz300308"),
        total_shares=Shares(1_000),
        sellable_shares=Shares(900),
        average_cost=Price(Decimal("90")),
        market_value=Money(Decimal("100000")),
    )
    snapshot = BrokerSnapshot(
        snapshot_id="snapshot-1",
        account=BrokerAccountFact(
            account_id_hash="a" * 64,
            account_type=AccountType.CASH,
            available_cash=Money(Decimal("100000")),
            total_assets=Money(Decimal("200000")),
        ),
        positions=(position,),
        orders=(),
        fills=(),
        session_date=date(2026, 8, 25),
        captured_at=datetime(2026, 8, 25, 1, tzinfo=UTC),
        broker_event_watermark=12,
        raw_payload_sha256="b" * 64,
        complete=True,
    )

    assert snapshot.positions == (position,)
    with pytest.raises(DomainValidationError, match="duplicate position"):
        replace(snapshot, positions=(position, position))
    with pytest.raises(DomainValidationError, match="sellable shares"):
        replace(position, sellable_shares=Shares(1_001))
