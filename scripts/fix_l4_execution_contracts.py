from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPLAY_MODEL = ROOT / "src/firmquant/execution/execution_replay.py"
REPLAY_RUNNER = ROOT / "src/firmquant/execution/replay_runner.py"
CLI_TEST = ROOT / "tests/integration/test_cli_operations.py"
REPLAY_TEST = ROOT / "tests/unit/execution/test_execution_replay.py"


def replace_once(path: Path, old: str, new: str, *, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    REPLAY_MODEL,
    "from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal\n",
    "from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal\n",
    label="directional tick rounding imports",
)
replace_once(
    REPLAY_MODEL,
    '_FEE_QUANTUM = Decimal("0.0001")\n_LOT_SIZE = 100\n',
    '_FEE_QUANTUM = Decimal("0.0001")\n_PRICE_TICK = Decimal("0.01")\n_LOT_SIZE = 100\n',
    label="price tick constant",
)
replace_once(
    REPLAY_MODEL,
    '''def _candidate_fill_price(order: ReplayOrder, bar: DailyBar, costs: ReplayCosts) -> Decimal:\n    slippage = costs.slippage_bps / _BPS\n    if order.side is ReplaySide.BUY:\n        nominal = bar.open if order.limit_price >= bar.open else order.limit_price\n        return min(order.limit_price, nominal * (_ONE + slippage))\n    nominal = bar.open if order.limit_price <= bar.open else order.limit_price\n    return max(order.limit_price, nominal * (_ONE - slippage))\n''',
    '''def _candidate_fill_price(order: ReplayOrder, bar: DailyBar, costs: ReplayCosts) -> Decimal:\n    slippage = costs.slippage_bps / _BPS\n    if order.side is ReplaySide.BUY:\n        nominal = bar.open if order.limit_price >= bar.open else order.limit_price\n        raw = nominal * (_ONE + slippage)\n        ticked = raw.quantize(_PRICE_TICK, rounding=ROUND_CEILING)\n        return min(order.limit_price, ticked)\n    nominal = bar.open if order.limit_price <= bar.open else order.limit_price\n    raw = nominal * (_ONE - slippage)\n    ticked = raw.quantize(_PRICE_TICK, rounding=ROUND_FLOOR)\n    return max(order.limit_price, ticked)\n''',
    label="directional tick fill price",
)

replace_once(
    REPLAY_RUNNER,
    '_ZERO = Decimal(0)\n_ONE = Decimal(1)\n',
    '_ZERO = Decimal(0)\n_ONE = Decimal(1)\n_AVERAGE_COST_QUANTUM = Decimal("0.00000001")\n',
    label="average cost quantum",
)
replace_once(
    REPLAY_RUNNER,
    '''        observed[symbol] = (old_cost * Decimal(old_shares) + added_cost) / Decimal(new_shares)\n        running_shares[symbol] = new_shares\n''',
    '''        raw_average_cost = (old_cost * Decimal(old_shares) + added_cost) / Decimal(new_shares)\n        observed[symbol] = raw_average_cost.quantize(_AVERAGE_COST_QUANTUM, rounding=ROUND_HALF_UP)\n        running_shares[symbol] = new_shares\n''',
    label="average cost broker precision",
)

replace_once(
    REPLAY_TEST,
    '''def test_fees_slippage_unfilled_and_determinism_are_stable() -> None:\n''',
    '''def test_fill_price_respects_a_share_tick_with_conservative_directional_rounding() -> None:\n    costs = ReplayCosts(\n        commission_rate=Decimal("0"),\n        minimum_commission=Decimal("0"),\n        sell_stamp_duty_rate=Decimal("0"),\n        transfer_fee_rate=Decimal("0"),\n        slippage_bps=Decimal("3"),\n    )\n    buy = execute_session(\n        ReplayAccount(cash=Decimal("100000"), positions={}, sellable={}),\n        (ReplayOrder("600000.SH", ReplaySide.BUY, 100, Decimal("10.10"), Decimal("1")),),\n        {"600000.SH": _bar("600000.SH")},\n        costs,\n    )\n    sell = execute_session(\n        ReplayAccount(\n            cash=Decimal("0"),\n            positions={"600000.SH": 100},\n            sellable={"600000.SH": 100},\n        ),\n        (ReplayOrder("600000.SH", ReplaySide.SELL, 100, Decimal("9.90"), Decimal("1")),),\n        {"600000.SH": _bar("600000.SH")},\n        costs,\n    )\n    assert buy.orders[0].fill_price == Decimal("10.01")\n    assert sell.orders[0].fill_price == Decimal("9.99")\n\n\ndef test_fees_slippage_unfilled_and_determinism_are_stable() -> None:\n''',
    label="tick rounding regression test",
)

