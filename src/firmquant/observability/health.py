"""Fail-closed, log-safe deployment diagnostics with read-only broker probes."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
import sys
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Never, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from firmquant.broker.gateway import BrokerHealth
from firmquant.broker.xtquant import XtQuantSdkDiagnosis, diagnose_xtquant_sdk
from firmquant.broker.xtquant_safety import XtQuantSafetyManifest
from firmquant.build_identity import load_locked_source_identity
from firmquant.config import BrokerAdapter, Mode, Settings
from firmquant.domain.broker_facts import (
    AccountType,
    BrokerAccountFact,
    BrokerFillFact,
    BrokerOrderFact,
    BrokerPositionFact,
    InstrumentFact,
    MarketSessionStatus,
    QuoteFact,
)
from firmquant.domain.values import Symbol
from firmquant.persistence.database import Database
from firmquant.persistence.schema import CURRENT_SCHEMA_VERSION
from firmquant.persistence.writer_lease import writer_lock_available
from firmquant.scheduling.clock import ClockGuard, ClockObservation, ClockValidationError
from firmquant.security.secrets import SecretBytes, SecretProvider
from firmquant.strategy.identity import StrategyIdentity
from firmquant.strategy.universe import UniversePolicy

_CHECK_NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_SUMMARY = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_SECRET_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")

type DetailValue = str | bool | int | None
type DoctorProbe = Callable[[], "CheckEvidence"]
type ManifestValidator = Callable[[Path], Mapping[str, DetailValue]]

REQUIRED_DOCTOR_CHECKS: tuple[str, ...] = (
    "python-dependencies",
    "uquant-source-identity",
    "data-directory",
    "data-manifest",
    "canonical-universe",
    "configuration",
    "secret-provider",
    "database",
    "single-instance-lock",
    "timezone-clock",
    "broker-sdk",
    "broker-client",
    "readonly-account",
    "program-trading-compliance",
    "live-mode-lock",
)


class DoctorConfigurationError(RuntimeError):
    """The diagnostic composition is incomplete or could hide a required check."""


class ReadOnlyDoctorBroker(Protocol):
    """Complete read authority surface; submit/cancel are deliberately absent."""

    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def health(self) -> BrokerHealth: ...

    def query_account(self) -> BrokerAccountFact: ...

    def query_positions(self) -> tuple[BrokerPositionFact, ...]: ...

    def query_orders(self) -> tuple[BrokerOrderFact, ...]: ...

    def query_fills(self) -> tuple[BrokerFillFact, ...]: ...

    def query_instrument(self, symbol: Symbol) -> InstrumentFact: ...

    def query_quote(self, symbol: Symbol) -> QuoteFact: ...

    def query_market_status(self) -> MarketSessionStatus: ...


def _canonical_details(
    details: Mapping[str, DetailValue],
) -> Mapping[str, DetailValue]:
    if not isinstance(details, Mapping):
        raise DoctorConfigurationError("doctor details must be a mapping")
    copied: dict[str, DetailValue] = {}
    for key, value in details.items():
        if not isinstance(key, str) or _CHECK_NAME.fullmatch(key.replace("_", "-")) is None:
            raise DoctorConfigurationError("doctor detail key is not canonical")
        if value is not None and type(value) not in {str, bool, int}:
            raise DoctorConfigurationError("doctor detail value is not log-safe")
        if isinstance(value, str) and (
            len(value) > 256
            or value != value.strip()
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise DoctorConfigurationError("doctor detail text is not canonical")
        copied[key] = value
    return MappingProxyType(copied)


@dataclass(frozen=True, slots=True)
class CheckEvidence:
    """Probe output before the stable check name is attached."""

    passed: bool
    summary: str
    details: Mapping[str, DetailValue]

    def __post_init__(self) -> None:
        if type(self.passed) is not bool:
            raise DoctorConfigurationError("doctor evidence passed must be bool")
        if not isinstance(self.summary, str) or _SUMMARY.fullmatch(self.summary) is None:
            raise DoctorConfigurationError("doctor evidence summary is not canonical")
        object.__setattr__(self, "details", _canonical_details(self.details))


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One stable, immutable and log-safe operator diagnostic."""

    name: str
    passed: bool
    summary: str
    details: Mapping[str, DetailValue]

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _CHECK_NAME.fullmatch(self.name) is None:
            raise DoctorConfigurationError("doctor result name is not canonical")
        if type(self.passed) is not bool:
            raise DoctorConfigurationError("doctor result passed must be bool")
        if not isinstance(self.summary, str) or _SUMMARY.fullmatch(self.summary) is None:
            raise DoctorConfigurationError("doctor result summary is not canonical")
        object.__setattr__(self, "details", _canonical_details(self.details))


