"""Audited local operator use cases behind the command-line control plane."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess  # nosec B404
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Never, Protocol, runtime_checkable

from firmquant.broker.replay import RecordedReplayBroker
from firmquant.build_identity import load_locked_source_identity
from firmquant.config import Mode, PathSettings, Settings, load_settings
from firmquant.domain.states import RuntimeState, RuntimeStatus
from firmquant.observability.health import Doctor, ReadOnlyDoctorBroker
from firmquant.persistence.audit import AuditLedger
from firmquant.persistence.backup import backup_state, verify_backup
from firmquant.persistence.database import Database
from firmquant.persistence.repositories import canonical_json
from firmquant.persistence.writer_lease import WriterLease
from firmquant.risk.arm import ArmBinding, ArmLease, ArmService
from firmquant.risk.capability import BrokerWriteCapability
from firmquant.scheduling.sessions import WorkflowReceiptStore
from firmquant.security.redaction import redact
from firmquant.security.secrets import EnvironmentSecretProvider, SecretProvider
from firmquant.strategy.identity import StrategyIdentity

_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_RECONCILIATION_ID = re.compile(r"^recon_[0-9a-f]{64}$")
_REASON_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_ACTIVE_BROKER_ORDER_STATES = (
    "PENDING_NEW",
    "ACKNOWLEDGED",
    "PARTIALLY_FILLED",
    "PENDING_CANCEL",
)
_CI_KEYS = frozenset(
    {
        "CI",
        "GITHUB_ACTIONS",
        "GITLAB_CI",
        "TF_BUILD",
        "JENKINS_URL",
        "BUILDKITE",
        "CIRCLECI",
    }
)
_DEFAULT_CONFIG = """# firmquant 本地配置; 初始化只创建无法实盘下单的 PAPER 模式。
schema_version = 1
mode = "PAPER"
live_trading_enabled = false
timezone = "Asia/Shanghai"

[broker]
adapter = "PAPER"

[paths]
state_directory = "var/state"
data_directory = "var/data"
report_directory = "var/reports"
backup_directory = "var/backups"

