"""Cross-platform deployment smoke with Windows paths and PAPER-only broker facts."""

from __future__ import annotations

import io
import json
import platform
import sys
import tempfile
from contextlib import redirect_stderr
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from firmquant.broker.paper import PaperBroker
from firmquant.cli import build_parser
from firmquant.cli import main as cli_main
from firmquant.config import PathSettings, Settings
from firmquant.domain.broker_facts import (
    AccountType,
    BrokerAccountFact,
    InstrumentFact,
    MarketSessionStatus,
    QuoteFact,
    SecurityStatus,
    SecurityType,
)
from firmquant.domain.values import Money, Price, Shares, Symbol
from firmquant.execution.policy import ExecutionPolicy, FeeSchedule, FillModel
from firmquant.observability.health import Doctor
from firmquant.persistence.audit import AuditLedger
from firmquant.persistence.backup import backup_state, verify_backup
from firmquant.persistence.database import Database
from firmquant.persistence.schema import CURRENT_SCHEMA_VERSION
from firmquant.persistence.writer_lease import WriterLease, WriterLeaseBusy
from firmquant.security.secrets import SecretBytes

NOW = datetime(2026, 8, 25, 8, 30, tzinfo=UTC)
SESSION = date(2026, 8, 25)


class FakeSecretProvider:
    """In-memory smoke provider; no environment or developer secret is read."""

    def __init__(self) -> None:
        self.requests = 0

    def get_secret(self, name: str) -> SecretBytes:
        if name != "WINDOWS_SMOKE":
            raise RuntimeError("unexpected smoke secret name")
        self.requests += 1
        return SecretBytes(b"ephemeral-smoke-only-material")


def _paper_broker() -> PaperBroker:
    symbol = Symbol.parse("sh688146")
    account = BrokerAccountFact(
        account_id_hash="a" * 64,
        account_type=AccountType.CASH,
        available_cash=Money(Decimal("100000.00")),
        total_assets=Money(Decimal("100000.00")),
    )
    instrument = InstrumentFact(
        symbol=symbol,
        security_type=SecurityType.EQUITY,
        status=SecurityStatus.TRADING,
        trading_unit=Shares(100),
        price_tick=Price(Decimal("0.01")),
        price_precision=2,
        lower_limit=Price(Decimal("9.00")),
        upper_limit=Price(Decimal("11.00")),
        session_date=SESSION,
        observed_at=NOW,
    )
    quote = QuoteFact(
        symbol=symbol,
        last_price=Price(Decimal("10.10")),
        previous_close=Price(Decimal("10.00")),
        bid_price=Price(Decimal("10.09")),
        ask_price=Price(Decimal("10.10")),
        volume=Shares(100_000),
        turnover=Money(Decimal("1010000.00")),
        lower_limit=Price(Decimal("9.00")),
        upper_limit=Price(Decimal("11.00")),
        market_status=MarketSessionStatus.CLOSED,
        sequence=1,
        session_date=SESSION,
        event_time=NOW,
        received_at=NOW,
    )
    policy = ExecutionPolicy(
        fill_model=FillModel(
            max_volume_participation=Decimal("0.005"),
            slippage_bps=Decimal("0"),
        ),
        fee_schedule=FeeSchedule(
            commission_rate=Decimal("0.0003"),
            minimum_commission=Decimal("5.00"),
            stamp_duty_rate=Decimal("0.001"),
            transfer_fee_rate=Decimal("0.00001"),
            fee_quantum=Decimal("0.01"),
        ),
    )
    return PaperBroker(
        account=account,
        positions=(),
        instruments=(instrument,),
        quotes=(quote,),
        market_status=MarketSessionStatus.CLOSED,
        policy=policy,
        clock=lambda: NOW,
    )


def _assert_writer_exclusion(database_path: Path) -> None:
    first = WriterLease.acquire(
        database_path,
        owner="windows-smoke-primary",
        ttl=timedelta(seconds=5),
        clock=lambda: NOW,
    )
    try:
        try:
            WriterLease.acquire(
                database_path,
                owner="windows-smoke-secondary",
                ttl=timedelta(seconds=5),
                clock=lambda: NOW,
            )
        except WriterLeaseBusy:
            pass
        else:
            raise AssertionError("second process obtained the account writer lease")
    finally:
        first.release()


