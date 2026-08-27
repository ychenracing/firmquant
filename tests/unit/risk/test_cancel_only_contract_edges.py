from __future__ import annotations

import pickle
from dataclasses import replace
from pathlib import Path

import pytest

from firmquant.broker.fake import BrokerOperation, ScriptedOutcome
from firmquant.config import Mode
from firmquant.domain.broker_facts import BrokerOrderStatus
from firmquant.persistence.account_authority import AccountBinding, AccountBindingRepository
from firmquant.persistence.database import Database
from firmquant.persistence.production_repository import MonotonicExecutionLedgerRepository
from firmquant.risk.cancel_only import CancelOnlyCapabilityFactory
from tests.fixtures.recovery_cases import (
    NOW,
    acknowledge_locally,
    broker_order,
    create_submitting_case,
    fake_recovery_broker,
)


def _open_case(database: Database):
    case = create_submitting_case(database)
    fact = broker_order(case.command)
    aggregate = acknowledge_locally(case, fact)
    return case, aggregate, fact


def _bind(database: Database, account_hash: str) -> None:
    account_type = fake_recovery_broker().query_account().account_type
    AccountBindingRepository(database).bind(
        AccountBinding.create(
            account_id_hash=account_hash,
            account_type=account_type,
            broker_snapshot_sha256="b" * 64,
            account_state_sha256="c" * 64,
            uquant_commit="1" * 40,
            uquant_code_fingerprint="d" * 64,
            data_hash="e" * 64,
            data_as_of="2026-08-25",
            data_symbols=("600519.SH",),
            created_at=NOW,
        )
    )


def _capability(database: Database, broker, *, mode: Mode = Mode.CANARY):
    return CancelOnlyCapabilityFactory(mode=mode).create(
        gateway=broker,
        ledger=MonotonicExecutionLedgerRepository(database),
        clock=lambda: NOW,
    )


def test_cancel_only_denies_disconnected_or_unbound_broker_before_write(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        _, aggregate, fact = _open_case(database)
        broker = fake_recovery_broker(orders=(fact,))
        capability = _capability(database, broker)

        unbound = capability.cancel_system_orders()
        assert unbound.denied_order_ids == (aggregate.broker_order_id,)
        assert unbound.cancel_calls == 0
        assert broker.cancelled_order_ids == ()

        _bind(database, broker.query_account().account_id_hash)
        broker.disconnect()
        disconnected = capability.cancel_system_orders()
        assert disconnected.denied_order_ids == (aggregate.broker_order_id,)
        assert disconnected.cancel_calls == 0
        assert broker.cancelled_order_ids == ()
    finally:
        database.close()


def test_cancel_only_denies_terminal_or_regressed_broker_identity(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        _, aggregate, fact = _open_case(database)
        terminal = replace(
            fact,
            status=BrokerOrderStatus.CANCELLED,
            event_sequence=fact.event_sequence + 1,
        )
        broker = fake_recovery_broker(orders=(terminal,))
        _bind(database, broker.query_account().account_id_hash)
        result = _capability(database, broker).cancel_system_orders()
        assert result.denied_order_ids == (aggregate.broker_order_id,)
        assert broker.cancelled_order_ids == ()

        database.close()
        database = Database.open(tmp_path / "regressed.sqlite3")
        _, aggregate, fact = _open_case(database)
        regressed = replace(fact, event_sequence=fact.event_sequence - 1)
        broker = fake_recovery_broker(orders=(regressed,))
        _bind(database, broker.query_account().account_id_hash)
        result = _capability(database, broker).cancel_system_orders()
        assert result.denied_order_ids == (aggregate.broker_order_id,)
        assert broker.cancelled_order_ids == ()
    finally:
        database.close()


def test_cancel_only_records_terminal_rejection_result(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        _, aggregate, fact = _open_case(database)
        broker = fake_recovery_broker(orders=(fact,))
        _bind(database, broker.query_account().account_id_hash)
        rejected = replace(
            fact,
            status=BrokerOrderStatus.REJECTED,
            event_sequence=fact.event_sequence + 1,
        )
        broker.script((ScriptedOutcome(BrokerOperation.CANCEL, response=rejected),))

        result = _capability(database, broker).cancel_system_orders()

        assert result.terminal_order_ids == (aggregate.broker_order_id,)
        assert result.cancelled_order_ids == ()
        assert result.unknown_order_ids == ()
        assert result.cancel_calls == 1
        assert broker.cancelled_order_ids == (aggregate.broker_order_id,)
    finally:
        database.close()


def test_cancel_only_nonterminal_cancel_return_becomes_unknown(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        _, aggregate, fact = _open_case(database)
        broker = fake_recovery_broker(orders=(fact,))
        _bind(database, broker.query_account().account_id_hash)
        pending = replace(
            fact,
            status=BrokerOrderStatus.PENDING_CANCEL,
            event_sequence=fact.event_sequence + 1,
        )
        broker.script((ScriptedOutcome(BrokerOperation.CANCEL, response=pending),))
        capability = _capability(database, broker)

        first = capability.cancel_system_orders()
        second = capability.cancel_system_orders()

        assert first.unknown_order_ids == (aggregate.broker_order_id,)
        assert first.cancel_calls == 1
        assert second.cancel_calls == 0
        assert broker.cancelled_order_ids == (aggregate.broker_order_id,)
        assert database.scalar("SELECT count(*) FROM broker_order_attempts WHERE state = 'UNKNOWN'") == 1
    finally:
        database.close()


def test_cancel_only_factory_and_opaque_object_contract(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        broker = fake_recovery_broker()
        ledger = MonotonicExecutionLedgerRepository(database)
        with pytest.raises(Exception, match="mode"):
            CancelOnlyCapabilityFactory(mode="LIVE")  # type: ignore[arg-type]

        factory = CancelOnlyCapabilityFactory(mode=Mode.PAPER)
        with pytest.raises(Exception, match="gateway"):
            factory.create(gateway=object(), ledger=ledger, clock=lambda: NOW)  # type: ignore[arg-type]
        with pytest.raises(Exception, match="ledger"):
            factory.create(gateway=broker, ledger=object(), clock=lambda: NOW)  # type: ignore[arg-type]
        with pytest.raises(Exception, match="clock"):
            factory.create(gateway=broker, ledger=ledger, clock=None)  # type: ignore[arg-type]

        capability = factory.create(gateway=broker, ledger=ledger, clock=lambda: NOW)
        assert repr(capability) == "<BrokerCancelOnlyCapability opaque>"
        with pytest.raises(TypeError, match="not serializable"):
            pickle.dumps(capability)
    finally:
        database.close()
