from __future__ import annotations

from datetime import date
from decimal import Decimal

from firmquant.execution.replay_metrics import (
    ExecutionReplayPoint,
    summarize_execution_replay,
)


def test_execution_aware_replay_reports_return_drawdown_turnover_slippage_and_tracking() -> None:
    points = (
        ExecutionReplayPoint(
            session=date(2026, 1, 2),
            equity=Decimal("100"),
            turnover_notional=Decimal("10"),
            slippage_cost=Decimal("0.10"),
            unfilled_loss=Decimal("0.20"),
            target_gross=Decimal("0.80"),
            actual_gross=Decimal("0.75"),
        ),
        ExecutionReplayPoint(
            session=date(2026, 1, 5),
            equity=Decimal("90"),
            turnover_notional=Decimal("20"),
            slippage_cost=Decimal("0.20"),
            unfilled_loss=Decimal("0.10"),
            target_gross=Decimal("0.70"),
            actual_gross=Decimal("0.60"),
        ),
        ExecutionReplayPoint(
            session=date(2026, 1, 6),
            equity=Decimal("120"),
            turnover_notional=Decimal("15"),
            slippage_cost=Decimal("0.15"),
            unfilled_loss=Decimal("0.05"),
            target_gross=Decimal("0.90"),
            actual_gross=Decimal("0.88"),
        ),
    )

    summary = summarize_execution_replay(points)

    assert summary.cumulative_return == Decimal("0.20")
    assert summary.max_drawdown == Decimal("0.10")
    assert summary.turnover_notional == Decimal("45")
    assert summary.slippage_cost == Decimal("0.45")
    assert summary.unfilled_loss == Decimal("0.35")
    assert summary.max_target_tracking_error == Decimal("0.10")
    assert summary.mean_target_tracking_error == Decimal("0.05666666666666666666666666667")
