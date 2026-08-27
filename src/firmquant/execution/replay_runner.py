"""Cross-session execution-aware replay over the locked uquant ProductionEngine path."""

from __future__ import annotations

import hashlib
import importlib
import json
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Protocol, cast
from zoneinfo import ZoneInfo

import pandas as pd

from firmquant.application.production_identity import current_clean_firmquant_commit
from firmquant.build_identity import load_locked_source_identity, verify_uquant_source_checkout
from firmquant.domain.broker_facts import (
    AccountType,
    BrokerAccountFact,
    BrokerFillFact,
    BrokerOrderFact,
    BrokerOrderStatus,
    BrokerPositionFact,
    BrokerSnapshot,
    FillStatus,
    InstrumentFact,
    MarketSessionStatus,
    PriceType,
    QuoteFact,
    SecurityStatus,
    SecurityType,
    Side,
)
from firmquant.domain.values import Money, Price, Shares, Symbol
from firmquant.execution.execution_replay import (
    DailyBar,
    ReplayAccount,
    ReplayCosts,
    ReplayOrder,
    ReplaySessionResult,
    ReplaySide,
    execute_session,
)
from firmquant.execution.planner import ExecutionBrokerSnapshot, ExecutionPlan, ExecutionPlanner
from firmquant.persistence.repositories import canonical_sha256
from firmquant.strategy.account_sync import sync_account
from firmquant.strategy.identity import StrategyIdentity
from firmquant.strategy.snapshots import DecisionSnapshot
from firmquant.strategy.universe import UniversePolicy

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_ACCOUNT_HASH = hashlib.sha256(b"firmquant-execution-replay-account").hexdigest()
_ZERO = Decimal(0)
_ONE = Decimal(1)


class ExecutionReplayError(RuntimeError):
    """Locked-data execution replay could not be proven causal and reproducible."""


