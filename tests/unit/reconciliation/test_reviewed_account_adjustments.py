from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any, cast

import firmquant.reconciliation.account_preflight as account_preflight

from firmquant.domain.values import Money, Shares, Symbol
from firmquant.persistence.account_authority import (
    AdjustmentCoverage,
    ReviewedAccountAdjustment,
    ReviewedAccountAdjustmentRepository,
)
from firmquant.persistence.database import Database
from firmquant.strategy.account_sync import sync_account
from tests.fixtures.broker_snapshots import completed_buy_snapshot, open_buy_account
from tests.fixtures.reconciliation_cases import NOW, healthy_reconciliation_facts


def _difference(**kwargs: object) -> str:
    function = cast(Any, getattr(account_preflight, "account_difference_sha256"))
    return cast(str, function(**kwargs))


def _evaluate(**kwargs: object):
    function = cast(Any, account_preflight.evaluate_account_preflight)
    return function(**kwargs)


def test_exact_reviewed_cash_difference_can_pass_preflight(tmp_path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        account = open_buy_account()
        facts = healthy_reconciliation_facts()
        snapshot = facts.broker_snapshot
        changed = replace(
            snapshot,
            account=replace(
                snapshot.account,
                available_cash=Money(Decimal("984.9")),
                total_assets=Money(Decimal("1984.9")),
            ),
            raw_payload_sha256="8" * 64,
        )
        difference = _difference(
            account_id_hash=changed.account.account_id_hash,
            symbol=None,
            session=changed.session_date,
            coverage=AdjustmentCoverage.AVAILABLE_CASH,
            broker_snapshot_sha256=changed.raw_payload_sha256,
            expected=Decimal("994.9"),
            observed=Decimal("984.9"),
        )
        adjustment = ReviewedAccountAdjustment.create(
            account_id_hash=changed.account.account_id_hash,
            symbol=Symbol.parse("sz300308"),
            session=changed.session_date,
            adjustment_type="CASH_DIVIDEND_REVIEW",
            coverage=AdjustmentCoverage.AVAILABLE_CASH,
            broker_snapshot_sha256=changed.raw_payload_sha256,
            difference_sha256=difference,
            audit_summary_sha256="f" * 64,
            created_at=NOW,
        )
        repository = ReviewedAccountAdjustmentRepository(database)
        assert repository.append(adjustment) is True

        result = _evaluate(
            snapshot=changed,
            account=account,
            operational_ledger=facts.operational_ledger,
            binding=__import__(
                "tests.unit.reconciliation.test_account_preflight",
                fromlist=["_binding"],
            )._binding(),
            cash_tolerance=Money(Decimal("0.01")),
            reviewed_adjustments=repository,
        )

        assert result.passed is True
        assert result.blockers == ()
        assert result.reviewed_adjustment_ids == (adjustment.adjustment_id,)
    finally:
        database.close()


def test_review_must_match_exact_snapshot_and_difference(tmp_path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        account = open_buy_account()
        facts = healthy_reconciliation_facts()
        snapshot = facts.broker_snapshot
        changed = replace(
            snapshot,
            account=replace(
                snapshot.account,
                available_cash=Money(Decimal("984.9")),
                total_assets=Money(Decimal("1984.9")),
            ),
            raw_payload_sha256="8" * 64,
        )
        repository = ReviewedAccountAdjustmentRepository(database)
        repository.append(
            ReviewedAccountAdjustment.create(
                account_id_hash=changed.account.account_id_hash,
                symbol=Symbol.parse("sz300308"),
                session=changed.session_date,
                adjustment_type="CASH_DIVIDEND_REVIEW",
                coverage=AdjustmentCoverage.AVAILABLE_CASH,
                broker_snapshot_sha256="9" * 64,
                difference_sha256=_difference(
                    account_id_hash=changed.account.account_id_hash,
                    symbol=None,
                    session=changed.session_date,
                    coverage=AdjustmentCoverage.AVAILABLE_CASH,
                    broker_snapshot_sha256="9" * 64,
                    expected=Decimal("994.9"),
                    observed=Decimal("984.9"),
                ),
                audit_summary_sha256="e" * 64,
                created_at=NOW,
            )
        )

        result = _evaluate(
            snapshot=changed,
            account=account,
            operational_ledger=facts.operational_ledger,
            binding=__import__(
                "tests.unit.reconciliation.test_account_preflight",
                fromlist=["_binding"],
            )._binding(),
            cash_tolerance=Money(Decimal("0.01")),
            reviewed_adjustments=repository,
        )

        assert result.passed is False
        assert "UNEXPLAINED_CASH_CHANGE" in result.blockers
        assert result.reviewed_adjustment_ids == ()
    finally:
        database.close()


def test_reviewed_position_share_change_still_requires_reviewed_account_state(tmp_path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        account = open_buy_account()
        sync_account(account, completed_buy_snapshot())
        facts = healthy_reconciliation_facts()
        snapshot = facts.broker_snapshot
        changed_position = replace(
            snapshot.positions[0],
            total_shares=Shares(50),
            market_value=Money(Decimal("500")),
        )
        changed = replace(
            snapshot,
            account=replace(snapshot.account, total_assets=Money(Decimal("1494.9"))),
            positions=(changed_position,),
            orders=(),
            fills=(),
            raw_payload_sha256="7" * 64,
        )
        operational = replace(
            facts.operational_ledger,
            orders=(),
            known_broker_fill_ids=frozenset(),
        )
        difference = _difference(
            account_id_hash=changed.account.account_id_hash,
            symbol=Symbol.parse("sz300308"),
            session=changed.session_date,
            coverage=AdjustmentCoverage.POSITION_TOTAL_SHARES,
            broker_snapshot_sha256=changed.raw_payload_sha256,
            expected=100,
            observed=50,
        )
        adjustment = ReviewedAccountAdjustment.create(
            account_id_hash=changed.account.account_id_hash,
            symbol=Symbol.parse("sz300308"),
            session=changed.session_date,
            adjustment_type="POSITION_CHANGE_REVIEW",
            coverage=AdjustmentCoverage.POSITION_TOTAL_SHARES,
            broker_snapshot_sha256=changed.raw_payload_sha256,
            difference_sha256=difference,
            audit_summary_sha256="d" * 64,
            created_at=NOW,
        )
        repository = ReviewedAccountAdjustmentRepository(database)
        repository.append(adjustment)

        result = _evaluate(
            snapshot=changed,
            account=account,
            operational_ledger=operational,
            binding=__import__(
                "tests.unit.reconciliation.test_account_preflight",
                fromlist=["_binding"],
            )._binding(),
            cash_tolerance=Money(Decimal("0.01")),
            reviewed_adjustments=repository,
        )

        assert result.passed is False
        assert "UNEXPLAINED_POSITION_CHANGE" in result.blockers
        assert "CORPORATE_ACTION_SUSPECTED" in result.blockers
        assert "REVIEWED_ACCOUNT_STATE_REQUIRED" in result.blockers
        assert result.reviewed_adjustment_ids == (adjustment.adjustment_id,)
    finally:
        database.close()
