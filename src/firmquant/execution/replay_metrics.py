"""Deterministic execution-aware replay metrics independent of strategy generation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


def _nonnegative(value: Decimal, *, label: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError(f"{label} must be a nonnegative finite Decimal")


def _fraction(value: Decimal, *, label: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or not Decimal(0) <= value <= Decimal(1):
        raise ValueError(f"{label} must be a Decimal between zero and one")


@dataclass(frozen=True, slots=True)
class ExecutionReplayPoint:
    session: date
    equity: Decimal
    turnover_notional: Decimal
    slippage_cost: Decimal
    unfilled_loss: Decimal
    target_gross: Decimal
    actual_gross: Decimal

    def __post_init__(self) -> None:
        if type(self.session) is not date:
            raise TypeError("replay session must be a calendar date")
        _nonnegative(self.equity, label="replay equity")
        if self.equity == 0:
            raise ValueError("replay equity must be positive")
        for label, value in (
            ("turnover notional", self.turnover_notional),
            ("slippage cost", self.slippage_cost),
            ("unfilled loss", self.unfilled_loss),
        ):
            _nonnegative(value, label=label)
        _fraction(self.target_gross, label="target gross")
        _fraction(self.actual_gross, label="actual gross")

    @property
    def target_tracking_error(self) -> Decimal:
        return abs(self.target_gross - self.actual_gross)


@dataclass(frozen=True, slots=True)
class ExecutionReplaySummary:
    cumulative_return: Decimal
    max_drawdown: Decimal
    turnover_notional: Decimal
    slippage_cost: Decimal
    unfilled_loss: Decimal
    max_target_tracking_error: Decimal
    mean_target_tracking_error: Decimal
    sessions: int


def summarize_execution_replay(
    points: tuple[ExecutionReplayPoint, ...],
) -> ExecutionReplaySummary:
    if not isinstance(points, tuple) or not points:
        raise ValueError("execution replay requires a non-empty point tuple")
    if not all(isinstance(point, ExecutionReplayPoint) for point in points):
        raise TypeError("execution replay points must be typed")
    sessions = tuple(point.session for point in points)
    if sessions != tuple(sorted(set(sessions))):
        raise ValueError("execution replay sessions must be sorted and unique")

    first = points[0].equity
    cumulative_return = points[-1].equity / first - Decimal(1)
    peak = points[0].equity
    max_drawdown = Decimal(0)
    for point in points:
        peak = max(peak, point.equity)
        drawdown = (peak - point.equity) / peak
        max_drawdown = max(max_drawdown, drawdown)

    tracking = tuple(point.target_tracking_error for point in points)
    return ExecutionReplaySummary(
        cumulative_return=cumulative_return,
        max_drawdown=max_drawdown,
        turnover_notional=sum(
            (point.turnover_notional for point in points),
            start=Decimal(0),
        ),
        slippage_cost=sum(
            (point.slippage_cost for point in points),
            start=Decimal(0),
        ),
        unfilled_loss=sum(
            (point.unfilled_loss for point in points),
            start=Decimal(0),
        ),
        max_target_tracking_error=max(tracking),
        mean_target_tracking_error=sum(tracking, start=Decimal(0)) / Decimal(len(tracking)),
        sessions=len(points),
    )


__all__ = (
    "ExecutionReplayPoint",
    "ExecutionReplaySummary",
    "summarize_execution_replay",
)
