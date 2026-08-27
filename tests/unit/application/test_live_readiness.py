from __future__ import annotations

from dataclasses import replace

from firmquant.application.readiness import MachineReadinessFacts, evaluate_live_readiness


def _ready() -> MachineReadinessFacts:
    return MachineReadinessFacts(
        clean_firmquant_identity=True,
        locked_uquant_identity=True,
        account_binding=True,
        configuration_identity=True,
        data_identity=True,
        calendar_coverage=True,
        clock_evidence=True,
        broker_readonly_smoke=True,
        smoke_identity_match=True,
        startup_reconciliation=True,
        intraday_reconciliation=True,
        eod_reconciliation=True,
        no_unresolved_orders=True,
        no_external_active_orders=True,
        control_channel_health=True,
        heartbeat_fresh=True,
        verified_backup=True,
        shadow_qualified=True,
        canary_qualified=True,
        no_unknown=True,
        no_duplicate_economic_orders=True,
        no_duplicate_fills=True,
        no_external_activity=True,
        kill_switch_clear=True,
    )


def test_shadow_cannot_authorize_live_without_independent_canary() -> None:
    result = evaluate_live_readiness(replace(_ready(), canary_qualified=False))
    assert result.software_ready is False
    assert "CANARY_NOT_QUALIFIED" in result.blockers


def test_missing_smoke_backup_and_canary_are_all_reported() -> None:
    result = evaluate_live_readiness(
        replace(
            _ready(),
            broker_readonly_smoke=False,
            verified_backup=False,
            canary_qualified=False,
        )
    )
    assert result.software_ready is False
    assert set(result.blockers) >= {
        "BROKER_READONLY_SMOKE_MISSING",
        "VERIFIED_BACKUP_MISSING",
        "CANARY_NOT_QUALIFIED",
    }


def test_all_machine_gates_must_pass() -> None:
    result = evaluate_live_readiness(_ready())
    assert result.software_ready is True
    assert result.passed is True
    assert result.blockers == ()