replace_once(
    CLI_TEST,
    "from datetime import UTC, date, datetime\nfrom pathlib import Path\n",
    "from datetime import UTC, date, datetime\nfrom decimal import Decimal\nfrom pathlib import Path\n",
    label="Decimal test import",
)
replace_once(
    CLI_TEST,
    '''from firmquant.application.operations import (\n''',
    '''from firmquant.application.execution_evidence import (\n    BlockerCode,\n    EvidenceIdentity,\n    EvidenceStage,\n    ExecutionObservation,\n    OrderObservation,\n    PositionObservation,\n    TargetObservation,\n)\nfrom firmquant.application.operations import (\n''',
    label="execution evidence test imports",
)
replace_once(
    CLI_TEST,
    "from firmquant.config import Mode\n",
    "from firmquant.application.production_identity import promotion_config_sha256\nfrom firmquant.application.promotion_store import PromotionStore\nfrom firmquant.config import Mode, load_settings\n",
    label="promotion test imports",
)
replace_once(
    CLI_TEST,
    '''            AuditLedger(database).append(\n                audit_event_id="shadow-ready-proof",\n                category="RUNTIME",\n                actor="session-coordinator",\n                payload={\n                    "schema": "firmquant.runtime-transition.v1",\n                    "mode": "SHADOW",\n                    "state": "READY",\n                    "revision": 3,\n                    "reason": "shadow startup reconciliation passed",\n                    "blockers": (),\n                },\n                created_at=NOW,\n            )\n''',
    '''            AuditLedger(database).append(\n                audit_event_id="shadow-ready-proof",\n                category="RUNTIME",\n                actor="session-coordinator",\n                payload={\n                    "schema": "firmquant.runtime-transition.v1",\n                    "mode": "SHADOW",\n                    "state": "READY",\n                    "revision": 3,\n                    "reason": "shadow startup reconciliation passed",\n                    "blockers": (),\n                },\n                created_at=NOW,\n            )\n            settings = load_settings(config)\n            promotion_sha = promotion_config_sha256(settings)\n            uquant_commit = StrategyIdentity.locked().uquant_commit\n            target = TargetObservation(\n                symbol="600000.SH",\n                target_shares=100,\n                target_weight=Decimal("0.10"),\n                reference_price=Decimal("10"),\n            )\n            position = PositionObservation(symbol="600000.SH", shares=100)\n            promotion_store = PromotionStore(database)\n            for index in range(settings.promotion.min_shadow_sessions):\n                orders = tuple(\n                    OrderObservation(\n                        execution_id=f"shadow-seed-{index}-{order_index}",\n                        uquant_order_id=f"shadow-uq-{index}-{order_index}",\n                        symbol="600000.SH",\n                        side="BUY",\n                        planned_shares=100,\n                        filled_shares=0,\n                        reference_price=Decimal("10"),\n                        blocker=BlockerCode.TARGET_ALREADY_SATISFIED,\n                    )\n                    for order_index in range(3)\n                )\n                promotion_store.append(\n                    ExecutionObservation(\n                        identity=EvidenceIdentity(\n                            stage=EvidenceStage.SHADOW,\n                            execution_session=date(2026, 7, index + 1),\n                            firmquant_commit=FIRMQUANT_COMMIT,\n                            uquant_commit=uquant_commit,\n                            promotion_config_sha256=promotion_sha,\n                            account_sha256="a" * 64,\n                            data_sha256="d" * 64,\n                            calendar_sha256="e" * 64,\n                        ),\n                        decision_id=f"shadow-decision-{index}",\n                        plan_id=f"shadow-plan-{index}",\n                        portfolio_equity=Decimal("10000"),\n                        planned_orders=orders,\n                        planning_blockers=(),\n                        targets=(target,),\n                        fills=(),\n                        actual_ending_positions=(position,),\n                        hypothetical_ending_positions=(position,),\n                        submit_count=0,\n                        cancel_count=0,\n                        rejection_count=0,\n                        unknown_count=0,\n                        external_activity=0,\n                        duplicate_economic_orders=0,\n                        duplicate_fills=0,\n                        data_quality_failures=0,\n                        created_at=NOW,\n                    )\n                )\n''',
    label="immutable shadow readiness seed",
)
