from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from firmquant.broker.fake import BrokerOperation, FakeBroker, ScriptedOutcome
from firmquant.broker.gateway import BrokerOrderCommand
from firmquant.broker.normalization import normalize_order
from firmquant.broker.paper import PaperBroker
from firmquant.domain.broker_facts import (
    AccountType,
    BrokerAccountFact,
    BrokerOrderStatus,
    BrokerPositionFact,
    BrokerSnapshot,
    InstrumentFact,
    MarketSessionStatus,
    PriceType,
    QuoteFact,
    SecurityStatus,
    SecurityType,
    Side,
)
from firmquant.domain.values import Money, Price, Shares, Symbol
from firmquant.execution.controller import ExecutionController, ExecutionSessionResult
from firmquant.execution.planner import (
    ExecutionBrokerSnapshot,
    ExecutionPlan,
    ExecutionPlanner,
)
from firmquant.execution.policy import ExecutionPolicy, FeeSchedule, FillModel
from firmquant.persistence.database import Database
from firmquant.persistence.repositories import (
    DecisionSnapshotRepository,
    ExecutionLedgerRepository,
)
from firmquant.strategy.identity import StrategyIdentity
from firmquant.strategy.snapshots import DecisionSnapshot

STRATEGY_SESSION = date(2026, 8, 24)
EXECUTION_SESSION = date(2026, 8, 25)
NOW = datetime(2026, 8, 25, 1, 31, tzinfo=UTC)
SELL_SYMBOL = Symbol.parse("sz300308")
BUY_SYMBOL = Symbol.parse("sz300502")


def fee_schedule() -> FeeSchedule:
    return FeeSchedule(
        commission_rate=Decimal("0.0003"),
        minimum_commission=Decimal("5.00"),
        stamp_duty_rate=Decimal("0.001"),
        transfer_fee_rate=Decimal("0.00001"),
        fee_quantum=Decimal("0.01"),
    )


def execution_policy() -> ExecutionPolicy:
    return ExecutionPolicy(
        fill_model=FillModel(
            max_volume_participation=Decimal("0.005"),
            slippage_bps=Decimal("0"),
        ),
        fee_schedule=fee_schedule(),
    )


def _instrument(symbol: Symbol) -> InstrumentFact:
    return InstrumentFact(
        symbol=symbol,
        security_type=SecurityType.EQUITY,
        status=SecurityStatus.TRADING,
        trading_unit=Shares(100),
        price_tick=Price(Decimal("0.01")),
        price_precision=2,
        lower_limit=Price(Decimal("9.00")),
        upper_limit=Price(Decimal("11.00")),
        session_date=EXECUTION_SESSION,
        observed_at=NOW,
    )


def _quote(symbol: Symbol, *, volume: int) -> QuoteFact:
    return QuoteFact(
        symbol=symbol,
        last_price=Price(Decimal("10.00")),
        previous_close=Price(Decimal("10.00")),
        bid_price=Price(Decimal("10.00")),
        ask_price=Price(Decimal("10.00")),
        volume=Shares(volume),
        turnover=Money(Decimal(volume * 10)),
        lower_limit=Price(Decimal("9.00")),
        upper_limit=Price(Decimal("11.00")),
        market_status=MarketSessionStatus.OPEN,
        sequence=10,
        session_date=EXECUTION_SESSION,
        event_time=NOW,
        received_at=NOW,
    )


def _account_and_positions() -> tuple[BrokerAccountFact, tuple[BrokerPositionFact, ...]]:
    position = BrokerPositionFact(
        symbol=SELL_SYMBOL,
        total_shares=Shares(1000),
        sellable_shares=Shares(1000),
        average_cost=Price(Decimal("9.50")),
        market_value=Money(Decimal("10000.00")),
    )
    account = BrokerAccountFact(
        account_id_hash="a" * 64,
        account_type=AccountType.CASH,
        available_cash=Money(Decimal("1000.00")),
        total_assets=Money(Decimal("11000.00")),
    )
    return account, (position,)


