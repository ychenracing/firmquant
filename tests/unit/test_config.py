from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from firmquant.config import (
    BrokerAdapter,
    BrokerSettings,
    ComplianceSettings,
    DeploymentCaps,
    Mode,
    Settings,
    load_settings,
)


def test_defaults_cannot_trade_live() -> None:
    settings = Settings()

    assert settings.mode is Mode.PAPER
    assert settings.live_trading_enabled is False
    assert settings.broker.adapter is BrokerAdapter.PAPER


def test_canary_requires_all_deployment_caps() -> None:
    with pytest.raises(ValidationError, match="CANARY deployment caps"):
        Settings(
            mode="CANARY",
            live_trading_enabled=True,
            broker={"adapter": "XTQUANT"},
            compliance={
                "program_trading_report_confirmed": True,
                "broker_api_authorized": True,
            },
        )


@pytest.mark.parametrize("mode", [Mode.REPLAY, Mode.PAPER, Mode.SHADOW])
def test_read_only_modes_reject_live_trading_enabled(mode: Mode) -> None:
    adapter = {
        Mode.REPLAY: BrokerAdapter.RECORDED_REPLAY,
        Mode.PAPER: BrokerAdapter.PAPER,
        Mode.SHADOW: BrokerAdapter.XTQUANT,
    }[mode]

    with pytest.raises(ValidationError, match="cannot enable live trading"):
        Settings(mode=mode, live_trading_enabled=True, broker=BrokerSettings(adapter=adapter))


def test_complete_canary_settings_are_explicit_and_frozen() -> None:
    settings = Settings(
        mode=Mode.CANARY,
        live_trading_enabled=True,
        broker=BrokerSettings(adapter=BrokerAdapter.XTQUANT),
        compliance=ComplianceSettings(
            program_trading_report_confirmed=True,
            broker_api_authorized=True,
        ),
        canary_caps=DeploymentCaps(
            max_order_notional=Decimal("10000"),
            max_daily_submitted_notional=Decimal("30000"),
            max_daily_filled_notional=Decimal("30000"),
            max_symbol_notional=Decimal("20000"),
            max_total_gross_notional=Decimal("50000"),
        ),
    )

    assert settings.mode is Mode.CANARY
    with pytest.raises(ValidationError, match="frozen"):
        settings.live_trading_enabled = False


def test_safe_repr_redacts_account_and_local_paths() -> None:
    settings = Settings(
        broker=BrokerSettings(
            adapter=BrokerAdapter.PAPER,
            account_alias="sensitive-account",
            xtquant_userdata_path=Path("C:/sensitive/userdata"),
        )
    )

    rendered = settings.safe_repr()

    assert "sensitive-account" not in rendered
    assert "C:/sensitive/userdata" not in rendered
    assert '"mode":"PAPER"' in rendered
    assert "sensitive-account" not in repr(settings)
    assert "C:/sensitive/userdata" not in str(settings)


def test_example_configuration_is_strict_and_safe() -> None:
    repository_root = Path(__file__).resolve().parents[2]

    settings = load_settings(repository_root / "config/firmquant.example.toml")

    assert settings == Settings()


def test_unknown_configuration_field_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "firmquant.toml"
    path.write_text('mode = "PAPER"\nunknown_switch = true\n', encoding="utf-8")

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        load_settings(path)
