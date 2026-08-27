"""Build live risk limits exclusively from deployment safety configuration."""

from __future__ import annotations

from datetime import timedelta

from firmquant.config import Settings
from firmquant.domain.values import Money

from .gate import RiskLimits


def risk_limits_from_settings(settings: Settings) -> RiskLimits:
    """Translate configured operational limits without introducing strategy economics."""

    if not isinstance(settings, Settings):
        raise TypeError("risk settings must be Settings")
    caps = settings.active_deployment_caps
    if caps is None:
        raise ValueError("real-money risk limits require configured deployment caps")
    runtime = settings.execution
    return RiskLimits(
        max_order_notional=Money(caps.max_order_notional),
        max_daily_submitted_notional=Money(caps.max_daily_submitted_notional),
        max_daily_filled_notional=Money(caps.max_daily_filled_notional),
        max_symbol_notional=Money(caps.max_symbol_notional),
        max_total_gross_notional=Money(caps.max_total_gross_notional),
        max_open_orders=runtime.max_open_orders,
        max_consecutive_rejections=runtime.max_consecutive_rejections,
        max_disconnect_duration=timedelta(seconds=runtime.max_disconnect_seconds),
        max_order_lifetime=timedelta(seconds=runtime.max_order_lifetime_seconds),
        max_submit_count_window=runtime.max_submit_count_window,
        max_cancel_count_window=runtime.max_cancel_count_window,
        max_quote_age=timedelta(seconds=runtime.max_quote_age_seconds),
        max_clock_drift=timedelta(seconds=runtime.max_clock_drift_seconds),
        max_price_deviation_bps=runtime.max_price_deviation_bps,
        max_equity_change_fraction=runtime.max_equity_change_fraction,
        max_intraday_loss_fraction=runtime.max_intraday_loss_fraction,
        max_capital_drawdown_fraction=runtime.max_capital_drawdown_fraction,
    )


__all__ = ("risk_limits_from_settings",)
