from __future__ import annotations

from decimal import Decimal

from firmquant.application.production_identity import promotion_config_sha256
from firmquant.config import (
    BrokerAdapter,
    BrokerSettings,
    ComplianceSettings,
    DeploymentCaps,
    Mode,
    Settings,
)


def caps(scale: str) -> DeploymentCaps:
    value = Decimal(scale)
    return DeploymentCaps(
        max_order_notional=value,
        max_daily_submitted_notional=value * 3,
        max_daily_filled_notional=value * 3,
        max_symbol_notional=value * 2,
        max_total_gross_notional=value * 5,
    )


def test_promotion_identity_survives_mode_switch_but_tracks_execution_contract() -> None:
    broker = BrokerSettings(
        adapter=BrokerAdapter.XTQUANT,
        account_alias="account-001",
        session_id=123456,
    )
    shadow = Settings(mode=Mode.SHADOW, broker=broker)
    canary = Settings(
        mode=Mode.CANARY,
        live_trading_enabled=True,
        broker=broker,
        compliance=ComplianceSettings(
            program_trading_report_confirmed=True,
            broker_api_authorized=True,
        ),
        canary_caps=caps("10000"),
    )

    assert promotion_config_sha256(shadow) == promotion_config_sha256(canary)

    changed = canary.model_copy(
        update={
            "execution": canary.execution.model_copy(
                update={"buy_window_seconds": canary.execution.buy_window_seconds + 1}
            )
        }
    )
    assert promotion_config_sha256(changed) != promotion_config_sha256(canary)


def test_promotion_identity_ignores_only_risk_shrinking_nominal_caps() -> None:
    broker = BrokerSettings(adapter=BrokerAdapter.XTQUANT, account_alias="account-001", session_id=1)
    first = Settings(
        mode=Mode.CANARY,
        live_trading_enabled=True,
        broker=broker,
        compliance=ComplianceSettings(
            program_trading_report_confirmed=True,
            broker_api_authorized=True,
        ),
        canary_caps=caps("10000"),
    )
    second = first.model_copy(update={"canary_caps": caps("5000")})
    assert promotion_config_sha256(first) == promotion_config_sha256(second)
