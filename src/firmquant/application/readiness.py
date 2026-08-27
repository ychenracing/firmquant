"""Pure, read-only evaluation of machine-verifiable LIVE software gates."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MachineReadinessFacts:
    clean_firmquant_identity: bool
    locked_uquant_identity: bool
    account_binding: bool
    configuration_identity: bool
    data_identity: bool
    calendar_coverage: bool
    clock_evidence: bool
    broker_readonly_smoke: bool
    smoke_identity_match: bool
    startup_reconciliation: bool
    intraday_reconciliation: bool
    eod_reconciliation: bool
    no_unresolved_orders: bool
    no_external_active_orders: bool
    control_channel_health: bool
    heartbeat_fresh: bool
    verified_backup: bool
    shadow_qualified: bool
    canary_qualified: bool
    no_unknown: bool
    no_duplicate_economic_orders: bool
    no_duplicate_fills: bool
    no_external_activity: bool
    kill_switch_clear: bool

    def __post_init__(self) -> None:
        for field_name in self.__dataclass_fields__:
            if not isinstance(getattr(self, field_name), bool):
                raise TypeError(f"{field_name} readiness fact must be bool")


@dataclass(frozen=True, slots=True)
class LiveReadinessResult:
    passed: bool
    software_ready: bool
    blockers: tuple[str, ...]


_BLOCKERS = (
    ("clean_firmquant_identity", "FIRMQ_IDENTITY_NOT_CLEAN"),
    ("locked_uquant_identity", "UQUANT_IDENTITY_NOT_LOCKED"),
    ("account_binding", "ACCOUNT_BINDING_MISSING"),
    ("configuration_identity", "CONFIGURATION_IDENTITY_MISMATCH"),
    ("data_identity", "DATA_IDENTITY_MISMATCH"),
    ("calendar_coverage", "CALENDAR_COVERAGE_INCOMPLETE"),
    ("clock_evidence", "CLOCK_EVIDENCE_MISSING"),
    ("broker_readonly_smoke", "BROKER_READONLY_SMOKE_MISSING"),
    ("smoke_identity_match", "BROKER_READONLY_SMOKE_IDENTITY_MISMATCH"),
    ("startup_reconciliation", "STARTUP_RECONCILIATION_MISSING"),
    ("intraday_reconciliation", "INTRADAY_RECONCILIATION_MISSING"),
    ("eod_reconciliation", "EOD_RECONCILIATION_MISSING"),
    ("no_unresolved_orders", "UNRESOLVED_ORDER_STATE"),
    ("no_external_active_orders", "EXTERNAL_BROKER_ORDER"),
    ("control_channel_health", "CONTROL_CHANNEL_UNHEALTHY"),
    ("heartbeat_fresh", "HEARTBEAT_STALE"),
    ("verified_backup", "VERIFIED_BACKUP_MISSING"),
    ("shadow_qualified", "SHADOW_NOT_QUALIFIED"),
    ("canary_qualified", "CANARY_NOT_QUALIFIED"),
    ("no_unknown", "UNRESOLVED_UNKNOWN"),
    ("no_duplicate_economic_orders", "DUPLICATE_ECONOMIC_ORDER"),
    ("no_duplicate_fills", "DUPLICATE_FILL"),
    ("no_external_activity", "EXTERNAL_ACTIVITY"),
    ("kill_switch_clear", "KILL_SWITCH_TRIPPED"),
)


def evaluate_live_readiness(facts: MachineReadinessFacts) -> LiveReadinessResult:
    """Return every failed software gate without mutating state or granting authority."""

    if not isinstance(facts, MachineReadinessFacts):
        raise TypeError("live readiness requires MachineReadinessFacts")
    blockers = tuple(code for field_name, code in _BLOCKERS if not getattr(facts, field_name))
    passed = not blockers
    return LiveReadinessResult(
        passed=passed,
        software_ready=passed,
        blockers=blockers,
    )


__all__ = ("LiveReadinessResult", "MachineReadinessFacts", "evaluate_live_readiness")