def _evidence(
    passed: bool,
    summary: str,
    **details: DetailValue,
) -> CheckEvidence:
    return CheckEvidence(passed=passed, summary=summary, details=details)


def _reject_json_constant(value: str) -> Never:
    raise ValueError(f"non-standard JSON constant: {value}")


def _json_object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _structural_manifest(path: Path) -> Mapping[str, DetailValue]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("data manifest is unavailable")
    content = path.read_bytes()
    if not content or len(content) > 16 * 1024 * 1024:
        raise ValueError("data manifest size is outside the safety boundary")
    payload: object = json.loads(
        content.decode("utf-8"),
        object_pairs_hook=_json_object_pairs,
        parse_constant=_reject_json_constant,
    )
    if not isinstance(payload, dict) or not payload:
        raise ValueError("data manifest must be a non-empty JSON object")
    return {
        "manifest_sha256": hashlib.sha256(content).hexdigest(),
        "semantic_validation": False,
    }


@contextmanager
def _broker_read_session(broker: ReadOnlyDoctorBroker) -> Iterator[BrokerHealth]:
    """Temporarily connect and always restore a previously disconnected broker."""

    initial = broker.health()
    connected_here = not initial.connected
    try:
        if connected_here:
            broker.connect()
        yield broker.health()
    finally:
        if connected_here:
            broker.disconnect()