def _target(symbol: Symbol, weight: float, event_suffix: str) -> dict[str, object]:
    return {
        "symbol": symbol.canonical,
        "weight": weight,
        "lifecycle": "CORE",
        "reduction_policy": "FIFO",
        "reason_code": "strategy_target",
        "exit_kind": "strategy",
        "event_id": "evt_" + event_suffix * 64,
        "event_signal_date": STRATEGY_SESSION.isoformat(),
        "event_target_weight_hex": weight.hex(),
        "origin_subsystem": "LEADER",
        "mechanism": "LEADER_SELECTION",
        "origin_lifecycle": "CORE",
        "replaces_symbol": None,
        "industry_at_entry": "optical",
        "industry_manifest_sha256": StrategyIdentity.locked().canonical_universe_sha256,
    }


def _pending_order(
    symbol: Symbol, side: Side, weight: float, order_id: str, event_suffix: str
) -> dict[str, object]:
    return {
        "order_id": order_id,
        "signal_date": STRATEGY_SESSION.isoformat(),
        "snapshot_kind": "ORIGIN",
        "symbol": symbol.canonical,
        "side": side.value,
        "target_weight": weight,
        "reduction_policy": "FIFO",
        "reason_code": "strategy_target",
        "exit_kind": "strategy",
        "event_id": "evt_" + event_suffix * 64,
        "origin_subsystem": "LEADER",
        "mechanism": "LEADER_SELECTION",
        "origin_lifecycle": "CORE",
        "replaces_symbol": None,
        "industry_at_entry": "optical",
        "industry_manifest_sha256": StrategyIdentity.locked().canonical_universe_sha256,
    }


def decision_snapshot(
    *,
    include_sell: bool = True,
    include_buy: bool = True,
    freeze_new_risk: bool = False,
) -> DecisionSnapshot:
    identity = StrategyIdentity.locked()
    targets: list[dict[str, object]] = []
    orders: list[dict[str, object]] = []
    if include_sell:
        targets.append(_target(SELL_SYMBOL, 0.0, "1"))
        orders.append(_pending_order(SELL_SYMBOL, Side.SELL, 0.0, "O-SELL-1", "1"))
    if include_buy:
        targets.append(_target(BUY_SYMBOL, 0.8, "2"))
        orders.append(_pending_order(BUY_SYMBOL, Side.BUY, 0.8, "O-BUY-1", "2"))
    payload: dict[str, object] = {
        "schema": "uquant.decision-control-plane.v2",
        "date": STRATEGY_SESSION.isoformat(),
        "opportunity": "TREND",
        "risk": {
            "state": "NORMAL",
            "target_gross_cap": 1.0,
            "system_gross_cap": 1.0,
        },
        "target_gross": 0.8,
        "targets": targets,
        "orders": orders,
        "effective_config_sha256": identity.config_fingerprint,
    }
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return DecisionSnapshot.create(
        strategy_session=STRATEGY_SESSION,
        request_fingerprint="9" * 64,
        input_fingerprint=hashlib.sha256(
            f"{include_sell}:{include_buy}:{freeze_new_risk}".encode()
        ).hexdigest(),
        firmquant_commit="f" * 40,
        identity=identity,
        data_manifest_sha256="d" * 64,
        broker_snapshot_sha256="b" * 64,
        account_before_sha256="c" * 64,
        account_after_sha256="e" * 64,
        uquant_payload=payload,
        uquant_decision_digest=hashlib.sha256(encoded).hexdigest(),
        risk_summary={
            "sentinel_mode": "FREEZE_ONLY",
            "freeze_new_risk": freeze_new_risk,
            "reasons": ["base_normal"],
            "target_gross_cap": 1.0,
        },
        created_at=datetime(2026, 8, 24, 9, tzinfo=UTC),
    )


