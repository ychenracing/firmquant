from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import firmquant.strategy.adapter as adapter_module
from firmquant.persistence.database import PersistenceError
from firmquant.strategy.account_sync import StrategySyncError
from firmquant.strategy.adapter import (
    DecisionRecoveryRequired,
    DecisionRequest,
    StrategyAdapter,
    StrategyAdapterError,
)
from firmquant.strategy.identity import StrategyIdentity
from firmquant.strategy.snapshots import DecisionSnapshot

SESSION = date(2026, 6, 30)
CREATED_AT = datetime(2026, 6, 30, 9, tzinfo=UTC)
BEFORE_SHA256 = "1" * 64
AFTER_SHA256 = "2" * 64
DATA_SHA256 = "3" * 64
BROKER_SHA256 = "4" * 64


@dataclass
class _Account:
    sha256: str
    code_hash: str | None = None
    data_hash: str | None = None
    data_hash_as_of: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "sha256": self.sha256,
            "code_hash": self.code_hash,
            "data_hash": self.data_hash,
            "data_hash_as_of": self.data_hash_as_of,
        }


class _Decision:
    def __init__(
        self,
        payload: dict[str, object],
        *,
        digest: str | None = None,
        risk_summary: dict[str, object] | None = None,
    ) -> None:
        self._payload = copy.deepcopy(payload)
        self.decision_digest = digest or _sha256(payload)
        self.risk_summary = {} if risk_summary is None else risk_summary

    def canonical_payload(self, *, effective_config_sha256: str) -> dict[str, object]:
        assert effective_config_sha256 == self._payload["effective_config_sha256"]
        return copy.deepcopy(self._payload)


@dataclass(frozen=True)
class _Harness:
    adapter: StrategyAdapter
    engine: MagicMock
    database: MagicMock
    repository: MagicMock
    commit: MagicMock
    identity: StrategyIdentity


def _sha256(value: dict[str, object]) -> str:
    encoded = json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _identity() -> StrategyIdentity:
    return StrategyIdentity(
        uquant_commit="a" * 40,
        uquant_tree="b" * 40,
        economic_code_fingerprint="5" * 64,
        account_code_fingerprint="6" * 64,
        config_fingerprint="7" * 64,
        public_api_contract_sha256="8" * 64,
        canonical_universe_sha256="9" * 64,
        universe_resource_sha256="a" * 64,
        wheel_sha256="b" * 64,
        package_manifest_sha256="c" * 64,
    )


def _payload(identity: StrategyIdentity) -> dict[str, object]:
    return {
        "date": SESSION.isoformat(),
        "effective_config_sha256": identity.config_fingerprint,
        "opportunity": "NO_TRADE",
        "risk": {},
        "targets": [],
        "orders": [],
    }


def _request(account: _Account | None = None) -> DecisionRequest:
    return DecisionRequest(
        strategy_session=SESSION,
        symbols=("sz300308",),
        account=account or _Account(BEFORE_SHA256),
        firmquant_commit="d" * 40,
        data_manifest_sha256=DATA_SHA256,
        broker_snapshot_sha256=BROKER_SHA256,
        created_at=CREATED_AT,
    )


def _set_engine_result(
    harness: _Harness,
    *,
    digest: str | None = None,
    code_hash: str | None = None,
    data_hash: str | None = None,
    data_hash_as_of: str | None = None,
    risk_summary: dict[str, object] | None = None,
) -> None:
    def decide(*, symbols: tuple[str, ...], as_of: str, account: _Account) -> _Decision:
        assert tuple(symbols) == ("sz300308",)
        assert as_of == SESSION.isoformat()
        account.sha256 = AFTER_SHA256
        account.code_hash = code_hash or harness.identity.economic_code_fingerprint
        account.data_hash = data_hash or DATA_SHA256
        account.data_hash_as_of = data_hash_as_of or SESSION.isoformat()
        return _Decision(
            _payload(harness.identity),
            digest=digest,
            risk_summary=risk_summary,
        )

    harness.engine.decide.side_effect = decide