class Doctor:
    """Run the complete, fixed deployment preflight without broker write authority."""

    def __init__(self, *, probes: Mapping[str, DoctorProbe]) -> None:
        if set(probes) != set(REQUIRED_DOCTOR_CHECKS):
            raise DoctorConfigurationError("doctor probes must contain exactly the fifteen required checks")
        copied: dict[str, DoctorProbe] = {}
        for name in REQUIRED_DOCTOR_CHECKS:
            probe = probes[name]
            if not callable(probe):
                raise DoctorConfigurationError(f"doctor probe is not callable: {name}")
            copied[name] = probe
        self._probes = MappingProxyType(copied)

    def run_named(self, name: str) -> CheckResult:
        """Run one known check; every ordinary probe exception becomes a safe failure."""

        if name not in self._probes:
            raise DoctorConfigurationError("unknown doctor check")
        try:
            evidence = self._probes[name]()
            if not isinstance(evidence, CheckEvidence):
                raise DoctorConfigurationError("doctor probe returned an invalid evidence type")
        except Exception as error:
            evidence = _evidence(
                False,
                "CHECK_EXCEPTION",
                error_type=type(error).__name__,
            )
        return CheckResult(
            name=name,
            passed=evidence.passed,
            summary=evidence.summary,
            details=evidence.details,
        )

    def run(self) -> tuple[CheckResult, ...]:
        """Run all required checks in stable operator-facing order."""

        return tuple(self.run_named(name) for name in REQUIRED_DOCTOR_CHECKS)

    @classmethod
    def for_local_environment(
        cls,
        settings: Settings,
        *,
        database_path: Path,
        broker: ReadOnlyDoctorBroker | None,
        secret_provider: SecretProvider | None = None,
        required_secret_names: tuple[str, ...] = (),
        data_manifest_path: Path | None = None,
        data_manifest_validator: ManifestValidator | None = None,
        safety_manifest_path: Path | None = None,
        repository_root: Path | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        clock_drift_seconds: Decimal | None = None,
        maximum_clock_drift_seconds: Decimal = Decimal("2"),
        write_capability_present: Callable[[], bool] = lambda: False,
        sdk_diagnosis: Callable[[], XtQuantSdkDiagnosis] = diagnose_xtquant_sdk,
    ) -> Doctor:
        """Compose all checks from explicit local dependencies and read-only ports."""

        if not isinstance(settings, Settings):
            raise DoctorConfigurationError("doctor settings must be validated Settings")
        if not callable(clock) or not callable(write_capability_present) or not callable(sdk_diagnosis):
            raise DoctorConfigurationError("doctor runtime probes must be callable")
        if (
            not isinstance(maximum_clock_drift_seconds, Decimal)
            or not maximum_clock_drift_seconds.is_finite()
            or maximum_clock_drift_seconds < 0
        ):
            raise DoctorConfigurationError("maximum clock drift must be a nonnegative Decimal")
        if (
            not isinstance(required_secret_names, tuple)
            or len(set(required_secret_names)) != len(required_secret_names)
            or any(
                not isinstance(name, str) or _SECRET_NAME.fullmatch(name) is None
                for name in required_secret_names
            )
        ):
            raise DoctorConfigurationError("required secret names must be a unique canonical tuple")
        ledger_path = Path(database_path)
        manifest_path = (
            Path(data_manifest_path)
            if data_manifest_path is not None
            else settings.paths.data_directory / "manifest.json"
        )
        manifest_validator = data_manifest_validator or _structural_manifest
        real_mode = settings.mode in {Mode.CANARY, Mode.LIVE}
        production_mode = settings.mode in {Mode.SHADOW, Mode.CANARY, Mode.LIVE}
        xtquant_required = settings.broker.adapter is BrokerAdapter.XTQUANT
        safety_manifest: XtQuantSafetyManifest | None = None
        if xtquant_required:
            manifest_source = safety_manifest_path or settings.broker.safety_manifest_path
            if manifest_source is not None:
                safety_manifest = XtQuantSafetyManifest.load(Path(manifest_source))

        def python_dependencies() -> CheckEvidence:
            dependencies = ("firmquant", "uquant", "pydantic", "tzdata")
            versions = tuple(importlib.metadata.version(name) for name in dependencies)
            passed = sys.version_info[:2] == (3, 12) and all(versions)
            return _evidence(
                passed,
                "PYTHON_DEPENDENCIES_OK" if passed else "PYTHON_DEPENDENCIES_INVALID",
                python=f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
                dependency_count=len(versions),
            )

        def uquant_identity() -> CheckEvidence:
            identity = StrategyIdentity.locked()
            identity.verify()
            source = load_locked_source_identity()
            if repository_root is not None:
                source.verify_firmquant_files(Path(repository_root))
            return _evidence(
                True,
                "UQUANT_IDENTITY_VERIFIED",
                uquant_commit=identity.uquant_commit,
                wheel_sha256=identity.wheel_sha256,
            )

        def data_directory() -> CheckEvidence:
            path = settings.paths.data_directory
            passed = path.is_dir() and not path.is_symlink()
            return _evidence(
                passed,
                "DATA_DIRECTORY_READY" if passed else "DATA_DIRECTORY_INVALID",
                exists=path.exists(),
                regular_directory=passed,
            )

        def data_manifest() -> CheckEvidence:
            details = dict(manifest_validator(manifest_path))
            semantic = details.get("semantic_validation") is True
            sufficient = semantic or settings.mode in {Mode.REPLAY, Mode.PAPER}
            details["required_semantic"] = settings.mode not in {Mode.REPLAY, Mode.PAPER}
            return CheckEvidence(
                passed=sufficient,
                summary="DATA_MANIFEST_VERIFIED" if sufficient else "DATA_MANIFEST_SEMANTICS_UNVERIFIED",
                details=details,
            )

        def canonical_universe() -> CheckEvidence:
            observed_at = clock()
            if observed_at.tzinfo is None or observed_at.utcoffset() is None:
                raise ValueError("doctor clock is not timezone-aware")
            universe = UniversePolicy.from_uquant(
                None,
                as_of=observed_at.astimezone(ZoneInfo(settings.timezone)).date(),
            )
            return _evidence(
                True,
                "CANONICAL_UNIVERSE_VERIFIED",
                universe_sha256=universe.manifest_sha256,
                symbol_count=len(universe.deployment_symbols),
            )

        def configuration() -> CheckEvidence:
            safe = settings.safe_repr().encode("utf-8")
            return _evidence(
                True,
                "CONFIGURATION_VALID",
                mode=settings.mode.value,
                live_trading_enabled=settings.live_trading_enabled,
                configuration_sha256=hashlib.sha256(safe).hexdigest(),
            )

        def secrets() -> CheckEvidence:
            if required_secret_names and secret_provider is None:
                return _evidence(
                    False,
                    "SECRET_PROVIDER_UNAVAILABLE",
                    provider_configured=False,
                    required_count=len(required_secret_names),
                )
            if secret_provider is not None:
                for name in required_secret_names:
                    material = secret_provider.get_secret(name)
                    if not isinstance(material, SecretBytes):
                        raise TypeError("secret provider returned an invalid type")
            return _evidence(
                True,
                "SECRET_PROVIDER_READY",
                provider_configured=secret_provider is not None,
                required_count=len(required_secret_names),
            )

        def database() -> CheckEvidence:
            opened = Database.open_read_only(ledger_path)
            try:
                opened.integrity_check()
                journal = opened.scalar("PRAGMA journal_mode")
                foreign_keys = opened.scalar("PRAGMA foreign_keys")
                synchronous = opened.scalar("PRAGMA synchronous")
                schema = opened.scalar("SELECT max(version) FROM schema_migrations")
            finally:
                opened.close()
            passed = (
                isinstance(journal, str)
                and journal.casefold() == "wal"
                and foreign_keys == 1
                and synchronous == 2
                and schema == CURRENT_SCHEMA_VERSION
            )
            return _evidence(
                passed,
                "DATABASE_DURABLE" if passed else "DATABASE_PRAGMA_INVALID",
                wal=isinstance(journal, str) and journal.casefold() == "wal",
                foreign_keys=foreign_keys == 1,
                synchronous_full=synchronous == 2,
                schema_current=schema == CURRENT_SCHEMA_VERSION,
            )

        def single_instance() -> CheckEvidence:
            observed_at = clock()
            if observed_at.tzinfo is None or observed_at.utcoffset() is None:
                raise ValueError("doctor clock is not timezone-aware")
            opened = Database.open_read_only(ledger_path)
            try:
                row = opened.query_one(
                    "SELECT expires_at, generation FROM writer_leases WHERE singleton_id = 1"
                )
            finally:
                opened.close()
            durable_available = True
            generation = 0
            if row is not None:
                expires_at = datetime.fromisoformat(str(row["expires_at"]))
                if expires_at.tzinfo is None or expires_at.utcoffset() is None:
                    raise ValueError("stored writer lease expiry is not timezone-aware")
                durable_available = expires_at <= observed_at
                generation = int(row["generation"])
            os_lock_available = writer_lock_available(ledger_path)
            passed = durable_available and os_lock_available
            return _evidence(
                passed,
                "WRITER_LEASE_AVAILABLE" if passed else "WRITER_LEASE_BUSY",
                durable_available=durable_available,
                os_lock_available=os_lock_available,
                generation=generation,
            )

        def timezone_clock() -> CheckEvidence:
            try:
                timezone = ZoneInfo(settings.timezone)
            except ZoneInfoNotFoundError as error:
                raise ValueError("configured timezone is unavailable") from error
            current = clock()
            if current.tzinfo is None or current.utcoffset() is None:
                raise ValueError("doctor clock is not timezone-aware")
            if production_mode:
                if broker is None or safety_manifest is None:
                    return _evidence(
                        False,
                        "CLOCK_DRIFT_UNVERIFIED",
                        timezone=timezone.key,
                        drift_verified=False,
                    )
                try:
                    with _broker_read_session(broker):
                        quote = broker.query_quote(safety_manifest.probe_symbol)
                    receipt = ClockGuard(
                        max_drift=timedelta(milliseconds=int(maximum_clock_drift_seconds * 1000))
                    ).verify(
                        ClockObservation(
                            system_time=current,
                            reference_time=quote.event_time,
                            local_timezone=settings.timezone,
                        )
                    )
                except ClockValidationError:
                    return _evidence(
                        False,
                        "CLOCK_DRIFT_EXCEEDED",
                        timezone=timezone.key,
                        drift_verified=True,
                    )
                return _evidence(
                    True,
                    "TIMEZONE_CLOCK_VERIFIED",
                    timezone=timezone.key,
                    drift_verified=True,
                    drift_milliseconds=receipt.drift_milliseconds,
                    clock_receipt_sha256=receipt.sha256,
                )
            if (
                clock_drift_seconds is None
                or not isinstance(clock_drift_seconds, Decimal)
                or not clock_drift_seconds.is_finite()
            ):
                return _evidence(
                    False,
                    "CLOCK_DRIFT_UNVERIFIED",
                    timezone=timezone.key,
                    drift_verified=False,
                )
            passed = abs(clock_drift_seconds) <= maximum_clock_drift_seconds
            return _evidence(
                passed,
                "TIMEZONE_CLOCK_VERIFIED" if passed else "CLOCK_DRIFT_EXCEEDED",
                timezone=timezone.key,
                drift_verified=True,
                drift_milliseconds=int(clock_drift_seconds * 1000),
            )

        def broker_sdk() -> CheckEvidence:
            diagnosis = sdk_diagnosis()
            if not isinstance(diagnosis, XtQuantSdkDiagnosis):
                raise TypeError("SDK diagnosis returned an invalid type")
            passed = diagnosis.available or not xtquant_required
            return _evidence(
                passed,
                "BROKER_SDK_READY"
                if passed
                else "XTQUANT_SDK_UNAVAILABLE"
                if xtquant_required
                else "BROKER_SDK_UNAVAILABLE",
                adapter=settings.broker.adapter.value,
                required=xtquant_required,
                available=diagnosis.available,
                readonly_smoke_completed=diagnosis.readonly_smoke_completed,
                real_order_calls=diagnosis.real_order_calls,
            )

        def broker_client() -> CheckEvidence:
            if broker is None:
                return _evidence(False, "BROKER_CLIENT_UNAVAILABLE", configured=False)
            with _broker_read_session(broker) as health:
                passed = health.connected and health.read_healthy
                return _evidence(
                    passed,
                    "BROKER_CLIENT_READ_HEALTHY" if passed else "BROKER_CLIENT_UNHEALTHY",
                    configured=True,
                    connected=health.connected,
                    read_healthy=health.read_healthy,
                    diagnostic_code=health.diagnostic_code,
                )

        def readonly_account() -> CheckEvidence:
            if broker is None:
                return _evidence(False, "READONLY_ACCOUNT_UNAVAILABLE", configured=False)
            if production_mode and safety_manifest is None:
                return _evidence(False, "XTQUANT_SAFETY_MANIFEST_UNAVAILABLE", configured=True)
            probe = safety_manifest.probe_symbol if safety_manifest is not None else Symbol.parse("000001.SZ")
            with _broker_read_session(broker):
                account = broker.query_account()
                positions = broker.query_positions()
                orders = broker.query_orders()
                fills = broker.query_fills()
                market_status = broker.query_market_status()
                instrument = broker.query_instrument(probe)
                quote = broker.query_quote(probe)
                health = broker.health()
            alias_configured = settings.broker.account_alias is not None or not production_mode
            manifest_verified = safety_manifest is not None or not production_mode
            passed = (
                isinstance(account, BrokerAccountFact)
                and account.account_type is AccountType.CASH
                and account.available_cash.value >= 0
                and isinstance(positions, tuple)
                and all(isinstance(item, BrokerPositionFact) for item in positions)
                and isinstance(orders, tuple)
                and all(isinstance(item, BrokerOrderFact) for item in orders)
                and isinstance(fills, tuple)
                and all(isinstance(item, BrokerFillFact) for item in fills)
                and isinstance(market_status, MarketSessionStatus)
                and isinstance(instrument, InstrumentFact)
                and isinstance(quote, QuoteFact)
                and alias_configured
                and manifest_verified
                and health.connected
                and health.read_healthy
            )
            return _evidence(
                passed,
                "READONLY_ACCOUNT_VERIFIED" if passed else "READONLY_ACCOUNT_INVALID",
                configured=True,
                account_type=account.account_type.value,
                cash_nonnegative=account.available_cash.value >= 0,
                position_count=len(positions),
                order_count=len(orders),
                fill_count=len(fills),
                market_status=market_status.value,
                instrument_symbol=instrument.symbol.canonical,
                quote_symbol=quote.symbol.canonical,
                account_alias_configured=alias_configured,
                safety_manifest_verified=manifest_verified,
                real_order_calls=0,
            )

        def compliance() -> CheckEvidence:
            confirmed = (
                settings.compliance.program_trading_report_confirmed
                and settings.compliance.broker_api_authorized
            )
            passed = confirmed or not real_mode
            summary = (
                "COMPLIANCE_CONFIRMED"
                if confirmed
                else "COMPLIANCE_MISSING"
                if real_mode
                else "COMPLIANCE_NOT_REQUIRED_READONLY"
            )
            return _evidence(
                passed,
                summary,
                required=real_mode,
                program_trading_report_confirmed=(settings.compliance.program_trading_report_confirmed),
                broker_api_authorized=settings.compliance.broker_api_authorized,
            )

        def live_lock() -> CheckEvidence:
            capability = write_capability_present()
            if type(capability) is not bool:
                raise TypeError("write capability presence probe must return bool")
            passed = not capability
            return _evidence(
                passed,
                "LIVE_WRITE_LOCKED" if passed else "LIVE_WRITE_CAPABILITY_PRESENT",
                mode=settings.mode.value,
                live_trading_enabled=settings.live_trading_enabled,
                write_capability=capability,
            )

        return cls(
            probes={
                "python-dependencies": python_dependencies,
                "uquant-source-identity": uquant_identity,
                "data-directory": data_directory,
                "data-manifest": data_manifest,
                "canonical-universe": canonical_universe,
                "configuration": configuration,
                "secret-provider": secrets,
                "database": database,
                "single-instance-lock": single_instance,
                "timezone-clock": timezone_clock,
                "broker-sdk": broker_sdk,
                "broker-client": broker_client,
                "readonly-account": readonly_account,
                "program-trading-compliance": compliance,
                "live-mode-lock": live_lock,
            }
        )


__all__ = (
    "REQUIRED_DOCTOR_CHECKS",
    "CheckEvidence",
    "CheckResult",
    "Doctor",
    "DoctorConfigurationError",
    "ReadOnlyDoctorBroker",
)
