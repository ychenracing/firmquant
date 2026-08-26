from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from firmquant.broker.gateway import (
    BrokerDisconnected,
    BrokerGateway,
    BrokerOrderCommand,
)
from firmquant.broker.normalization import (
    normalize_account,
    normalize_instrument,
    normalize_order,
    normalize_quote,
)
from firmquant.domain.broker_facts import (
    BrokerAccountFact,
    BrokerOrderFact,
    InstrumentFact,
    MarketSessionStatus,
    PriceType,
    QuoteFact,
    Side,
)
from firmquant.domain.values import Price, Shares, Symbol

NOW = datetime(2026, 8, 25, 1, 31, tzinfo=UTC)
SESSION = date(2026, 8, 25)


def account_payload() -> dict[str, object]:
    return {
        "account_id_hash": "a" * 64,
        "account_type": "CASH",
        "available_cash": "100000.00",
        "total_assets": "100000.00",
    }


def instrument_payload() -> dict[str, object]:
    return {
        "symbol": "600519.SH",
        "security_type": "EQUITY",
        "status": "TRADING",
        "trading_unit": 100,
        "price_tick": "0.01",
        "price_precision": 2,
        "lower_limit": "9.00",
        "upper_limit": "11.00",
        "session_date": SESSION.isoformat(),
        "observed_at": NOW.isoformat(),
    }


def quote_payload(*, sequence: int = 10) -> dict[str, object]:
    return {
        "symbol": "600519.SH",
        "last_price": "10.10",
        "previous_close": "10.00",
        "bid_price": "10.09",
        "ask_price": "10.10",
        "volume": 100000,
        "turnover": "1010000.00",
        "lower_limit": "9.00",
        "upper_limit": "11.00",
        "market_status": "OPEN",
        "sequence": sequence,
        "session_date": SESSION.isoformat(),
        "event_time": NOW.isoformat(),
    }


def order_payload(
    *,
    broker_order_id: str = "broker-order-1",
    status: str = "ACKNOWLEDGED",
    sequence: int = 20,
    event_time: datetime = NOW,
) -> dict[str, object]:
    return {
        "broker_order_id": broker_order_id,
        "client_order_id": "uquant-order-1",
        "symbol": "600519.SH",
        "side": "BUY",
        "price_type": "LIMIT",
        "status": status,
        "requested_shares": 100,
        "filled_shares": 0,
        "limit_price": "10.10",
        "session_date": SESSION.isoformat(),
        "event_time": event_time.isoformat(),
        "event_sequence": sequence,
    }


def order_event(
    *,
    event_id: str = "event-order-1",
    broker_order_id: str = "broker-order-1",
    status: str = "ACKNOWLEDGED",
    sequence: int = 20,
    event_time: datetime = NOW,
) -> dict[str, object]:
    return {
        "event_id": event_id,
        "event_type": "ORDER",
        "payload": order_payload(
            broker_order_id=broker_order_id,
            status=status,
            sequence=sequence,
            event_time=event_time,
        ),
    }


@dataclass(frozen=True, slots=True)
class GatewayFacts:
    account: BrokerAccountFact
    order: BrokerOrderFact
    instrument: InstrumentFact
    quote: QuoteFact


def gateway_facts() -> GatewayFacts:
    return GatewayFacts(
        account=normalize_account(account_payload()),
        order=normalize_order(order_payload(), received_at=NOW),
        instrument=normalize_instrument(instrument_payload()),
        quote=normalize_quote(quote_payload(), received_at=NOW),
    )


def order_command(
    *,
    side: Side = Side.BUY,
    shares: int = 100,
    limit_price: str = "10.10",
    identity: str = "1",
) -> BrokerOrderCommand:
    execution_digest = hashlib.sha256(f"execution:{identity}".encode()).hexdigest()
    idempotency_key = hashlib.sha256(f"idempotency:{identity}".encode()).hexdigest()
    return BrokerOrderCommand(
        execution_id="exec_" + execution_digest,
        idempotency_key=idempotency_key,
        client_order_id=f"uquant-order-{identity}",
        symbol=Symbol.parse("600519.SH"),
        side=side,
        price_type=PriceType.LIMIT,
        requested_shares=Shares(shares),
        limit_price=Price(Decimal(limit_price)),
        strategy_session=SESSION,
    )


def assert_read_gateway_contract(gateway: BrokerGateway) -> None:
    """Exercise the same normalized read contract for every BrokerGateway."""

    facts = gateway_facts()
    symbol = facts.instrument.symbol
    assert isinstance(gateway, BrokerGateway)
    assert gateway.health().connected is False
    gateway.connect()
    assert gateway.query_account() == facts.account
    assert gateway.query_positions() == ()
    assert gateway.query_orders() == ()
    assert gateway.query_fills() == ()
    assert gateway.query_instrument(symbol) == facts.instrument
    assert gateway.query_quote(symbol) == facts.quote
    assert gateway.query_market_status() == facts.quote.market_status
    gateway.disconnect()
    with pytest.raises(BrokerDisconnected):
        gateway.query_account()


def recording_state() -> dict[str, object]:
    return {
        "schema": "firmquant.broker-recording.v1",
        "record_type": "STATE",
        "captured_at": NOW.isoformat(),
        "account": account_payload(),
        "positions": [],
        "orders": [],
        "fills": [],
        "instruments": [instrument_payload()],
        "quotes": [quote_payload()],
        "market_status": MarketSessionStatus.OPEN.value,
    }


def write_recording(
    path: Path, events: list[dict[str, object]], *, state: dict[str, object] | None = None
) -> None:
    records = [state or recording_state()]
    records.extend(
        {
            "schema": "firmquant.broker-recording.v1",
            "record_type": "EVENT",
            "event": event,
        }
        for event in events
    )
    path.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
            for record in records
        ),
        encoding="utf-8",
    )
