from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "src/firmquant/execution/replay_runner.py"
WORKFLOW = ROOT / ".github/workflows/replay-account-sync-repair.yml"


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")
    return text.replace(old, new, 1)


def replace_between(text: str, start: str, end: str, new: str, *, label: str) -> str:
    start_index = text.find(start)
    if start_index < 0:
        raise RuntimeError(f"{label}: start marker missing")
    end_index = text.find(end, start_index)
    if end_index < 0:
        raise RuntimeError(f"{label}: end marker missing")
    return text[:start_index] + new + text[end_index:]


text = TARGET.read_text(encoding="utf-8")
text = replace_once(
    text,
    "    BrokerAccountFact,\n    BrokerPositionFact,\n    BrokerSnapshot,\n",
    "    BrokerAccountFact,\n    BrokerFillFact,\n    BrokerOrderFact,\n    BrokerOrderStatus,\n    BrokerPositionFact,\n    BrokerSnapshot,\n    FillStatus,\n",
    label="broker fact imports",
)
text = replace_once(
    text,
    "    MarketSessionStatus,\n    QuoteFact,\n",
    "    MarketSessionStatus,\n    PriceType,\n    QuoteFact,\n",
    label="price type import",
)

plan_symbols_start = "def _plan_symbols(snapshot: DecisionSnapshot) -> tuple[str, ...]:\n"
plan_symbols_end = "\n\ndef _replay_costs"
plan_symbols = '''def _plan_symbols(snapshot: DecisionSnapshot) -> tuple[str, ...]:
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
'''
text = replace_between(text, plan_symbols_start, plan_symbols_end, plan_symbols, label="decision symbols")

text = replace_once(
    text,
    "def _replay_costs(config: Any) -> ReplayCosts:\n",
    "def _replay_costs(config: Any, *, max_price_deviation_bps: Decimal) -> ReplayCosts:\n",
    label="replay costs signature",
)
text = replace_once(
    text,
    "        max_price_deviation_bps=Decimal(\"200\"),\n",
    "        max_price_deviation_bps=max_price_deviation_bps,\n",
    label="configured price deviation",
)

snapshot_start = "def _snapshot(\n"
snapshot_end = "\n\ndef _execution_facts"
snapshot = '''def _snapshot(
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
'''
text = replace_between(text, snapshot_start, snapshot_end, snapshot, label="snapshot with broker economics")

text = replace_once(
    text,
    "def _execution_facts(\n    account: ReplayAccount,\n    plan_symbols: tuple[str, ...],\n",
    "def _execution_facts(\n    account: ReplayAccount,\n    average_costs: dict[str, Decimal],\n    plan_symbols: tuple[str, ...],\n",
    label="execution facts signature",
)
text = replace_once(
    text,
    "        account,\n        bars,\n        session=session,\n",
    "        account,\n        bars,\n        average_costs=average_costs,\n        session=session,\n",
    label="execution facts snapshot average costs",
)

tracking_start = "def _tracking(\n"
tracking_end = "\n\ndef _max_drawdown"
tracking = '''def _tracking(
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
        raw_target_shares = int((target_equity * weight / bar.open).to_integral_value(rounding="ROUND_FLOOR"))
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
    result: object,
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
    result: object | None,
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
        broker_order_id = "replay-order:" + hashlib.sha256(
            f"{session.isoformat()}\0{planned.uquant_order_id}".encode()
        ).hexdigest()
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
            fill_id = "replay-fill:" + hashlib.sha256(
                f"{session.isoformat()}\0{planned.uquant_order_id}\0{filled}\0{fill_price}".encode()
            ).hexdigest()
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
    for blocker in plan.blockers:
        sequence += 1
        broker_order_id = "replay-order:" + hashlib.sha256(
            f"{session.isoformat()}\0{blocker.uquant_order_id}".encode()
        ).hexdigest()
        order_payload = {
            "schema": "firmquant.execution-replay-order.v1",
            "broker_order_id": broker_order_id,
            "client_order_id": blocker.uquant_order_id,
            "symbol": blocker.symbol.canonical,
            "side": blocker.side.value,
            "requested_shares": blocker.requested_shares.value,
            "filled_shares": 0,
            "status": BrokerOrderStatus.CANCELLED.value,
            "session": session.isoformat(),
            "reason": blocker.reason_code,
        }
        orders.append(
            BrokerOrderFact(
                broker_order_id=broker_order_id,
                client_order_id=blocker.uquant_order_id,
                symbol=blocker.symbol,
                side=blocker.side,
                price_type=PriceType.LIMIT,
                status=BrokerOrderStatus.CANCELLED,
                requested_shares=blocker.requested_shares,
                filled_shares=Shares(0),
                limit_price=blocker.reference_price,
                session_date=session,
                event_time=_timestamp(session, time(14, 55)),
                received_at=_timestamp(session, time(14, 55)),
                event_sequence=sequence,
                raw_payload_sha256=canonical_sha256(order_payload),
            )
        )
    return tuple(orders), tuple(fills)
'''
text = replace_between(text, tracking_start, tracking_end, tracking, label="tracking and broker sync")

