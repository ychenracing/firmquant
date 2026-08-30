from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import cast

import pandas as pd
import pytest

from firmquant.execution import replay_runner as runner
from firmquant.execution.execution_replay import DailyBar, ReplayAccount

_DIGEST = "d" * 64
_FIRMQUANT_COMMIT = "f" * 40
_SESSIONS = (date(2026, 8, 10), date(2026, 8, 11), date(2026, 8, 12))
_SYMBOL = "600000.SH"


@dataclass(slots=True)
class _Identity:
    uquant_commit: str = "a" * 40
    config_fingerprint: str = "b" * 64
    canonical_universe_sha256: str = "c" * 64
    verified: bool = False

    def verify(self) -> None:
        self.verified = True


class _IdentityFactory:
    value = _Identity()

    @classmethod
    def locked(cls) -> _Identity:
        return cls.value


@dataclass(frozen=True, slots=True)
class _Policy:
    deployment_symbols: tuple[str, ...] = (_SYMBOL,)

    @classmethod
    def from_uquant(cls, _raw: object, *, as_of: date) -> _Policy:
        assert as_of == _SESSIONS[-1]
        return cls()


@dataclass(frozen=True, slots=True)
class _Config:
    initial_cash: Decimal = Decimal("100000")
    max_volume_participation: Decimal = Decimal("0.1")
    slippage: Decimal = Decimal("0.0005")
    commission_rate: Decimal = Decimal("0.0003")
    min_commission: Decimal = Decimal("5")
    stamp_duty: Decimal = Decimal("0.0005")
    transfer_fee: Decimal = Decimal("0.00001")


@dataclass(slots=True)
class _StrategyAccount:
    data_hash: str | None = None


@dataclass(frozen=True, slots=True)
class _Decision:
    session: str


@dataclass(frozen=True, slots=True)
class _Pending:
    session: date


@dataclass(slots=True)
class _Engine:
    bind_data_identity: bool = True
    cfg: _Config = field(default_factory=_Config)
    decided_sessions: list[str] = field(default_factory=list)

    def backtest(
        self,
        *,
        symbols: tuple[str, ...],
        start: str,
        end: str,
        initial_cash: float | None = None,
    ) -> dict[str, Decimal]:
        assert symbols == (_SYMBOL,)
        assert (start, end, initial_cash) == ("2026-08-10", "2026-08-12", None)
        return {"total_return": Decimal("0.125")}

    def decide(self, *, symbols: tuple[str, ...], as_of: str, account: object) -> _Decision:
        assert symbols == (_SYMBOL,)
        strategy_account = cast(_StrategyAccount, account)
        if self.bind_data_identity:
            strategy_account.data_hash = _DIGEST
        self.decided_sessions.append(as_of)
        return _Decision(as_of)


@dataclass(frozen=True, slots=True)
class _Money:
    value: Decimal


@dataclass(frozen=True, slots=True)
class _BrokerAccount:
    total_assets: _Money


@dataclass(frozen=True, slots=True)
class _BrokerSnapshot:
    account: _BrokerAccount


@dataclass(frozen=True, slots=True)
class _ExecutionFacts:
    broker_snapshot: _BrokerSnapshot


@dataclass(frozen=True, slots=True)
class _Plan:
    orders: tuple[()] = ()


@dataclass(slots=True)
class _Planner:
    calls: list[date] = field(default_factory=list)

    def plan(self, pending: _Pending, facts: _ExecutionFacts) -> _Plan:
        assert facts.broker_snapshot.account.total_assets.value == Decimal("100000")
        self.calls.append(pending.session)
        return _Plan()


@dataclass(slots=True)
class _Harness:
    engine: _Engine
    planner: _Planner
    restarts: list[date]


def _bar(session: date) -> DailyBar:
    return DailyBar(
        session=session,
        symbol=_SYMBOL,
        open=Decimal("10"),
        high=Decimal("10"),
        low=Decimal("10"),
        close=Decimal("10"),
        previous_close=Decimal("10"),
        volume=100_000,
        suspended=False,
        limit_up=Decimal("11"),
        limit_down=Decimal("9"),
    )


