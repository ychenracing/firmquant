from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from firmquant.broker.fake import BrokerOperation, ScriptedOutcome
from firmquant.domain.broker_facts import BrokerOrderStatus
from firmquant.domain.orders import OrderState
from firmquant.persistence.account_authority import AccountBinding, AccountBindingRepository
from firmquant.persistence.database import Database
from firmquant.persistence.production_repository import MonotonicExecutionLedgerRepository
from firmquant.risk.capability import CancelOnlyCapabilityFactory
from firmquant.config import Mode
from tests.fixtures.recovery_cases import (
    NOW,
    acknowledge_locally,
    broker_order,
    cancelled_locally,
    create_submitting_case,
    fake_recovery_broker,
)


def _bind_account(database: Database, account_hash: str) -> None:
    binding = AccountBinding.create(
        account_id_hash=account_hash,
        account_type=fake_recovery_broker().query_account().account_type,
        broker_snapshot_sha256="b" * 64,
        account_state_sha256="c" * 64,
        uquant_commit="1" * 40,
        uquant_code_fingerprint="d" * 64,
        data_hash="e" * 64,
        data_as_of="2026-08-25",
        data_symbols=("600519.SH",),
        created_at=NOW,
    )
    AccountBindingRepository(database).bind(binding)


def _open_case(database: Database):
    case = create_submitting_case(database)
    acknowledged_fact = broker_order(case.command)
    acknowledged = acknowledge_locally(case, acknowledged_fact)
    return case, acknowledged, acknowledged_fact


def _capability(database: Database, broker, *, mode: Mode = Mode.CANARY):
    return CancelOnlyCapabilityFactory(mode=mode).create(
        gateway=broker,
        ledger=MonotonicExecutionLedgerRepository(database),
        clock=lambda: NOW,
    )


def test_cancel_only_type_has_no_submit_and_cancels_system_order_while_halted_or_disarmed(
    tmp_path: Path,
) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        case, acknowledged, acknowledged_fact = _open_case(database)
        broker = fake_recovery_broker(orders=(acknowledged_fact,))
        _bind_account(database, broker.query_account().account_id_hash)
        cancelled = replace(
            acknowledged_fact,
            status=BrokerOrderStatus.CANCELLED,
            event_sequence=acknowledged_fact.event_sequence + 1,
        )
        broker.script((ScriptedOutcome(BrokerOperation.CANCEL, response=cancelled),))
        capability = _capability(database, broker)

        assert not hasattr(capability, "submit_order")
        result = capability.cancel_system_orders()
        current = MonotonicExecutionLedgerRepository(database).load(case.aggregate.intent.execution_id)
        assert current is not None
        assert current.state is OrderState.CANCELLED
        assert result.cancelled_order_ids == (acknowledged.broker_order_id,)
        assert broker.cancelled_order_ids == (acknowledged.broker_order_id,)
    finally:
        database.close()


