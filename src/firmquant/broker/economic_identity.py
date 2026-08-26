"""Recoverable broker wrapper translating MiniQMT tags to uquant economic order ids."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from firmquant.domain.broker_facts import (
    BrokerAccountFact,
    BrokerFillFact,
    BrokerOrderFact,
    BrokerPositionFact,
    InstrumentFact,
    MarketSessionStatus,
    QuoteFact,
)
from firmquant.domain.values import Symbol
from firmquant.persistence.database import Database

from .client_identity import is_client_order_tag, resolve_uquant_order_id
from .gateway import (
    BrokerEventSink,
    BrokerFactUnavailable,
    BrokerGateway,
    BrokerHealth,
    BrokerOrderCommand,
)


class EconomicIdentityBroker:
    """Translate only firmquant-owned broker tags; never guess external order ownership."""

    __slots__ = ("_database", "_gateway", "_sink", "_wrapped_sink")

    def __init__(self, *, gateway: BrokerGateway, database: Database) -> None:
        if not isinstance(gateway, BrokerGateway):
            raise TypeError("economic identity wrapper requires BrokerGateway")
        if not isinstance(database, Database):
            raise TypeError("economic identity wrapper requires Database")
        self._gateway = gateway
        self._database = database
        self._sink: BrokerEventSink | None = None
        self._wrapped_sink: BrokerEventSink | None = None

    def _known_uquant_order_ids(self) -> frozenset[str]:
        rows = self._database.query_all(
            "SELECT uquant_order_id FROM execution_intents ORDER BY uquant_order_id"
        )
        return frozenset(str(row["uquant_order_id"]) for row in rows)

    def _economic_id(self, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise BrokerFactUnavailable("broker client order identity is malformed")
        known = self._known_uquant_order_ids()
        if value in known:
            return value
        if not is_client_order_tag(value):
            return value
        try:
            return resolve_uquant_order_id(value, known)
        except ValueError as error:
            raise BrokerFactUnavailable(
                "firmquant broker tag has no provable economic identity"
            ) from error

    def _order(self, fact: BrokerOrderFact) -> BrokerOrderFact:
        economic_id = self._economic_id(fact.client_order_id)
        return fact if economic_id == fact.client_order_id else replace(fact, client_order_id=economic_id)

    def _event(self, event: Mapping[str, object]) -> Mapping[str, object]:
        event_type = event.get("event_type")
        raw_payload = event.get("payload")
        if event_type not in {"ORDER", "ORDER_ERROR"} or not isinstance(raw_payload, Mapping):
            return event
        payload = dict(raw_payload)
        if "client_order_id" not in payload:
            return event
        economic_id = self._economic_id(payload["client_order_id"])
        if economic_id == payload["client_order_id"]:
            return event
        payload["client_order_id"] = economic_id
        translated = dict(event)
        translated["payload"] = payload
        return translated

    def connect(self) -> None:
        self._gateway.connect()

    def disconnect(self) -> None:
        self._gateway.disconnect()

    def health(self) -> BrokerHealth:
        return self._gateway.health()

    def query_account(self) -> BrokerAccountFact:
        return self._gateway.query_account()

    def query_positions(self) -> tuple[BrokerPositionFact, ...]:
        return self._gateway.query_positions()

    def query_orders(self) -> tuple[BrokerOrderFact, ...]:
        return tuple(self._order(order) for order in self._gateway.query_orders())

    def query_fills(self) -> tuple[BrokerFillFact, ...]:
        return self._gateway.query_fills()

    def query_instrument(self, symbol: Symbol) -> InstrumentFact:
        return self._gateway.query_instrument(symbol)

    def query_quote(self, symbol: Symbol) -> QuoteFact:
        return self._gateway.query_quote(symbol)

    def query_market_status(self) -> MarketSessionStatus:
        return self._gateway.query_market_status()

    def submit_order(self, command: BrokerOrderCommand) -> BrokerOrderFact:
        return self._order(self._gateway.submit_order(command))

    def cancel_order(self, broker_order_id: str) -> BrokerOrderFact:
        return self._order(self._gateway.cancel_order(broker_order_id))

    def subscribe(self, callback_sink: BrokerEventSink) -> None:
        if not callable(callback_sink):
            raise TypeError("economic identity callback sink must be callable")
        if self._sink is not None and self._sink is not callback_sink:
            raise ValueError("economic identity callback sink is already registered")
        if self._wrapped_sink is None:
            self._sink = callback_sink

            def translated(untrusted_event: Mapping[str, object]) -> None:
                callback_sink(self._event(untrusted_event))

            self._wrapped_sink = translated
            self._gateway.subscribe(translated)

    def __repr__(self) -> str:
        return "<EconomicIdentityBroker>"


__all__ = ("EconomicIdentityBroker",)