def _harness(monkeypatch: pytest.MonkeyPatch) -> _Harness:
    identity = _identity()
    engine = MagicMock()
    database = MagicMock()
    repository = MagicMock()
    repository.find_by_input.return_value = None
    repository.for_session.return_value = ()
    policy = MagicMock()
    policy.allowed.return_value = True
    adapter = StrategyAdapter(
        engine=engine,
        database=database,
        source_checkout=Path("."),
        universe_policy=policy,
    )
    monkeypatch.setattr(adapter, "_verified_identity", lambda: identity)
    monkeypatch.setattr(adapter, "_snapshots", repository)
    monkeypatch.setattr(adapter_module, "_account_sha256", lambda account: account.sha256)
    commit = MagicMock()
    monkeypatch.setattr(adapter_module, "commit_prepared_account", commit)
    harness = _Harness(adapter, engine, database, repository, commit, identity)
    _set_engine_result(harness)
    return harness


def _snapshot(harness: _Harness, request: DecisionRequest) -> DecisionSnapshot:
    request_fingerprint, input_fingerprint = StrategyAdapter._fingerprints(
        request,
        symbols=("sz300308",),
        identity=harness.identity,
        account_before_sha256=BEFORE_SHA256,
    )
    payload = _payload(harness.identity)
    return DecisionSnapshot.create(
        strategy_session=SESSION,
        request_fingerprint=request_fingerprint,
        input_fingerprint=input_fingerprint,
        firmquant_commit=request.firmquant_commit,
        identity=harness.identity,
        data_manifest_sha256=DATA_SHA256,
        broker_snapshot_sha256=BROKER_SHA256,
        account_before_sha256=BEFORE_SHA256,
        account_after_sha256=AFTER_SHA256,
        uquant_payload=payload,
        uquant_decision_digest=_sha256(payload),
        risk_summary={},
        created_at=CREATED_AT,
    )


def test_decide_once_appends_before_committing_prepared_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(monkeypatch)
    request = _request()
    events: list[str] = []
    harness.repository.append.side_effect = lambda _snapshot: events.append("append") or True
    harness.commit.side_effect = lambda *_args, **_kwargs: events.append("commit")

    snapshot = harness.adapter.decide_once(request)

    assert events == ["append", "commit"]
    harness.database.transaction.assert_called_once_with()
    harness.repository.append.assert_called_once_with(snapshot)
    harness.commit.assert_called_once()
    target, prepared = harness.commit.call_args.args
    assert target is request.account
    assert prepared is not request.account
    assert prepared.sha256 == AFTER_SHA256
    assert harness.commit.call_args.kwargs == {"expected_sha256": AFTER_SHA256}


def test_decide_once_wraps_engine_failure_without_appending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(monkeypatch)
    harness.engine.decide.side_effect = RuntimeError("engine unavailable")

    with pytest.raises(StrategyAdapterError, match=r"ProductionEngine\.decide\(\) failed"):
        harness.adapter.decide_once(_request())

    harness.repository.append.assert_not_called()
    harness.commit.assert_not_called()


@pytest.mark.parametrize(
    ("drift", "message"),
    [
        ("digest", "decision digest differs"),
        ("code", "code fingerprint was not advanced"),
        ("data", "data manifest differs"),
        ("session", "data as_of differs"),
    ],
)
def test_decide_once_rejects_engine_identity_drift_before_append(
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
    message: str,
) -> None:
    harness = _harness(monkeypatch)
    kwargs: dict[str, str] = {}
    if drift == "digest":
        kwargs["digest"] = "f" * 64
    elif drift == "code":
        kwargs["code_hash"] = "f" * 64
    elif drift == "data":
        kwargs["data_hash"] = "f" * 64
    else:
        kwargs["data_hash_as_of"] = "2026-06-29"
    _set_engine_result(harness, **kwargs)

    with pytest.raises(StrategyAdapterError, match=message):
        harness.adapter.decide_once(_request())

    harness.repository.append.assert_not_called()
    harness.commit.assert_not_called()


