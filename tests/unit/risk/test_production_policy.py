from __future__ import annotations

from decimal import Decimal

import pytest

from firmquant.config import ConfigurationError, Settings


def _assign(payload: dict[str, object], path: str, value: object) -> None:
    section, field = path.split(".", maxsplit=1)
    nested = payload.setdefault(section, {})
    assert isinstance(nested, dict)
    nested[field] = value


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("execution.max_quote_age_seconds", 6),
        ("execution.max_clock_drift_seconds", 3),
        ("execution.max_disconnect_seconds", 31),
        ("execution.max_price_deviation_bps", "201"),
        ("execution.max_equity_change_fraction", "0.10000001"),
        ("execution.max_intraday_loss_fraction", "0.08000001"),
        ("execution.max_capital_drawdown_fraction", "0.25000001"),
        ("execution.max_arm_ttl_seconds", 901),
        ("promotion.min_shadow_sessions", 19),
        ("promotion.min_shadow_orders", 49),
        ("promotion.max_target_tracking_error", "0.05000001"),
        ("promotion.min_canary_sessions", 2),
        ("promotion.min_canary_orders", 2),
        ("promotion.min_canary_fills", 0),
        ("promotion.max_canary_target_tracking_error", "0.05000001"),
    ],
)
def test_settings_reject_every_production_policy_relaxation(path: str, value: object) -> None:
    payload: dict[str, object] = {}
    _assign(payload, path, value)

    with pytest.raises(ConfigurationError, match="PRODUCTION_SAFETY_POLICY"):
        Settings.model_validate(payload)


def test_production_policy_accepts_stricter_configuration_and_has_canonical_identity() -> None:
    from firmquant.risk.production_policy import ProductionSafetyPolicy

    settings = Settings.model_validate(
        {
            "execution": {
                "max_quote_age_seconds": 4,
                "max_clock_drift_seconds": 1,
                "max_disconnect_seconds": 20,
                "max_price_deviation_bps": "150",
                "max_equity_change_fraction": "0.09",
                "max_intraday_loss_fraction": "0.07",
                "max_capital_drawdown_fraction": "0.20",
                "max_arm_ttl_seconds": 600,
            },
            "promotion": {
                "min_shadow_sessions": 21,
                "min_shadow_orders": 51,
                "max_target_tracking_error": "0.04",
                "min_canary_sessions": 4,
                "min_canary_orders": 4,
                "min_canary_fills": 2,
                "max_canary_target_tracking_error": "0.04",
            },
        }
    )

    policy = ProductionSafetyPolicy.from_settings(settings)

    assert policy.max_quote_age_seconds == 4
    assert policy.max_price_deviation_bps == Decimal("150")
    assert policy.min_shadow_sessions == 21
    assert policy.max_arm_ttl_seconds == 600
    assert policy.payload()["schema"] == "firmquant.production-safety-policy.v1"
    assert len(policy.sha256) == 64
