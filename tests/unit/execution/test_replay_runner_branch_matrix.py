from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from zoneinfo import ZoneInfo

import pytest

from firmquant.domain.broker_facts import BrokerOrderStatus, Side
from firmquant.domain.values import Price, Shares, Symbol
from firmquant.execution import replay_runner as runner
from firmquant.execution.execution_replay import DailyBar, ReplayAccount, ReplaySessionResult, ReplaySide
from firmquant.execution.planner import ExecutionPlan, PlannedOrder
from firmquant.persistence.repositories import canonical_sha256
from firmquant.strategy.identity import StrategyIdentity
from firmquant.strategy.snapshots import DecisionSnapshot

_FIRST = date(2026, 8, 10)
_SECOND = date(2026, 8, 11)
_SYMBOL = Symbol.parse("600000.SH").canonical
_DIGEST = "d" * 64
_FIRMQUANT_COMMIT = "f" * 40


def _identity() -> StrategyIdentity:
    return StrategyIdentity(
        uquant_commit="a" * 40,
        uquant_tree="1" * 40,
        economic_code_fingerprint="2" * 64,
        account_code_fingerprint="3" * 64,
        config_fingerprint="4" * 64,
        public_api_contract_sha256="5" * 64,
        canonical_universe_sha256="6" * 64,
        universe_resource_sha256="7" * 64,
        wheel_sha256="8" * 64,
        package_manifest_sha256="9" * 64,
    )