class _ProductionEngine(Protocol):
    cfg: Any

    def decide(self, *, symbols: tuple[str, ...], as_of: str, account: object) -> Any: ...
    def backtest(
        self,
        *,
        symbols: tuple[str, ...],
        start: str,
        end: str,
        initial_cash: float | None = None,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ReplaySummary:
    theoretical_uquant_cumulative_return: Decimal
    firmquant_execution_aware_cumulative_return: Decimal
    return_gap: Decimal
    maximum_drawdown: Decimal
    turnover_notional: Decimal
    turnover_ratio: Decimal
    commissions: Decimal
    stamp_duty: Decimal
    transfer_fee: Decimal
    slippage_cost: Decimal
    unfilled_loss: Decimal
    max_target_tracking_error: Decimal
    mean_target_tracking_error: Decimal
    notional_weighted_target_tracking_error: Decimal
    planned_orders: int
    filled_orders: int
    unfilled_orders: int
    partial_fill_count: int
    price_limit_blocks: int
    suspension_blocks: int
    incomplete_sell_blocked_buys: int
    firmquant_commit: str
    uquant_commit: str
    uquant_config_sha256: str
    universe_sha256: str
    frozen_data_manifest_sha256: str
    input_start: date
    input_end: date

    def payload(self) -> dict[str, object]:
        return {
            "schema": "firmquant.execution-aware-replay.v1",
            "theoretical_uquant_cumulative_return": _decimal_text(self.theoretical_uquant_cumulative_return),
            "firmquant_execution_aware_cumulative_return": _decimal_text(
                self.firmquant_execution_aware_cumulative_return
            ),
            "return_gap": _decimal_text(self.return_gap),
            "maximum_drawdown": _decimal_text(self.maximum_drawdown),
            "turnover_notional": _decimal_text(self.turnover_notional),
            "turnover_ratio": _decimal_text(self.turnover_ratio),
            "commissions": _decimal_text(self.commissions),
            "stamp_duty": _decimal_text(self.stamp_duty),
            "transfer_fee": _decimal_text(self.transfer_fee),
            "slippage_cost": _decimal_text(self.slippage_cost),
            "unfilled_loss": _decimal_text(self.unfilled_loss),
            "max_target_tracking_error": _decimal_text(self.max_target_tracking_error),
            "mean_target_tracking_error": _decimal_text(self.mean_target_tracking_error),
            "notional_weighted_target_tracking_error": _decimal_text(
                self.notional_weighted_target_tracking_error
            ),
            "planned_orders": self.planned_orders,
            "filled_orders": self.filled_orders,
            "unfilled_orders": self.unfilled_orders,
            "partial_fill_count": self.partial_fill_count,
            "price_limit_blocks": self.price_limit_blocks,
            "suspension_blocks": self.suspension_blocks,
            "incomplete_sell_blocked_buys": self.incomplete_sell_blocked_buys,
            "identity": {
                "firmquant_commit": self.firmquant_commit,
                "uquant_commit": self.uquant_commit,
                "uquant_config_sha256": self.uquant_config_sha256,
                "universe_sha256": self.universe_sha256,
                "frozen_data_manifest_sha256": self.frozen_data_manifest_sha256,
            },
            "input_date_range": {
                "start": self.input_start.isoformat(),
                "end": self.input_end.isoformat(),
            },
        }

    def canonical_json(self) -> str:
        return json.dumps(
            self.payload(),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _sha256_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ExecutionReplayError(f"replay input is not a regular file: {path.name}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _engine(source_checkout: Path, data_root: Path) -> _ProductionEngine:
    verify_uquant_source_checkout(load_locked_source_identity(), source_checkout)
    module = importlib.import_module("uquant.engine")
    module_file = getattr(module, "__file__", None)
    expected = (source_checkout / "uquant/engine.py").resolve()
    if not isinstance(module_file, str) or Path(module_file).resolve() != expected:
        raise ExecutionReplayError(
            "uquant.engine is not imported from the verified source checkout; place the checkout first on PYTHONPATH"
        )
    engine_type = getattr(module, "ProductionEngine", None)
    if not isinstance(engine_type, type):
        raise ExecutionReplayError("locked ProductionEngine is unavailable")
    return cast(_ProductionEngine, engine_type(data_root))


def _account_state(initial_cash: float) -> object:
    module = importlib.import_module("uquant.types")
    account_type = getattr(module, "AccountState", None)
    if not isinstance(account_type, type):
        raise ExecutionReplayError("locked uquant AccountState is unavailable")
    empty = getattr(account_type, "empty", None)
    if not callable(empty):
        raise ExecutionReplayError("locked uquant AccountState.empty is unavailable")
    return empty(initial_cash)


def _account_sha256(account: object) -> str:
    module = importlib.import_module("uquant.account")
    function = getattr(module, "economic_state_sha256", None)
    if not callable(function):
        raise ExecutionReplayError("locked uquant account identity is unavailable")
    value = function(account)
    if not isinstance(value, str) or len(value) != 64:
        raise ExecutionReplayError("locked uquant account identity is malformed")
    return value


def _uquant_symbol(symbol: str) -> str:
    parsed = Symbol.parse(symbol)
    return parsed.market.value.lower() + parsed.code


def _canonical_symbol(raw: str) -> str:
    value = raw.strip().lower()
    if len(value) != 8 or value[:2] not in {"sh", "sz", "bj"} or not value[2:].isdigit():
        raise ExecutionReplayError(f"frozen data filename is not an A-share symbol: {raw}")
    return f"{value[2:]}.{value[:2].upper()}"


def _load_panels(data_root: Path, symbols: tuple[str, ...]) -> dict[str, pd.DataFrame]:
    panels: dict[str, pd.DataFrame] = {}
    required = tuple(sorted(set(symbols) | {"000300.SH", "000682.SH"}))
    for symbol in required:
        path = data_root / f"{_uquant_symbol(symbol)}.csv"
        if not path.is_file() or path.is_symlink():
            continue
        frame = pd.read_csv(path)
        expected = {"date", "open", "high", "low", "close", "volume"}
        if not expected <= set(frame.columns):
            raise ExecutionReplayError(f"frozen data schema is incomplete: {path.name}")
        frame["date"] = pd.to_datetime(frame["date"], errors="raise")
        frame = frame.sort_values("date").drop_duplicates("date", keep=False).set_index("date")
        panels[symbol] = frame
    if "000300.SH" not in panels or "000682.SH" not in panels:
        raise ExecutionReplayError("frozen reference-index coverage is missing")
    return panels


def _sessions(panels: dict[str, pd.DataFrame], start: date, end: date) -> tuple[date, ...]:
    index = panels["000682.SH"].index.intersection(panels["000300.SH"].index)
    selected = index[(index >= pd.Timestamp(start)) & (index <= pd.Timestamp(end))]
    if len(selected) < 2:
        raise ExecutionReplayError("execution replay requires at least two trading sessions")
    return tuple(cast(date, item.date()) for item in selected)


def _bar_row(panel: pd.DataFrame, session: date) -> pd.Series | None:
    timestamp = pd.Timestamp(session)
    if timestamp not in panel.index:
        return None
    row = panel.loc[timestamp]
    if isinstance(row, pd.DataFrame):
        raise ExecutionReplayError("frozen data contains duplicate sessions")
    return cast(pd.Series, row)


def _previous_close(panel: pd.DataFrame, session: date) -> Decimal | None:
    prior = panel.index[panel.index < pd.Timestamp(session)]
    if len(prior) == 0:
        return None
    return Decimal(str(panel.loc[prior[-1], "close"]))


def _listing_session_number(panel: pd.DataFrame, session: date) -> int:
    return int((panel.index <= pd.Timestamp(session)).sum())


def _limit_fraction(symbol: Symbol, panel: pd.DataFrame, session: date) -> Decimal | None:
    # First five listing sessions have no ordinary daily band on the current A-share boards.
    if _listing_session_number(panel, session) <= 5:
        return None
    if symbol.market.value == "BJ":
        return Decimal("0.30")
    if symbol.code.startswith(("300", "301", "688", "689")):
        return Decimal("0.20")
    return Decimal("0.10")


def _tick_price(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _daily_bar(symbol: str, panel: pd.DataFrame, session: date) -> DailyBar:
    row = _bar_row(panel, session)
    previous = _previous_close(panel, session)
    parsed = Symbol.parse(symbol)
    if previous is None:
        raise ExecutionReplayError(f"previous close is missing for {symbol} on {session}")
    if row is None:
        # Missing row on an authoritative index trading session is a suspension, not a fabricated K-line.
        fraction = _limit_fraction(parsed, panel, session) or _ONE
        return DailyBar(
            session=session,
            symbol=symbol,
            open=previous,
            high=previous,
            low=previous,
            close=previous,
            previous_close=previous,
            volume=0,
            suspended=True,
            limit_up=_tick_price(previous * (_ONE + fraction)),
            limit_down=_tick_price(previous * max(_ONE - fraction, Decimal("0.01"))),
        )
    fraction = _limit_fraction(parsed, panel, session)
    if fraction is None:
        upper = _tick_price(previous * Decimal("10"))
        lower = _tick_price(previous * Decimal("0.10"))
    else:
        upper = _tick_price(previous * (_ONE + fraction))
        lower = _tick_price(previous * (_ONE - fraction))
    return DailyBar(
        session=session,
        symbol=symbol,
        open=Decimal(str(row["open"])),
        high=Decimal(str(row["high"])),
        low=Decimal(str(row["low"])),
        close=Decimal(str(row["close"])),
        previous_close=previous,
        volume=max(0, int(Decimal(str(row["volume"])))),
        suspended=False,
        limit_up=upper,
        limit_down=lower,
    )


def _timestamp(session: date, clock: time) -> datetime:
    return datetime.combine(session, clock, tzinfo=_SHANGHAI)


def _market_value(account: ReplayAccount, bars: dict[str, DailyBar], *, field: str) -> Decimal:
    total = account.cash
    for symbol, shares in account.positions.items():
        bar = bars.get(symbol)
        if bar is None:
            raise ExecutionReplayError(f"marking bar is unavailable for held symbol {symbol}")
        price = getattr(bar, field)
        if not isinstance(price, Decimal):
            raise ExecutionReplayError("daily bar price is malformed")
        total += price * Decimal(shares)
    return total


def _snapshot(
    account: ReplayAccount,
    bars: dict[str, DailyBar],
    *,
    average_costs: dict[str, Decimal],
    session: date,
    captured_at: datetime,
    field: str,
    orders: tuple[BrokerOrderFact, ...] = (),
    fills: tuple[BrokerFillFact, ...] = (),
) -> BrokerSnapshot:
    assets = _market_value(account, bars, field=field)
    positions: list[BrokerPositionFact] = []
    for symbol_text, shares in sorted(account.positions.items()):
        bar = bars[symbol_text]
        price = getattr(bar, field)
        average_cost = average_costs.get(symbol_text)
        if average_cost is None or average_cost <= 0:
            raise ExecutionReplayError(f"average cost is unavailable for held symbol {symbol_text}")
        positions.append(
            BrokerPositionFact(
                symbol=Symbol.parse(symbol_text),
                total_shares=Shares(shares),
                sellable_shares=Shares(account.sellable.get(symbol_text, 0)),
                average_cost=Price(average_cost),
                market_value=Money(price * Decimal(shares)),
            )
        )
    payload = {
        "schema": "firmquant.execution-replay-broker-snapshot.v1",
        "session": session.isoformat(),
        "captured_at": captured_at.isoformat(),
        "cash": _decimal_text(account.cash),
        "assets": _decimal_text(assets),
        "positions": dict(sorted(account.positions.items())),
        "sellable": dict(sorted(account.sellable.items())),
        "average_costs": {key: _decimal_text(value) for key, value in sorted(average_costs.items())},
        "orders": [item.raw_payload_sha256 for item in orders],
        "fills": [item.raw_payload_sha256 for item in fills],
        "field": field,
    }
    digest = canonical_sha256(payload)
    return BrokerSnapshot(
        snapshot_id="replay_" + digest,
        account=BrokerAccountFact(
            account_id_hash=_ACCOUNT_HASH,
            account_type=AccountType.CASH,
            available_cash=Money(account.cash),
            total_assets=Money(assets),
        ),
        positions=tuple(positions),
        orders=orders,
        fills=fills,
        session_date=session,
        captured_at=captured_at,
        broker_event_watermark=len(orders) + len(fills),
        raw_payload_sha256=digest,
        complete=True,
    )


def _execution_facts(
    account: ReplayAccount,
    average_costs: dict[str, Decimal],
    plan_symbols: tuple[str, ...],
    panels: dict[str, pd.DataFrame],
    *,
    session: date,
) -> tuple[ExecutionBrokerSnapshot, dict[str, DailyBar]]:
    bars: dict[str, DailyBar] = {}
    symbols = tuple(sorted(set(plan_symbols) | set(account.positions)))
    for symbol in symbols:
        panel = panels.get(symbol)
        if panel is None:
            raise ExecutionReplayError(f"frozen panel is unavailable for {symbol}")
        bars[symbol] = _daily_bar(symbol, panel, session)
    snapshot = _snapshot(
        account,
        bars,
        average_costs=average_costs,
        session=session,
        captured_at=_timestamp(session, time(9, 30)),
        field="open",
    )
    instruments: list[InstrumentFact] = []
    quotes: list[QuoteFact] = []
    for symbol_text in symbols:
        bar = bars[symbol_text]
        symbol = Symbol.parse(symbol_text)
        status = SecurityStatus.SUSPENDED if bar.suspended else SecurityStatus.TRADING
        instrument = InstrumentFact(
            symbol=symbol,
            security_type=SecurityType.EQUITY,
            status=status,
            trading_unit=Shares(100),
            price_tick=Price(Decimal("0.01")),
            price_precision=2,
            lower_limit=Price(bar.limit_down),
            upper_limit=Price(bar.limit_up),
            session_date=session,
            observed_at=_timestamp(session, time(9, 30)),
        )
        quote_price = Price(bar.open)
        quote = QuoteFact(
            symbol=symbol,
            last_price=quote_price,
            previous_close=Price(bar.previous_close),
            bid_price=None if bar.suspended else quote_price,
            ask_price=None if bar.suspended else quote_price,
            volume=Shares(bar.volume),
            turnover=Money(bar.open * Decimal(bar.volume)),
            lower_limit=Price(bar.limit_down),
            upper_limit=Price(bar.limit_up),
            market_status=MarketSessionStatus.OPEN,
            sequence=0,
            session_date=session,
            event_time=_timestamp(session, time(9, 30)),
            received_at=_timestamp(session, time(9, 30)),
        )
        instruments.append(instrument)
        quotes.append(quote)
    return (
        ExecutionBrokerSnapshot(
            broker_snapshot=snapshot,
            instruments=tuple(instruments),
            quotes=tuple(quotes),
            market_status=MarketSessionStatus.OPEN,
        ),
        bars,
    )


def _decision_snapshot(
    *,
    decision: Any,
    session: date,
    firmquant_commit: str,
    identity: StrategyIdentity,
    data_sha256: str,
    broker_sha256: str,
    before_sha256: str,
    after_sha256: str,
) -> DecisionSnapshot:
    payload = decision.canonical_payload(effective_config_sha256=identity.config_fingerprint)
    request = canonical_sha256(
        {
            "schema": "firmquant.execution-replay-decision-request.v1",
            "session": session.isoformat(),
            "firmquant_commit": firmquant_commit,
            "uquant_commit": identity.uquant_commit,
            "data_sha256": data_sha256,
            "broker_sha256": broker_sha256,
        }
    )
    input_fingerprint = canonical_sha256(
        {
            "schema": "firmquant.execution-replay-decision-input.v1",
            "request": request,
            "account": before_sha256,
        }
    )
    return DecisionSnapshot.create(
        strategy_session=session,
        request_fingerprint=request,
        input_fingerprint=input_fingerprint,
        firmquant_commit=firmquant_commit,
        identity=identity,
        data_manifest_sha256=data_sha256,
        broker_snapshot_sha256=broker_sha256,
        account_before_sha256=before_sha256,
        account_after_sha256=after_sha256,
        uquant_payload=payload,
        uquant_decision_digest=decision.decision_digest,
        risk_summary=decision.risk_summary,
        created_at=_timestamp(session, time(15, 10)),
    )


def _plan_symbols(snapshot: DecisionSnapshot) -> tuple[str, ...]:
    payload = json.loads(snapshot.payload_json)
    if not isinstance(payload, dict):
        raise ExecutionReplayError("decision payload is unavailable")
    values: set[str] = set()
    for field in ("pending_orders", "targets"):
        items = payload.get(field)
        if not isinstance(items, list):
            raise ExecutionReplayError(f"decision {field} are unavailable")
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("symbol"), str):
                values.add(Symbol.parse(str(item["symbol"])).canonical)
    return tuple(sorted(values))


def _replay_costs(config: Any, *, max_price_deviation_bps: Decimal) -> ReplayCosts:
    slippage = Decimal(str(config.slippage)) * Decimal("10000")
    return ReplayCosts(
        commission_rate=Decimal(str(config.commission_rate)),
        minimum_commission=Decimal(str(config.min_commission)),
        sell_stamp_duty_rate=Decimal(str(config.stamp_duty)),
        transfer_fee_rate=Decimal(str(config.transfer_fee)),
        slippage_bps=slippage,
        max_price_deviation_bps=max_price_deviation_bps,
    )


def _replay_orders(
    plan: ExecutionPlan,
    account: ReplayAccount,
    max_volume_participation: Decimal,
) -> tuple[ReplayOrder, ...]:
    total_buy_cash = sum(
        (
            item.limit_price.value * Decimal(item.authorized_shares.value)
            for item in plan.orders
            if item.side is Side.BUY
        ),
        start=_ZERO,
    )
    cash_dependency = total_buy_cash > account.cash
    return tuple(
        ReplayOrder(
            symbol=item.symbol.canonical,
            side=ReplaySide(item.side.value),
            shares=item.authorized_shares.value,
            limit_price=item.limit_price.value,
            max_volume_participation=max_volume_participation,
            depends_on_sell_proceeds=item.side is Side.BUY and cash_dependency,
        )
        for item in plan.orders
    )


def _tracking(
    decision: DecisionSnapshot,
    account: ReplayAccount,
    bars: dict[str, DailyBar],
    *,
    target_equity: Decimal,
) -> tuple[list[Decimal], Decimal, Decimal]:
    if target_equity <= 0:
        raise ExecutionReplayError("tracking target equity must be positive")
    actual_equity = _market_value(account, bars, field="open")
    raw_targets = decision.uquant_payload.get("targets")
    if not isinstance(raw_targets, list):
        raise ExecutionReplayError("decision targets are unavailable for tracking")
    target_weights: dict[str, Decimal] = {}
    for raw in raw_targets:
        if not isinstance(raw, dict) or not isinstance(raw.get("symbol"), str):
            raise ExecutionReplayError("decision target payload is malformed")
        weight_raw = raw.get("weight")
        if isinstance(weight_raw, bool) or not isinstance(weight_raw, (int, float, str)):
            raise ExecutionReplayError("decision target weight is malformed")
        weight = Decimal(str(weight_raw))
        if not weight.is_finite() or not _ZERO <= weight <= _ONE:
            raise ExecutionReplayError("decision target weight is outside bounds")
        target_weights[Symbol.parse(str(raw["symbol"])).canonical] = weight
    symbols = tuple(sorted(set(target_weights) | set(account.positions)))
    errors: list[Decimal] = []
    weighted = _ZERO
    notional = _ZERO
    for symbol in symbols:
        bar = bars.get(symbol)
        if bar is None:
            raise ExecutionReplayError(f"tracking bar is unavailable for {symbol}")
        weight = target_weights.get(symbol, _ZERO)
        raw_target_shares = int((target_equity * weight / bar.open).to_integral_value(rounding=ROUND_FLOOR))
        target_shares = raw_target_shares - raw_target_shares % 100
        actual_shares = account.positions.get(symbol, 0)
        actual_notional = Decimal(actual_shares) * bar.open
        actual_weight = _ZERO if actual_equity <= 0 else actual_notional / actual_equity
        error = abs(weight - actual_weight)
        errors.append(error)
        target_notional = Decimal(target_shares) * bar.open
        symbol_notional = max(target_notional, actual_notional)
        weighted += error * symbol_notional
        notional += symbol_notional
    return errors, weighted, notional


def _updated_average_costs(
    before: ReplayAccount,
    after: ReplayAccount,
    prior: dict[str, Decimal],
    result: ReplaySessionResult,
) -> dict[str, Decimal]:
    observed = dict(prior)
    order_results = getattr(result, "orders", None)
    if not isinstance(order_results, tuple):
        raise ExecutionReplayError("execution result orders are unavailable")
    running_shares = dict(before.positions)
    for item in order_results:
        side = getattr(item, "side", None)
        symbol = getattr(item, "symbol", None)
        filled = getattr(item, "filled_shares", None)
        price = getattr(item, "fill_price", None)
        commission = getattr(item, "commission", None)
        transfer = getattr(item, "transfer_fee", None)
        if not isinstance(symbol, str) or isinstance(filled, bool) or not isinstance(filled, int):
            raise ExecutionReplayError("execution result is malformed")
        if filled <= 0:
            continue
        if side is ReplaySide.SELL:
            remaining = running_shares.get(symbol, 0) - filled
            running_shares[symbol] = max(remaining, 0)
            if remaining <= 0:
                observed.pop(symbol, None)
            continue
        if side is not ReplaySide.BUY or not isinstance(price, Decimal):
            raise ExecutionReplayError("execution fill economics are malformed")
        if not isinstance(commission, Decimal) or not isinstance(transfer, Decimal):
            raise ExecutionReplayError("execution fill fees are malformed")
        old_shares = running_shares.get(symbol, 0)
        old_cost = observed.get(symbol, price)
        added_cost = price * Decimal(filled) + commission + transfer
        new_shares = old_shares + filled
        observed[symbol] = (old_cost * Decimal(old_shares) + added_cost) / Decimal(new_shares)
        running_shares[symbol] = new_shares
    if set(observed) != set(after.positions):
        raise ExecutionReplayError("average-cost state differs from replay positions")
    return observed


def _broker_execution_facts(
    plan: ExecutionPlan,
    result: ReplaySessionResult | None,
    *,
    session: date,
) -> tuple[tuple[BrokerOrderFact, ...], tuple[BrokerFillFact, ...]]:
    result_orders = () if result is None else getattr(result, "orders", ())
    if not isinstance(result_orders, tuple):
        raise ExecutionReplayError("execution result orders are unavailable")
    by_key = {(item.symbol, item.side.value): item for item in result_orders}
    orders: list[BrokerOrderFact] = []
    fills: list[BrokerFillFact] = []
    sequence = 0
    for planned in plan.orders:
        sequence += 1
        key = (planned.symbol.canonical, planned.side.value)
        observed = by_key.get(key)
        if observed is None:
            raise ExecutionReplayError("planned execution result is missing")
        filled = int(observed.filled_shares)
        requested = planned.authorized_shares.value
        broker_order_id = (
            "replay-order:"
            + hashlib.sha256(f"{session.isoformat()} {planned.uquant_order_id}".encode()).hexdigest()
        )
        status = BrokerOrderStatus.FILLED if filled == requested else BrokerOrderStatus.CANCELLED
        order_payload = {
            "schema": "firmquant.execution-replay-order.v1",
            "broker_order_id": broker_order_id,
            "client_order_id": planned.uquant_order_id,
            "symbol": planned.symbol.canonical,
            "side": planned.side.value,
            "requested_shares": requested,
            "filled_shares": filled,
            "status": status.value,
            "session": session.isoformat(),
        }
        order_hash = canonical_sha256(order_payload)
        orders.append(
            BrokerOrderFact(
                broker_order_id=broker_order_id,
                client_order_id=planned.uquant_order_id,
                symbol=planned.symbol,
                side=planned.side,
                price_type=PriceType.LIMIT,
                status=status,
                requested_shares=Shares(requested),
                filled_shares=Shares(filled),
                limit_price=planned.limit_price,
                session_date=session,
                event_time=_timestamp(session, time(14, 55)),
                received_at=_timestamp(session, time(14, 55)),
                event_sequence=sequence,
                raw_payload_sha256=order_hash,
            )
        )
        if filled > 0:
            fill_price = observed.fill_price
            if not isinstance(fill_price, Decimal):
                raise ExecutionReplayError("filled replay order has no price")
            sequence += 1
            fill_id = (
                "replay-fill:"
                + hashlib.sha256(
                    f"{session.isoformat()} {planned.uquant_order_id} {filled} {fill_price}".encode()
                ).hexdigest()
            )
            fill_payload = {
                "schema": "firmquant.execution-replay-fill.v1",
                "fill_id": fill_id,
                "broker_order_id": broker_order_id,
                "symbol": planned.symbol.canonical,
                "side": planned.side.value,
                "shares": filled,
                "price": _decimal_text(fill_price),
                "session": session.isoformat(),
            }
            fills.append(
                BrokerFillFact(
                    broker_fill_id=fill_id,
                    broker_order_id=broker_order_id,
                    symbol=planned.symbol,
                    side=planned.side,
                    status=FillStatus.CONFIRMED,
                    shares=Shares(filled),
                    price=Price(fill_price),
                    commission=Money(observed.commission),
                    stamp_duty=Money(observed.stamp_duty),
                    transfer_fee=Money(observed.transfer_fee),
                    session_date=session,
                    event_time=_timestamp(session, time(14, 55)),
                    received_at=_timestamp(session, time(14, 55)),
                    event_sequence=sequence,
                    raw_payload_sha256=canonical_sha256(fill_payload),
                )
            )
    return tuple(orders), tuple(fills)


def _max_drawdown(equity: list[Decimal]) -> Decimal:
    peak = _ZERO
    maximum = _ZERO
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            maximum = max(maximum, _ONE - value / peak)
    return maximum


def run_execution_replay(
    *,
    source_checkout: Path,
    data_root: Path,
    start: date,
    end: date,
    max_price_deviation_bps: Decimal,
) -> ReplaySummary:
    """Run a deterministic causal execution replay without any broker write surface."""

    if type(start) is not date or type(end) is not date or start >= end:
        raise ValueError("execution replay date range is invalid")
    source_checkout = Path(source_checkout).resolve()
    data_root = Path(data_root).resolve()
    identity = StrategyIdentity.locked()
    identity.verify()
    source = load_locked_source_identity()
    verify_uquant_source_checkout(source, source_checkout)
    firmquant_commit = current_clean_firmquant_commit()
    manifest_sha = _sha256_file(data_root / "DATA_MANIFEST.json")
    policy = UniversePolicy.from_uquant(None, as_of=end)
    symbols = policy.deployment_symbols
    panels = _load_panels(data_root, symbols)
    sessions = _sessions(panels, start, end)
    actual_start, actual_end = sessions[0], sessions[-1]

    theoretical_engine = _engine(source_checkout, data_root)
    theoretical = theoretical_engine.backtest(
        symbols=symbols,
        start=actual_start.isoformat(),
        end=actual_end.isoformat(),
        initial_cash=None,
    )
    theoretical_return = Decimal(str(theoretical["total_return"]))

    engine = _engine(source_checkout, data_root)
    initial_cash = Decimal(str(engine.cfg.initial_cash))
    strategy_account = _account_state(float(initial_cash))
    replay_account = ReplayAccount(cash=initial_cash, positions={}, sellable={})
    average_costs: dict[str, Decimal] = {}
    planner = ExecutionPlanner()
    costs = _replay_costs(engine.cfg, max_price_deviation_bps=max_price_deviation_bps)
    pending: DecisionSnapshot | None = None
    equity: list[Decimal] = []
    commissions = stamp = transfer = slippage = unfilled_loss = turnover = _ZERO
    planned_orders = filled_orders = unfilled_orders = partials = 0
    price_limit_blocks = suspension_blocks = incomplete_sell_blocks = 0
    tracking_errors: list[Decimal] = []
    weighted_tracking = _ZERO
    tracking_notional = _ZERO

    for session in sessions:
        broker_orders: tuple[BrokerOrderFact, ...] = ()
        broker_fills: tuple[BrokerFillFact, ...] = ()
        if pending is not None:
            replay_account = replay_account.roll_session()
            facts, execution_bars = _execution_facts(
                replay_account,
                average_costs,
                _plan_symbols(pending),
                panels,
                session=session,
            )
            plan = planner.plan(pending, facts)
            target_equity = facts.broker_snapshot.account.total_assets.value
            before_execution = replay_account
            execution_result: ReplaySessionResult | None = None
            orders = _replay_orders(
                plan,
                replay_account,
                Decimal(str(engine.cfg.max_volume_participation)),
            )
            if orders:
                result = execute_session(replay_account, orders, execution_bars, costs)
                execution_result = result
                replay_account = result.ending_account
                average_costs = _updated_average_costs(
                    before_execution, replay_account, average_costs, result
                )
                commissions += result.commissions
                stamp += result.stamp_duty
                transfer += result.transfer_fees
                slippage += result.slippage_cost
                unfilled_loss += result.unfilled_loss
                turnover += result.turnover_notional
                planned_orders += len(result.orders)
                filled_orders += sum(item.filled_shares > 0 for item in result.orders)
                unfilled_orders += sum(item.filled_shares < item.requested_shares for item in result.orders)
                partials += result.partial_fill_count
                price_limit_blocks += result.price_limit_blocks
                suspension_blocks += result.suspension_blocks
                incomplete_sell_blocks += result.incomplete_sell_blocked_buys
            broker_orders, broker_fills = _broker_execution_facts(plan, execution_result, session=session)
            current_bars = {
                symbol: _daily_bar(symbol, panels[symbol], session)
                for symbol in sorted(set(replay_account.positions) | set(_plan_symbols(pending)))
                if symbol in panels
            }
            errors, weighted, notion = _tracking(
                pending, replay_account, current_bars, target_equity=target_equity
            )
            tracking_errors.extend(errors)
            weighted_tracking += weighted
            tracking_notional += notion
        else:
            current_bars = {}

        mark_symbols = tuple(sorted(set(replay_account.positions) | set(symbols)))
        mark_bars = {
            symbol: _daily_bar(symbol, panels[symbol], session)
            for symbol in mark_symbols
            if symbol in panels and _previous_close(panels[symbol], session) is not None
        }
        held_missing = set(replay_account.positions) - set(mark_bars)
        if held_missing:
            raise ExecutionReplayError(f"cannot mark held symbol: {sorted(held_missing)[0]}")
        close_snapshot = _snapshot(
            replay_account,
            mark_bars,
            average_costs=average_costs,
            session=session,
            captured_at=_timestamp(session, time(15, 5)),
            field="close",
            orders=broker_orders,
            fills=broker_fills,
        )
        sync_account(strategy_account, close_snapshot)
        close_equity = _market_value(replay_account, mark_bars, field="close")
        equity.append(close_equity)
        before = _account_sha256(strategy_account)
        decision = engine.decide(symbols=symbols, as_of=session.isoformat(), account=strategy_account)
        after = _account_sha256(strategy_account)
        data_hash = getattr(strategy_account, "data_hash", None)
        if not isinstance(data_hash, str) or len(data_hash) != 64:
            raise ExecutionReplayError("uquant decision did not bind a data identity")
        pending = _decision_snapshot(
            decision=decision,
            session=session,
            firmquant_commit=firmquant_commit,
            identity=identity,
            data_sha256=data_hash,
            broker_sha256=close_snapshot.raw_payload_sha256,
            before_sha256=before,
            after_sha256=after,
        )

    final_equity = equity[-1]
    execution_return = final_equity / initial_cash - _ONE
    maximum_drawdown = _max_drawdown(equity)
    max_tracking = max(tracking_errors, default=_ZERO)
    mean_tracking = (
        _ZERO if not tracking_errors else sum(tracking_errors, start=_ZERO) / Decimal(len(tracking_errors))
    )
    notional_tracking = _ZERO if tracking_notional == 0 else weighted_tracking / tracking_notional
    return ReplaySummary(
        theoretical_uquant_cumulative_return=theoretical_return,
        firmquant_execution_aware_cumulative_return=execution_return,
        return_gap=execution_return - theoretical_return,
        maximum_drawdown=maximum_drawdown,
        turnover_notional=turnover,
        turnover_ratio=turnover / initial_cash,
        commissions=commissions,
        stamp_duty=stamp,
        transfer_fee=transfer,
        slippage_cost=slippage,
        unfilled_loss=unfilled_loss,
        max_target_tracking_error=max_tracking,
        mean_target_tracking_error=mean_tracking,
        notional_weighted_target_tracking_error=notional_tracking,
        planned_orders=planned_orders,
        filled_orders=filled_orders,
        unfilled_orders=unfilled_orders,
        partial_fill_count=partials,
        price_limit_blocks=price_limit_blocks,
        suspension_blocks=suspension_blocks,
        incomplete_sell_blocked_buys=incomplete_sell_blocks,
        firmquant_commit=firmquant_commit,
        uquant_commit=identity.uquant_commit,
        uquant_config_sha256=identity.config_fingerprint,
        universe_sha256=identity.canonical_universe_sha256,
        frozen_data_manifest_sha256=manifest_sha,
        input_start=actual_start,
        input_end=actual_end,
    )


__all__ = ("ExecutionReplayError", "ReplaySummary", "run_execution_replay")
