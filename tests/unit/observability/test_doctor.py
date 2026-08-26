from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from firmquant.broker.gateway import BrokerHealth
from firmquant.config import PathSettings, Settings
from firmquant.domain.broker_facts import AccountType, BrokerAccountFact
from firmquant.domain.values import Money
from firmquant.observability.health import (
    REQUIRED_DOCTOR_CHECKS,
    CheckEvidence,
    Doctor,
    DoctorConfigurationError,
)
from firmquant.persistence.database import Database
from firmquant.persistence.writer_lease import WriterLease
from firmquant.security.secrets import SecretBytes

NOW = datetime(2026, 8, 25, 8, 30, tzinfo=UTC)


class FakeSecretProvider:
    def __init__(self) -> None:
        self.requested_names: list[str] = []

    def get_secret(self, name: str) -> SecretBytes:
        self.requested_names.append(name)
        return SecretBytes(b"fake-windows-smoke-material")


class ReadOnlyPaperProbe:
    def __init__(self) -> None:
        self.connected = False
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.account_queries = 0

    def connect(self) -> None:
        self.connect_calls += 1
        self.connected = True

    def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.connected = False

    def health(self) -> BrokerHealth:
        return BrokerHealth(
            connected=self.connected,
            read_healthy=self.connected,
            write_healthy=self.connected,
            observed_at=NOW,
            diagnostic_code="CONNECTED" if self.connected else "DISCONNECTED",
        )

    def query_account(self) -> BrokerAccountFact:
        if not self.connected:
            raise RuntimeError("read probe is disconnected")
        self.account_queries += 1
        return BrokerAccountFact(
            account_id_hash="a" * 64,
            account_type=AccountType.CASH,
            available_cash=Money(Decimal("100000.00")),
            total_assets=Money(Decimal("100000.00")),
        )


class FailAfterConnectProbe(ReadOnlyPaperProbe):
    def health(self) -> BrokerHealth:
        if self.connected:
            raise RuntimeError("sensitive-client-health-payload")
        return super().health()


def _passing_probes() -> dict[str, object]:
    probes: dict[str, object] = {}
    for name in REQUIRED_DOCTOR_CHECKS:
        probes[name] = lambda name=name: CheckEvidence(
            passed=True,
            summary="PASS",
            details={"probe": name},
        )
    return probes


def test_doctor_runs_exactly_the_fifteen_required_checks_in_stable_order() -> None:
    doctor = Doctor(probes=_passing_probes())

    results = doctor.run()

    assert tuple(result.name for result in results) == REQUIRED_DOCTOR_CHECKS
    assert len(results) == 15
    assert all(result.passed for result in results)


def test_doctor_rejects_missing_or_extra_check_contracts() -> None:
    missing = _passing_probes()
    missing.pop("database")
    with pytest.raises(DoctorConfigurationError, match="exactly"):
        Doctor(probes=missing)

    extra = _passing_probes()
    extra["surprise"] = lambda: CheckEvidence(True, "PASS", {})
    with pytest.raises(DoctorConfigurationError, match="exactly"):
        Doctor(probes=extra)


def test_probe_exception_fails_closed_without_exposing_exception_text() -> None:
    probes = _passing_probes()

    def fail_with_sensitive_text() -> CheckEvidence:
        raise RuntimeError("secret-account-raw-value")

    probes["secret-provider"] = fail_with_sensitive_text
    doctor = Doctor(probes=probes)

    result = doctor.run_named("secret-provider")

    assert result.passed is False
    assert result.summary == "CHECK_EXCEPTION"
    assert result.details == {"error_type": "RuntimeError"}
    assert "secret-account-raw-value" not in repr(result)


