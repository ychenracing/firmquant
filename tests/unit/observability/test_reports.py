from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from firmquant.domain.broker_facts import Side
from firmquant.observability.reports import (
    DailyReport,
    DailyReportRenderer,
    ExecutionFill,
    OrderLifecycle,
    TargetActualDifference,
)

NOW = datetime(2026, 8, 26, 8, tzinfo=UTC)


def report() -> DailyReport:
    return DailyReport(
        session=date(2026, 8, 26),
        strategy_session=date(2026, 8, 25),
        generated_at=NOW,
        decision_id="decision_" + "1" * 64,
        available_cash=Decimal("23000.1200"),
        total_assets=Decimal("100000.0000"),
        actual_gross=Decimal("0.7700"),
        target_gross=Decimal("0.8000"),
        target_actual_differences=(
            TargetActualDifference(
                symbol="300502.SZ",
                target_weight=Decimal("0.8000"),
                actual_weight=Decimal("0.7700"),
            ),
        ),
        orders=(
            OrderLifecycle(
                uquant_order_id="O-BUY-1",
                execution_id="execution-1",
                broker_order_id="broker-1",
                symbol="300502.SZ",
                side=Side.BUY,
                requested_shares=1000,
                filled_shares=700,
                state="REJECTED",
                reason_code="PRICE_LIMIT_BLOCKED",
            ),
        ),
        fills=(
            ExecutionFill(
                broker_fill_id="fill-1",
                broker_order_id="broker-1",
                symbol="300502.SZ",
                side=Side.BUY,
                shares=700,
                price=Decimal("10.0500"),
                commission=Decimal("5.0000"),
                stamp_duty=Decimal("0"),
                transfer_fee=Decimal("0.1400"),
                planned_price=Decimal("10.0000"),
                next_open_price=None,
            ),
        ),
        risk_events=("STALE_QUOTE",),
        reconciliation_passed=False,
        reconciliation_blockers=("POSITION_MISMATCH",),
        runtime_state="HALTED",
        health_blockers=("RECONCILIATION_MISMATCH",),
    )


def test_markdown_and_json_keep_rejections_differences_fees_and_missing_references() -> None:
    renderer = DailyReportRenderer()
    observed = report()

    payload = json.loads(renderer.render_json(observed))
    markdown = renderer.render_markdown(observed)

    assert payload["rejected_orders"][0]["reason_code"] == "PRICE_LIMIT_BLOCKED"
    assert payload["target_actual_differences"][0]["difference_weight"] == "-0.0300"
    assert payload["fills"][0]["total_fees"] == "5.1400"
    assert payload["fills"][0]["slippage_vs_plan_bps"] == "50.0000"
    assert payload["fills"][0]["next_open_price"] is None
    assert payload["fills"][0]["slippage_vs_next_open_bps"] is None
    assert payload["reconciliation"]["passed"] is False
    for required in (
        "PRICE_LIMIT_BLOCKED",
        "POSITION_MISMATCH",
        "300502.SZ",
        "缺少下一开盘价参考",
    ):
        assert required in markdown


def test_report_write_is_atomic_and_both_files_represent_the_same_report(
    tmp_path: Path,
) -> None:
    renderer = DailyReportRenderer()
    observed = report()

    receipt = renderer.write(observed, tmp_path)

    json_path = tmp_path / "2026-08-26.json"
    markdown_path = tmp_path / "2026-08-26.md"
    assert receipt.json_sha256
    assert receipt.markdown_sha256
    assert json.loads(json_path.read_text(encoding="utf-8"))["report_id"] == observed.report_id
    assert observed.report_id in markdown_path.read_text(encoding="utf-8")
    assert not tuple(tmp_path.glob("*.tmp"))
