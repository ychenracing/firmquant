"""Strict deployment configuration with real trading disabled by default."""

from __future__ import annotations

import json
import tomllib
from collections.abc import Mapping
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Annotated, ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ConfigurationError(RuntimeError):
    """Raised when a configuration file cannot be read as strict TOML."""


class SafeConfigModel(BaseModel):
    """Frozen configuration model with an explicit log-safe representation."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)
    sensitive_fields: ClassVar[frozenset[str]] = frozenset()

    @classmethod
    def _safe_value(cls, value: object) -> object:
        if isinstance(value, SafeConfigModel):
            return value.safe_mapping()
        if isinstance(value, Path):
            return "<redacted-path>"
        if isinstance(value, StrEnum):
            return value.value
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, Mapping):
            return {str(key): cls._safe_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set, frozenset)):
            return [cls._safe_value(item) for item in value]
        return value

    def safe_mapping(self) -> dict[str, object]:
        """Return configuration values with paths and explicitly sensitive fields hidden."""

        rendered: dict[str, object] = {}
        for name in self.__class__.model_fields:
            value = getattr(self, name)
            rendered[name] = "<redacted>" if name in self.sensitive_fields else self._safe_value(value)
        return rendered

    def safe_repr(self) -> str:
        """Return deterministic JSON suitable for logs and diagnostics."""

        return json.dumps(
            self.safe_mapping(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.safe_repr()})"


class Mode(StrEnum):
    """Execution connectivity and broker-write authority mode."""

    REPLAY = "REPLAY"
    PAPER = "PAPER"
    SHADOW = "SHADOW"
    CANARY = "CANARY"
    LIVE = "LIVE"


class BrokerAdapter(StrEnum):
    """Broker implementation selected by deployment configuration."""

    RECORDED_REPLAY = "RECORDED_REPLAY"
    PAPER = "PAPER"
    XTQUANT = "XTQUANT"


PositiveInteger = Annotated[int, Field(gt=0, strict=True)]
NonNegativeInteger = Annotated[int, Field(ge=0, strict=True)]
PositiveNotional = Annotated[
    Decimal,
    Field(gt=Decimal("0"), max_digits=20, decimal_places=4),
]
SafeFraction = Annotated[
    Decimal,
    Field(ge=Decimal("0"), le=Decimal("1"), max_digits=10, decimal_places=8),
]


class BrokerSettings(SafeConfigModel):
    """Broker selection and local references; never broker credentials."""

    sensitive_fields: ClassVar[frozenset[str]] = frozenset(
        {
            "account_alias",
            "xtquant_userdata_path",
            "safety_manifest_path",
        }
    )

    adapter: BrokerAdapter = BrokerAdapter.PAPER
    account_alias: str | None = Field(default=None, min_length=1, max_length=128, repr=False)
    xtquant_userdata_path: Path | None = Field(default=None, repr=False)
    session_id: PositiveInteger | None = Field(default=None, repr=False)
    safety_manifest_path: Path | None = Field(default=None, repr=False)


class ComplianceSettings(SafeConfigModel):
    """Explicit operator attestations required before real broker writes."""

    program_trading_report_confirmed: bool = Field(default=False, strict=True)
    broker_api_authorized: bool = Field(default=False, strict=True)


class DeploymentCaps(SafeConfigModel):
    """Mode-specific absolute safety caps; none are strategy parameters."""

    max_order_notional: PositiveNotional
    max_daily_submitted_notional: PositiveNotional
    max_daily_filled_notional: PositiveNotional
    max_symbol_notional: PositiveNotional
    max_total_gross_notional: PositiveNotional

    @field_validator("*", mode="before")
    @classmethod
    def reject_binary_float(cls, value: object) -> object:
        if isinstance(value, float):
            raise ValueError("deployment notionals must not use binary float")
        return value

    @model_validator(mode="after")
    def validate_cap_ordering(self) -> Self:
        if self.max_order_notional > self.max_symbol_notional:
            raise ValueError("max_order_notional cannot exceed max_symbol_notional")
        if self.max_symbol_notional > self.max_total_gross_notional:
            raise ValueError("max_symbol_notional cannot exceed max_total_gross_notional")
        if self.max_order_notional > self.max_daily_submitted_notional:
            raise ValueError("max_order_notional cannot exceed max_daily_submitted_notional")
        if self.max_order_notional > self.max_daily_filled_notional:
            raise ValueError("max_order_notional cannot exceed max_daily_filled_notional")
        return self


class ExecutionRuntimeSettings(SafeConfigModel):
    """Operational execution/risk limits that may only shrink uquant intent."""

    sell_window_seconds: PositiveInteger = 300
    buy_window_seconds: PositiveInteger = 300
    min_order_lifetime_seconds: PositiveInteger = 3
    poll_interval_seconds: PositiveInteger = 1
    max_order_lifetime_seconds: PositiveInteger = 120
    max_open_orders: PositiveInteger = 4
    max_consecutive_rejections: PositiveInteger = 3
    max_disconnect_seconds: PositiveInteger = 30
    max_submit_count_window: PositiveInteger = 20
    max_cancel_count_window: PositiveInteger = 20
    max_quote_age_seconds: PositiveInteger = 5
    max_clock_drift_seconds: PositiveInteger = 2
    max_price_deviation_bps: Decimal = Decimal("200")
    max_equity_change_fraction: SafeFraction = Decimal("0.10")
    max_intraday_loss_fraction: SafeFraction = Decimal("0.08")
    max_capital_drawdown_fraction: SafeFraction = Decimal("0.25")
    max_arm_ttl_seconds: PositiveInteger = 900

    @field_validator(
        "max_price_deviation_bps",
        "max_equity_change_fraction",
        "max_intraday_loss_fraction",
        "max_capital_drawdown_fraction",
        mode="before",
    )
    @classmethod
    def reject_binary_float(cls, value: object) -> object:
        if isinstance(value, float):
            raise ValueError("risk decimals must not use binary float")
        return value

    @model_validator(mode="after")
    def validate_execution_limits(self) -> Self:
        if not self.max_price_deviation_bps.is_finite() or not (
            Decimal("0") <= self.max_price_deviation_bps <= Decimal("10000")
        ):
            raise ValueError("max_price_deviation_bps must be between 0 and 10000")
        if self.min_order_lifetime_seconds > self.max_order_lifetime_seconds:
            raise ValueError("minimum order lifetime cannot exceed maximum order lifetime")
        if self.poll_interval_seconds > self.max_order_lifetime_seconds:
            raise ValueError("poll interval cannot exceed maximum order lifetime")
        return self


class PromotionSettings(SafeConfigModel):
    """Frozen evidence thresholds required before a real-money mode can be armed."""

    min_shadow_sessions: NonNegativeInteger = 20
    min_shadow_orders: NonNegativeInteger = 50
    max_target_tracking_error: SafeFraction = Decimal("0.05")
    min_canary_sessions: NonNegativeInteger = 3
    min_canary_orders: NonNegativeInteger = 3
    min_canary_fills: NonNegativeInteger = 1
    max_canary_target_tracking_error: SafeFraction = Decimal("0.05")

    @field_validator(
        "max_target_tracking_error",
        "max_canary_target_tracking_error",
        mode="before",
    )
    @classmethod
    def reject_binary_float(cls, value: object) -> object:
        if isinstance(value, float):
            raise ValueError("promotion thresholds must not use binary float")
        return value


class PathSettings(SafeConfigModel):
    """Local state locations; every value is redacted from safe representations."""

    state_directory: Path = Field(default=Path("var/state"), repr=False)
    data_directory: Path = Field(default=Path("var/data"), repr=False)
    report_directory: Path = Field(default=Path("var/reports"), repr=False)
    backup_directory: Path = Field(default=Path("var/backups"), repr=False)
    uquant_source_checkout: Path | None = Field(default=None, repr=False)


class Settings(SafeConfigModel):
    """Top-level deployment settings with cross-field fail-closed validation."""

    schema_version: Literal[1] = 1
    mode: Mode = Mode.PAPER
    live_trading_enabled: bool = Field(default=False, strict=True)
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    broker: BrokerSettings = Field(default_factory=BrokerSettings)
    paths: PathSettings = Field(default_factory=PathSettings)
    compliance: ComplianceSettings = Field(default_factory=ComplianceSettings)
    execution: ExecutionRuntimeSettings = Field(default_factory=ExecutionRuntimeSettings)
    promotion: PromotionSettings = Field(default_factory=PromotionSettings)
    canary_caps: DeploymentCaps | None = None
    live_caps: DeploymentCaps | None = None

    @property
    def active_deployment_caps(self) -> DeploymentCaps | None:
        if self.mode is Mode.CANARY:
            return self.canary_caps
        if self.mode is Mode.LIVE:
            return self.live_caps
        return None

    def xtquant_runtime_blockers(self) -> tuple[str, ...]:
        """Return deterministic local prerequisites without touching proprietary SDK state."""

        if self.mode not in {Mode.SHADOW, Mode.CANARY, Mode.LIVE}:
            return ()
        blockers: list[str] = []
        if self.broker.account_alias is None:
            blockers.append("XTQUANT_ACCOUNT_ALIAS_MISSING")
        if self.broker.xtquant_userdata_path is None:
            blockers.append("XTQUANT_USERDATA_PATH_MISSING")
        if self.broker.session_id is None:
            blockers.append("XTQUANT_SESSION_ID_MISSING")
        if self.broker.safety_manifest_path is None:
            blockers.append("XTQUANT_SAFETY_MANIFEST_MISSING")
        if self.paths.uquant_source_checkout is None:
            blockers.append("UQUANT_SOURCE_CHECKOUT_MISSING")
        return tuple(blockers)

    @model_validator(mode="after")
    def validate_mode_authority(self) -> Self:
        from firmquant.risk.production_policy import ProductionSafetyPolicy

        ProductionSafetyPolicy.from_settings(self)
        read_only_modes = {Mode.REPLAY, Mode.PAPER, Mode.SHADOW}
        real_modes = {Mode.CANARY, Mode.LIVE}
        if self.mode in read_only_modes and self.live_trading_enabled:
            raise ValueError(f"{self.mode.value} cannot enable live trading")
        if self.mode in real_modes and not self.live_trading_enabled:
            raise ValueError(f"{self.mode.value} requires live_trading_enabled=true")

        expected_adapter = {
            Mode.REPLAY: BrokerAdapter.RECORDED_REPLAY,
            Mode.PAPER: BrokerAdapter.PAPER,
            Mode.SHADOW: BrokerAdapter.XTQUANT,
            Mode.CANARY: BrokerAdapter.XTQUANT,
            Mode.LIVE: BrokerAdapter.XTQUANT,
        }[self.mode]
        if self.broker.adapter is not expected_adapter:
            raise ValueError(f"{self.mode.value} requires broker adapter {expected_adapter.value}")

        if self.mode in real_modes and not (
            self.compliance.program_trading_report_confirmed and self.compliance.broker_api_authorized
        ):
            raise ValueError(f"{self.mode.value} requires both compliance confirmations")
        if self.mode is Mode.CANARY and self.canary_caps is None:
            raise ValueError("CANARY deployment caps have no defaults and must all be configured")
        if self.mode is Mode.LIVE and self.live_caps is None:
            raise ValueError("LIVE deployment caps have no defaults and must all be configured")
        return self


def load_settings(path: Path) -> Settings:
    """Load one strict TOML file without environment-variable authority escalation."""

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ConfigurationError(f"cannot read configuration file: {path.name}") from exc
    if len(raw) > 1024 * 1024:
        raise ConfigurationError("configuration file exceeds the 1 MiB safety limit")
    try:
        text = raw.decode("utf-8")
        payload = tomllib.loads(text)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError("configuration file is not valid UTF-8 TOML") from exc
    return Settings.model_validate(payload)


__all__ = (
    "BrokerAdapter",
    "BrokerSettings",
    "ComplianceSettings",
    "ConfigurationError",
    "DeploymentCaps",
    "ExecutionRuntimeSettings",
    "Mode",
    "PathSettings",
    "PromotionSettings",
    "Settings",
    "load_settings",
)
