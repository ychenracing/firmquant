"""Read-only composition of machine-verifiable production readiness facts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from firmquant.application.execution_evidence import EvidenceStage
from firmquant.application.production_identity import (
    configuration_sha256,
    current_clean_firmquant_commit,
    promotion_config_sha256,
)
from firmquant.application.promotion_store import PromotionStore
from firmquant.application.readiness import MachineReadinessFacts, evaluate_live_readiness
from firmquant.broker.production_smoke import ProductionSmokeStore
from firmquant.broker.xtquant_safety import XtQuantSafetyManifest
from firmquant.build_identity import load_locked_source_identity, verify_uquant_source_checkout
from firmquant.config import Settings
from firmquant.market_data.calendar_manifest import load_trading_calendar_manifest
from firmquant.market_data.generations import DataGenerationStore
from firmquant.persistence.database import Database
from firmquant.strategy.identity import StrategyIdentity

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_ACTIVE_ORDER_STATES = (
    "PENDING_NEW",
    "ACKNOWLEDGED",
    "PARTIALLY_FILLED",
    "PENDING_CANCEL",
)
_UNRESOLVED_INTENT_STATES = (
    "SUBMITTING",
    "CANCEL_REQUESTED",
    "UNKNOWN",
)


class LiveReadinessRuntimeError(RuntimeError):
    """Readiness evidence could not be interpreted safely."""


@dataclass(frozen=True, slots=True)
class LiveReadinessSnapshot:
    passed: bool
    software_ready: bool
    blockers: tuple[str, ...]
    firmquant_commit: str | None
    uquant_commit: str
    account_hash: str | None
    config_sha256: str
    promotion_config_sha256: str
    data_sha256: str | None
    calendar_sha256: str | None
    smoke_observed_at: str | None
    backup_id: str | None
    shadow_sessions: int
    canary_sessions: int
    heartbeat_age_seconds: float | None
    armed: bool
    kill_switch: bool

    def payload(self) -> dict[str, object]:
        value = asdict(self)
        value["blockers"] = list(self.blockers)
        return value


def _resolved(config_path: Path, value: Path) -> Path:
    candidate = Path(value)
    if candidate.is_absolute():
        return candidate.resolve()
    return (config_path.parent / candidate).resolve()


def _file_sha256(path: Path) -> str | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _json_mapping(value: object) -> dict[str, object] | None:
    return value if isinstance(value, dict) and all(isinstance(key, str) for key in value) else None


def _latest_account_hash(database: Database) -> str | None:
    value = database.scalar(
        "SELECT account_id_hash FROM broker_snapshots ORDER BY captured_at DESC,snapshot_id DESC LIMIT 1"
    )
    return value if isinstance(value, str) and len(value) == 64 else None


def _latest_reconciliation_passed(database: Database, kind: str) -> bool:
    row = database.query_one(
        "SELECT passed,blockers_json FROM reconciliation_runs WHERE kind = ? "
        "ORDER BY started_at DESC,reconciliation_id DESC LIMIT 1",
        (kind,),
    )
    if row is None or row["passed"] != 1:
        return False
    try:
        blockers: object = json.loads(str(row["blockers_json"]))
    except json.JSONDecodeError:
        return False
    return blockers == []


def _latest_backup_matches(
    database: Database,
    *,
    firmquant_commit: str | None,
    uquant_commit: str,
    config_sha256: str,
    account_sha256: str | None,
    calendar_sha256: str | None,
    active_data_manifest_sha256: str | None,
    strategy_data_manifest_sha256: str | None,
) -> tuple[bool, str | None]:
    rows = database.query_all(
        "SELECT backup_id,manifest_json,verification_status FROM backup_receipts "
        "ORDER BY created_at DESC,backup_id DESC"
    )
    for row in rows:
        if row["verification_status"] != "VERIFIED":
            continue
        try:
            manifest: object = json.loads(str(row["manifest_json"]))
        except json.JSONDecodeError:
            continue
        root = _json_mapping(manifest)
        if root is None or root.get("schema_version") != 2:
            continue
        deployment = _json_mapping(root.get("deployment"))
        if deployment is None:
            continue
        expected = {
            "firmquant_commit": firmquant_commit,
            "uquant_commit": uquant_commit,
            "config_sha256": config_sha256,
            "account_sha256": account_sha256,
            "calendar_sha256": calendar_sha256,
            "active_data_manifest_sha256": active_data_manifest_sha256,
            "strategy_data_manifest_sha256": strategy_data_manifest_sha256,
        }
        if all(value is not None and deployment.get(key) == value for key, value in expected.items()):
            return True, str(row["backup_id"])
    return False, None


def _heartbeat(database: Database, now: datetime) -> tuple[bool, bool, float | None]:
    row = database.query_one(
        "SELECT observed_at,control_request_state FROM production_heartbeat WHERE singleton_id=1"
    )
    if row is None:
        return False, False, None
    try:
        observed = datetime.fromisoformat(str(row["observed_at"]))
    except ValueError:
        return False, False, None
    if observed.tzinfo is None or observed.utcoffset() is None:
        return False, False, None
    age = (now.astimezone(UTC) - observed.astimezone(UTC)).total_seconds()
    if age < 0:
        return False, False, age
    return age <= 30.0, str(row["control_request_state"]) == "IDLE", age


def _kill_switch(database: Database) -> bool:
    row = database.query_one(
        "SELECT code FROM risk_events WHERE code IN ('KILL_SWITCH_TRIPPED','KILL_SWITCH_RESET') "
        "ORDER BY created_at DESC,rowid DESC LIMIT 1"
    )
    return row is not None and str(row["code"]) == "KILL_SWITCH_TRIPPED"


def _armed(database: Database, now: datetime) -> bool:
    row = database.query_one(
        "SELECT expires_at FROM arm_leases WHERE revoked_at IS NULL ORDER BY issued_at DESC,lease_id DESC LIMIT 1"
    )
    if row is None:
        return False
    try:
        expires = datetime.fromisoformat(str(row["expires_at"]))
    except ValueError:
        return False
    return expires.tzinfo is not None and expires.utcoffset() is not None and now < expires


def collect_live_readiness(
    *,
    settings: Settings,
    config_path: Path,
    database: Database,
    now: datetime,
) -> LiveReadinessSnapshot:
    """Collect every machine gate without creating leases, orders, or approvals."""

    if not isinstance(settings, Settings):
        raise TypeError("live readiness requires Settings")
    if not isinstance(database, Database):
        raise TypeError("live readiness requires Database")
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("live readiness clock must be timezone-aware")
    config_path = Path(config_path).resolve()
    config_digest = configuration_sha256(config_path)
    promotion_digest = promotion_config_sha256(settings)
    source = load_locked_source_identity()
    uquant_commit = source.uquant_commit

    try:
        firmquant_commit = current_clean_firmquant_commit()
        clean_firmquant = True
    except RuntimeError:
        firmquant_commit = None
        clean_firmquant = False

    locked_uquant = False
    try:
        identity = StrategyIdentity.locked()
        identity.verify()
        checkout = settings.paths.uquant_source_checkout
        if checkout is not None:
            verify_uquant_source_checkout(source, _resolved(config_path, checkout))
            locked_uquant = True
    except Exception:
        locked_uquant = False

    account_hash = _latest_account_hash(database)
    binding = database.query_one("SELECT * FROM account_bindings WHERE singleton_id=1")
    account_binding = (
        binding is not None
        and account_hash is not None
        and str(binding["account_id_hash"]) == account_hash
        and str(binding["uquant_commit"]) == uquant_commit
    )

    latest_decision = database.query_one(
        "SELECT uquant_commit,uquant_config_fingerprint,data_manifest_sha256,firmquant_commit "
        "FROM decision_snapshots ORDER BY strategy_session DESC,created_at DESC LIMIT 1"
    )
    configuration_identity = (
        latest_decision is not None
        and str(latest_decision["uquant_commit"]) == uquant_commit
        and str(latest_decision["uquant_config_fingerprint"]) == source.config_fingerprint
        and (firmquant_commit is None or str(latest_decision["firmquant_commit"]) == firmquant_commit)
    )

    data_root = _resolved(config_path, settings.paths.data_directory)
    strategy_data_path = data_root / ".firmquant-data-manifest.json"
    strategy_data_sha256 = _file_sha256(strategy_data_path)
    latest_decision_data = None if latest_decision is None else str(latest_decision["data_manifest_sha256"])
    data_identity = strategy_data_sha256 is not None and latest_decision_data == strategy_data_sha256

    active_manifest_sha256: str | None = None
    try:
        active = DataGenerationStore(_resolved(config_path, settings.paths.state_directory)).active()
        active_manifest_sha256 = active.manifest_sha256
    except Exception:
        active_manifest_sha256 = None

    calendar_path = data_root / "trading_calendar.json"
    calendar_sha256 = _file_sha256(calendar_path)
    calendar_coverage = False
    try:
        calendar = load_trading_calendar_manifest(calendar_path)
        session = now.astimezone(_SHANGHAI).date()
        calendar_coverage = calendar.covered_from <= session <= calendar.covered_through
    except Exception:
        calendar_coverage = False

    safety: XtQuantSafetyManifest | None = None
    if settings.broker.safety_manifest_path is not None:
        try:
            safety = XtQuantSafetyManifest.load(_resolved(config_path, settings.broker.safety_manifest_path))
        except Exception:
            safety = None

    smoke = None
    if firmquant_commit is not None and account_hash is not None and safety is not None:
        smoke = ProductionSmokeStore(database).latest(
            firmquant_commit=firmquant_commit,
            uquant_commit=uquant_commit,
            config_sha256=config_digest,
            account_hash=account_hash,
            safety_manifest_sha256=safety.sha256,
        )
    broker_readonly_smoke = smoke is not None and smoke.read_healthy and smoke.real_order_calls == 0
    smoke_identity_match = broker_readonly_smoke

    heartbeat_fresh, control_channel_health, heartbeat_age = _heartbeat(database, now)
    clock_evidence = heartbeat_age is not None and heartbeat_age >= 0

    unresolved_value = database.scalar(
        "SELECT count(*) FROM execution_intents WHERE state IN (?,?,?)",
        _UNRESOLVED_INTENT_STATES,
    )
    unresolved = (
        unresolved_value
        if isinstance(unresolved_value, int) and not isinstance(unresolved_value, bool)
        else -1
    )
    external_value = database.scalar(
        "SELECT count(*) FROM broker_orders WHERE ownership IN ('EXTERNAL','UNKNOWN') "
        "AND status IN (?,?,?,?)",
        _ACTIVE_ORDER_STATES,
    )
    external_active = (
        external_value if isinstance(external_value, int) and not isinstance(external_value, bool) else -1
    )

    unknown_value = database.scalar("SELECT count(*) FROM execution_intents WHERE state='UNKNOWN'")
    unknown_count = (
        unknown_value if isinstance(unknown_value, int) and not isinstance(unknown_value, bool) else -1
    )
    duplicate_orders_value = database.scalar(
        "SELECT count(*) FROM (SELECT decision_id,uquant_order_id FROM execution_intents "
        "GROUP BY decision_id,uquant_order_id HAVING count(*) > 1)"
    )
    duplicate_orders = (
        duplicate_orders_value
        if isinstance(duplicate_orders_value, int) and not isinstance(duplicate_orders_value, bool)
        else -1
    )
    duplicate_fills_value = database.scalar(
        "SELECT count(*) FROM (SELECT broker_order_id,symbol,side,shares,price,session_date,event_time "
        "FROM fills GROUP BY broker_order_id,symbol,side,shares,price,session_date,event_time HAVING count(*) > 1)"
    )
    duplicate_fills = (
        duplicate_fills_value
        if isinstance(duplicate_fills_value, int) and not isinstance(duplicate_fills_value, bool)
        else -1
    )
    external_evidence_value = database.scalar(
        "SELECT count(*) FROM broker_orders WHERE ownership IN ('EXTERNAL','UNKNOWN')"
    )
    external_evidence = (
        external_evidence_value
        if isinstance(external_evidence_value, int) and not isinstance(external_evidence_value, bool)
        else -1
    )

    store = PromotionStore(database)
    shadow_aggregate = None
    canary_aggregate = None
    shadow_qualified = canary_qualified = False
    if firmquant_commit is not None and account_hash is not None:
        thresholds = settings.promotion
        shadow_aggregate = store.aggregate(
            stage=EvidenceStage.SHADOW,
            firmquant_commit=firmquant_commit,
            uquant_commit=uquant_commit,
            config_sha256=promotion_digest,
            account_hash=account_hash,
        )
        canary_aggregate = store.aggregate(
            stage=EvidenceStage.CANARY,
            firmquant_commit=firmquant_commit,
            uquant_commit=uquant_commit,
            config_sha256=promotion_digest,
            account_hash=account_hash,
        )
        shadow_qualified = store.qualifies(
            stage=EvidenceStage.SHADOW,
            firmquant_commit=firmquant_commit,
            uquant_commit=uquant_commit,
            config_sha256=promotion_digest,
            account_hash=account_hash,
            min_sessions=thresholds.min_shadow_sessions,
            min_orders=thresholds.min_shadow_orders,
            max_tracking_error=thresholds.max_target_tracking_error,
        )
        canary_qualified = store.qualifies(
            stage=EvidenceStage.CANARY,
            firmquant_commit=firmquant_commit,
            uquant_commit=uquant_commit,
            config_sha256=promotion_digest,
            account_hash=account_hash,
            min_sessions=thresholds.min_canary_sessions,
            min_orders=thresholds.min_canary_orders,
            min_fills=thresholds.min_canary_fills,
            max_tracking_error=thresholds.max_canary_target_tracking_error,
        )

    backup_ok, backup_id = _latest_backup_matches(
        database,
        firmquant_commit=firmquant_commit,
        uquant_commit=uquant_commit,
        config_sha256=config_digest,
        account_sha256=account_hash,
        calendar_sha256=calendar_sha256,
        active_data_manifest_sha256=active_manifest_sha256,
        strategy_data_manifest_sha256=strategy_data_sha256,
    )
    kill_switch = _kill_switch(database)
    facts = MachineReadinessFacts(
        clean_firmquant_identity=clean_firmquant,
        locked_uquant_identity=locked_uquant,
        account_binding=account_binding,
        configuration_identity=configuration_identity,
        data_identity=data_identity,
        calendar_coverage=calendar_coverage,
        clock_evidence=clock_evidence,
        broker_readonly_smoke=broker_readonly_smoke,
        smoke_identity_match=smoke_identity_match,
        startup_reconciliation=_latest_reconciliation_passed(database, "STARTUP"),
        intraday_reconciliation=_latest_reconciliation_passed(database, "INTRADAY"),
        eod_reconciliation=_latest_reconciliation_passed(database, "EOD"),
        no_unresolved_orders=unresolved == 0,
        no_external_active_orders=external_active == 0,
        control_channel_health=control_channel_health,
        heartbeat_fresh=heartbeat_fresh,
        verified_backup=backup_ok,
        shadow_qualified=shadow_qualified,
        canary_qualified=canary_qualified,
        no_unknown=unknown_count == 0,
        no_duplicate_economic_orders=duplicate_orders == 0,
        no_duplicate_fills=duplicate_fills == 0,
        no_external_activity=external_evidence == 0,
        kill_switch_clear=not kill_switch,
    )
    result = evaluate_live_readiness(facts)
    return LiveReadinessSnapshot(
        passed=result.passed,
        software_ready=result.software_ready,
        blockers=result.blockers,
        firmquant_commit=firmquant_commit,
        uquant_commit=uquant_commit,
        account_hash=account_hash,
        config_sha256=config_digest,
        promotion_config_sha256=promotion_digest,
        data_sha256=strategy_data_sha256,
        calendar_sha256=calendar_sha256,
        smoke_observed_at=None if smoke is None else smoke.observed_at.isoformat(),
        backup_id=backup_id,
        shadow_sessions=0 if shadow_aggregate is None else shadow_aggregate.observed_sessions,
        canary_sessions=0 if canary_aggregate is None else canary_aggregate.observed_sessions,
        heartbeat_age_seconds=heartbeat_age,
        armed=_armed(database, now),
        kill_switch=kill_switch,
    )


__all__ = ("LiveReadinessRuntimeError", "LiveReadinessSnapshot", "collect_live_readiness")
