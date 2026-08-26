from __future__ import annotations

from decimal import Decimal

import pytest

from firmquant.config import (
    BrokerAdapter,
    BrokerSettings,
    ComplianceSettings,
    DeploymentCaps,
    Mode,
    Settings,
)
from firmquant.domain.values import Money
from firmquant.risk.runtime import risk_limits_from_settings


def caps() -> DeploymentCaps:
    return DeploymentCaps(
        max_order_notional=Decimal("10000"),
        max_daily_submitted_notional=Decimal("30000"),
        max_daily_filled_notional=Decimal("30000"),
        max_symbol_notional=Decimal("20000"),
        max_total_gross_notional=Decimal("50000"),
    )


def test_live_risk_limits_are_derived_only_from_deployment_settings() -> None:
    settings = Settings(
        mode=Mode.LIVE,
        live_trading_enabled=True,
        broker=BrokerSettings(adapter=BrokerAdapter.XTQUANT),
        compliance=ComplianceSettings(
            program_trading_report_confirmed=True,
            broker_api_authorized=True,
        ),
        live_caps=caps(),
    )

    limits = risk_limits_from_settings(settings)

    assert limits.max_order_notional == Money(Decimal("10000"))
    assert limits.max_total_gross_notional == Money(Decimal("50000"))
    assert limits.max_open_orders == settings.execution.max_open_orders
    assert limits.max_quote_age.total_seconds() == settings.execution.max_quote_age_seconds
    assert limits.max_capital_drawdown_fraction == settings.execution.max_capital_drawdown_fraction


def test_non_real_mode_has_no_real_money_risk_caps() -> None:
    with pytest.raises(ValueError, match="deployment caps"):
        risk_limits_from_settings(Settings())
    with pytest.raises(TypeError, match="Settings"):
        risk_limits_from_settings(object())  # type: ignore[arg-type]