def test_decide_once_append_failure_never_commits_account(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _harness(monkeypatch)
    harness.repository.append.side_effect = PersistenceError("disk full")

    with pytest.raises(StrategyAdapterError, match="could not be durably appended"):
        harness.adapter.decide_once(_request())

    harness.commit.assert_not_called()


def test_decide_once_commit_failure_requires_recovery_after_durable_append(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(monkeypatch)
    harness.commit.side_effect = StrategySyncError("commit rejected")

    with pytest.raises(DecisionRecoveryRequired, match="snapshot is durable"):
        harness.adapter.decide_once(_request())

    harness.repository.append.assert_called_once()


def test_recover_rejects_non_snapshot_before_identity_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _harness(monkeypatch)

    with pytest.raises(StrategyAdapterError, match="requires DecisionSnapshot"):
        harness.adapter.recover_existing_decision(_request(), object())  # type: ignore[arg-type]

    harness.engine.decide.assert_not_called()


def test_recover_requires_exact_immutable_before_state(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _harness(monkeypatch)
    request = _request()
    snapshot = _snapshot(harness, request)
    drifted = replace(request, account=_Account("e" * 64))

    with pytest.raises(DecisionRecoveryRequired, match="immutable before-state"):
        harness.adapter.recover_existing_decision(drifted, snapshot)

    harness.engine.decide.assert_not_called()


def test_recover_rejects_request_fingerprint_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _harness(monkeypatch)
    request = _request()
    snapshot = _snapshot(harness, request)
    drifted = replace(request, broker_snapshot_sha256="e" * 64)

    with pytest.raises(DecisionRecoveryRequired, match="input identity differs"):
        harness.adapter.recover_existing_decision(drifted, snapshot)

    harness.engine.decide.assert_not_called()


def test_recover_wraps_recomputation_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _harness(monkeypatch)
    request = _request()
    snapshot = _snapshot(harness, request)
    harness.engine.decide.side_effect = ValueError("bad data")

    with pytest.raises(DecisionRecoveryRequired, match="recomputation failed"):
        harness.adapter.recover_existing_decision(request, snapshot)

    harness.commit.assert_not_called()


def test_recover_rejects_noncanonical_recomputed_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _harness(monkeypatch)
    request = _request()
    snapshot = _snapshot(harness, request)
    _set_engine_result(harness, digest="f" * 64)

    with pytest.raises(DecisionRecoveryRequired, match="digest is not canonical"):
        harness.adapter.recover_existing_decision(request, snapshot)

    harness.commit.assert_not_called()


@pytest.mark.parametrize(
    ("drift", "message"),
    [
        ("code", "account code identity differs"),
        ("data", "account data identity differs"),
        ("session", "account data session differs"),
    ],
)
def test_recover_rejects_recomputed_account_identity_drift(
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
    message: str,
) -> None:
    harness = _harness(monkeypatch)
    request = _request()
    snapshot = _snapshot(harness, request)
    kwargs = {
        "code_hash": "f" * 64 if drift == "code" else None,
        "data_hash": "f" * 64 if drift == "data" else None,
        "data_hash_as_of": "2026-06-29" if drift == "session" else None,
    }
    _set_engine_result(harness, **kwargs)

    with pytest.raises(DecisionRecoveryRequired, match=message):
        harness.adapter.recover_existing_decision(request, snapshot)

    harness.commit.assert_not_called()


def test_recover_rejects_candidate_that_differs_from_durable_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(monkeypatch)
    request = _request()
    snapshot = _snapshot(harness, request)
    _set_engine_result(harness, risk_summary={"freeze_new_risk": True})

    with pytest.raises(DecisionRecoveryRequired, match="differs from immutable durable snapshot"):
        harness.adapter.recover_existing_decision(request, snapshot)

    harness.commit.assert_not_called()


def test_recover_wraps_prepared_account_commit_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _harness(monkeypatch)
    request = _request()
    snapshot = _snapshot(harness, request)
    harness.commit.side_effect = StrategySyncError("commit rejected")

    with pytest.raises(DecisionRecoveryRequired, match="could not be applied"):
        harness.adapter.recover_existing_decision(request, snapshot)

    harness.commit.assert_called_once()


def test_recover_commits_exact_recomputed_after_state(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _harness(monkeypatch)
    request = _request()
    snapshot = _snapshot(harness, request)

    recovered = harness.adapter.recover_existing_decision(request, snapshot)

    assert recovered is snapshot
    harness.commit.assert_called_once()
    assert harness.commit.call_args.kwargs == {"expected_sha256": AFTER_SHA256}
