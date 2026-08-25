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
            rendered[name] = (
                "<redacted>" if name in self.sensitive_fields else self._safe_value(value)
            )
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


class BrokerSettings(SafeConfigModel):
    """Broker selection and local references; never broker credentials."""

    sensitive_fields: ClassVar[frozenset[str]] = frozenset(
        {"account_alias", "xtquant_userdata_path"}
    )

    adapter: BrokerAdapter = BrokerAdapter.PAPER
    account_alias: str | None = Field(default=None, min_length=1, max_length=128, repr=False)
    xtquant_userdata_path: Path | None = Field(default=None, repr=False)


class ComplianceSettings(SafeConfigModel):
    """Explicit operator attestations required before real broker writes."""

    program_trading_report_confirmed: bool = Field(default=False, strict=True)
    broker_api_authorized: bool = Field(default=False, strict=True)


PositiveNotional = Annotated[
    Decimal,
    Field(gt=Decimal("0"), max_digits=20, decimal_places=4),
]


class DeploymentCaps(SafeConfigModel):
    """CANARY-only deployment safety caps; none are strategy parameters."""

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


class PathSettings(SafeConfigModel):
    """Local state locations; every value is redacted from safe representations."""

    state_directory: Path = Field(default=Path("var/state"), repr=False)
    data_directory: Path = Field(default=Path("var/data"), repr=False)
    report_directory: Path = Field(default=Path("var/reports"), repr=False)
    backup_directory: Path = Field(default=Path("var/backups"), repr=False)


class Settings(SafeConfigModel):
    """Top-level deployment settings with cross-field fail-closed validation."""

    schema_version: Literal[1] = 1
    mode: Mode = Mode.PAPER
    live_trading_enabled: bool = Field(default=False, strict=True)
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    broker: BrokerSettings = Field(default_factory=BrokerSettings)
    paths: PathSettings = Field(default_factory=PathSettings)
    compliance: ComplianceSettings = Field(default_factory=ComplianceSettings)
    canary_caps: DeploymentCaps | None = None

    @model_validator(mode="after")
    def validate_mode_authority(self) -> Self:
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
            raise ValueError(
                f"{self.mode.value} requires broker adapter {expected_adapter.value}"
            )

        if self.mode in real_modes and not (
            self.compliance.program_trading_report_confirmed
            and self.compliance.broker_api_authorized
        ):
            raise ValueError(f"{self.mode.value} requires both compliance confirmations")
        if self.mode is Mode.CANARY and self.canary_caps is None:
            raise ValueError("CANARY deployment caps have no defaults and must all be configured")
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
    "Mode",
    "PathSettings",
    "Settings",
    "load_settings",
)
