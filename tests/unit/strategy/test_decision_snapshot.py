from __future__ import annotations

import dataclasses
import hashlib
import json
from datetime import UTC, date, datetime, timedelta

import pytest

from firmquant.strategy.identity import StrategyIdentity
from firmquant.strategy.snapshots import DecisionSnapshot, DecisionSnapshotError


def _uquant_payload() -> dict[str, object]:
    return {
        "schema": "uquant.decision-control-plane.v2",
        "date": "2026-06-30",
        "opportunity": "TREND",
        "risk": {
            "state": "NORMAL",
            "target_gross_cap": 1.0,
            "system_gross_cap": 1.0,
        },
        "target_gross": 0.5,
        "targets": [
            {
                "symbol": "sz300308",
                "weight": 0.5,
                "lifecycle": "CORE",
                "reduction_policy": "FIFO",
                "reason_code": "strategy_target",
                "exit_kind": "strategy",
                "event_id": "evt_" + "1" * 64,
                "event_signal_date": "2026-06-30",
                "event_target_weight_hex": (0.5).hex(),
                "origin_subsystem": "LEADER",
                "mechanism": "LEADER_SELECTION",
                "origin_lifecycle": "CORE",
                "replaces_symbol": None,
                "industry_at_entry": "optical",
                "industry_manifest_sha256": "0" * 64,
            }
        ],
        "orders": [
            {
                "order_id": "O000000001",
                "signal_date": "2026-06-30",
                "snapshot_kind": "ORIGIN",
                "symbol": "sz300308",
                "side": "BUY",
                "target_weight": 0.5,
                "reduction_policy": "FIFO",
                "reason_code": "strategy_target",
                "exit_kind": "strategy",
                "event_id": "evt_" + "1" * 64,
                "origin_subsystem": "LEADER",
                "mechanism": "LEADER_SELECTION",
                "origin_lifecycle": "CORE",
                "replaces_symbol": None,
                "industry_at_entry": "optical",
                "industry_manifest_sha256": "0" * 64,
            }
        ],
        "effective_config_sha256": "2" * 64,
    }


def _decision_digest(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _snapshot(*, created_at: datetime) -> DecisionSnapshot:
    identity = StrategyIdentity.locked()
    payload = _uquant_payload()
    payload["effective_config_sha256"] = identity.config_fingerprint
    return DecisionSnapshot.create(
        strategy_session=date(2026, 6, 30),
        request_fingerprint="9" * 64,
        input_fingerprint="a" * 64,
        firmquant_commit="f" * 40,
        identity=identity,
        data_manifest_sha256="d" * 64,
        broker_snapshot_sha256="b" * 64,
        account_before_sha256="c" * 64,
        account_after_sha256="e" * 64,
        uquant_payload=payload,
        uquant_decision_digest=_decision_digest(payload),
        risk_summary={
            "sentinel_mode": "FREEZE_ONLY",
            "freeze_new_risk": False,
            "reasons": ["base_normal"],
            "target_gross_cap": 1.0,
        },
        created_at=created_at,
    )


def test_snapshot_is_canonical_immutable_and_contains_complete_decision_evidence() -> None:
    snapshot = _snapshot(created_at=datetime(2026, 6, 30, 9, tzinfo=UTC))
    canonical = json.loads(snapshot.canonical_json())

    assert canonical["strategy_session"] == "2026-06-30"
    assert canonical["request_fingerprint"] == "9" * 64
    assert canonical["opportunity"] == "TREND"
    assert canonical["risk"]["state"] == "NORMAL"
    assert canonical["sentinel"] == {
        "freeze_new_risk": False,
        "sentinel_mode": "FREEZE_ONLY",
    }
    assert canonical["targets"] == snapshot.uquant_payload["targets"]
    assert canonical["pending_orders"] == snapshot.uquant_payload["orders"]
    assert canonical["reason_codes"] == ["base_normal", "strategy_target"]
    assert snapshot.payload_sha256 == hashlib.sha256(snapshot.canonical_json().encode("utf-8")).hexdigest()

    detached = snapshot.uquant_payload
    detached["opportunity"] = "WEAK"
    assert snapshot.uquant_payload["opportunity"] == "TREND"
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.decision_id = "changed"  # type: ignore[misc]


def test_decision_id_is_stable_for_identical_economic_input_and_output() -> None:
    first = _snapshot(created_at=datetime(2026, 6, 30, 9, tzinfo=UTC))
    second = _snapshot(created_at=datetime(2026, 6, 30, 9, tzinfo=UTC) + timedelta(minutes=1))

    assert first.decision_id == second.decision_id
    assert first.payload_sha256 != second.payload_sha256


def test_snapshot_rejects_decision_digest_or_config_identity_drift() -> None:
    identity = StrategyIdentity.locked()
    payload = _uquant_payload()

    with pytest.raises(DecisionSnapshotError, match="decision digest"):
        DecisionSnapshot.create(
            strategy_session=date(2026, 6, 30),
            request_fingerprint="9" * 64,
            input_fingerprint="a" * 64,
            firmquant_commit="f" * 40,
            identity=identity,
            data_manifest_sha256="d" * 64,
            broker_snapshot_sha256="b" * 64,
            account_before_sha256="c" * 64,
            account_after_sha256="e" * 64,
            uquant_payload=payload,
            uquant_decision_digest="9" * 64,
            risk_summary={},
            created_at=datetime(2026, 6, 30, 9, tzinfo=UTC),
        )
