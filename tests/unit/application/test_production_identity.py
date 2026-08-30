from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

import firmquant.application.production_identity as identity
from firmquant.application.execution_evidence import EvidenceIdentity, EvidenceStage
from firmquant.application.production_identity import promotion_config_sha256
from firmquant.config import (
    BrokerAdapter,
    BrokerSettings,
    ComplianceSettings,
    DeploymentCaps,
    Mode,
    Settings,
)
from firmquant.risk.production_policy import ProductionSafetyPolicy


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


def _deployment_identity() -> identity.DeploymentIdentity:
    return identity.DeploymentIdentity(
        firmquant_commit="a" * 40,
        uquant_commit="b" * 40,
        uquant_tree="c" * 40,
        uquant_package_manifest_sha256="1" * 64,
        uquant_code_fingerprint="2" * 64,
        uquant_config_fingerprint="3" * 64,
        semantic_config_sha256="4" * 64,
        raw_config_sha256="5" * 64,
        xtquant_safety_manifest_sha256="6" * 64,
        account_id_hash="7" * 64,
        account_authority_epoch=2,
        mode_epoch=3,
        mode=Mode.SHADOW,
        caps_sha256="8" * 64,
        production_policy_sha256="9" * 64,
    )


def _operational_identity(
    deployment: identity.DeploymentIdentity,
) -> identity.OperationalEvidenceIdentity:
    started = datetime(2026, 8, 30, 1, 2, 3, tzinfo=UTC)
    return identity.OperationalEvidenceIdentity(
        deployment_identity=deployment,
        account_state_sha256="d" * 64,
        broker_snapshot_id="snapshot-1",
        broker_snapshot_sha256="e" * 64,
        broker_event_watermark=10,
        snapshot_started_at=started,
        snapshot_completed_at=started + timedelta(seconds=2),
        snapshot_duration_ms=2000,
        calendar_sha256="f" * 64,
        active_data_generation_sha256="0" * 64,
        strategy_data_manifest_sha256="1" * 64,
        strategy_session=date(2026, 8, 30),
        decision_id=None,
        phase="PRE_DECISION",
        kind="READINESS",
    )


def test_deployment_identity_is_stable_across_account_state_changes() -> None:
    first = _operational_identity(_deployment_identity())
    second = replace(first, account_state_sha256="2" * 64)

    assert first.deployment_identity_sha256 == second.deployment_identity_sha256
    assert first.sha256 != second.sha256
    assert "account_state_sha256" not in first.deployment_identity.payload()
    assert first.payload()["deployment_identity_sha256"] == first.deployment_identity.sha256


def test_identity_json_round_trip_is_strictly_canonical_and_verifies_sha256() -> None:
    deployment = _deployment_identity()
    operational = _operational_identity(deployment)

    assert identity.parse_identity(deployment.canonical_json, expected_sha256=deployment.sha256) == deployment
    assert (
        identity.parse_identity(operational.canonical_json, expected_sha256=operational.sha256) == operational
    )

    reordered = json.dumps(deployment.payload(), separators=(",", ":"), sort_keys=False)
    assert reordered != deployment.canonical_json
    with pytest.raises(identity.IdentityError, match="canonical"):
        identity.parse_identity(reordered)
    with pytest.raises(identity.IdentityError, match="SHA-256"):
        identity.parse_identity(deployment.canonical_json, expected_sha256="0" * 64)


@pytest.mark.parametrize(
    "raw",
    [
        '{"schema":"x","schema":"y"}',
        '{"schema":"x","value":NaN}',
        '{"schema":"x","value":1.5}',
        '{"schema":"unknown"}',
    ],
)
def test_identity_rejects_ambiguous_or_unknown_json(raw: str) -> None:
    with pytest.raises(identity.IdentityError):
        identity.parse_identity(raw)


def test_identity_rejects_noncanonical_text_and_requires_positive_epochs() -> None:
    deployment = _deployment_identity()
    with pytest.raises(identity.IdentityError, match="canonical text"):
        replace(deployment, account_id_hash=" 7" + "7" * 62)
    with pytest.raises(identity.IdentityError, match="positive"):
        replace(deployment, account_authority_epoch=0)
    with pytest.raises(identity.IdentityError, match="positive"):
        replace(deployment, mode_epoch=0)


def test_operational_timestamps_are_utc_and_monotonic_duration_is_independent() -> None:
    deployment = _deployment_identity()
    operational = _operational_identity(deployment)
    independently_measured = replace(operational, snapshot_duration_ms=1999)
    assert independently_measured.snapshot_duration_ms == 1999

    non_utc = operational.snapshot_started_at.astimezone(timezone(timedelta(hours=8)))
    with pytest.raises(identity.IdentityError, match="UTC"):
        replace(operational, snapshot_started_at=non_utc)


@pytest.mark.parametrize("unsafe", ["\ud800", "\u0085", "\u202e"])
def test_identity_rejects_unsafe_unicode_text(unsafe: str) -> None:
    with pytest.raises(identity.IdentityError, match="canonical text"):
        replace(_operational_identity(_deployment_identity()), phase="PHASE" + unsafe)


def test_semantic_policy_and_caps_hashes_normalize_decimal_scale() -> None:
    first = Settings.model_validate(
        {
            "execution": {
                "max_equity_change_fraction": "0.10",
                "max_intraday_loss_fraction": "-0",
            }
        }
    )
    second = Settings.model_validate(
        {
            "execution": {
                "max_equity_change_fraction": "0.10000000",
                "max_intraday_loss_fraction": "0.00000000",
            }
        }
    )
    assert (
        ProductionSafetyPolicy.from_settings(first).sha256
        == ProductionSafetyPolicy.from_settings(second).sha256
    )
    assert identity.semantic_config_sha256(first) == identity.semantic_config_sha256(second)

    canary_first = Settings(
        mode=Mode.CANARY,
        live_trading_enabled=True,
        broker=BrokerSettings(adapter=BrokerAdapter.XTQUANT),
        compliance=ComplianceSettings(
            program_trading_report_confirmed=True,
            broker_api_authorized=True,
        ),
        canary_caps=caps("10000.0"),
    )
    canary_second = canary_first.model_copy(update={"canary_caps": caps("10000.0000")})
    assert identity.deployment_caps_sha256(canary_first) == identity.deployment_caps_sha256(canary_second)


def test_execution_evidence_aggregates_by_stable_deployment_not_account_state() -> None:
    first_operational = _operational_identity(_deployment_identity())
    second_operational = replace(first_operational, account_state_sha256="2" * 64)
    first = EvidenceIdentity(
        stage=EvidenceStage.SHADOW,
        execution_session=date(2026, 8, 30),
        firmquant_commit="a" * 40,
        uquant_commit="b" * 40,
        promotion_config_sha256="4" * 64,
        account_sha256="7" * 64,
        data_sha256="1" * 64,
        calendar_sha256="f" * 64,
        operational_identity=first_operational,
    )
    second = replace(first, operational_identity=second_operational)

    assert first.stable_payload == second.stable_payload
    assert first.sha256 != second.sha256
    assert first.payload()["operational_identity"] == first_operational.payload()
