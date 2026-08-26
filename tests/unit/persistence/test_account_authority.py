from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from firmquant.persistence.account_authority import (
    AccountBinding,
    AccountBindingRepository,
    AdjustmentCoverage,
    ReviewedAccountAdjustment,
    ReviewedAccountAdjustmentRepository,
)

from firmquant.domain.broker_facts import AccountType
from firmquant.domain.values import Symbol
from firmquant.persistence.database import Database

NOW = datetime(2026, 1, 6, 3, tzinfo=UTC)


def test_account_binding_is_singleton_and_round_trips_exact_identity(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    repository = AccountBindingRepository(database)
    binding = AccountBinding.create(
        account_id_hash="a" * 64,
        account_type=AccountType.CASH,
        broker_snapshot_sha256="b" * 64,
        account_state_sha256="c" * 64,
        uquant_commit="1" * 40,
        uquant_code_fingerprint="d" * 64,
        data_hash="e" * 64,
        data_as_of="2026-01-06",
        data_symbols=("sz300308",),
        created_at=NOW,
    )
    try:
        assert repository.load() is None
        assert repository.bind(binding) == binding
        assert repository.load() == binding
        assert repository.bind(binding) == binding
        assert database.scalar("SELECT count(*) FROM account_bindings") == 1
    finally:
        database.close()


def test_reviewed_adjustment_only_covers_the_exact_reviewed_difference(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    repository = ReviewedAccountAdjustmentRepository(database)
    symbol = Symbol.parse("sz300308")
    adjustment = ReviewedAccountAdjustment.create(
        account_id_hash="a" * 64,
        symbol=symbol,
        session=date(2026, 1, 6),
        adjustment_type="CORPORATE_ACTION",
        coverage=AdjustmentCoverage.POSITION_TOTAL_SHARES,
        broker_snapshot_sha256="b" * 64,
        difference_sha256="c" * 64,
        audit_summary_sha256="d" * 64,
        created_at=NOW,
    )
    try:
        repository.append(adjustment)
        assert repository.covers(
            account_id_hash="a" * 64,
            symbol=symbol,
            session=date(2026, 1, 6),
            coverage=AdjustmentCoverage.POSITION_TOTAL_SHARES,
            broker_snapshot_sha256="b" * 64,
            difference_sha256="c" * 64,
        )
        assert not repository.covers(
            account_id_hash="a" * 64,
            symbol=symbol,
            session=date(2026, 1, 6),
            coverage=AdjustmentCoverage.POSITION_SELLABLE_SHARES,
            broker_snapshot_sha256="b" * 64,
            difference_sha256="c" * 64,
        )
        assert not repository.covers(
            account_id_hash="a" * 64,
            symbol=symbol,
            session=date(2026, 1, 6),
            coverage=AdjustmentCoverage.POSITION_TOTAL_SHARES,
            broker_snapshot_sha256="b" * 64,
            difference_sha256="f" * 64,
        )
    finally:
        database.close()