def test_cancel_only_does_not_require_arm_or_quote_freshness(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        case, acknowledged, acknowledged_fact = _open_case(database)
        broker = fake_recovery_broker(orders=(acknowledged_fact,))
        _bind_account(database, broker.query_account().account_id_hash)
        with database.transaction():
            database.write(
                """
                INSERT INTO arm_leases(
                    lease_id, mode, host_hash, account_hash, firmquant_commit,
                    uquant_commit, config_sha256, identity_payload_sha256,
                    issued_at, expires_at, revoked_at, revoke_reason, lease_mac
                ) VALUES (?, 'CANARY', ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
                """,
                (
                    "arm_" + "e" * 32,
                    "f" * 64,
                    broker.query_account().account_id_hash,
                    "1" * 40,
                    "2" * 40,
                    "3" * 64,
                    "4" * 64,
                    (NOW - timedelta(minutes=10)).isoformat(),
                    (NOW - timedelta(minutes=5)).isoformat(),
                    "5" * 64,
                ),
            )
        cancelled = replace(
            acknowledged_fact,
            status=BrokerOrderStatus.CANCELLED,
            event_sequence=acknowledged_fact.event_sequence + 1,
        )
        broker.script((ScriptedOutcome(BrokerOperation.CANCEL, response=cancelled),))

        result = _capability(database, broker).cancel_system_orders()
        assert result.cancelled_order_ids == (acknowledged.broker_order_id,)
        assert broker.cancelled_order_ids == (acknowledged.broker_order_id,)
        assert MonotonicExecutionLedgerRepository(database).load(
            case.aggregate.intent.execution_id
        ).state is OrderState.CANCELLED
    finally:
        database.close()


def test_cancel_only_ignores_external_and_terminal_orders(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        case = create_submitting_case(database)
        terminal, terminal_fact = cancelled_locally(case)
        broker = fake_recovery_broker(orders=(terminal_fact,))
        _bind_account(database, broker.query_account().account_id_hash)

        result = _capability(database, broker).cancel_system_orders()
        assert terminal.state is OrderState.CANCELLED
        assert result.cancelled_order_ids == ()
        assert broker.cancelled_order_ids == ()

        # A broker-only external order is never a trusted cancellation candidate.
        external = replace(
            terminal_fact,
            broker_order_id="external-order-1",
            client_order_id="manual-order-1",
            status=BrokerOrderStatus.ACKNOWLEDGED,
            event_sequence=99,
        )
        broker = fake_recovery_broker(orders=(external,))
        result = _capability(database, broker).cancel_system_orders()
        assert result.cancelled_order_ids == ()
        assert broker.cancelled_order_ids == ()
    finally:
        database.close()


def test_cancel_unknown_is_durable_and_never_repeated(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        case, acknowledged, acknowledged_fact = _open_case(database)
        broker = fake_recovery_broker(orders=(acknowledged_fact,))
        _bind_account(database, broker.query_account().account_id_hash)
        broker.script((ScriptedOutcome(BrokerOperation.CANCEL, error=TimeoutError("lost response")),))
        capability = _capability(database, broker)

        first = capability.cancel_system_orders()
        assert first.unknown_order_ids == (acknowledged.broker_order_id,)
        assert broker.cancelled_order_ids == (acknowledged.broker_order_id,)
        current = MonotonicExecutionLedgerRepository(database).load(case.aggregate.intent.execution_id)
        assert current is not None and current.state is OrderState.UNKNOWN

        second = capability.cancel_system_orders()
        assert second.cancelled_order_ids == ()
        assert second.unknown_order_ids == ()
        assert broker.cancelled_order_ids == (acknowledged.broker_order_id,)
        assert database.scalar(
            "SELECT count(*) FROM broker_order_attempts WHERE state = 'UNKNOWN'"
        ) == 1
    finally:
        database.close()


def test_cancel_only_revalidates_account_and_broker_identity_before_write(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        _, _, acknowledged_fact = _open_case(database)
        broker = fake_recovery_broker(orders=(acknowledged_fact,))
        _bind_account(database, "f" * 64)
        result = _capability(database, broker).cancel_system_orders()
        assert result.denied_order_ids == (acknowledged_fact.broker_order_id,)
        assert broker.cancelled_order_ids == ()

        # Correct account, but broker identity drift: still no write.
        database.close()
        database = Database.open(tmp_path / "identity.sqlite3")
        _, _, acknowledged_fact = _open_case(database)
        broker = fake_recovery_broker(
            orders=(replace(acknowledged_fact, client_order_id="different-order"),)
        )
        _bind_account(database, broker.query_account().account_id_hash)
        result = _capability(database, broker).cancel_system_orders()
        assert result.denied_order_ids == (acknowledged_fact.broker_order_id,)
        assert broker.cancelled_order_ids == ()
    finally:
        database.close()


def test_cancel_only_is_structurally_disabled_outside_canary_live(tmp_path: Path) -> None:
    for mode in (Mode.PAPER, Mode.REPLAY, Mode.SHADOW):
        database = Database.open(tmp_path / f"{mode.value.lower()}.sqlite3")
        try:
            _, _, acknowledged_fact = _open_case(database)
            broker = fake_recovery_broker(orders=(acknowledged_fact,))
            _bind_account(database, broker.query_account().account_id_hash)
            result = _capability(database, broker, mode=mode).cancel_system_orders()
            assert result.cancelled_order_ids == ()
            assert result.mode_write_forbidden is True
            assert broker.cancelled_order_ids == ()
        finally:
            database.close()
