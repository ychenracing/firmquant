from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from firmquant.application.execution_evidence import (
    EvidenceIdentity,
    EvidenceStage,
    ExecutionObservation,
    PositionObservation,
    TargetObservation,
)
from firmquant.application.production_identity import (
    DeploymentIdentity,
    OperationalEvidenceIdentity,
)
from firmquant.application.promotion_store import PromotionStore
from firmquant.config import Mode
from firmquant.persistence.audit import AuditLedger
from firmquant.persistence.database import Database

NOW = datetime(2026, 8, 25, 8, 0, tzinfo=UTC)


def _deployment(stage: EvidenceStage) -> DeploymentIdentity:
    return DeploymentIdentity(
        firmquant_commit="f" * 40,
        uquant_commit="1" * 40,
        uquant_tree="2" * 40,
        uquant_package_manifest_sha256="3" * 64,
        uquant_code_fingerprint="4" * 64,
        uquant_config_fingerprint="5" * 64,
        semantic_config_sha256="c" * 64,
        raw_config_sha256="6" * 64,
        xtquant_safety_manifest_sha256="7" * 64,
        account_id_hash="a" * 64,
        account_authority_epoch=2,
        mode_epoch=3,
        mode=Mode.SHADOW if stage is EvidenceStage.SHADOW else Mode.CANARY,
        caps_sha256="8" * 64,
        production_policy_sha256="9" * 64,
    )


def observation(*, stage: EvidenceStage, session: date, canonical: bool = True) -> ExecutionObservation:
    decision_id = "decision-" + session.isoformat()
    deployment = _deployment(stage)
    operational = OperationalEvidenceIdentity(
        deployment_identity=deployment,
        account_state_sha256="b" * 64,
        broker_snapshot_id="snapshot-" + session.isoformat(),
        broker_snapshot_sha256="0" * 64,
        broker_event_watermark=1,
        snapshot_started_at=NOW,
        snapshot_completed_at=NOW,
        snapshot_duration_ms=0,
        calendar_sha256="e" * 64,
        active_data_generation_sha256="d" * 64,
        strategy_data_manifest_sha256="d" * 64,
        strategy_session=session,
        decision_id=decision_id,
        phase="EXECUTION",
        kind="PROMOTION_EVIDENCE",
    )
    identity = EvidenceIdentity(
        stage=stage,
        execution_session=session,
        firmquant_commit="f" * 40,
        uquant_commit="1" * 40,
        promotion_config_sha256="c" * 64,
        account_sha256="a" * 64,
        data_sha256="d" * 64,
        calendar_sha256="e" * 64,
        operational_identity=operational if canonical else None,
    )
    position = PositionObservation(symbol="600000.SH", shares=100)
    target = TargetObservation(
        symbol="600000.SH",
        target_shares=100,
        target_weight=Decimal("0.10"),
        reference_price=Decimal("10"),
    )
    return ExecutionObservation(
        identity=identity,
        decision_id=decision_id,
        plan_id="plan-" + session.isoformat(),
        portfolio_equity=Decimal("10000"),
        planned_orders=(),
        planning_blockers=(),
        targets=(target,),
        fills=(),
        actual_ending_positions=(position,),
        hypothetical_ending_positions=(position,),
        submit_count=0,
        cancel_count=0,
        rejection_count=0,
        unknown_count=0,
        external_activity=0,
        duplicate_economic_orders=0,
        duplicate_fills=0,
        data_quality_failures=0,
        created_at=NOW,
    )


def _selection(stage: EvidenceStage) -> dict[str, object]:
    deployment = _deployment(stage)
    return {
        "stage": stage,
        "deployment_identity_sha256": deployment.sha256,
        "account_authority_epoch": deployment.account_authority_epoch,
        "mode_epoch": deployment.mode_epoch,
        "mode": deployment.mode,
    }


def test_promotion_store_derives_sessions_from_immutable_details(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        store = PromotionStore(database)
        first = observation(stage=EvidenceStage.SHADOW, session=date(2026, 8, 24))
        second = observation(stage=EvidenceStage.SHADOW, session=date(2026, 8, 25))

        assert store.append(first) is True
        assert store.append(first) is False
        assert store.append(second) is True

        aggregate = store.aggregate(
            **_selection(EvidenceStage.SHADOW),
        )
        assert aggregate is not None
        assert aggregate.observed_sessions == 2
        assert aggregate.order_count == 0
        assert aggregate.max_tracking_error == Decimal("0")
        assert (
            store.aggregate(
                stage=EvidenceStage.SHADOW,
                firmquant_commit=first.identity.firmquant_commit,
                uquant_commit=first.identity.uquant_commit,
                config_sha256=first.identity.promotion_config_sha256,
                account_hash=first.identity.account_sha256,
            )
            is None
        )
        assert (
            database.scalar("SELECT count(*) FROM audit_events WHERE category = 'EXECUTION_OBSERVATION'") == 2
        )
    finally:
        database.close()


def test_shadow_and_canary_qualification_are_independent(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        store = PromotionStore(database)
        shadow = observation(stage=EvidenceStage.SHADOW, session=date(2026, 8, 24))
        canary = replace(
            observation(stage=EvidenceStage.CANARY, session=date(2026, 8, 25)),
            submit_count=1,
        )
        store.append(shadow)
        store.append(canary)

        assert store.qualifies(
            **_selection(EvidenceStage.SHADOW),
            min_sessions=1,
            min_orders=0,
            min_fills=0,
            max_tracking_error=Decimal("0.05"),
        )
        assert store.qualifies(
            **_selection(EvidenceStage.CANARY),
            min_sessions=1,
            min_orders=0,
            min_fills=0,
            max_tracking_error=Decimal("0.05"),
        )
        assert not store.qualifies(
            **{
                **_selection(EvidenceStage.CANARY),
                "deployment_identity_sha256": "0" * 64,
            },
            min_sessions=1,
            min_orders=0,
            min_fills=0,
            max_tracking_error=Decimal("0.05"),
        )
    finally:
        database.close()


def test_canonical_promotion_selector_rejects_non_digest_deployment_identity(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        store = PromotionStore(database)

        with pytest.raises(ValueError, match="deployment identity SHA-256"):
            store.aggregate(
                **{
                    **_selection(EvidenceStage.SHADOW),
                    "deployment_identity_sha256": "g" * 64,
                }
            )
    finally:
        database.close()


def test_legacy_v1_evidence_is_historical_only_and_never_qualifies(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        legacy = observation(
            stage=EvidenceStage.SHADOW,
            session=date(2026, 8, 24),
            canonical=False,
        )
        store = PromotionStore(database)
        with pytest.raises(ValueError, match="canonical operational identity"):
            store.append(legacy)
        with database.transaction():
            AuditLedger(database).append(
                audit_event_id="historical-v1-observation",
                category="EXECUTION_OBSERVATION",
                actor="historical-import",
                payload=legacy.payload(),
                created_at=NOW,
            )

        assert store.aggregate(**_selection(EvidenceStage.SHADOW)) is None
        assert not store.qualifies(
            **_selection(EvidenceStage.SHADOW),
            min_sessions=1,
            min_orders=0,
            min_fills=0,
            max_tracking_error=Decimal("0.05"),
        )
    finally:
        database.close()