def execution_snapshot() -> ExecutionBrokerSnapshot:
    account, positions = _account_and_positions()
    instruments = (_instrument(SELL_SYMBOL), _instrument(BUY_SYMBOL))
    quotes = (_quote(SELL_SYMBOL, volume=20_000), _quote(BUY_SYMBOL, volume=100_000))
    broker_snapshot = BrokerSnapshot(
        snapshot_id="execution-start-snapshot",
        account=account,
        positions=positions,
        orders=(),
        fills=(),
        session_date=EXECUTION_SESSION,
        captured_at=NOW,
        broker_event_watermark=0,
        raw_payload_sha256="8" * 64,
        complete=True,
    )
    return ExecutionBrokerSnapshot(
        broker_snapshot=broker_snapshot,
        instruments=instruments,
        quotes=quotes,
        market_status=MarketSessionStatus.OPEN,
    )


class PersistenceCheckingPaperBroker(PaperBroker):
    def __init__(self, *, database: Database, **kwargs: object) -> None:
        self.persistence_checks: list[str] = []
        self._test_database = database
        super().__init__(**kwargs)  # type: ignore[arg-type]

    def submit_order(self, command: BrokerOrderCommand):  # type: ignore[no-untyped-def]
        row = self._test_database.query_one(
            "SELECT state FROM execution_intents WHERE execution_id = ?",
            (command.execution_id,),
        )
        assert row is not None and row["state"] == "SUBMITTING"
        command_row = self._test_database.query_one(
            """
            SELECT command_kind FROM order_commands oc
            JOIN broker_order_attempts boa ON boa.attempt_id = oc.attempt_id
            WHERE boa.execution_id = ? ORDER BY boa.attempt_number DESC LIMIT 1
            """,
            (command.execution_id,),
        )
        assert command_row is not None and command_row["command_kind"] == "SUBMIT"
        self.persistence_checks.append("SUBMITTING_BEFORE_SUBMIT")
        return super().submit_order(command)

    def cancel_order(self, broker_order_id: str):  # type: ignore[no-untyped-def]
        row = self._test_database.query_one(
            """
            SELECT ei.state, ei.execution_id FROM execution_intents ei
            JOIN broker_orders bo ON bo.execution_id = ei.execution_id
            WHERE bo.broker_order_id = ?
            """,
            (broker_order_id,),
        )
        assert row is not None and row["state"] == "CANCEL_REQUESTED"
        command_row = self._test_database.query_one(
            """
            SELECT command_kind FROM order_commands oc
            JOIN broker_order_attempts boa ON boa.attempt_id = oc.attempt_id
            WHERE boa.execution_id = ? ORDER BY boa.attempt_number DESC LIMIT 1
            """,
            (row["execution_id"],),
        )
        assert command_row is not None and command_row["command_kind"] == "CANCEL"
        self.persistence_checks.append("CANCEL_REQUESTED_BEFORE_CANCEL")
        return super().cancel_order(broker_order_id)