def test_local_doctor_proves_paper_is_live_locked_and_uses_only_read_probes(
    tmp_path: Path,
) -> None:
    state_directory = tmp_path / "state"
    data_directory = tmp_path / "data"
    report_directory = tmp_path / "reports"
    backup_directory = tmp_path / "backups"
    for directory in (
        state_directory,
        data_directory,
        report_directory,
        backup_directory,
    ):
        directory.mkdir()
    (data_directory / "manifest.json").write_text(
        '{"schema":"uquant-data-manifest-smoke-v1","session":"2026-08-25"}',
        encoding="utf-8",
    )
    settings = Settings(
        paths=PathSettings(
            state_directory=state_directory,
            data_directory=data_directory,
            report_directory=report_directory,
            backup_directory=backup_directory,
        )
    )
    with WriterLease.acquire(
        state_directory / "firmquant.sqlite3",
        owner="doctor-paper-fixture",
        clock=lambda: NOW,
    ):
        pass
    secret_provider = FakeSecretProvider()
    broker = ReadOnlyPaperProbe()
    doctor = Doctor.for_local_environment(
        settings,
        database_path=state_directory / "firmquant.sqlite3",
        broker=broker,
        secret_provider=secret_provider,
        required_secret_names=("WINDOWS_SMOKE",),
        clock=lambda: NOW,
        clock_drift_seconds=Decimal("0"),
        write_capability_present=lambda: False,
    )

    results = doctor.run()
    live_lock = doctor.run_named("live-mode-lock")

    assert all(result.passed for result in results), results
    assert live_lock.passed is True
    assert live_lock.details["write_capability"] is False
    assert live_lock.details["mode"] == "PAPER"
    assert secret_provider.requested_names == ["WINDOWS_SMOKE"]
    assert broker.account_queries == 1
    assert broker.connect_calls == broker.disconnect_calls == 2


def test_local_doctor_fails_live_lock_when_runtime_write_capability_exists(
    tmp_path: Path,
) -> None:
    paths = PathSettings(
        state_directory=tmp_path,
        data_directory=tmp_path,
        report_directory=tmp_path,
        backup_directory=tmp_path,
    )
    doctor = Doctor.for_local_environment(
        Settings(paths=paths),
        database_path=tmp_path / "firmquant.sqlite3",
        broker=ReadOnlyPaperProbe(),
        clock=lambda: NOW,
        clock_drift_seconds=Decimal("0"),
        write_capability_present=lambda: True,
    )

    result = doctor.run_named("live-mode-lock")

    assert result.passed is False
    assert result.details["write_capability"] is True


def test_broker_probe_disconnects_when_post_connect_health_read_fails(
    tmp_path: Path,
) -> None:
    broker = FailAfterConnectProbe()
    paths = PathSettings(
        state_directory=tmp_path,
        data_directory=tmp_path,
        report_directory=tmp_path,
        backup_directory=tmp_path,
    )
    doctor = Doctor.for_local_environment(
        Settings(paths=paths),
        database_path=tmp_path / "firmquant.sqlite3",
        broker=broker,
        clock=lambda: NOW,
        clock_drift_seconds=Decimal("0"),
    )

    result = doctor.run_named("broker-client")

    assert result.passed is False
    assert result.details == {"error_type": "RuntimeError"}
    assert broker.disconnect_calls == 1
    assert broker.connected is False


def test_database_and_single_instance_diagnostics_never_open_a_write_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "firmquant.sqlite3"
    with WriterLease.acquire(
        database_path,
        owner="doctor-readonly-fixture",
        clock=lambda: NOW,
    ):
        pass
    paths = PathSettings(
        state_directory=tmp_path,
        data_directory=tmp_path,
        report_directory=tmp_path,
        backup_directory=tmp_path,
    )
    doctor = Doctor.for_local_environment(
        Settings(paths=paths),
        database_path=database_path,
        broker=ReadOnlyPaperProbe(),
        clock=lambda: NOW,
        clock_drift_seconds=Decimal("0"),
    )

    def forbid_write_open(*_args: object, **_kwargs: object) -> Database:
        raise AssertionError("doctor attempted a write-capable database open")

    monkeypatch.setattr(Database, "open", forbid_write_open)

    assert doctor.run_named("database").passed is True
    assert doctor.run_named("single-instance-lock").passed is True
