from __future__ import annotations

from types import SimpleNamespace

import pytest

import tests.unit.application.test_production_services_acceptance as base
from firmquant.application.close_checkpoint import CloseStep
from firmquant.reconciliation.models import ReconciliationKind
from tests.fixtures.session_cases import execution_snapshot


def test_recovered_close_revalidates_eod_before_decision(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with base.hook_case(tmp_path) as (hooks, _writer, _broker, _accounts):
        old_snapshot_sha = "1" * 64
        hooks._close.append(
            base.EXECUTION_SESSION,
            CloseStep.EOD_RECONCILED,
            evidence={
                "reconciliation_id": "recon_" + "1" * 64,
                "broker_snapshot_sha256": old_snapshot_sha,
                "broker_snapshot_id": "old-snapshot",
            },
            created_at=base.NOW,
        )
        hooks._close.append(
            base.EXECUTION_SESSION,
            CloseStep.DATA_VALIDATED,
            evidence={
                "data_manifest_sha256": "d" * 64,
                "governance_manifest_sha256": "g" * 64,
                "data_generation_id": "gen-test",
                "fetch_attempts": 1,
            },
            created_at=base.NOW,
        )

        fresh_snapshot = execution_snapshot().broker_snapshot
        fresh_reconciliation = "recon_" + "2" * 64
        reconcile_calls: list[ReconciliationKind] = []

        def reconcile(kind: ReconciliationKind):
            reconcile_calls.append(kind)
            return (
                SimpleNamespace(reconciliation_id=fresh_reconciliation),
                fresh_snapshot,
                base.Account(),
            )

        captured: dict[str, str] = {}

        def stop_after_authority(
            _session,
            *,
            data_manifest_sha256: str,
            broker_snapshot_sha256: str,
            reconciliation_id: str,
        ) -> int:
            captured.update(
                data_manifest_sha256=data_manifest_sha256,
                broker_snapshot_sha256=broker_snapshot_sha256,
                reconciliation_id=reconciliation_id,
            )
            raise RuntimeError("stop after authority capture")

        monkeypatch.setattr(hooks, "_reconcile", reconcile)
        monkeypatch.setattr(hooks, "_post_close_decision", stop_after_authority)

        with pytest.raises(RuntimeError, match="authority capture"):
            hooks._close_session(base.EXECUTION_SESSION)

        assert reconcile_calls == [ReconciliationKind.EOD]
        assert captured["data_manifest_sha256"] == "d" * 64
        assert captured["broker_snapshot_sha256"] == fresh_snapshot.raw_payload_sha256
        assert captured["broker_snapshot_sha256"] != old_snapshot_sha
        assert captured["reconciliation_id"] == fresh_reconciliation