def _assert_cli_is_fail_closed() -> None:
    parsed = build_parser().parse_args(["status"])
    if parsed.command != "status":
        raise AssertionError("CLI parser did not preserve the status command")
    diagnostic = io.StringIO()
    with redirect_stderr(diagnostic):
        return_code = cli_main(["status"])
    rendered = diagnostic.getvalue()
    if (
        return_code != 2
        or "CONFIGURATION_UNAVAILABLE" not in rendered
        or "没有执行未授权券商写操作" not in rendered
    ):
        raise AssertionError("CLI without configuration did not fail closed")


def run_smoke() -> dict[str, object]:
    """Exercise local primitives without instantiating or contacting XtQuant."""

    if sys.version_info[:2] != (3, 12):
        raise AssertionError("Windows deployment requires Python 3.12")
    timezone = ZoneInfo("Asia/Shanghai")
    broker = _paper_broker()
    secret_provider = FakeSecretProvider()
    with tempfile.TemporaryDirectory(prefix="firmquant-windows-smoke-") as temporary:
        root = Path(temporary).resolve()
        state_directory = root / "state"
        data_directory = root / "data"
        report_directory = root / "reports"
        backup_directory = root / "backups"
        for directory in (
            state_directory,
            data_directory,
            report_directory,
            backup_directory,
        ):
            directory.mkdir()
            if directory.is_symlink() or root not in directory.parents:
                raise AssertionError("smoke path escaped its temporary root")
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
        database_path = state_directory / "firmquant.sqlite3"
        with WriterLease.acquire(
            database_path,
            owner="windows-smoke-bootstrap",
            clock=lambda: NOW,
        ):
            pass
        doctor = Doctor.for_local_environment(
            settings,
            database_path=database_path,
            broker=broker,
            secret_provider=secret_provider,
            required_secret_names=("WINDOWS_SMOKE",),
            clock=lambda: NOW,
            clock_drift_seconds=Decimal("0"),
            write_capability_present=lambda: False,
        )
        checks = doctor.run()
        failed = tuple(check.name for check in checks if not check.passed)
        if failed:
            raise AssertionError(f"doctor checks failed: {','.join(failed)}")
        broker.connect()
        try:
            if broker.query_orders():
                raise AssertionError("PAPER smoke unexpectedly contains an order")
        finally:
            broker.disconnect()
        if secret_provider.requests != 1:
            raise AssertionError("fake secret provider was not exercised exactly once")

        database = Database.open(database_path)
        try:
            if str(database.scalar("PRAGMA journal_mode")).casefold() != "wal":
                raise AssertionError("SQLite WAL mode is not active")
            if database.scalar("PRAGMA foreign_keys") != 1:
                raise AssertionError("SQLite foreign keys are not active")
            if database.scalar("PRAGMA synchronous") != 2:
                raise AssertionError("SQLite synchronous mode is not FULL")
            with database.transaction():
                AuditLedger(database).append(
                    audit_event_id="windows-smoke",
                    category="DEPLOYMENT",
                    actor="system",
                    payload={"broker_adapter": "PAPER", "real_order_calls": 0},
                    created_at=NOW,
                )
            receipt = backup_state(database, backup_directory, created_at=NOW)
        finally:
            database.close()
        verification = verify_backup(
            receipt.bundle_path,
            expected_manifest_sha256=receipt.manifest_sha256,
        )
        if verification.schema_version != CURRENT_SCHEMA_VERSION:
            raise AssertionError("restored database schema is not current")
        if verification.audit_count != 1:
            raise AssertionError("restored audit chain does not match the source")
        _assert_writer_exclusion(database_path)
        _assert_cli_is_fail_closed()
        sdk = next(check for check in checks if check.name == "broker-sdk")
        return {
            "backup_restore_verified": True,
            "broker_adapter": "PAPER",
            "doctor_checks": len(checks),
            "os": platform.system(),
            "python": f"{sys.version_info.major}.{sys.version_info.minor}",
            "real_order_calls": 0,
            "schema_version": verification.schema_version,
            "timezone": timezone.key,
            "xtquant_sdk_available": sdk.details["available"],
            "xtquant_readonly_smoke_completed": sdk.details["readonly_smoke_completed"],
        }


def main() -> int:
    print(json.dumps(run_smoke(), separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