def _install_runloop_harness(
    monkeypatch: pytest.MonkeyPatch,
    *,
    bind_data_identity: bool = True,
    markable: bool = True,
    tracking: tuple[list[Decimal], Decimal, Decimal] = ([], Decimal(0), Decimal(0)),
    restart_with_holding: bool = False,
) -> _Harness:
    identity = _Identity()
    _IdentityFactory.value = identity
    engine = _Engine(bind_data_identity=bind_data_identity)
    planner = _Planner()
    restarts: list[date] = []
    panels = {_SYMBOL: pd.DataFrame()}

    monkeypatch.setattr(runner, "StrategyIdentity", _IdentityFactory)
    monkeypatch.setattr(runner, "load_locked_source_identity", lambda: object())
    monkeypatch.setattr(runner, "verify_uquant_source_checkout", lambda _source, _path: None)
    monkeypatch.setattr(runner, "current_clean_firmquant_commit", lambda: _FIRMQUANT_COMMIT)
    monkeypatch.setattr(runner, "_sha256_file", lambda _path: _DIGEST)
    monkeypatch.setattr(runner, "UniversePolicy", _Policy)
    monkeypatch.setattr(runner, "_load_panels", lambda _root, _symbols: panels)
    monkeypatch.setattr(runner, "_sessions", lambda _panels, _start, _end: _SESSIONS)
    monkeypatch.setattr(runner, "_engine", lambda _source, _root: engine)
    monkeypatch.setattr(runner, "_account_state", lambda _cash: _StrategyAccount())
    monkeypatch.setattr(runner, "ExecutionPlanner", lambda: planner)
    monkeypatch.setattr(
        runner,
        "_execution_facts",
        lambda _account, _costs, _symbols, _panels, *, session: (
            _ExecutionFacts(_BrokerSnapshot(_BrokerAccount(_Money(Decimal("100000"))))),
            {},
        ),
    )
    monkeypatch.setattr(runner, "_plan_symbols", lambda _pending: ())
    monkeypatch.setattr(runner, "_replay_orders", lambda _plan, _account, _participation: ())
    monkeypatch.setattr(runner, "_tracking", lambda _pending, _account, _bars, *, target_equity: tracking)
    monkeypatch.setattr(runner, "_daily_bar", lambda _symbol, _panel, session: _bar(session))
    monkeypatch.setattr(
        runner,
        "_previous_close",
        lambda _panel, _session: Decimal("10") if markable else None,
    )
    monkeypatch.setattr(runner, "sync_account", lambda _account, _snapshot: None)
    monkeypatch.setattr(runner, "_account_sha256", lambda _account: _DIGEST)
    monkeypatch.setattr(
        runner,
        "_decision_snapshot",
        lambda *, session, **_kwargs: _Pending(session),
    )

    def restart_roundtrip(
        *,
        strategy_account: _StrategyAccount,
        replay_account: ReplayAccount,
        average_costs: dict[str, Decimal],
        pending: _Pending,
        source_checkout: Path,
        data_root: Path,
    ) -> tuple[_StrategyAccount, ReplayAccount, dict[str, Decimal], _Pending, _Engine]:
        del source_checkout, data_root
        restarts.append(pending.session)
        if restart_with_holding:
            replay_account = ReplayAccount(
                cash=replay_account.cash - Decimal("1000"),
                positions={_SYMBOL: 100},
                sellable={_SYMBOL: 100},
            )
            average_costs = {_SYMBOL: Decimal("10")}
        return strategy_account, replay_account, average_costs, pending, engine

    monkeypatch.setattr(runner, "_restart_roundtrip", restart_roundtrip)
    return _Harness(engine=engine, planner=planner, restarts=restarts)


def _run(*, restart_each_session: bool = False) -> runner.ReplaySummary:
    return runner.run_execution_replay(
        source_checkout=Path("/not-used/source"),
        data_root=Path("/not-used/data"),
        start=_SESSIONS[0],
        end=_SESSIONS[-1],
        max_price_deviation_bps=Decimal("100"),
        restart_each_session=restart_each_session,
    )


def test_runloop_returns_identity_bound_zero_order_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _install_runloop_harness(monkeypatch)

    summary = _run()

    assert harness.engine.decided_sessions == [item.isoformat() for item in _SESSIONS]
    assert harness.planner.calls == list(_SESSIONS[:-1])
    assert summary.theoretical_uquant_cumulative_return == Decimal("0.125")
    assert summary.firmquant_execution_aware_cumulative_return == 0
    assert summary.return_gap == Decimal("-0.125")
    assert summary.maximum_drawdown == 0
    assert summary.turnover_notional == 0
    assert summary.turnover_ratio == 0
    assert summary.planned_orders == 0
    assert summary.filled_orders == 0
    assert summary.unfilled_orders == 0
    assert summary.partial_fill_count == 0
    assert summary.firmquant_commit == _FIRMQUANT_COMMIT
    assert summary.uquant_commit == "a" * 40
    assert summary.frozen_data_manifest_sha256 == _DIGEST
    assert (summary.input_start, summary.input_end) == (_SESSIONS[0], _SESSIONS[-1])


def test_pending_empty_plans_aggregate_tracking_without_orders_and_restart_each_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _install_runloop_harness(
        monkeypatch,
        tracking=([Decimal("0.25")], Decimal("25"), Decimal("100")),
    )

    summary = _run(restart_each_session=True)

    assert harness.planner.calls == list(_SESSIONS[:-1])
    assert harness.restarts == list(_SESSIONS)
    assert summary.planned_orders == 0
    assert summary.max_target_tracking_error == Decimal("0.25")
    assert summary.mean_target_tracking_error == Decimal("0.25")
    assert summary.notional_weighted_target_tracking_error == Decimal("0.25")


def test_runloop_rejects_held_symbol_without_authoritative_mark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _install_runloop_harness(
        monkeypatch,
        markable=False,
        restart_with_holding=True,
    )

    with pytest.raises(runner.ExecutionReplayError, match=r"cannot mark held symbol: 600000\.SH"):
        _run(restart_each_session=True)

    assert harness.restarts == [_SESSIONS[0]]


def test_runloop_rejects_decision_without_data_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _install_runloop_harness(monkeypatch, bind_data_identity=False)

    with pytest.raises(runner.ExecutionReplayError, match="did not bind a data identity"):
        _run()

    assert harness.engine.decided_sessions == [_SESSIONS[0].isoformat()]
    assert harness.planner.calls == []