def test_decision_snapshot_binds_request_and_account_input_fingerprints() -> None:
    identity = _identity()
    payload = {
        "date": _FIRST.isoformat(),
        "effective_config_sha256": identity.config_fingerprint,
        "opportunity": "next-open",
        "risk": {},
        "targets": [],
        "orders": [],
    }
    decision = SimpleNamespace(
        canonical_payload=lambda *, effective_config_sha256: {
            **payload,
            "effective_config_sha256": effective_config_sha256,
        },
        decision_digest=hashlib.sha256(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest(),
        risk_summary={},
    )

    snapshot = runner._decision_snapshot(
        decision=decision,
        session=_FIRST,
        firmquant_commit=_FIRMQUANT_COMMIT,
        identity=identity,
        data_sha256="a" * 64,
        broker_sha256="b" * 64,
        before_sha256="c" * 64,
        after_sha256="e" * 64,
    )

    expected_request = canonical_sha256(
        {
            "schema": "firmquant.execution-replay-decision-request.v1",
            "session": _FIRST.isoformat(),
            "firmquant_commit": _FIRMQUANT_COMMIT,
            "uquant_commit": identity.uquant_commit,
            "data_sha256": "a" * 64,
            "broker_sha256": "b" * 64,
        }
    )
    assert snapshot.request_fingerprint == expected_request
    assert snapshot.input_fingerprint == canonical_sha256(
        {
            "schema": "firmquant.execution-replay-decision-input.v1",
            "request": expected_request,
            "account": "c" * 64,
        }
    )
    assert snapshot.account_after_sha256 == "e" * 64


def test_market_value_rejects_a_non_decimal_mark() -> None:
    account = ReplayAccount(
        cash=Decimal("100"),
        positions={_SYMBOL: 100},
        sellable={_SYMBOL: 100},
    )

    with pytest.raises(runner.ExecutionReplayError, match="daily bar price is malformed"):
        runner._market_value(
            account,
            {_SYMBOL: cast(DailyBar, SimpleNamespace(close="10"))},
            field="close",
        )


def test_execution_replay_restart_switch_is_strictly_boolean(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="restart_each_session must be bool"):
        runner.run_execution_replay(
            source_checkout=tmp_path,
            data_root=tmp_path,
            start=_FIRST,
            end=_SECOND,
            max_price_deviation_bps=Decimal("100"),
            restart_each_session=1,  # type: ignore[arg-type]
        )


@dataclass(slots=True)
class _IdentityView:
    uquant_commit: str = "a" * 40
    config_fingerprint: str = "4" * 64
    canonical_universe_sha256: str = "6" * 64

    def verify(self) -> None:
        return None


class _IdentityFactory:
    @classmethod
    def locked(cls) -> _IdentityView:
        return _IdentityView()


@dataclass(frozen=True, slots=True)
class _Policy:
    deployment_symbols: tuple[str, ...] = (_SYMBOL,)

    @classmethod
    def from_uquant(cls, _raw: object, *, as_of: date) -> _Policy:
        assert as_of == _SECOND
        return cls()


@dataclass(frozen=True, slots=True)
class _Config:
    initial_cash: Decimal = Decimal("100000")
    max_volume_participation: Decimal = Decimal("0.10")
    slippage: Decimal = Decimal("0")
    commission_rate: Decimal = Decimal("0.0003")
    min_commission: Decimal = Decimal("5")
    stamp_duty: Decimal = Decimal("0.0005")
    transfer_fee: Decimal = Decimal("0.00001")


@dataclass(slots=True)
class _Engine:
    cfg: _Config = field(default_factory=_Config)

    def backtest(
        self,
        *,
        symbols: tuple[str, ...],
        start: str,
        end: str,
        initial_cash: float | None = None,
    ) -> dict[str, Decimal]:
        assert symbols == (_SYMBOL,)
        assert (start, end, initial_cash) == (_FIRST.isoformat(), _SECOND.isoformat(), None)
        return {"total_return": Decimal("0")}

    def decide(self, *, symbols: tuple[str, ...], as_of: str, account: object) -> object:
        assert symbols == (_SYMBOL,)
        cast(SimpleNamespace, account).data_hash = _DIGEST
        return SimpleNamespace(session=as_of)


def _bar(session: date, *, symbol: str = _SYMBOL, price: Decimal = Decimal("10")) -> DailyBar:
    return DailyBar(
        session=session,
        symbol=symbol,
        open=price,
        high=price,
        low=price,
        close=price,
        previous_close=price,
        volume=100_000,
        suspended=False,
        limit_up=price * Decimal("1.1"),
        limit_down=price * Decimal("0.9"),
    )


def _buy_plan() -> ExecutionPlan:
    return _plan(
        _planned_order(
            symbol="600000.SH",
            side=Side.BUY,
            shares=100,
            price="10",
        )
    )


def _planned_order(*, symbol: str, side: Side, shares: int, price: str) -> PlannedOrder:
    return PlannedOrder(
        decision_id="decision-1",
        uquant_order_id=f"uquant-{side.value.lower()}-{symbol}",
        symbol=Symbol.parse(symbol),
        side=side,
        target_weight=Decimal("0.01"),
        uquant_authorized_shares=Shares(shares),
        current_shares=Shares(shares if side is Side.SELL else 0),
        target_shares=Shares(0 if side is Side.SELL else shares),
        trading_unit=Shares(100),
        limit_price=Price(Decimal(price)),
        strategy_session=_FIRST,
        execution_session=_SECOND,
        uquant_source_sha="a" * 40,
        reason_code="TARGET_REBALANCE",
    )


def _plan(*orders: PlannedOrder) -> ExecutionPlan:
    return ExecutionPlan(
        plan_id="plan-1",
        decision_id="decision-1",
        strategy_session=_FIRST,
        execution_session=_SECOND,
        broker_snapshot_sha256="b" * 64,
        orders=orders,
        blockers=(),
        created_at=datetime(2026, 8, 11, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
    )


@pytest.mark.parametrize(
    ("cash", "depends_on_sell"),
    [(Decimal("999"), True), (Decimal("1000"), False)],
)
def test_replay_orders_cover_buy_cash_dependency_and_sell_independence(
    cash: Decimal,
    depends_on_sell: bool,
) -> None:
    sell_symbol = Symbol.parse("000001.SZ").canonical
    orders = runner._replay_orders(
        _plan(
            _planned_order(symbol="000001.SZ", side=Side.SELL, shares=100, price="8"),
            _planned_order(symbol="600000.SH", side=Side.BUY, shares=100, price="10"),
        ),
        ReplayAccount(
            cash=cash,
            positions={sell_symbol: 100},
            sellable={sell_symbol: 100},
        ),
        Decimal("0.25"),
    )

    assert orders[0].side is ReplaySide.SELL
    assert orders[0].depends_on_sell_proceeds is False
    assert orders[1].side is ReplaySide.BUY
    assert orders[1].depends_on_sell_proceeds is depends_on_sell


def _tracking_decision(targets: object) -> DecisionSnapshot:
    return cast(DecisionSnapshot, SimpleNamespace(uquant_payload={"targets": targets}))


def test_tracking_covers_target_only_account_only_and_zero_equity_paths() -> None:
    other = Symbol.parse("000001.SZ").canonical
    bars = {
        _SYMBOL: _bar(_SECOND),
        other: _bar(_SECOND, symbol=other, price=Decimal("20")),
    }
    account = ReplayAccount(
        cash=Decimal("400"),
        positions={_SYMBOL: 40, other: 10},
        sellable={_SYMBOL: 40, other: 10},
    )

    errors, weighted, notional = runner._tracking(
        _tracking_decision([{"symbol": "600000.SH", "weight": "0.5"}]),
        account,
        bars,
        target_equity=Decimal("1000"),
    )
    assert errors == [Decimal("0.1"), Decimal("0.2")]
    assert (weighted, notional) == (Decimal("80.0"), Decimal("600"))

    assert runner._tracking(
        _tracking_decision([]),
        ReplayAccount(cash=Decimal(0), positions={}, sellable={}),
        {},
        target_equity=Decimal("1000"),
    ) == ([], Decimal(0), Decimal(0))


@pytest.mark.parametrize(
    ("targets", "message"),
    [
        (None, "targets are unavailable"),
        ([None], "payload is malformed"),
        ([{"symbol": 1, "weight": 1}], "payload is malformed"),
        ([{"symbol": "600000.SH", "weight": True}], "weight is malformed"),
        ([{"symbol": "600000.SH", "weight": None}], "weight is malformed"),
        ([{"symbol": "600000.SH", "weight": "NaN"}], "outside bounds"),
        ([{"symbol": "600000.SH", "weight": "-0.01"}], "outside bounds"),
        ([{"symbol": "600000.SH", "weight": "1.01"}], "outside bounds"),
    ],
)
def test_tracking_branch_matrix_rejects_unusable_target_evidence(
    targets: object,
    message: str,
) -> None:
    with pytest.raises(runner.ExecutionReplayError, match=message):
        runner._tracking(
            _tracking_decision(targets),
            ReplayAccount(cash=Decimal("1000"), positions={}, sellable={}),
            {},
            target_equity=Decimal("1000"),
        )


def test_tracking_rejects_nonpositive_equity_and_missing_symbol_bar() -> None:
    decision = _tracking_decision([{"symbol": "600000.SH", "weight": 1}])
    account = ReplayAccount(cash=Decimal("1000"), positions={}, sellable={})
    with pytest.raises(runner.ExecutionReplayError, match="target equity must be positive"):
        runner._tracking(decision, account, {}, target_equity=Decimal(0))
    with pytest.raises(runner.ExecutionReplayError, match="bar is unavailable"):
        runner._tracking(decision, account, {}, target_equity=Decimal("1000"))


def _result(*items: object) -> ReplaySessionResult:
    return cast(ReplaySessionResult, SimpleNamespace(orders=items))


def test_average_cost_branch_matrix_covers_zero_buy_and_sell_fills() -> None:
    sell = Symbol.parse("000001.SZ").canonical
    before = ReplayAccount(
        cash=Decimal("1000"),
        positions={_SYMBOL: 100, sell: 200},
        sellable={_SYMBOL: 100, sell: 200},
    )
    after = ReplayAccount(
        cash=Decimal("1000"),
        positions={_SYMBOL: 200, sell: 100},
        sellable={_SYMBOL: 100, sell: 100},
    )
    result = _result(
        SimpleNamespace(symbol=_SYMBOL, filled_shares=0),
        SimpleNamespace(symbol=sell, filled_shares=100, side=ReplaySide.SELL),
        SimpleNamespace(
            symbol=_SYMBOL,
            filled_shares=100,
            side=ReplaySide.BUY,
            fill_price=Decimal("10"),
            commission=Decimal("1"),
            transfer_fee=Decimal("1"),
        ),
    )

    assert runner._updated_average_costs(
        before,
        after,
        {_SYMBOL: Decimal("5"), sell: Decimal("7")},
        result,
    ) == {_SYMBOL: Decimal("7.51000000"), sell: Decimal("7")}

    fully_sold = ReplayAccount(cash=Decimal("1000"), positions={}, sellable={})
    assert (
        runner._updated_average_costs(
            ReplayAccount(cash=Decimal("1000"), positions={sell: 100}, sellable={sell: 100}),
            fully_sold,
            {sell: Decimal("7")},
            _result(SimpleNamespace(symbol=sell, filled_shares=100, side=ReplaySide.SELL)),
        )
        == {}
    )


@pytest.mark.parametrize(
    ("orders", "message"),
    [
        ([], "orders are unavailable"),
        ((SimpleNamespace(symbol=None, filled_shares=1),), "result is malformed"),
        ((SimpleNamespace(symbol=_SYMBOL, filled_shares=True),), "result is malformed"),
        (
            (
                SimpleNamespace(
                    symbol=_SYMBOL,
                    filled_shares=100,
                    side=None,
                    fill_price=Decimal("10"),
                ),
            ),
            "fill economics are malformed",
        ),
        (
            (
                SimpleNamespace(
                    symbol=_SYMBOL,
                    filled_shares=100,
                    side=ReplaySide.BUY,
                    fill_price=None,
                ),
            ),
            "fill economics are malformed",
        ),
        (
            (
                SimpleNamespace(
                    symbol=_SYMBOL,
                    filled_shares=100,
                    side=ReplaySide.BUY,
                    fill_price=Decimal("10"),
                    commission=None,
                    transfer_fee=Decimal(0),
                ),
            ),
            "fill fees are malformed",
        ),
        (
            (
                SimpleNamespace(
                    symbol=_SYMBOL,
                    filled_shares=100,
                    side=ReplaySide.BUY,
                    fill_price=Decimal("10"),
                    commission=Decimal(0),
                    transfer_fee=None,
                ),
            ),
            "fill fees are malformed",
        ),
    ],
)
def test_average_cost_branch_matrix_rejects_malformed_results(
    orders: object,
    message: str,
) -> None:
    empty = ReplayAccount(cash=Decimal("1000"), positions={}, sellable={})
    with pytest.raises(runner.ExecutionReplayError, match=message):
        runner._updated_average_costs(
            empty,
            empty,
            {},
            cast(ReplaySessionResult, SimpleNamespace(orders=orders)),
        )


def test_average_costs_require_exact_ending_position_keys() -> None:
    empty = ReplayAccount(cash=Decimal("1000"), positions={}, sellable={})
    after = ReplayAccount(cash=Decimal(0), positions={_SYMBOL: 100}, sellable={})
    with pytest.raises(runner.ExecutionReplayError, match="differs from replay positions"):
        runner._updated_average_costs(empty, after, {}, _result())


def _observed_order(*, filled: int, price: Decimal | None) -> SimpleNamespace:
    return SimpleNamespace(
        symbol=_SYMBOL,
        side=ReplaySide.BUY,
        filled_shares=filled,
        fill_price=price,
        commission=Decimal("5"),
        stamp_duty=Decimal(0),
        transfer_fee=Decimal("0.01"),
    )


def test_broker_fact_branch_matrix_covers_empty_filled_and_cancelled_orders() -> None:
    assert runner._broker_execution_facts(_plan(), None, session=_SECOND) == ((), ())

    planned = _planned_order(symbol="600000.SH", side=Side.BUY, shares=100, price="10")
    orders, fills = runner._broker_execution_facts(
        _plan(planned),
        _result(_observed_order(filled=100, price=Decimal("10"))),
        session=_SECOND,
    )
    assert orders[0].status is BrokerOrderStatus.FILLED
    assert len(fills) == 1

    cancelled, no_fills = runner._broker_execution_facts(
        _plan(planned),
        _result(_observed_order(filled=0, price=None)),
        session=_SECOND,
    )
    assert cancelled[0].status is BrokerOrderStatus.CANCELLED
    assert no_fills == ()


def test_broker_fact_branch_matrix_rejects_incomplete_results() -> None:
    planned = _planned_order(symbol="600000.SH", side=Side.BUY, shares=100, price="10")
    with pytest.raises(runner.ExecutionReplayError, match="orders are unavailable"):
        runner._broker_execution_facts(
            _plan(planned),
            cast(ReplaySessionResult, SimpleNamespace(orders=[])),
            session=_SECOND,
        )
    with pytest.raises(runner.ExecutionReplayError, match="result is missing"):
        runner._broker_execution_facts(_plan(planned), None, session=_SECOND)
    with pytest.raises(runner.ExecutionReplayError, match="has no price"):
        runner._broker_execution_facts(
            _plan(planned),
            _result(_observed_order(filled=100, price=None)),
            session=_SECOND,
        )


def test_runloop_executes_a_real_order_and_aggregates_its_economics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _Engine()
    plan = _buy_plan()
    panel = object()
    facts = SimpleNamespace(
        broker_snapshot=SimpleNamespace(
            account=SimpleNamespace(total_assets=SimpleNamespace(value=Decimal("100000")))
        )
    )

    monkeypatch.setattr(runner, "StrategyIdentity", _IdentityFactory)
    monkeypatch.setattr(runner, "load_locked_source_identity", lambda: object())
    monkeypatch.setattr(runner, "verify_uquant_source_checkout", lambda _source, _path: None)
    monkeypatch.setattr(runner, "current_clean_firmquant_commit", lambda: _FIRMQUANT_COMMIT)
    monkeypatch.setattr(runner, "_sha256_file", lambda _path: _DIGEST)
    monkeypatch.setattr(runner, "UniversePolicy", _Policy)
    monkeypatch.setattr(runner, "_load_panels", lambda _root, _symbols: {_SYMBOL: panel})
    monkeypatch.setattr(runner, "_sessions", lambda _panels, _start, _end: (_FIRST, _SECOND))
    monkeypatch.setattr(runner, "_engine", lambda _source, _root: engine)
    monkeypatch.setattr(runner, "_account_state", lambda _cash: SimpleNamespace(data_hash=None))
    monkeypatch.setattr(
        runner,
        "ExecutionPlanner",
        lambda: SimpleNamespace(plan=lambda _pending, _facts: plan),
    )
    monkeypatch.setattr(
        runner,
        "_execution_facts",
        lambda _account, _costs, _symbols, _panels, *, session: (facts, {_SYMBOL: _bar(session)}),
    )
    monkeypatch.setattr(runner, "_plan_symbols", lambda _pending: (_SYMBOL,))
    monkeypatch.setattr(runner, "_tracking", lambda *_args, **_kwargs: ([], Decimal(0), Decimal(0)))
    monkeypatch.setattr(runner, "_daily_bar", lambda _symbol, _panel, session: _bar(session))
    monkeypatch.setattr(runner, "_previous_close", lambda _panel, _session: Decimal("10"))
    monkeypatch.setattr(
        runner,
        "_snapshot",
        lambda *_args, **_kwargs: SimpleNamespace(raw_payload_sha256="b" * 64),
    )
    monkeypatch.setattr(runner, "sync_account", lambda _account, _snapshot: None)
    monkeypatch.setattr(runner, "_account_sha256", lambda _account: "c" * 64)
    monkeypatch.setattr(
        runner,
        "_decision_snapshot",
        lambda *, session, **_kwargs: SimpleNamespace(strategy_session=session),
    )

    summary = runner.run_execution_replay(
        source_checkout=Path("/unused/source"),
        data_root=Path("/unused/data"),
        start=_FIRST,
        end=_SECOND,
        max_price_deviation_bps=Decimal("100"),
    )

    assert summary.planned_orders == 1
    assert summary.filled_orders == 1
    assert summary.unfilled_orders == 0
    assert summary.partial_fill_count == 0
    assert summary.commissions == Decimal("5.0000")
    assert summary.transfer_fee == Decimal("0.0100")
    assert summary.turnover_notional == Decimal("1000.0000")
    assert summary.firmquant_execution_aware_cumulative_return == Decimal("-0.0000501")