[compliance]
program_trading_report_confirmed = false
broker_api_authorized = false
"""

type RunPort = Callable[[Mode], Mapping[str, object]]
type ReconciliationPort = Callable[[Database], "OperatorReconciliation"]
type ReportPort = Callable[[date | None, Database], Mapping[str, object]]
type AccountBootstrapPort = Callable[[Path | None], Mapping[str, object]]
type DurableCancellationExecutor = Callable[[BrokerWriteCapability, tuple[str, ...]], tuple[str, ...]]
type DoctorBrokerProvider = Callable[[], ReadOnlyDoctorBroker | None]
type FirmquantCommitProvider = Callable[[], str]
type Clock = Callable[[], datetime]


class OperatorCommand(StrEnum):
    """Stable local control operations; values exactly match the CLI surface."""

    INIT = "init"
    DOCTOR = "doctor"
    RUN = "run"
    STATUS = "status"
    ARM_LIVE = "arm-live"
    DISARM = "disarm"
    HALT = "halt"
    RESUME = "resume"
    RECONCILE = "reconcile"
    BOOTSTRAP_ACCOUNT = "bootstrap-account"
    DECISIONS = "decisions"
    ORDERS = "orders"
    FILLS = "fills"
    REPORT = "report"
    REPLAY = "replay"
    BACKUP = "backup"
    VERIFY_BACKUP = "verify-backup"
    CANCEL_SYSTEM_ORDERS = "cancel-system-orders"


class OperatorCommandDenied(RuntimeError):
    """A fail-closed command denial containing only a stable safe code."""

    def __init__(self, reason_code: str) -> None:
        if not isinstance(reason_code, str) or _REASON_CODE.fullmatch(reason_code) is None:
            raise ValueError("operator denial reason code is not canonical")
        self.reason_code = reason_code
        super().__init__(reason_code)


def _reject_json_constant(value: str) -> Never:
    raise ValueError(f"non-standard JSON constant: {value}")


def _safe_payload(payload: Mapping[str, object]) -> Mapping[str, object]:
    if not isinstance(payload, Mapping) or not all(isinstance(key, str) for key in payload):
        raise TypeError("operator result payload must be a text-keyed mapping")
    protected = redact(dict(payload))
    if not isinstance(protected, dict):
        raise TypeError("operator result redaction did not return an object")
    encoded = json.dumps(
        protected,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    decoded: object = json.loads(encoded, parse_constant=_reject_json_constant)
    if not isinstance(decoded, dict):
        raise TypeError("operator result payload root must be an object")
    return MappingProxyType(decoded)


def _canonical_reason(value: str | None) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 256
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("operator reason must be canonical text")
    return value


@dataclass(frozen=True, slots=True)
class OperatorRequest:
    """Strict parsed command values; no secret or confirmation phrase is retained."""

    command: OperatorCommand
    output_json: bool = False
    mode: Mode | None = None
    session: date | None = None
    events_path: Path | None = None
    bundle_path: Path | None = None
    account_state_path: Path | None = None
    ttl_seconds: int = 300
    limit: int = 100
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.command, OperatorCommand):
            raise TypeError("operator command must be typed")
        if type(self.output_json) is not bool:
            raise TypeError("operator JSON output flag must be bool")
        if self.mode is not None and not isinstance(self.mode, Mode):
            raise TypeError("operator run mode must be typed")
        if self.session is not None and type(self.session) is not date:
            raise TypeError("operator session must be a date")
        for value in (self.events_path, self.bundle_path, self.account_state_path):
            if value is not None and not isinstance(value, Path):
                raise TypeError("operator path values must be pathlib.Path")
        if (
            isinstance(self.ttl_seconds, bool)
            or not isinstance(self.ttl_seconds, int)
            or not 1 <= self.ttl_seconds <= 900
        ):
            raise ValueError("operator arm TTL must be between 1 and 900 seconds")
        if isinstance(self.limit, bool) or not isinstance(self.limit, int) or not 1 <= self.limit <= 1000:
            raise ValueError("operator query limit must be between 1 and 1000")
        object.__setattr__(self, "reason", _canonical_reason(self.reason))


@dataclass(frozen=True, slots=True)
class OperatorInteraction:
    """Ephemeral terminal input boundary deliberately excluded from representations."""

    interactive_terminal: bool
    confirmation_reader: Callable[[str], str]
    environment: Mapping[str, str]

    def __post_init__(self) -> None:
        if type(self.interactive_terminal) is not bool:
            raise TypeError("operator terminal flag must be bool")
        if not callable(self.confirmation_reader):
            raise TypeError("operator confirmation reader must be callable")
        if not isinstance(self.environment, Mapping) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in self.environment.items()
        ):
            raise TypeError("operator environment must be text mapping")
        object.__setattr__(self, "environment", MappingProxyType(dict(self.environment)))

    def __repr__(self) -> str:
        return "<OperatorInteraction redacted>"


@dataclass(frozen=True, slots=True)
class OperatorResult:
    """Log-safe application response rendered by the CLI."""

    message: str
    payload: Mapping[str, object]
    exit_code: int = 0

    def __post_init__(self) -> None:
        if (
            not isinstance(self.message, str)
            or not self.message
            or self.message != self.message.strip()
            or any(ord(character) < 32 or ord(character) == 127 for character in self.message)
        ):
            raise ValueError("operator result message must be canonical text")
        if isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int):
            raise TypeError("operator result exit code must be integer")
        if not 0 <= self.exit_code <= 255:
            raise ValueError("operator result exit code is outside process range")
        object.__setattr__(self, "payload", _safe_payload(self.payload))


@dataclass(frozen=True, slots=True)
class OperatorReconciliation:
    """Minimal application evidence returned by a complete reconciliation port."""

    reconciliation_id: str
    passed: bool
    blockers: tuple[str, ...]

    def __post_init__(self) -> None:
        if _RECONCILIATION_ID.fullmatch(self.reconciliation_id) is None:
            raise ValueError("operator reconciliation id is not canonical")
        if type(self.passed) is not bool:
            raise TypeError("operator reconciliation passed flag must be bool")
        if not isinstance(self.blockers, tuple) or tuple(sorted(set(self.blockers))) != self.blockers:
            raise ValueError("operator reconciliation blockers must be sorted and unique")
        if any(
            not isinstance(blocker, str) or not blocker or blocker != blocker.strip() or len(blocker) > 128
            for blocker in self.blockers
        ):
            raise ValueError("operator reconciliation blocker is not canonical")
        if self.passed != (not self.blockers):
            raise ValueError("operator reconciliation outcome contradicts blockers")


@runtime_checkable
class OperatorService(Protocol):
    """Only business boundary reachable from the local CLI parser."""

    def execute(
        self,
        request: OperatorRequest,
        interaction: OperatorInteraction,
    ) -> OperatorResult: ...


@runtime_checkable
class SystemOrderCancellationPort(Protocol):
    """Mode-specific cancellation use case; real implementations must be capability-bound."""

    def cancel_system_orders(self, broker_order_ids: tuple[str, ...]) -> tuple[str, ...]: ...


class CapabilityBoundSystemOrderCanceller:
    """Opaque real-order cancellation port constructed only with write capability."""

    __slots__ = ("_capability", "_executor")

    def __init__(
        self,
        *,
        capability: BrokerWriteCapability,
        durable_executor: DurableCancellationExecutor,
    ) -> None:
        if not isinstance(capability, BrokerWriteCapability):
            raise TypeError("real cancellation requires BrokerWriteCapability")
        if not callable(durable_executor):
            raise TypeError("real cancellation durable executor must be callable")
        self._capability = capability
        self._executor = durable_executor

    def cancel_system_orders(self, broker_order_ids: tuple[str, ...]) -> tuple[str, ...]:
        return self._executor(self._capability, broker_order_ids)

    def __repr__(self) -> str:
        return "<CapabilityBoundSystemOrderCanceller redacted>"


def _clean_git_commit(repository_root: Path) -> str:
    executable = shutil.which("git")
    if executable is None:
        return "UNKNOWN"
    try:
        completed = subprocess.run(  # nosec B603
            [executable, "rev-parse", "HEAD^{commit}"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
        )
        status = subprocess.run(  # nosec B603
            [executable, "status", "--porcelain", "--untracked-files=all"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "UNKNOWN"
    commit = completed.stdout.strip()
    return commit if _SHA1.fullmatch(commit) is not None and not status.stdout.strip() else "UNKNOWN"


def _default_firmquant_commit() -> str:
    repository_root = Path(__file__).resolve().parents[3]
    commit = _clean_git_commit(repository_root)
    if commit == "UNKNOWN":
        return commit
    try:
        load_locked_source_identity().verify_firmquant_files(repository_root)
    except Exception:
        return "UNKNOWN"
    return commit


def _clock_value(clock: Clock) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise OperatorCommandDenied("CLOCK_UNAVAILABLE")
    return value


def _ci_detected(environment: Mapping[str, str]) -> bool:
    return any(
        (value := environment.get(key)) is not None
        and value.strip().casefold() not in {"", "0", "false", "no"}
        for key in _CI_KEYS
    )


class LocalOperatorService:
    """Single-account local operations backed by the durable SQLite ledger."""

    def __init__(
        self,
        *,
        config_path: Path,
        clock: Clock = lambda: datetime.now(UTC),
        firmquant_commit_provider: FirmquantCommitProvider = _default_firmquant_commit,
        secret_provider: SecretProvider | None = None,
        runner: RunPort | None = None,
        reconciler: ReconciliationPort | None = None,
        reporter: ReportPort | None = None,
        account_bootstrapper: AccountBootstrapPort | None = None,
        doctor_broker_provider: DoctorBrokerProvider | None = None,
        system_order_canceller: SystemOrderCancellationPort | None = None,
    ) -> None:
        if not isinstance(config_path, Path):
            raise TypeError("operator configuration path must be pathlib.Path")
        if not callable(clock) or not callable(firmquant_commit_provider):
            raise TypeError("operator clock and commit provider must be callable")
        for dependency in (
            runner,
            reconciler,
            reporter,
            account_bootstrapper,
            doctor_broker_provider,
        ):
            if dependency is not None and not callable(dependency):
                raise TypeError("operator optional ports must be callable")
        if system_order_canceller is not None and not isinstance(
            system_order_canceller, SystemOrderCancellationPort
        ):
            raise TypeError("operator cancellation port does not satisfy its contract")
        self._config_path = config_path
        self._clock = clock
        self._firmquant_commit_provider = firmquant_commit_provider
        self._secret_provider = secret_provider
        self._runner = runner
        self._reconciler = reconciler
        self._reporter = reporter
        self._account_bootstrapper = account_bootstrapper
        self._doctor_broker_provider = doctor_broker_provider
        self._system_order_canceller = system_order_canceller

    def execute(
        self,
        request: OperatorRequest,
        interaction: OperatorInteraction,
    ) -> OperatorResult:
        if not isinstance(request, OperatorRequest):
            raise TypeError("operator request must be typed")
        if not isinstance(interaction, OperatorInteraction):
            raise TypeError("operator interaction must be typed")
        handlers: dict[OperatorCommand, Callable[[], OperatorResult]] = {
            OperatorCommand.INIT: lambda: self._initialize(),
            OperatorCommand.DOCTOR: lambda: self._doctor(),
            OperatorCommand.RUN: lambda: self._run(request),
            OperatorCommand.STATUS: lambda: self._status(interaction),
            OperatorCommand.ARM_LIVE: lambda: self._arm_live(request, interaction),
            OperatorCommand.DISARM: lambda: self._disarm(request),
            OperatorCommand.HALT: lambda: self._halt(request),
            OperatorCommand.RESUME: lambda: self._resume(interaction),
            OperatorCommand.RECONCILE: lambda: self._reconcile(),
            OperatorCommand.BOOTSTRAP_ACCOUNT: lambda: self._bootstrap_account(request),
            OperatorCommand.DECISIONS: lambda: self._decisions(request),
            OperatorCommand.ORDERS: lambda: self._orders(request),
            OperatorCommand.FILLS: lambda: self._fills(request),
            OperatorCommand.REPORT: lambda: self._report(request),
            OperatorCommand.REPLAY: lambda: self._replay(request),
            OperatorCommand.BACKUP: lambda: self._backup(request),
            OperatorCommand.VERIFY_BACKUP: lambda: self._verify_backup(request),
            OperatorCommand.CANCEL_SYSTEM_ORDERS: lambda: self._cancel_system_orders(),
        }
        return handlers[request.command]()

    def _now(self) -> datetime:
        return _clock_value(self._clock)

    def _settings(self) -> Settings:
        if self._config_path.is_symlink() or not self._config_path.is_file():
            raise OperatorCommandDenied("CONFIGURATION_UNAVAILABLE")
        try:
            return load_settings(self._config_path)
        except Exception as error:
            raise OperatorCommandDenied("CONFIGURATION_INVALID") from error

    def _resolved(self, path: Path) -> Path:
        return path if path.is_absolute() else self._config_path.parent / path

    def _resolved_paths(self, settings: Settings) -> PathSettings:
        return PathSettings(
            state_directory=self._resolved(settings.paths.state_directory),
            data_directory=self._resolved(settings.paths.data_directory),
            report_directory=self._resolved(settings.paths.report_directory),
            backup_directory=self._resolved(settings.paths.backup_directory),
        )

    def _database_path(self, settings: Settings) -> Path:
        return self._resolved(settings.paths.state_directory) / "firmquant.sqlite3"

    def _configuration_sha256(self) -> str:
        try:
            content = self._config_path.read_bytes()
        except OSError as error:
            raise OperatorCommandDenied("CONFIGURATION_UNAVAILABLE") from error
        return hashlib.sha256(content).hexdigest()

    def _firmquant_commit(self) -> str:
        try:
            value = self._firmquant_commit_provider()
        except Exception as error:
            raise OperatorCommandDenied("FIRMQUANT_IDENTITY_UNAVAILABLE") from error
        if not isinstance(value, str) or _SHA1.fullmatch(value) is None:
            raise OperatorCommandDenied("FIRMQUANT_IDENTITY_UNAVAILABLE")
        return value

    @staticmethod
    def _event_id(command: OperatorCommand, now: datetime) -> str:
        nonce = os.urandom(16)
        digest = hashlib.sha256(command.value.encode() + now.isoformat().encode() + nonce).hexdigest()
        return f"operator:{command.value}:{digest}"

    @staticmethod
    def _ensure_directory(path: Path) -> None:
        if path.is_symlink():
            raise OperatorCommandDenied("STATE_PATH_INVALID")
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise OperatorCommandDenied("STATE_PATH_UNAVAILABLE") from error
        if not path.is_dir() or path.is_symlink():
            raise OperatorCommandDenied("STATE_PATH_INVALID")

    def _create_default_config(self) -> None:
        parent = self._config_path.parent
        self._ensure_directory(parent)
        if self._config_path.exists() or self._config_path.is_symlink():
            return
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self._config_path, flags, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(_DEFAULT_CONFIG)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            return
        except OSError as error:
            raise OperatorCommandDenied("CONFIGURATION_CREATE_FAILED") from error

    def _append_operator_audit(
        self,
        lease: WriterLease,
        *,
        command: OperatorCommand,
        payload: Mapping[str, object],
        created_at: datetime,
    ) -> None:
        lease.assert_current()
        with lease.database.transaction():
            AuditLedger(lease.database).append(
                audit_event_id=self._event_id(command, created_at),
                category="OPERATOR",
                actor="local-cli",
                payload={"schema": "firmquant.operator-command.v1", **dict(payload)},
                created_at=created_at,
            )

    def _initialize(self) -> OperatorResult:
        self._create_default_config()
        settings = self._settings()
        paths = self._resolved_paths(settings)
        for path in (
            paths.state_directory,
            paths.data_directory,
            paths.report_directory,
            paths.backup_directory,
        ):
            self._ensure_directory(path)
        database_path = paths.state_directory / "firmquant.sqlite3"
        with WriterLease.acquire(
            database_path,
            owner="operator-init",
            clock=self._clock,
        ) as lease:
            AuditLedger(lease.database).verify()
            existing = lease.database.scalar(
                "SELECT count(*) FROM audit_events WHERE category = 'OPERATOR' "
                "AND json_extract(payload_json, '$.command') = 'init'"
            )
            if existing == 0:
                self._append_operator_audit(
                    lease,
                    command=OperatorCommand.INIT,
                    payload={
                        "command": OperatorCommand.INIT.value,
                        "mode": settings.mode.value,
                        "configuration_sha256": self._configuration_sha256(),
                    },
                    created_at=self._now(),
                )
        return OperatorResult(
            message="本地状态已初始化; 没有执行券商写操作。",
            payload={
                "initialized": True,
                "mode": settings.mode.value,
                "live_trading_enabled": settings.live_trading_enabled,
                "database": "firmquant.sqlite3",
            },
        )

    def _doctor(self) -> OperatorResult:
        settings = self._settings()
        paths = self._resolved_paths(settings)
        resolved_settings = settings.model_copy(update={"paths": paths})
        broker: ReadOnlyDoctorBroker | None = None
        if self._doctor_broker_provider is not None:
            broker = self._doctor_broker_provider()
        doctor = Doctor.for_local_environment(
            resolved_settings,
            database_path=paths.state_directory / "firmquant.sqlite3",
            broker=broker,
            repository_root=Path(__file__).resolve().parents[3],
            clock=self._clock,
            clock_drift_seconds=None,
        )
        results = doctor.run()
        payload = {
            "passed": all(result.passed for result in results),
            "checks": [
                {
                    "name": result.name,
                    "passed": result.passed,
                    "summary": result.summary,
                    "details": dict(result.details),
                }
                for result in results
            ],
        }
        return OperatorResult(
            message="诊断完成; 失败项会继续阻止实盘。",
            payload=payload,
            exit_code=0 if payload["passed"] else 2,
        )

    def _run(self, request: OperatorRequest) -> OperatorResult:
        settings = self._settings()
        mode = settings.mode if request.mode is None else request.mode
        if mode is not settings.mode:
            raise OperatorCommandDenied("RUN_MODE_CONFIG_MISMATCH")
        if self._runner is None:
            raise OperatorCommandDenied("RUNTIME_COMPOSITION_UNAVAILABLE")
        payload = self._runner(mode)
        return OperatorResult(message="运行 session 已结束。", payload=payload)

    @staticmethod
    def _runtime_from_row(database: Database, fallback_mode: Mode) -> tuple[Mode, RuntimeStatus]:
        row = database.query_one("SELECT * FROM runtime_state WHERE singleton_id = 1")
        if row is None:
            return fallback_mode, RuntimeStatus.initial()
        try:
            mode = Mode(str(row["mode"]))
            parsed: object = json.loads(
                str(row["blockers_json"]),
                parse_constant=_reject_json_constant,
            )
            if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
                raise ValueError
            status = RuntimeStatus(
                state=RuntimeState(str(row["state"])),
                revision=int(row["revision"]),
                reason=str(row["reason"]),
                blockers=tuple(parsed),
            )
        except (TypeError, ValueError) as error:
            raise OperatorCommandDenied("RUNTIME_STATE_INVALID") from error
        return mode, status

    @staticmethod
    def _latest_snapshot(database: Database) -> sqlite3.Row | None:
        return database.query_one(
            "SELECT * FROM broker_snapshots ORDER BY captured_at DESC, snapshot_id DESC LIMIT 1"
        )

    @staticmethod
    def _unresolved_orders(database: Database) -> int:
        execution_count = database.scalar(
            "SELECT count(*) FROM execution_intents WHERE state IN ('SUBMITTING','UNKNOWN')"
        )
        attempt_count = database.scalar("SELECT count(*) FROM broker_order_attempts WHERE state = 'UNKNOWN'")
        return LocalOperatorService._count(execution_count) + LocalOperatorService._count(attempt_count)

    @staticmethod
    def _count(value: object | None) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise OperatorCommandDenied("DATABASE_STATE_INVALID")
        return value

    @staticmethod
    def _external_active_orders(database: Database) -> int:
        value = database.scalar(
            "SELECT count(*) FROM broker_orders WHERE ownership != 'SYSTEM' AND status IN (?,?,?,?)",
            _ACTIVE_BROKER_ORDER_STATES,
        )
        return LocalOperatorService._count(value)

    @staticmethod
    def _kill_switch_tripped(database: Database, status: RuntimeStatus | None = None) -> bool:
        row = database.query_one(
            "SELECT code FROM risk_events WHERE code IN "
            "('KILL_SWITCH_TRIPPED','KILL_SWITCH_RESET') ORDER BY created_at DESC, rowid DESC LIMIT 1"
        )
        if row is not None:
            return str(row["code"]) == "KILL_SWITCH_TRIPPED"
        return status is not None and "KILL_SWITCH" in status.blockers

    def _arm_status(
        self,
        database: Database,
        *,
        settings: Settings,
        firmquant_commit: str,
        now: datetime,
        interaction: OperatorInteraction,
    ) -> tuple[bool, str | None, tuple[str, ...]]:
        row = database.query_one(
            "SELECT * FROM arm_leases WHERE revoked_at IS NULL ORDER BY issued_at DESC, lease_id DESC LIMIT 1"
        )
        if row is None:
            return False, None, ()
        try:
            issued_at = datetime.fromisoformat(str(row["issued_at"]))
            expires_at = datetime.fromisoformat(str(row["expires_at"]))
            if (
                issued_at.tzinfo is None
                or issued_at.utcoffset() is None
                or expires_at.tzinfo is None
                or expires_at.utcoffset() is None
            ):
                raise ValueError
        except ValueError as error:
            raise OperatorCommandDenied("ARM_LEASE_INVALID") from error
        blockers: list[str] = []
        snapshot = self._latest_snapshot(database)
        expected_account = None if snapshot is None else str(snapshot["account_id_hash"])
        expected_host = hashlib.sha256(socket.gethostname().encode("utf-8")).hexdigest()
        identity: StrategyIdentity | None = None
        try:
            identity = StrategyIdentity.locked()
            identity.verify()
        except Exception:
            blockers.append("UQUANT_IDENTITY_UNAVAILABLE")
        if now >= expires_at:
            blockers.append("ARM_LEASE_EXPIRED")
        if str(row["mode"]) != settings.mode.value:
            blockers.append("ARM_MODE_CHANGED")
        if str(row["host_hash"]) != expected_host:
            blockers.append("ARM_HOST_CHANGED")
        if expected_account is None or str(row["account_hash"]) != expected_account:
            blockers.append("ARM_ACCOUNT_CHANGED")
        if str(row["firmquant_commit"]) != firmquant_commit:
            blockers.append("ARM_CODE_CHANGED")
        if identity is None or str(row["uquant_commit"]) != identity.uquant_commit:
            blockers.append("UQUANT_CODE_IDENTITY_DRIFT")
        if str(row["config_sha256"]) != self._configuration_sha256():
            blockers.append("CONFIG_CHANGED_AFTER_ARM")
        if expected_account is not None and identity is not None:
            try:
                binding = ArmBinding(
                    mode=settings.mode,
                    host_hash=expected_host,
                    account_hash=expected_account,
                    firmquant_commit=firmquant_commit,
                    uquant_commit=identity.uquant_commit,
                    config_sha256=self._configuration_sha256(),
                )
                lease = ArmLease(
                    lease_id=str(row["lease_id"]),
                    mode=Mode(str(row["mode"])),
                    host_hash=str(row["host_hash"]),
                    account_hash=str(row["account_hash"]),
                    firmquant_commit=str(row["firmquant_commit"]),
                    uquant_commit=str(row["uquant_commit"]),
                    config_sha256=str(row["config_sha256"]),
                    identity_payload_sha256=str(row["identity_payload_sha256"]),
                    issued_at=issued_at,
                    expires_at=expires_at,
                    lease_mac=str(row["lease_mac"]),
                )
                arm_service = ArmService(
                    mac_key=self._secret_provider_for(interaction).get_secret("ARM_MAC_KEY")
                )
                arm_service.verify(lease, binding=binding, now=now)
            except Exception:
                blockers.append("ARM_LEASE_AUTHENTICATION_FAILED")
        return not blockers, expires_at.astimezone(UTC).isoformat(), tuple(sorted(blockers))

    @staticmethod
    def _gross_values(database: Database) -> tuple[str | None, str | None]:
        snapshot = database.query_one(
            """
            SELECT b.snapshot_id, c.available_cash, c.total_assets
            FROM broker_snapshots b JOIN cash_snapshots c ON c.snapshot_id = b.snapshot_id
            ORDER BY b.captured_at DESC, b.snapshot_id DESC LIMIT 1
            """
        )
        if snapshot is None:
            return None, None
        try:
            cash = Decimal(str(snapshot["available_cash"]))
            assets = Decimal(str(snapshot["total_assets"]))
            market_rows = database.query_all(
                "SELECT market_value FROM position_snapshots WHERE snapshot_id = ?",
                (str(snapshot["snapshot_id"]),),
            )
            gross_value = sum(
                (Decimal(str(row["market_value"])) for row in market_rows),
                start=Decimal(0),
            )
        except (InvalidOperation, ValueError) as error:
            raise OperatorCommandDenied("ACCOUNT_SNAPSHOT_INVALID") from error
        if not all(value.is_finite() and value >= 0 for value in (cash, assets, gross_value)):
            raise OperatorCommandDenied("ACCOUNT_SNAPSHOT_INVALID")
        gross = Decimal(0) if assets == 0 else gross_value / assets
        return format(cash, "f"), format(gross.normalize(), "f")

    @staticmethod
    def _target_gross(database: Database) -> str | None:
        row = database.query_one(
            "SELECT payload_json FROM decision_snapshots ORDER BY strategy_session DESC, created_at DESC LIMIT 1"
        )
        if row is None:
            return None
        try:
            payload: object = json.loads(
                str(row["payload_json"]),
                parse_constant=_reject_json_constant,
            )
            if not isinstance(payload, dict):
                raise ValueError
            upstream = payload.get("uquant_payload")
            if not isinstance(upstream, dict):
                raise ValueError
            value = upstream.get("target_gross")
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                raise ValueError
            target = Decimal(str(value))
            if not target.is_finite() or target < 0:
                raise ValueError
        except (InvalidOperation, ValueError) as error:
            raise OperatorCommandDenied("DECISION_SNAPSHOT_INVALID") from error
        return format(target.normalize(), "f")

    def _status_snapshot(
        self,
        database: Database,
        *,
        settings: Settings,
        interaction: OperatorInteraction,
    ) -> Mapping[str, object]:
        AuditLedger(database).verify()
        now = self._now()
        stored_mode, status = self._runtime_from_row(database, settings.mode)
        try:
            firmquant_commit = self._firmquant_commit()
        except OperatorCommandDenied:
            firmquant_commit = "UNKNOWN"
        arm_blockers: tuple[str, ...]
        if firmquant_commit == "UNKNOWN":
            armed, expires_at, arm_blockers = False, None, ("FIRMQUANT_IDENTITY_UNAVAILABLE",)
        else:
            armed, expires_at, arm_blockers = self._arm_status(
                database,
                settings=settings,
                firmquant_commit=firmquant_commit,
                now=now,
                interaction=interaction,
            )
        unresolved = self._unresolved_orders(database)
        external = self._external_active_orders(database)
        blockers = set(status.blockers) | set(arm_blockers)
        if stored_mode is not settings.mode:
            blockers.add("CONFIG_MODE_DRIFT")
        if unresolved:
            blockers.add("UNRESOLVED_ORDER_STATE")
        if external:
            blockers.add("EXTERNAL_BROKER_ORDER")
        latest_reconciliation = database.query_one(
            "SELECT completed_at, passed, blockers_json FROM reconciliation_runs "
            "ORDER BY started_at DESC, reconciliation_id DESC LIMIT 1"
        )
        if latest_reconciliation is not None and latest_reconciliation["passed"] != 1:
            blockers.add("RECONCILIATION_MISMATCH")
        current_cash, actual_gross = self._gross_values(database)
        strategy_session = database.scalar(
            "SELECT strategy_session FROM decision_snapshots "
            "ORDER BY strategy_session DESC, created_at DESC LIMIT 1"
        )
        last_quote = database.scalar("SELECT max(received_at) FROM broker_events WHERE event_type = 'QUOTE'")
        source = load_locked_source_identity()
        heartbeat = database.query_one(
            "SELECT * FROM production_heartbeat WHERE singleton_id = 1"
        )
        heartbeat_age: float | None = None
        process_health = "NOT_RUNNING"
        broker_connection = "NOT_RUNNING"
        broker_read_healthy = False
        broker_write_healthy = False
        if heartbeat is None:
            blockers.add("PROCESS_NOT_RUNNING")
        else:
            try:
                heartbeat_at = datetime.fromisoformat(str(heartbeat["observed_at"]))
                if heartbeat_at.tzinfo is None or heartbeat_at.utcoffset() is None:
                    raise ValueError
                age = now - heartbeat_at
                heartbeat_age = age.total_seconds()
                if heartbeat_age < 0:
                    raise ValueError
            except ValueError as error:
                raise OperatorCommandDenied("HEARTBEAT_INVALID") from error
            broker_connection = (
                "CONNECTED" if int(heartbeat["broker_connected"]) == 1 else "DISCONNECTED"
            )
            broker_read_healthy = int(heartbeat["broker_read_healthy"]) == 1
            broker_write_healthy = int(heartbeat["broker_write_healthy"]) == 1
            if heartbeat_age > 30.0:
                process_health = "STALE"
                blockers.add("HEARTBEAT_STALE")
            else:
                process_health = "HEALTHY"
        effective_state = (
            status.state.value if process_health == "HEALTHY" else RuntimeState.HALTED.value
        )
        return {
            "mode": settings.mode.value,
            "runtime_state": effective_state,
            "stored_runtime_state": status.state.value,
            "process_health": process_health,
            "heartbeat_age": heartbeat_age,
            "armed": armed,
            "arm_expires_at": expires_at,
            "firmquant_commit": firmquant_commit,
            "uquant_commit": source.uquant_commit,
            "strategy_session": strategy_session,
            "broker_connection": broker_connection,
            "broker_read_healthy": broker_read_healthy,
            "broker_write_healthy": broker_write_healthy,
            "last_quote": (last_quote if heartbeat is None else heartbeat["last_quote"]),
            "last_reconciliation": (
                None if heartbeat is None else heartbeat["last_reconciliation"]
            ),
            "last_broker_event": None if heartbeat is None else heartbeat["last_broker_event"],
            "last_decision": None if heartbeat is None else heartbeat["last_decision"],
            "last_execution": None if heartbeat is None else heartbeat["last_execution"],
            "control_request_state": None if heartbeat is None else heartbeat["control_request_state"],
            "writer_generation": None if heartbeat is None else heartbeat["writer_generation"],
            "process_id": None if heartbeat is None else heartbeat["process_id"],
            "host_hash": None if heartbeat is None else heartbeat["host_hash"],
            "pending_events": None if heartbeat is None else heartbeat["pending_events"],
            "unresolved_orders": unresolved,
            "current_cash": current_cash,
            "actual_gross": actual_gross,
            "target_gross": self._target_gross(database),
            "kill_switch": self._kill_switch_tripped(database, status),
            "blockers": sorted(blockers),
        }

    def _status(self, interaction: OperatorInteraction) -> OperatorResult:
        settings = self._settings()
        database_path = self._database_path(settings)
        if not database_path.is_file() or database_path.is_symlink():
            raise OperatorCommandDenied("INITIALIZATION_REQUIRED")
        database = Database.open_read_only(database_path)
        try:
            with database.transaction("DEFERRED"):
                payload = self._status_snapshot(
                    database,
                    settings=settings,
                    interaction=interaction,
                )
        finally:
            database.close()
        return OperatorResult(message="运行状态已读取。", payload=payload)

    @staticmethod
    def _shadow_validated(database: Database) -> bool:
        rows = database.query_all(
            "SELECT payload_json FROM audit_events WHERE category = 'RUNTIME' ORDER BY sequence"
        )
        for row in rows:
            try:
                payload: object = json.loads(
                    str(row["payload_json"]),
                    parse_constant=_reject_json_constant,
                )
            except (json.JSONDecodeError, ValueError):
                return False
            if (
                isinstance(payload, dict)
                and payload.get("mode") == Mode.SHADOW.value
                and payload.get("state") == RuntimeState.READY.value
            ):
                return True
        return False

    @staticmethod
    def _active_arm_preconditions(database: Database, now: datetime) -> str:
        row = database.query_one(
            "SELECT account_id_hash, captured_at FROM broker_snapshots "
            "ORDER BY captured_at DESC, snapshot_id DESC LIMIT 1"
        )
        if row is None:
            raise OperatorCommandDenied("BROKER_SNAPSHOT_MISSING")
        try:
            captured_at = datetime.fromisoformat(str(row["captured_at"]))
            if captured_at.tzinfo is None or captured_at.utcoffset() is None:
                raise ValueError
        except ValueError as error:
            raise OperatorCommandDenied("BROKER_SNAPSHOT_INVALID") from error
        age = now - captured_at
        if age < timedelta(0) or age > timedelta(minutes=5):
            raise OperatorCommandDenied("BROKER_SNAPSHOT_STALE")
        return str(row["account_id_hash"])

    def _secret_provider_for(self, interaction: OperatorInteraction) -> SecretProvider:
        if self._secret_provider is not None:
            return self._secret_provider
        return EnvironmentSecretProvider(environment=interaction.environment)

    def _arm_live(
        self,
        request: OperatorRequest,
        interaction: OperatorInteraction,
    ) -> OperatorResult:
        if not interaction.interactive_terminal:
            raise OperatorCommandDenied("ARM_INTERACTIVE_TERMINAL_REQUIRED")
        if _ci_detected(interaction.environment):
            raise OperatorCommandDenied("ARM_FORBIDDEN_IN_CI")
        settings = self._settings()
        if settings.mode not in {Mode.CANARY, Mode.LIVE}:
            raise OperatorCommandDenied("MODE_NOT_LIVE_WRITABLE")
        now = self._now()
        database_path = self._database_path(settings)
        with WriterLease.acquire(
            database_path,
            owner="operator-arm-live",
            clock=self._clock,
        ) as writer:
            database = writer.database
            AuditLedger(database).verify()
            stored_mode, status = self._runtime_from_row(database, settings.mode)
            if stored_mode is not settings.mode or status.state is not RuntimeState.READY:
                raise OperatorCommandDenied("STARTUP_RECONCILIATION_REQUIRED")
            if self._kill_switch_tripped(database, status):
                raise OperatorCommandDenied("KILL_SWITCH_TRIPPED")
            reconciliation = database.query_one(
                "SELECT passed, blockers_json FROM reconciliation_runs "
                "WHERE kind = 'STARTUP' ORDER BY started_at DESC, reconciliation_id DESC LIMIT 1"
            )
            if reconciliation is None or reconciliation["passed"] != 1:
                raise OperatorCommandDenied("STARTUP_RECONCILIATION_REQUIRED")
            if not self._shadow_validated(database):
                raise OperatorCommandDenied("SHADOW_VALIDATION_REQUIRED")
            if self._unresolved_orders(database):
                raise OperatorCommandDenied("UNRESOLVED_ORDER_STATE")
            if self._external_active_orders(database):
                raise OperatorCommandDenied("EXTERNAL_BROKER_ORDER")
            account_hash = self._active_arm_preconditions(database, now)
            firmquant_commit = self._firmquant_commit()
            try:
                identity = StrategyIdentity.locked()
                identity.verify()
            except Exception as error:
                raise OperatorCommandDenied("UQUANT_IDENTITY_UNAVAILABLE") from error
            binding = ArmBinding(
                mode=settings.mode,
                host_hash=writer.host_hash,
                account_hash=account_hash,
                firmquant_commit=firmquant_commit,
                uquant_commit=identity.uquant_commit,
                config_sha256=self._configuration_sha256(),
            )
            try:
                arm_service = ArmService(
                    mac_key=self._secret_provider_for(interaction).get_secret("ARM_MAC_KEY")
                )
            except Exception as error:
                raise OperatorCommandDenied("ARM_SECRET_UNAVAILABLE") from error
            phrase = arm_service.confirmation_phrase(settings.mode)
            try:
                arm_lease = arm_service.issue(
                    binding,
                    now=now,
                    interactive_terminal=interaction.interactive_terminal,
                    environment=interaction.environment,
                    confirmation_reader=lambda: interaction.confirmation_reader(f"请输入确认短语: {phrase}"),
                    ttl=timedelta(seconds=request.ttl_seconds),
                )
            except Exception as error:
                raise OperatorCommandDenied("ARM_CONFIRMATION_REJECTED") from error
            writer.assert_current()
            with database.transaction():
                database.write(
                    "UPDATE arm_leases SET revoked_at = ?, revoke_reason = ? WHERE revoked_at IS NULL",
                    (now.isoformat(), "superseded by explicit arm"),
                )
                database.write(
                    """
                    INSERT INTO arm_leases(
                        lease_id, mode, host_hash, account_hash, firmquant_commit,
                        uquant_commit, config_sha256, identity_payload_sha256,
                        issued_at, expires_at, revoked_at, revoke_reason, lease_mac
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
                    """,
                    (
                        arm_lease.lease_id,
                        arm_lease.mode.value,
                        arm_lease.host_hash,
                        arm_lease.account_hash,
                        arm_lease.firmquant_commit,
                        arm_lease.uquant_commit,
                        arm_lease.config_sha256,
                        arm_lease.identity_payload_sha256,
                        arm_lease.issued_at.isoformat(),
                        arm_lease.expires_at.isoformat(),
                        arm_lease.lease_mac,
                    ),
                )
                AuditLedger(database).append(
                    audit_event_id=self._event_id(OperatorCommand.ARM_LIVE, now),
                    category="ARM",
                    actor="local-cli",
                    payload={
                        "schema": "firmquant.arm-operation.v1",
                        "command": OperatorCommand.ARM_LIVE.value,
                        "lease_id": arm_lease.lease_id,
                        "mode": arm_lease.mode.value,
                        "identity_payload_sha256": arm_lease.identity_payload_sha256,
                        "expires_at": arm_lease.expires_at.astimezone(UTC).isoformat(),
                    },
                    created_at=now,
                )
        return OperatorResult(
            message="短时实盘 lease 已创建; 每次券商写操作仍会重新校验全部门禁。",
            payload={
                "armed": True,
                "lease_id": arm_lease.lease_id,
                "mode": arm_lease.mode.value,
                "expires_at": arm_lease.expires_at.astimezone(UTC).isoformat(),
            },
        )

    @staticmethod
    def _save_transition(
        receipts: WorkflowReceiptStore,
        *,
        mode: Mode,
        previous: RuntimeStatus,
        target: RuntimeState,
        reason: str,
        blockers: tuple[str, ...],
        created_at: datetime,
    ) -> RuntimeStatus:
        current = previous.transition(target, reason=reason, blockers=blockers)
        if current != previous:
            receipts.save_runtime(
                mode=mode,
                previous=previous,
                current=current,
                created_at=created_at,
            )
        return current

    def _runtime_control(
        self,
        writer: WriterLease,
        *,
        fallback_mode: Mode,
    ) -> tuple[Mode, RuntimeStatus, WorkflowReceiptStore]:
        mode, status = self._runtime_from_row(writer.database, fallback_mode)
        return mode, status, WorkflowReceiptStore(writer_lease=writer)

    def _transition_halted(
        self,
        receipts: WorkflowReceiptStore,
        *,
        mode: Mode,
        status: RuntimeStatus,
        reason: str,
        blockers: tuple[str, ...],
        now: datetime,
    ) -> RuntimeStatus:
        if status.state is RuntimeState.STOPPING:
            status = self._save_transition(
                receipts,
                mode=mode,
                previous=status,
                target=RuntimeState.DISARMED,
                reason="operator stop completed before halt",
                blockers=(),
                created_at=now,
            )
        if status.state is RuntimeState.DISARMED:
            status = self._save_transition(
                receipts,
                mode=mode,
                previous=status,
                target=RuntimeState.STARTING,
                reason="operator emergency control started",
                blockers=(),
                created_at=now,
            )
        return self._save_transition(
            receipts,
            mode=mode,
            previous=status,
            target=RuntimeState.HALTED,
            reason=reason,
            blockers=tuple(sorted(set(blockers))),
            created_at=now,
        )

    def _disarm(self, request: OperatorRequest) -> OperatorResult:
        settings = self._settings()
        now = self._now()
        with WriterLease.acquire(
            self._database_path(settings),
            owner="operator-disarm",
            clock=self._clock,
        ) as writer:
            mode, status, receipts = self._runtime_control(writer, fallback_mode=settings.mode)
            tripped = self._kill_switch_tripped(writer.database, status)
            with writer.database.transaction():
                cursor = writer.database.write(
                    "UPDATE arm_leases SET revoked_at = ?, revoke_reason = ? WHERE revoked_at IS NULL",
                    (now.isoformat(), "explicit operator disarm"),
                )
            if status.state is not RuntimeState.DISARMED:
                if status.state is not RuntimeState.STOPPING:
                    status = self._save_transition(
                        receipts,
                        mode=mode,
                        previous=status,
                        target=RuntimeState.STOPPING,
                        reason="operator disarm requested",
                        blockers=status.blockers,
                        created_at=now,
                    )
                status = self._save_transition(
                    receipts,
                    mode=mode,
                    previous=status,
                    target=RuntimeState.DISARMED,
                    reason="operator disarmed",
                    blockers=("KILL_SWITCH",) if tripped else (),
                    created_at=now,
                )
            self._append_operator_audit(
                writer,
                command=OperatorCommand.DISARM,
                payload={
                    "command": OperatorCommand.DISARM.value,
                    "revoked_lease_count": cursor.rowcount,
                    "reason_sha256": hashlib.sha256(
                        (request.reason or "explicit operator disarm").encode("utf-8")
                    ).hexdigest(),
                },
                created_at=now,
            )
        return OperatorResult(
            message="实盘 lease 已撤销; 没有执行券商写操作。",
            payload={
                "armed": False,
                "revoked_lease_count": cursor.rowcount,
                "runtime_state": status.state.value,
            },
        )

    def _halt(self, request: OperatorRequest) -> OperatorResult:
        settings = self._settings()
        now = self._now()
        reason_sha256 = hashlib.sha256(
            (request.reason or "operator emergency stop").encode("utf-8")
        ).hexdigest()
        with WriterLease.acquire(
            self._database_path(settings),
            owner="operator-halt",
            clock=self._clock,
        ) as writer:
            mode, status, receipts = self._runtime_control(writer, fallback_mode=settings.mode)
            with writer.database.transaction():
                writer.database.write(
                    "UPDATE arm_leases SET revoked_at = ?, revoke_reason = ? WHERE revoked_at IS NULL",
                    (now.isoformat(), "operator kill switch"),
                )
            status = self._transition_halted(
                receipts,
                mode=mode,
                status=status,
                reason="operator kill switch",
                blockers=tuple(sorted(set(status.blockers) | {"KILL_SWITCH"})),
                now=now,
            )
            event_id = self._event_id(OperatorCommand.HALT, now)
            with writer.database.transaction():
                payload = {
                    "schema": "firmquant.kill-switch.v1",
                    "reason_sha256": reason_sha256,
                }
                payload_json = canonical_json(payload)
                writer.database.write(
                    """
                    INSERT INTO risk_events(
                        risk_event_id, severity, code, execution_id, symbol,
                        payload_json, payload_sha256, created_at
                    ) VALUES (?, 'CRITICAL', 'KILL_SWITCH_TRIPPED', NULL, NULL, ?, ?, ?)
                    """,
                    (
                        event_id,
                        payload_json,
                        hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
                        now.isoformat(),
                    ),
                )
                AuditLedger(writer.database).append(
                    audit_event_id=event_id + ":audit",
                    category="RISK",
                    actor="local-cli",
                    payload={
                        "schema": "firmquant.kill-switch.v1",
                        "command": OperatorCommand.HALT.value,
                        "reason_sha256": reason_sha256,
                        "runtime_state": status.state.value,
                    },
                    created_at=now,
                )
        return OperatorResult(
            message="kill switch 已触发, 系统进入 HALTED; 未自动清仓。",
            payload={
                "runtime_state": status.state.value,
                "kill_switch": True,
                "armed": False,
                "blockers": list(status.blockers),
            },
        )

    @staticmethod
    def _resume_phrase() -> str:
        return "RESUME FIRMQUANT AFTER RECONCILIATION"

    def _resume(self, interaction: OperatorInteraction) -> OperatorResult:
        if not interaction.interactive_terminal:
            raise OperatorCommandDenied("RESUME_INTERACTIVE_TERMINAL_REQUIRED")
        phrase = self._resume_phrase()
        if interaction.confirmation_reader(f"请输入确认短语: {phrase}") != phrase:
            raise OperatorCommandDenied("RESUME_CONFIRMATION_REJECTED")
        if self._reconciler is None:
            raise OperatorCommandDenied("RECONCILIATION_PORT_UNAVAILABLE")
        settings = self._settings()
        now = self._now()
        with WriterLease.acquire(
            self._database_path(settings),
            owner="operator-resume",
            clock=self._clock,
        ) as writer:
            mode, status, receipts = self._runtime_control(writer, fallback_mode=settings.mode)
            if status.state is not RuntimeState.HALTED:
                raise OperatorCommandDenied("RUNTIME_NOT_HALTED")
            kill_switch = self._kill_switch_tripped(writer.database, status)
            with writer.database.transaction():
                revoked = writer.database.write(
                    "UPDATE arm_leases SET revoked_at = ?, revoke_reason = ? WHERE revoked_at IS NULL",
                    (now.isoformat(), "resume requires explicit re-arm"),
                )
                AuditLedger(writer.database).append(
                    audit_event_id=self._event_id(OperatorCommand.RESUME, now),
                    category="ARM",
                    actor="local-cli",
                    payload={
                        "schema": "firmquant.arm-operation.v1",
                        "command": OperatorCommand.RESUME.value,
                        "action": "REVOKE_FOR_RESUME",
                        "revoked_lease_count": revoked.rowcount,
                    },
                    created_at=now,
                )

            def failure_blockers(blockers: tuple[str, ...]) -> tuple[str, ...]:
                observed = set(blockers)
                if kill_switch:
                    observed.add("KILL_SWITCH")
                return tuple(sorted(observed))

            status = self._save_transition(
                receipts,
                mode=mode,
                previous=status,
                target=RuntimeState.RECONCILING,
                reason="explicit resume reconciliation",
                blockers=(),
                created_at=now,
            )
            try:
                raw_reconciliation: object = self._reconciler(writer.database)
            except Exception as error:
                status = self._transition_halted(
                    receipts,
                    mode=mode,
                    status=status,
                    reason="resume reconciliation raised an exception",
                    blockers=failure_blockers(("RECONCILIATION_EXCEPTION",)),
                    now=self._now(),
                )
                raise OperatorCommandDenied("RECONCILIATION_FAILED") from error
            if not isinstance(raw_reconciliation, OperatorReconciliation):
                self._transition_halted(
                    receipts,
                    mode=mode,
                    status=status,
                    reason="resume reconciliation returned invalid evidence",
                    blockers=failure_blockers(("RECONCILIATION_RESULT_INVALID",)),
                    now=self._now(),
                )
                raise OperatorCommandDenied("RECONCILIATION_RESULT_INVALID")
            reconciliation = raw_reconciliation
            if not reconciliation.passed:
                self._transition_halted(
                    receipts,
                    mode=mode,
                    status=status,
                    reason="resume reconciliation did not pass",
                    blockers=failure_blockers(reconciliation.blockers),
                    now=self._now(),
                )
                raise OperatorCommandDenied("RECONCILIATION_FAILED")
            reset_at = self._now()
            if kill_switch:
                event_id = self._event_id(OperatorCommand.RESUME, reset_at)
                with writer.database.transaction():
                    payload_json = canonical_json(
                        {
                            "schema": "firmquant.kill-switch.v1",
                            "reconciliation_id": reconciliation.reconciliation_id,
                        }
                    )
                    writer.database.write(
                        """
                        INSERT INTO risk_events(
                            risk_event_id, severity, code, execution_id, symbol,
                            payload_json, payload_sha256, created_at
                        ) VALUES (?, 'INFO', 'KILL_SWITCH_RESET', NULL, NULL, ?, ?, ?)
                        """,
                        (
                            event_id,
                            payload_json,
                            hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
                            reset_at.isoformat(),
                        ),
                    )
                    AuditLedger(writer.database).append(
                        audit_event_id=event_id + ":audit",
                        category="RISK",
                        actor="local-cli",
                        payload={
                            "schema": "firmquant.kill-switch.v1",
                            "command": OperatorCommand.RESUME.value,
                            "reconciliation_id": reconciliation.reconciliation_id,
                        },
                        created_at=reset_at,
                    )
            else:
                self._append_operator_audit(
                    writer,
                    command=OperatorCommand.RESUME,
                    payload={
                        "command": OperatorCommand.RESUME.value,
                        "reconciliation_id": reconciliation.reconciliation_id,
                    },
                    created_at=reset_at,
                )
            status = self._save_transition(
                receipts,
                mode=mode,
                previous=status,
                target=RuntimeState.READY,
                reason="resume reconciliation passed",
                blockers=(),
                created_at=reset_at,
            )
        return OperatorResult(
            message="重新对账通过, 运行状态恢复 READY; 实盘仍需重新 arm。",
            payload={
                "runtime_state": status.state.value,
                "reconciliation_id": reconciliation.reconciliation_id,
                "armed": False,
            },
        )

    def _bootstrap_account(self, request: OperatorRequest) -> OperatorResult:
        if self._account_bootstrapper is None:
            raise OperatorCommandDenied("ACCOUNT_BOOTSTRAP_PORT_UNAVAILABLE")
        try:
            payload = self._account_bootstrapper(request.account_state_path)
        except OperatorCommandDenied:
            raise
        except Exception as error:
            raise OperatorCommandDenied("ACCOUNT_BOOTSTRAP_FAILED") from error
        if not isinstance(payload, Mapping):
            raise OperatorCommandDenied("ACCOUNT_BOOTSTRAP_RESULT_INVALID")
        return OperatorResult(
            message="账户权威基线已建立; 未发送任何券商写请求。",
            payload=payload,
        )

    def _reconcile(self) -> OperatorResult:
        if self._reconciler is None:
            raise OperatorCommandDenied("RECONCILIATION_PORT_UNAVAILABLE")
        settings = self._settings()
        now = self._now()
        with WriterLease.acquire(
            self._database_path(settings),
            owner="operator-reconcile",
            clock=self._clock,
        ) as writer:
            mode, status, receipts = self._runtime_control(writer, fallback_mode=settings.mode)
            was_halted = status.state is RuntimeState.HALTED
            kill_switch = self._kill_switch_tripped(writer.database, status)
            if status.state is RuntimeState.STOPPING:
                raise OperatorCommandDenied("RUNTIME_STOPPING")
            if status.state is RuntimeState.DISARMED:
                status = self._save_transition(
                    receipts,
                    mode=mode,
                    previous=status,
                    target=RuntimeState.STARTING,
                    reason="manual reconciliation requested",
                    blockers=(),
                    created_at=now,
                )
            if status.state is not RuntimeState.RECONCILING:
                status = self._save_transition(
                    receipts,
                    mode=mode,
                    previous=status,
                    target=RuntimeState.RECONCILING,
                    reason="manual reconciliation running",
                    blockers=(),
                    created_at=now,
                )
            try:
                raw_reconciliation: object = self._reconciler(writer.database)
            except Exception as error:
                self._transition_halted(
                    receipts,
                    mode=mode,
                    status=status,
                    reason="manual reconciliation raised an exception",
                    blockers=("RECONCILIATION_EXCEPTION",),
                    now=self._now(),
                )
                raise OperatorCommandDenied("RECONCILIATION_FAILED") from error
            if not isinstance(raw_reconciliation, OperatorReconciliation):
                self._transition_halted(
                    receipts,
                    mode=mode,
                    status=status,
                    reason="manual reconciliation returned invalid evidence",
                    blockers=("RECONCILIATION_RESULT_INVALID",),
                    now=self._now(),
                )
                raise OperatorCommandDenied("RECONCILIATION_RESULT_INVALID")
            reconciliation = raw_reconciliation
            if not reconciliation.passed or kill_switch or was_halted:
                observed_blockers = set(reconciliation.blockers)
                if kill_switch:
                    observed_blockers.add("KILL_SWITCH")
                if was_halted:
                    observed_blockers.add("EXPLICIT_RESUME_REQUIRED")
                status = self._transition_halted(
                    receipts,
                    mode=mode,
                    status=status,
                    reason="manual reconciliation requires explicit resume",
                    blockers=tuple(sorted(observed_blockers)),
                    now=self._now(),
                )
            else:
                status = self._save_transition(
                    receipts,
                    mode=mode,
                    previous=status,
                    target=RuntimeState.READY,
                    reason="manual reconciliation passed",
                    blockers=(),
                    created_at=self._now(),
                )
        return OperatorResult(
            message="完整对账已执行。",
            payload={
                "reconciliation_id": reconciliation.reconciliation_id,
                "passed": reconciliation.passed,
                "blockers": list(status.blockers),
                "runtime_state": status.state.value,
            },
            exit_code=0 if status.state is RuntimeState.READY else 2,
        )

    def _open_read_database(self) -> tuple[Settings, Database]:
        settings = self._settings()
        path = self._database_path(settings)
        if not path.is_file() or path.is_symlink():
            raise OperatorCommandDenied("INITIALIZATION_REQUIRED")
        database = Database.open_read_only(path)
        try:
            AuditLedger(database).verify()
        except Exception:
            database.close()
            raise
        return settings, database

    def _decisions(self, request: OperatorRequest) -> OperatorResult:
        _, database = self._open_read_database()
        try:
            if request.session is None:
                rows = database.query_all(
                    "SELECT decision_id, strategy_session, input_fingerprint, payload_sha256, "
                    "created_at, supersedes_decision_id FROM decision_snapshots "
                    "ORDER BY strategy_session DESC, created_at DESC LIMIT ?",
                    (request.limit,),
                )
            else:
                rows = database.query_all(
                    "SELECT decision_id, strategy_session, input_fingerprint, payload_sha256, "
                    "created_at, supersedes_decision_id FROM decision_snapshots "
                    "WHERE strategy_session = ? "
                    "ORDER BY strategy_session DESC, created_at DESC LIMIT ?",
                    (request.session.isoformat(), request.limit),
                )
            items = [
                {
                    "decision_id": row["decision_id"],
                    "strategy_session": row["strategy_session"],
                    "input_fingerprint": row["input_fingerprint"],
                    "payload_sha256": row["payload_sha256"],
                    "created_at": row["created_at"],
                    "supersedes_decision_id": row["supersedes_decision_id"],
                }
                for row in rows
            ]
        finally:
            database.close()
        return OperatorResult(
            message="决策快照已读取。",
            payload={"count": len(items), "decisions": items},
        )

    def _orders(self, request: OperatorRequest) -> OperatorResult:
        _, database = self._open_read_database()
        try:
            rows = database.query_all(
                """
                SELECT i.execution_id, i.decision_id, i.uquant_order_id, i.symbol, i.side,
                       i.requested_shares, i.filled_shares, i.state, i.strategy_session,
                       b.broker_order_id, b.status AS broker_status, b.ownership
                FROM execution_intents i
                LEFT JOIN broker_orders b ON b.execution_id = i.execution_id
                ORDER BY i.created_at DESC, i.execution_id DESC LIMIT ?
                """,
                (request.limit,),
            )
            items = [{key: row[key] for key in row} for row in rows]
        finally:
            database.close()
        return OperatorResult(
            message="订单生命周期已读取。",
            payload={"count": len(items), "orders": items},
        )

    def _fills(self, request: OperatorRequest) -> OperatorResult:
        _, database = self._open_read_database()
        try:
            rows = database.query_all(
                """
                SELECT broker_fill_id, broker_order_id, execution_id, symbol, side, shares,
                       price, commission, stamp_duty, transfer_fee, session_date, event_time
                FROM fills ORDER BY event_time DESC, broker_fill_id DESC LIMIT ?
                """,
                (request.limit,),
            )
            items = [{key: row[key] for key in row} for row in rows]
        finally:
            database.close()
        return OperatorResult(
            message="成交事实已读取。",
            payload={"count": len(items), "fills": items},
        )

    def _report(self, request: OperatorRequest) -> OperatorResult:
        if self._reporter is not None:
            settings = self._settings()
            with WriterLease.acquire(
                self._database_path(settings),
                owner="operator-report",
                clock=self._clock,
            ) as writer:
                payload = self._reporter(request.session, writer.database)
        else:
            settings, database = self._open_read_database()
            try:
                report_directory = self._resolved(settings.paths.report_directory)
                if request.session is None:
                    candidates = sorted(report_directory.glob("*.json"), reverse=True)
                else:
                    candidates = [report_directory / f"{request.session.isoformat()}.json"]
                if not candidates or not candidates[0].is_file() or candidates[0].is_symlink():
                    raise OperatorCommandDenied("REPORT_UNAVAILABLE")
                report_path = candidates[0]
                if report_path.stat().st_size > 16 * 1024 * 1024:
                    raise OperatorCommandDenied("REPORT_INVALID")
                parsed: object = json.loads(
                    report_path.read_text(encoding="utf-8"),
                    parse_constant=_reject_json_constant,
                )
                if not isinstance(parsed, dict):
                    raise OperatorCommandDenied("REPORT_INVALID")
                payload = parsed
            finally:
                database.close()
        return OperatorResult(message="session 报告已读取。", payload=payload)

    def _replay(self, request: OperatorRequest) -> OperatorResult:
        path = request.events_path
        if path is None:
            raise OperatorCommandDenied("REPLAY_EVENTS_REQUIRED")
        broker = RecordedReplayBroker.from_jsonl(path)
        events: list[Mapping[str, object]] = []

        def collect_event(untrusted_event: Mapping[str, object]) -> None:
            events.append(dict(untrusted_event))

        broker.connect()
        try:
            account = broker.query_account()
            positions = broker.query_positions()
            orders = broker.query_orders()
            fills = broker.query_fills()
            broker.subscribe(collect_event)
        finally:
            broker.disconnect()
        return OperatorResult(
            message="冻结事件已确定性重放; 真实券商写调用为零。",
            payload={
                "state_sha256": broker.state_sha256,
                "account_type": account.account_type.value,
                "position_count": len(positions),
                "order_count": len(orders),
                "fill_count": len(fills),
                "event_count": len(events),
                "write_attempts": len(broker.write_attempts),
            },
        )

    def _backup(self, request: OperatorRequest) -> OperatorResult:
        settings = self._settings()
        destination = self._resolved(settings.paths.backup_directory)
        with WriterLease.acquire(
            self._database_path(settings),
            owner="operator-backup",
            clock=self._clock,
        ) as writer:
            receipt = backup_state(
                writer.database,
                destination,
                account_state_path=request.account_state_path,
                created_at=self._now(),
            )
        return OperatorResult(
            message="一致性备份已创建并完成恢复验证。",
            payload={
                "backup_id": receipt.backup_id,
                "bundle": receipt.bundle_path.name,
                "database_sha256": receipt.database_sha256,
                "account_state_sha256": receipt.account_state_sha256,
                "manifest_sha256": receipt.manifest_sha256,
                "audit_count": receipt.audit_count,
                "audit_head_hash": receipt.audit_head_hash,
                "created_at": receipt.created_at,
            },
        )

    def _verify_backup(self, request: OperatorRequest) -> OperatorResult:
        if request.bundle_path is None:
            raise OperatorCommandDenied("BACKUP_BUNDLE_REQUIRED")
        verification = verify_backup(request.bundle_path)
        return OperatorResult(
            message="备份已在隔离目录完成恢复验证。",
            payload={
                "backup_id": verification.backup_id,
                "database_sha256": verification.database_sha256,
                "account_state_sha256": verification.account_state_sha256,
                "manifest_sha256": verification.manifest_sha256,
                "audit_count": verification.audit_count,
                "audit_head_hash": verification.audit_head_hash,
                "schema_version": verification.schema_version,
                "verified": True,
            },
        )

    def _cancel_system_orders(self) -> OperatorResult:
        if self._system_order_canceller is None:
            raise OperatorCommandDenied("WRITE_CAPABILITY_UNAVAILABLE")
        settings, database = self._open_read_database()
        try:
            if settings.mode in {Mode.REPLAY, Mode.SHADOW}:
                raise OperatorCommandDenied("MODE_NOT_WRITE_CAPABLE")
            if settings.mode in {Mode.CANARY, Mode.LIVE} and not isinstance(
                self._system_order_canceller,
                CapabilityBoundSystemOrderCanceller,
            ):
                raise OperatorCommandDenied("WRITE_CAPABILITY_UNAVAILABLE")
            rows = database.query_all(
                "SELECT broker_order_id FROM broker_orders WHERE ownership = 'SYSTEM' "
                "AND execution_id IS NOT NULL AND status IN (?,?,?,?) "
                "ORDER BY broker_order_id",
                _ACTIVE_BROKER_ORDER_STATES,
            )
            order_ids = tuple(str(row["broker_order_id"]) for row in rows)
        finally:
            database.close()
        if not order_ids:
            return OperatorResult(
                message="没有可取消的 firmquant 系统订单。",
                payload={"requested_order_ids": [], "cancelled_order_ids": []},
            )
        cancelled = self._system_order_canceller.cancel_system_orders(order_ids)
        if (
            not isinstance(cancelled, tuple)
            or len(set(cancelled)) != len(cancelled)
            or not set(cancelled).issubset(order_ids)
        ):
            raise OperatorCommandDenied("CANCEL_RESULT_INVALID")
        return OperatorResult(
            message="系统订单取消请求已通过能力门禁执行。",
            payload={
                "requested_order_ids": list(order_ids),
                "cancelled_order_ids": list(cancelled),
            },
        )


def create_local_operator_service(config_path: Path) -> OperatorService:
    """Compose the installed CLI with concrete local runtime and reconciliation ports."""

    from firmquant.application.composition import compose_operator_ports

    ports = compose_operator_ports(config_path)
    return LocalOperatorService(
        config_path=config_path,
        runner=ports.run,
        reconciler=ports.reconcile,
        reporter=ports.report,
        account_bootstrapper=ports.bootstrap_account,
        doctor_broker_provider=ports.doctor_broker,
        system_order_canceller=ports,
    )


__all__ = (
    "AccountBootstrapPort",
    "CapabilityBoundSystemOrderCanceller",
    "LocalOperatorService",
    "OperatorCommand",
    "OperatorCommandDenied",
    "OperatorInteraction",
    "OperatorReconciliation",
    "OperatorRequest",
    "OperatorResult",
    "OperatorService",
    "SystemOrderCancellationPort",
    "create_local_operator_service",
)
