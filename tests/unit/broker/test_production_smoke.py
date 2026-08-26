from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from firmquant.broker.fake import FakeBroker
from firmquant.broker.production_smoke import ProductionSmokeStore, run_readonly_production_smoke
from firmquant.domain.broker_facts import MarketSessionStatus
from firmquant.persistence.database import Database
from tests.fixtures.session_cases import execution_snapshot

NOW = datetime(2026, 8, 25, 1, 31, tzinfo=UTC)


def test_readonly_smoke_queries_authoritative_facts_and_records_zero_writes(tmp_path: Path) -> None:
    facts = execution_snapshot()
    snapshot = facts.broker_snapshot
    broker = FakeBroker(
        account=snapshot.account,
        positions=snapshot.positions,
        orders=snapshot.orders,
        fills=snapshot.fills,
        instruments=facts.instruments,
        quotes=facts.quotes,
        market_status=MarketSessionStatus.OPEN,
        clock=lambda: NOW,
    )
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        broker.connect()
        receipt = run_readonly_production_smoke(
            broker=broker,
            database=database,
            probe_symbol=facts.quotes[0].symbol,
            firmquant_commit="f" * 40,
            uquant_commit="1" * 40,
            config_sha256="c" * 64,
            safety_manifest_sha256="b" * 64,
            clock=lambda: NOW,
        )
        assert receipt.read_healthy is True
        assert receipt.real_order_calls == 0
        assert receipt.account_hash == snapshot.account.account_id_hash
        assert broker.submitted_commands == ()
        assert broker.cancelled_order_ids == ()

        stored = ProductionSmokeStore(database).latest(
            firmquant_commit="f" * 40,
            uquant_commit="1" * 40,
            config_sha256="c" * 64,
            account_hash=snapshot.account.account_id_hash,
            safety_manifest_sha256="b" * 64,
        )
        assert stored == receipt
    finally:
        broker.disconnect()
        database.close()


def test_smoke_identity_mismatch_is_not_accepted_for_live_gate(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        store = ProductionSmokeStore(database)
        assert (
            store.latest(
                firmquant_commit="f" * 40,
                uquant_commit="1" * 40,
                config_sha256="c" * 64,
                account_hash="a" * 64,
                safety_manifest_sha256="b" * 64,
            )
            is None
        )
    finally:
        database.close()