text = replace_once(
    text,
    "    end: date,\n) -> ReplaySummary:\n",
    "    end: date,\n    max_price_deviation_bps: Decimal,\n) -> ReplaySummary:\n",
    label="runner configured deviation signature",
)
text = replace_once(
    text,
    "    costs = _replay_costs(engine.cfg)\n",
    "    costs = _replay_costs(engine.cfg, max_price_deviation_bps=max_price_deviation_bps)\n",
    label="runner configured deviation call",
)
text = replace_once(
    text,
    "    replay_account = ReplayAccount(cash=initial_cash, positions={}, sellable={})\n",
    "    replay_account = ReplayAccount(cash=initial_cash, positions={}, sellable={})\n    average_costs: dict[str, Decimal] = {}\n",
    label="runner average-cost state",
)
text = replace_once(
    text,
    "    for session in sessions:\n        if pending is not None:\n",
    "    for session in sessions:\n        broker_orders: tuple[BrokerOrderFact, ...] = ()\n        broker_fills: tuple[BrokerFillFact, ...] = ()\n        if pending is not None:\n",
    label="runner broker facts state",
)
text = replace_once(
    text,
    "            facts, execution_bars = _execution_facts(\n                replay_account,\n                _plan_symbols(pending),\n",
    "            facts, execution_bars = _execution_facts(\n                replay_account,\n                average_costs,\n                _plan_symbols(pending),\n",
    label="runner execution facts average costs",
)
text = replace_once(
    text,
    "            plan = planner.plan(pending, facts)\n            orders = _replay_orders(\n",
    "            plan = planner.plan(pending, facts)\n            target_equity = facts.broker_snapshot.account.total_assets.value\n            before_execution = replay_account\n            execution_result: object | None = None\n            orders = _replay_orders(\n",
    label="runner execution prestate",
)
text = replace_once(
    text,
    "                result = execute_session(replay_account, orders, execution_bars, costs)\n                replay_account = result.ending_account\n",
    "                result = execute_session(replay_account, orders, execution_bars, costs)\n                execution_result = result\n                replay_account = result.ending_account\n                average_costs = _updated_average_costs(\n                    before_execution, replay_account, average_costs, result\n                )\n",
    label="runner average-cost update",
)
text = replace_once(
    text,
    "                incomplete_sell_blocks += result.incomplete_sell_blocked_buys\n            current_bars = {\n",
    "                incomplete_sell_blocks += result.incomplete_sell_blocked_buys\n            broker_orders, broker_fills = _broker_execution_facts(\n                plan, execution_result, session=session\n            )\n            current_bars = {\n",
    label="runner broker fact generation",
)
text = replace_once(
    text,
    "            errors, weighted, notion = _tracking(plan, replay_account, current_bars)\n",
    "            errors, weighted, notion = _tracking(\n                pending, replay_account, current_bars, target_equity=target_equity\n            )\n",
    label="runner complete target tracking",
)
text = replace_once(
    text,
    "            replay_account,\n            mark_bars,\n            session=session,\n",
    "            replay_account,\n            mark_bars,\n            average_costs=average_costs,\n            session=session,\n",
    label="runner close snapshot average costs",
)
text = replace_once(
    text,
    "            field=\"close\",\n        )\n",
    "            field=\"close\",\n            orders=broker_orders,\n            fills=broker_fills,\n        )\n",
    label="runner close broker facts",
)

TARGET.write_text(text, encoding="utf-8")
Path(__file__).unlink()
WORKFLOW.unlink()
