"""Production deployment identities used by smoke, promotion, arm, and runtime gates."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import shutil
import subprocess  # nosec B404
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Never, cast

from firmquant.build_identity import SourceIdentity, load_locked_source_identity
from firmquant.config import DeploymentCaps, Mode, Settings
from firmquant.persistence.repositories import canonical_sha256
from firmquant.risk.production_policy import ProductionSafetyPolicy, canonical_decimal_text

_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class IdentityError(RuntimeError):
    """Raised when a deployment or operational identity is not exact and canonical."""


def _canonical_text(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or unicodedata.normalize("NFC", value) != value
        or any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value)
    ):
        raise IdentityError(f"{label} must be non-empty canonical text")
    return value


def _digest(value: object, *, label: str, pattern: re.Pattern[str] = _SHA256) -> str:
    rendered = _canonical_text(value, label=label)
    if pattern.fullmatch(rendered) is None:
        raise IdentityError(f"{label} must be a canonical lowercase digest")
    return rendered


def _count(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise IdentityError(f"{label} must be a nonnegative integer")
    return value


def _positive_count(value: object, *, label: str) -> int:
    observed = _count(value, label=label)
    if observed == 0:
        raise IdentityError(f"{label} must be a positive integer")
    return observed


def _require_utc_datetime(value: object, *, label: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise IdentityError(f"{label} must be a canonical UTC timestamp")


def _canonical_json(payload: object) -> str:
    try:
        return json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise IdentityError("identity payload is not canonical JSON") from error


def _payload_sha256(payload: object) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _caps_payload(caps: DeploymentCaps | None, *, mode: Mode) -> dict[str, object]:
    values: dict[str, object] | None = None
    if caps is not None:
        values = {
            "max_order_notional": canonical_decimal_text(caps.max_order_notional),
            "max_daily_submitted_notional": canonical_decimal_text(caps.max_daily_submitted_notional),
            "max_daily_filled_notional": canonical_decimal_text(caps.max_daily_filled_notional),
            "max_symbol_notional": canonical_decimal_text(caps.max_symbol_notional),
            "max_total_gross_notional": canonical_decimal_text(caps.max_total_gross_notional),
        }
    return {
        "schema": "firmquant.deployment-caps.v1",
        "mode": mode.value,
        "caps": values,
    }


def deployment_caps_sha256(settings: Settings) -> str:
    """Hash the caps selected by the configured mode, including explicit mode binding."""

    if not isinstance(settings, Settings):
        raise TypeError("deployment caps identity requires Settings")
    return _payload_sha256(_caps_payload(settings.active_deployment_caps, mode=settings.mode))


def semantic_config_sha256(settings: Settings) -> str:
    """Hash normalized non-secret semantics together with effective policy and caps."""

    if not isinstance(settings, Settings):
        raise TypeError("semantic configuration identity requires Settings")
    policy = ProductionSafetyPolicy.from_settings(settings)
    execution = settings.execution
    promotion = settings.promotion
    payload = {
        "schema": "firmquant.semantic-configuration.v1",
        "settings_schema_version": settings.schema_version,
        "mode": settings.mode.value,
        "live_trading_enabled": settings.live_trading_enabled,
        "timezone": settings.timezone,
        "broker": {
            "adapter": settings.broker.adapter.value,
            "session_id": settings.broker.session_id,
        },
        "compliance": {
            "program_trading_report_confirmed": (settings.compliance.program_trading_report_confirmed),
            "broker_api_authorized": settings.compliance.broker_api_authorized,
        },
        "execution": {
            "sell_window_seconds": execution.sell_window_seconds,
            "buy_window_seconds": execution.buy_window_seconds,
            "min_order_lifetime_seconds": execution.min_order_lifetime_seconds,
            "poll_interval_seconds": execution.poll_interval_seconds,
            "max_order_lifetime_seconds": execution.max_order_lifetime_seconds,
            "max_open_orders": execution.max_open_orders,
            "max_consecutive_rejections": execution.max_consecutive_rejections,
            "max_disconnect_seconds": execution.max_disconnect_seconds,
            "max_submit_count_window": execution.max_submit_count_window,
            "max_cancel_count_window": execution.max_cancel_count_window,
            "max_quote_age_seconds": execution.max_quote_age_seconds,
            "max_clock_drift_seconds": execution.max_clock_drift_seconds,
            "max_price_deviation_bps": canonical_decimal_text(execution.max_price_deviation_bps),
            "max_equity_change_fraction": canonical_decimal_text(execution.max_equity_change_fraction),
            "max_intraday_loss_fraction": canonical_decimal_text(execution.max_intraday_loss_fraction),
            "max_capital_drawdown_fraction": canonical_decimal_text(execution.max_capital_drawdown_fraction),
            "max_arm_ttl_seconds": execution.max_arm_ttl_seconds,
        },
        "promotion": {
            "min_shadow_sessions": promotion.min_shadow_sessions,
            "min_shadow_orders": promotion.min_shadow_orders,
            "max_target_tracking_error": canonical_decimal_text(promotion.max_target_tracking_error),
            "min_canary_sessions": promotion.min_canary_sessions,
            "min_canary_orders": promotion.min_canary_orders,
            "min_canary_fills": promotion.min_canary_fills,
            "max_canary_target_tracking_error": canonical_decimal_text(
                promotion.max_canary_target_tracking_error
            ),
        },
        "caps_sha256": deployment_caps_sha256(settings),
        "production_policy_sha256": policy.sha256,
    }
    return _payload_sha256(payload)


@dataclass(frozen=True, slots=True)
class DeploymentIdentity:
    """Stable identity for reviewed source, authority, mode, policy, and configuration."""

    firmquant_commit: str
    uquant_commit: str
    uquant_tree: str
    uquant_package_manifest_sha256: str
    uquant_code_fingerprint: str
    uquant_config_fingerprint: str
    semantic_config_sha256: str
    raw_config_sha256: str
    xtquant_safety_manifest_sha256: str
    account_id_hash: str
    account_authority_epoch: int
    mode_epoch: int
    mode: Mode
    caps_sha256: str
    production_policy_sha256: str

    def __post_init__(self) -> None:
        _digest(self.firmquant_commit, label="firmquant commit", pattern=_GIT_SHA)
        _digest(self.uquant_commit, label="uquant commit", pattern=_GIT_SHA)
        _digest(self.uquant_tree, label="uquant tree", pattern=_GIT_SHA)
        for label, value in (
            ("uquant package manifest SHA-256", self.uquant_package_manifest_sha256),
            ("uquant code fingerprint", self.uquant_code_fingerprint),
            ("uquant configuration fingerprint", self.uquant_config_fingerprint),
            ("semantic configuration SHA-256", self.semantic_config_sha256),
            ("raw configuration SHA-256", self.raw_config_sha256),
            ("XtQuant safety manifest SHA-256", self.xtquant_safety_manifest_sha256),
            ("account id hash", self.account_id_hash),
            ("caps SHA-256", self.caps_sha256),
            ("production policy SHA-256", self.production_policy_sha256),
        ):
            _digest(value, label=label)
        _positive_count(self.account_authority_epoch, label="account authority epoch")
        _positive_count(self.mode_epoch, label="mode epoch")
        if not isinstance(self.mode, Mode):
            raise IdentityError("deployment mode must be typed")

    @classmethod
    def from_inputs(
        cls,
        *,
        firmquant_commit: str,
        source_identity: SourceIdentity,
        settings: Settings,
        raw_config_sha256: str,
        xtquant_safety_manifest_sha256: str,
        account_id_hash: str,
        account_authority_epoch: int,
        mode_epoch: int,
    ) -> DeploymentIdentity:
        if not isinstance(source_identity, SourceIdentity):
            raise TypeError("deployment identity requires SourceIdentity")
        if not isinstance(settings, Settings):
            raise TypeError("deployment identity requires Settings")
        policy = ProductionSafetyPolicy.from_settings(settings)
        code_fingerprint = _payload_sha256(
            {
                "schema": "firmquant.uquant-code-fingerprint.v1",
                "economic_code_fingerprint": source_identity.economic_code_fingerprint,
                "account_code_fingerprint": source_identity.account_code_fingerprint,
                "public_api_contract_sha256": source_identity.public_api_contract_sha256,
                "universe_sha256": source_identity.universe_sha256,
            }
        )
        return cls(
            firmquant_commit=firmquant_commit,
            uquant_commit=source_identity.uquant_commit,
            uquant_tree=source_identity.uquant_tree,
            uquant_package_manifest_sha256=source_identity.uquant_package_manifest_sha256,
            uquant_code_fingerprint=code_fingerprint,
            uquant_config_fingerprint=source_identity.config_fingerprint,
            semantic_config_sha256=semantic_config_sha256(settings),
            raw_config_sha256=raw_config_sha256,
            xtquant_safety_manifest_sha256=xtquant_safety_manifest_sha256,
            account_id_hash=account_id_hash,
            account_authority_epoch=account_authority_epoch,
            mode_epoch=mode_epoch,
            mode=settings.mode,
            caps_sha256=deployment_caps_sha256(settings),
            production_policy_sha256=policy.sha256,
        )

    def payload(self) -> dict[str, object]:
        return {
            "schema": "firmquant.deployment-identity.v1",
            "firmquant_commit": self.firmquant_commit,
            "uquant_commit": self.uquant_commit,
            "uquant_tree": self.uquant_tree,
            "uquant_package_manifest_sha256": self.uquant_package_manifest_sha256,
            "uquant_code_fingerprint": self.uquant_code_fingerprint,
            "uquant_config_fingerprint": self.uquant_config_fingerprint,
            "semantic_config_sha256": self.semantic_config_sha256,
            "raw_config_sha256": self.raw_config_sha256,
            "xtquant_safety_manifest_sha256": self.xtquant_safety_manifest_sha256,
            "account_id_hash": self.account_id_hash,
            "account_authority_epoch": self.account_authority_epoch,
            "mode_epoch": self.mode_epoch,
            "mode": self.mode.value,
            "caps_sha256": self.caps_sha256,
            "production_policy_sha256": self.production_policy_sha256,
        }

    @property
    def canonical_json(self) -> str:
        return _canonical_json(self.payload())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class OperationalEvidenceIdentity:
    """Mutable identity for one exact snapshot-backed operational observation."""

    deployment_identity: DeploymentIdentity
    account_state_sha256: str
    broker_snapshot_id: str
    broker_snapshot_sha256: str
    broker_event_watermark: int
    snapshot_started_at: datetime
    snapshot_completed_at: datetime
    snapshot_duration_ms: int
    calendar_sha256: str
    active_data_generation_sha256: str
    strategy_data_manifest_sha256: str
    strategy_session: date
    decision_id: str | None
    phase: str
    kind: str

    def __post_init__(self) -> None:
        if not isinstance(self.deployment_identity, DeploymentIdentity):
            raise IdentityError("operational evidence requires DeploymentIdentity")
        for label, value in (
            ("account state SHA-256", self.account_state_sha256),
            ("broker snapshot SHA-256", self.broker_snapshot_sha256),
            ("calendar SHA-256", self.calendar_sha256),
            ("active data generation SHA-256", self.active_data_generation_sha256),
            ("strategy data manifest SHA-256", self.strategy_data_manifest_sha256),
        ):
            _digest(value, label=label)
        _canonical_text(self.broker_snapshot_id, label="broker snapshot id")
        _count(self.broker_event_watermark, label="broker event watermark")
        _count(self.snapshot_duration_ms, label="snapshot duration")
        for temporal_label, temporal_value in (
            ("snapshot started at", self.snapshot_started_at),
            ("snapshot completed at", self.snapshot_completed_at),
        ):
            _require_utc_datetime(temporal_value, label=temporal_label)
        if self.snapshot_completed_at < self.snapshot_started_at:
            raise IdentityError("snapshot completion precedes start")
        if type(self.strategy_session) is not date:
            raise IdentityError("strategy session must be a calendar date")
        if self.decision_id is not None:
            _canonical_text(self.decision_id, label="decision id")
        _canonical_text(self.phase, label="evidence phase")
        _canonical_text(self.kind, label="evidence kind")

    @property
    def deployment_identity_sha256(self) -> str:
        return self.deployment_identity.sha256

    def payload(self) -> dict[str, object]:
        return {
            "schema": "firmquant.operational-evidence-identity.v1",
            "deployment_identity": self.deployment_identity.payload(),
            "deployment_identity_sha256": self.deployment_identity_sha256,
            "account_state_sha256": self.account_state_sha256,
            "broker_snapshot_id": self.broker_snapshot_id,
            "broker_snapshot_sha256": self.broker_snapshot_sha256,
            "broker_event_watermark": self.broker_event_watermark,
            "snapshot_started_at": self.snapshot_started_at.isoformat(),
            "snapshot_completed_at": self.snapshot_completed_at.isoformat(),
            "snapshot_duration_ms": self.snapshot_duration_ms,
            "calendar_sha256": self.calendar_sha256,
            "active_data_generation_sha256": self.active_data_generation_sha256,
            "strategy_data_manifest_sha256": self.strategy_data_manifest_sha256,
            "strategy_session": self.strategy_session.isoformat(),
            "decision_id": self.decision_id,
            "phase": self.phase,
            "kind": self.kind,
        }

    @property
    def canonical_json(self) -> str:
        return _canonical_json(self.payload())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()


def _reject_json_float(_value: str) -> Never:
    raise IdentityError("identity JSON must not contain binary floats")


def _reject_json_constant(_value: str) -> Never:
    raise IdentityError("identity JSON contains a non-standard constant")


def _object_from_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise IdentityError(f"identity JSON contains duplicate field: {key}")
        result[key] = value
    return result


def _mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise IdentityError(f"{label} must be a JSON object")
    return cast(Mapping[str, object], value)


def _require_fields(mapping: Mapping[str, object], expected: frozenset[str], *, label: str) -> None:
    if set(mapping) != expected:
        raise IdentityError(f"{label} fields do not match the canonical schema")


_DEPLOYMENT_FIELDS = frozenset(
    {
        "schema",
        "firmquant_commit",
        "uquant_commit",
        "uquant_tree",
        "uquant_package_manifest_sha256",
        "uquant_code_fingerprint",
        "uquant_config_fingerprint",
        "semantic_config_sha256",
        "raw_config_sha256",
        "xtquant_safety_manifest_sha256",
        "account_id_hash",
        "account_authority_epoch",
        "mode_epoch",
        "mode",
        "caps_sha256",
        "production_policy_sha256",
    }
)


def _deployment_from_payload(payload: Mapping[str, object]) -> DeploymentIdentity:
    _require_fields(payload, _DEPLOYMENT_FIELDS, label="deployment identity")
    if payload["schema"] != "firmquant.deployment-identity.v1":
        raise IdentityError("deployment identity schema is unsupported")
    try:
        mode = Mode(_canonical_text(payload["mode"], label="deployment mode"))
    except ValueError as error:
        raise IdentityError("deployment mode is invalid") from error
    return DeploymentIdentity(
        firmquant_commit=cast(str, payload["firmquant_commit"]),
        uquant_commit=cast(str, payload["uquant_commit"]),
        uquant_tree=cast(str, payload["uquant_tree"]),
        uquant_package_manifest_sha256=cast(str, payload["uquant_package_manifest_sha256"]),
        uquant_code_fingerprint=cast(str, payload["uquant_code_fingerprint"]),
        uquant_config_fingerprint=cast(str, payload["uquant_config_fingerprint"]),
        semantic_config_sha256=cast(str, payload["semantic_config_sha256"]),
        raw_config_sha256=cast(str, payload["raw_config_sha256"]),
        xtquant_safety_manifest_sha256=cast(str, payload["xtquant_safety_manifest_sha256"]),
        account_id_hash=cast(str, payload["account_id_hash"]),
        account_authority_epoch=_positive_count(
            payload["account_authority_epoch"], label="account authority epoch"
        ),
        mode_epoch=_positive_count(payload["mode_epoch"], label="mode epoch"),
        mode=mode,
        caps_sha256=cast(str, payload["caps_sha256"]),
        production_policy_sha256=cast(str, payload["production_policy_sha256"]),
    )


_OPERATIONAL_FIELDS = frozenset(
    {
        "schema",
        "deployment_identity",
        "deployment_identity_sha256",
        "account_state_sha256",
        "broker_snapshot_id",
        "broker_snapshot_sha256",
        "broker_event_watermark",
        "snapshot_started_at",
        "snapshot_completed_at",
        "snapshot_duration_ms",
        "calendar_sha256",
        "active_data_generation_sha256",
        "strategy_data_manifest_sha256",
        "strategy_session",
        "decision_id",
        "phase",
        "kind",
    }
)


def _datetime(value: object, *, label: str) -> datetime:
    text = _canonical_text(value, label=label)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise IdentityError(f"{label} is invalid") from error
    if parsed.isoformat() != text:
        raise IdentityError(f"{label} is not canonical")
    return parsed


def _date(value: object, *, label: str) -> date:
    text = _canonical_text(value, label=label)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as error:
        raise IdentityError(f"{label} is invalid") from error
    if parsed.isoformat() != text:
        raise IdentityError(f"{label} is not canonical")
    return parsed


def _operational_from_payload(payload: Mapping[str, object]) -> OperationalEvidenceIdentity:
    _require_fields(payload, _OPERATIONAL_FIELDS, label="operational evidence identity")
    if payload["schema"] != "firmquant.operational-evidence-identity.v1":
        raise IdentityError("operational evidence identity schema is unsupported")
    deployment = _deployment_from_payload(
        _mapping(payload["deployment_identity"], label="deployment identity")
    )
    observed_deployment_sha256 = _digest(
        payload["deployment_identity_sha256"], label="deployment identity SHA-256"
    )
    if not secrets.compare_digest(deployment.sha256, observed_deployment_sha256):
        raise IdentityError("deployment identity SHA-256 does not match its payload")
    decision = payload["decision_id"]
    if decision is not None:
        decision = _canonical_text(decision, label="decision id")
    return OperationalEvidenceIdentity(
        deployment_identity=deployment,
        account_state_sha256=cast(str, payload["account_state_sha256"]),
        broker_snapshot_id=cast(str, payload["broker_snapshot_id"]),
        broker_snapshot_sha256=cast(str, payload["broker_snapshot_sha256"]),
        broker_event_watermark=_count(payload["broker_event_watermark"], label="broker event watermark"),
        snapshot_started_at=_datetime(payload["snapshot_started_at"], label="snapshot started at"),
        snapshot_completed_at=_datetime(payload["snapshot_completed_at"], label="snapshot completed at"),
        snapshot_duration_ms=_count(payload["snapshot_duration_ms"], label="snapshot duration"),
        calendar_sha256=cast(str, payload["calendar_sha256"]),
        active_data_generation_sha256=cast(str, payload["active_data_generation_sha256"]),
        strategy_data_manifest_sha256=cast(str, payload["strategy_data_manifest_sha256"]),
        strategy_session=_date(payload["strategy_session"], label="strategy session"),
        decision_id=decision,
        phase=cast(str, payload["phase"]),
        kind=cast(str, payload["kind"]),
    )


def parse_identity(
    raw: str | bytes,
    *,
    expected_sha256: str | None = None,
) -> DeploymentIdentity | OperationalEvidenceIdentity:
    """Parse only exact canonical identity JSON and optionally verify its digest."""

    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    except UnicodeDecodeError as error:
        raise IdentityError("identity is not canonical UTF-8 JSON") from error
    if not isinstance(text, str):
        raise TypeError("identity JSON must be str or bytes")
    try:
        decoded = json.loads(
            text,
            object_pairs_hook=_object_from_pairs,
            parse_float=_reject_json_float,
            parse_constant=_reject_json_constant,
        )
    except json.JSONDecodeError as error:
        raise IdentityError("identity is not canonical JSON") from error
    payload = _mapping(decoded, label="identity root")
    schema = payload.get("schema")
    if schema == "firmquant.deployment-identity.v1":
        identity: DeploymentIdentity | OperationalEvidenceIdentity = _deployment_from_payload(payload)
    elif schema == "firmquant.operational-evidence-identity.v1":
        identity = _operational_from_payload(payload)
    else:
        raise IdentityError("identity schema is unsupported")
    if text != identity.canonical_json:
        raise IdentityError("identity JSON is not in canonical form")
    if expected_sha256 is not None:
        expected = _digest(expected_sha256, label="expected identity SHA-256")
        if not secrets.compare_digest(identity.sha256, expected):
            raise IdentityError("identity SHA-256 verification failed")
    return identity


def configuration_sha256(path: Path) -> str:
    candidate = Path(path)
    if candidate.is_symlink() or not candidate.is_file():
        raise RuntimeError("production configuration is unavailable")
    try:
        return hashlib.sha256(candidate.read_bytes()).hexdigest()
    except OSError as error:
        raise RuntimeError("production configuration cannot be read") from error


def promotion_config_sha256(settings: Settings) -> str:
    """Hash the SHADOW-observed execution contract while excluding risk-shrinking live caps."""

    if not isinstance(settings, Settings):
        raise TypeError("promotion identity requires Settings")
    broker = settings.broker
    paths = settings.paths
    account_hash = hashlib.sha256((broker.account_alias or "").encode("utf-8")).hexdigest()
    return canonical_sha256(
        {
            "schema": "firmquant.promotion-config.v1",
            "timezone": settings.timezone,
            "broker": {
                "adapter": broker.adapter,
                "account_alias_sha256": account_hash,
                "userdata_path": (
                    None if broker.xtquant_userdata_path is None else str(broker.xtquant_userdata_path)
                ),
                "session_id": broker.session_id,
                "safety_manifest_path": (
                    None if broker.safety_manifest_path is None else str(broker.safety_manifest_path)
                ),
            },
            "data_directory": str(paths.data_directory),
            "uquant_source_checkout": (
                None if paths.uquant_source_checkout is None else str(paths.uquant_source_checkout)
            ),
            "execution": settings.execution.model_dump(mode="python"),
        }
    )


def current_clean_firmquant_commit(repository_root: Path | None = None) -> str:
    """Require a clean Git checkout whose identity-bearing files match the locked build record."""

    root = Path(__file__).resolve().parents[3] if repository_root is None else Path(repository_root).resolve()
    executable = shutil.which("git")
    if executable is None:
        raise RuntimeError("git is required to prove firmquant production identity")
    try:
        commit = subprocess.run(  # nosec B603
            [executable, "rev-parse", "HEAD^{commit}"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        status = subprocess.run(  # nosec B603
            [executable, "status", "--porcelain", "--untracked-files=all"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise RuntimeError("firmquant Git identity cannot be inspected") from error
    if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
        raise RuntimeError("firmquant commit identity is invalid")
    if status:
        raise RuntimeError("firmquant production checkout must be clean")
    try:
        load_locked_source_identity().verify_firmquant_files(root)
    except Exception as error:
        raise RuntimeError("firmquant build identity does not match reviewed files") from error
    return commit


__all__ = (
    "DeploymentIdentity",
    "IdentityError",
    "OperationalEvidenceIdentity",
    "configuration_sha256",
    "current_clean_firmquant_commit",
    "deployment_caps_sha256",
    "parse_identity",
    "promotion_config_sha256",
    "semantic_config_sha256",
)
