from __future__ import annotations

import inspect
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import get_type_hints

import pytest

from firmquant.broker.gateway import (
    BrokerEventSink,
    BrokerGateway,
    BrokerHealth,
    BrokerOrderCommand,
)
from firmquant.domain.broker_facts import PriceType, Side
from firmquant.domain.errors import DomainValidationError
from firmquant.domain.values import Price, Shares, Symbol


def _required_methods(protocol: type[object]) -> set[str]:
    return {
        name
        for name, member in protocol.__dict__.items()
        if not name.startswith("_") and inspect.isfunction(member)
    }


def test_gateway_protocol_surface_is_complete_and_narrow() -> None:
    assert _required_methods(BrokerGateway) == {
        "connect",
        "disconnect",
        "health",
        "query_account",
        "query_positions",
        "query_orders",
        "query_fills",
        "query_instrument",
        "query_quote",
        "query_market_status",
        "submit_order",
        "cancel_order",
        "subscribe",
    }


def test_gateway_write_boundary_requires_typed_command_and_sink() -> None:
    submit_hints = get_type_hints(BrokerGateway.submit_order)
    subscribe_hints = get_type_hints(BrokerGateway.subscribe)

    assert submit_hints["command"] is BrokerOrderCommand
    assert subscribe_hints["callback_sink"] is BrokerEventSink


def test_order_command_is_limit_only_and_economically_identified() -> None:
    command = BrokerOrderCommand(
        execution_id="exec_" + "a" * 64,
        idempotency_key="b" * 64,
        client_order_id="uquant-order-1",
        symbol=Symbol.parse("600519.SH"),
        side=Side.BUY,
        price_type=PriceType.LIMIT,
        requested_shares=Shares(100),
        limit_price=Price(Decimal("10.25")),
        strategy_session=date(2026, 8, 24),
    )

    assert command.symbol.canonical == "sh600519"
    assert command.requested_shares == Shares(100)


def test_gateway_health_requires_aware_observation_time() -> None:
    with pytest.raises(DomainValidationError, match="timezone-aware"):
        BrokerHealth(
            connected=True,
            read_healthy=True,
            write_healthy=False,
            observed_at=datetime(2026, 8, 25, 9, 0),
            diagnostic_code="READ_ONLY",
        )

    health = BrokerHealth(
        connected=True,
        read_healthy=True,
        write_healthy=False,
        observed_at=datetime(2026, 8, 25, 9, 0, tzinfo=UTC),
        diagnostic_code="READ_ONLY",
    )
    assert health.read_healthy is True