@dataclass(slots=True)
class SessionCase:
    root: Path
    persistence_checks: tuple[str, ...] = ()
    last_intent_states: tuple[str, ...] = ()

    def _database(self, name: str) -> Database:
        return Database.open(self.root / name)

    @staticmethod
    def _persist_snapshot(database: Database, snapshot: DecisionSnapshot) -> None:
        repository = DecisionSnapshotRepository(database)
        with database.transaction():
            repository.append(snapshot)

    def _paper_broker(
        self, database: Database, snapshot: ExecutionBrokerSnapshot
    ) -> PersistenceCheckingPaperBroker:
        return PersistenceCheckingPaperBroker(
            database=database,
            account=snapshot.broker_snapshot.account,
            positions=snapshot.broker_snapshot.positions,
            instruments=snapshot.instruments,
            quotes=snapshot.quotes,
            market_status=snapshot.market_status,
            policy=execution_policy(),
            clock=lambda: NOW,
        )

    def _run_paper(
        self,
        *,
        name: str,
        decision: DecisionSnapshot,
        execution_facts: ExecutionBrokerSnapshot | None = None,
        before_execute: Callable[[PaperBroker, ExecutionPlan], None] | None = None,
    ) -> ExecutionSessionResult:
        database = self._database(name)
        try:
            self._persist_snapshot(database, decision)
            facts = execution_facts or execution_snapshot()
            plan = ExecutionPlanner().plan(decision, facts)
            broker = self._paper_broker(database, facts)
            broker.connect()
            if before_execute is not None:
                before_execute(broker, plan)
            controller = ExecutionController(
                gateway=broker,
                ledger=ExecutionLedgerRepository(database),
                fee_schedule=fee_schedule(),
                clock=lambda: NOW,
                cancel_open_orders_at_end=True,
            )
            result = controller.execute(plan)
            self.persistence_checks = tuple(broker.persistence_checks)
            self.last_intent_states = tuple(
                str(row["state"])
                for row in database.query_all(
                    "SELECT state FROM execution_intents ORDER BY side DESC, symbol"
                )
            )
            return result
        finally:
            database.close()

    def run_with_partial_sell(self) -> ExecutionSessionResult:
        return self._run_paper(
            name="partial-sell.db",
            decision=decision_snapshot(include_sell=True, include_buy=True),
        )

    def run_with_deadline_cancel(self) -> ExecutionSessionResult:
        def move_ask(broker: PaperBroker, _: ExecutionPlan) -> None:
            quote = broker.query_quote(BUY_SYMBOL)
            broker.set_quote(
                replace(
                    quote,
                    last_price=Price(Decimal("10.50")),
                    bid_price=Price(Decimal("10.49")),
                    ask_price=Price(Decimal("10.50")),
                    sequence=quote.sequence + 1,
                )
            )

        facts = execution_snapshot()
        rich_account = replace(
            facts.broker_snapshot.account,
            available_cash=Money(Decimal("2000.00")),
            total_assets=Money(Decimal("12000.00")),
        )
        rich_facts = replace(
            facts,
            broker_snapshot=replace(facts.broker_snapshot, account=rich_account),
        )
        return self._run_paper(
            name="deadline-cancel.db",
            decision=decision_snapshot(include_sell=False, include_buy=True),
            execution_facts=rich_facts,
            before_execute=move_ask,
        )

    def run_submit_timeout_twice(
        self,
    ) -> tuple[ExecutionSessionResult, ExecutionSessionResult, int, str]:
        database = self._database("submit-timeout.db")
        try:
            decision = decision_snapshot(include_sell=True, include_buy=False)
            self._persist_snapshot(database, decision)
            facts = execution_snapshot()
            plan = ExecutionPlanner().plan(decision, facts)
            planned = plan.orders[0]
            response_payload = {
                "broker_order_id": "fake-accepted-order",
                "client_order_id": planned.uquant_order_id,
                "symbol": planned.symbol.canonical,
                "side": planned.side.value,
                "price_type": PriceType.LIMIT.value,
                "status": BrokerOrderStatus.ACKNOWLEDGED.value,
                "requested_shares": planned.uquant_authorized_shares.value,
                "filled_shares": 0,
                "limit_price": planned.limit_price.canonical,
                "session_date": EXECUTION_SESSION.isoformat(),
                "event_time": NOW.isoformat(),
                "event_sequence": 20,
            }
            response = normalize_order(response_payload, received_at=NOW)
            broker = FakeBroker(
                account=facts.broker_snapshot.account,
                positions=facts.broker_snapshot.positions,
                orders=(),
                fills=(),
                instruments=facts.instruments,
                quotes=facts.quotes,
                market_status=facts.market_status,
                clock=lambda: NOW,
            )
            broker.connect()
            broker.script(
                [
                    ScriptedOutcome(
                        operation=BrokerOperation.SUBMIT,
                        response=response,
                        error=TimeoutError("submit response lost"),
                    )
                ]
            )
            controller = ExecutionController(
                gateway=broker,
                ledger=ExecutionLedgerRepository(database),
                fee_schedule=fee_schedule(),
                clock=lambda: NOW,
                cancel_open_orders_at_end=True,
            )
            first = controller.execute(plan)
            second = controller.execute(plan)
            state = str(
                database.scalar(
                    "SELECT state FROM execution_intents WHERE decision_id = ?",
                    (decision.decision_id,),
                )
            )
            return first, second, len(broker.submitted_commands), state
        finally:
            database.close()
