"""Immutable canonical DecisionSnapshot evidence derived from one uquant decision."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Final, Never

from .identity import StrategyIdentity

_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA: Final = re.compile(r"^[0-9a-f]{40}$")
_DECISION_ID: Final = re.compile(r"^decision_[0-9a-f]{64}$")


class DecisionSnapshotError(RuntimeError):
    """Raised when decision evidence is incomplete, ambiguous, or non-canonical."""


def _reject_constant(value: str) -> Never:
    raise DecisionSnapshotError(f"decision snapshot contains non-standard constant: {value}")


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise DecisionSnapshotError(f"decision snapshot contains duplicate key: {key}")
        result[key] = value
    return result


def _json_copy(value: object, *, path: str = "payload") -> object:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise DecisionSnapshotError(f"{path} contains non-finite float")
        return value
    if isinstance(value, Mapping):
        copied: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise DecisionSnapshotError(f"{path} contains non-text object key")
            copied[key] = _json_copy(item, path=f"{path}.{key}")
        return copied
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_copy(item, path=f"{path}[{index}]") for index, item in enumerate(value)]
    raise DecisionSnapshotError(f"{path} contains unsupported value {type(value).__name__}")


def _canonical_json(value: object) -> str:
    copied = _json_copy(value)
    return json.dumps(
        copied,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _parse_json_object(value: str) -> dict[str, object]:
    try:
        payload: object = json.loads(
            value,
            object_pairs_hook=_pairs,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise DecisionSnapshotError("decision snapshot is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise DecisionSnapshotError("decision snapshot root must be an object")
    return payload


def _require_digest(value: str, *, label: str, pattern: re.Pattern[str] = _SHA256) -> None:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise DecisionSnapshotError(f"{label} must be a canonical lowercase digest")


def _require_aware(value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise DecisionSnapshotError("decision snapshot created_at must be timezone-aware")


def _uquant_decision_sha256(payload: Mapping[str, object]) -> str:
    copied = _json_copy(payload, path="uquant_payload")
    encoded = json.dumps(copied, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _mapping(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise DecisionSnapshotError(f"decision snapshot {label} must be an object")
    return value


def _list(value: object, *, label: str) -> list[object]:
    if not isinstance(value, list):
        raise DecisionSnapshotError(f"decision snapshot {label} must be an array")
    return value


def _reason_codes(
    *,
    targets: list[object],
    orders: list[object],
    risk_summary: Mapping[str, object],
) -> list[str]:
    reasons: set[str] = set()
    for item in (*targets, *orders):
        if isinstance(item, dict):
            reason = item.get("reason_code")
            if isinstance(reason, str) and reason:
                reasons.add(reason)
    risk_reasons = risk_summary.get("reasons", [])
    if isinstance(risk_reasons, list):
        reasons.update(reason for reason in risk_reasons if isinstance(reason, str) and reason)
    return sorted(reasons)


@dataclass(frozen=True, slots=True)
class DecisionSnapshot:
    """One append-only, self-verifying strategy decision audit record."""

    strategy_session: date
    decision_id: str
    request_fingerprint: str
    input_fingerprint: str
    firmquant_commit: str
    uquant_commit: str
    uquant_code_fingerprint: str
    uquant_config_fingerprint: str
    data_manifest_sha256: str
    universe_manifest_sha256: str
    broker_snapshot_sha256: str
    account_before_sha256: str
    account_after_sha256: str
    payload_json: str
    payload_sha256: str
    created_at: datetime
    supersedes_decision_id: str | None = None

    def __post_init__(self) -> None:
        if type(self.strategy_session) is not date:
            raise DecisionSnapshotError("strategy_session must be a calendar date")
        if _DECISION_ID.fullmatch(self.decision_id) is None:
            raise DecisionSnapshotError("decision_id is not canonical")
        _require_digest(self.request_fingerprint, label="request fingerprint")
        _require_digest(self.input_fingerprint, label="input fingerprint")
        _require_digest(self.firmquant_commit, label="firmquant commit", pattern=_GIT_SHA)
        _require_digest(self.uquant_commit, label="uquant commit", pattern=_GIT_SHA)
        for label, value in (
            ("uquant code fingerprint", self.uquant_code_fingerprint),
            ("uquant config fingerprint", self.uquant_config_fingerprint),
            ("data manifest SHA-256", self.data_manifest_sha256),
            ("universe manifest SHA-256", self.universe_manifest_sha256),
            ("broker snapshot SHA-256", self.broker_snapshot_sha256),
            ("account before SHA-256", self.account_before_sha256),
            ("account after SHA-256", self.account_after_sha256),
            ("payload SHA-256", self.payload_sha256),
        ):
            _require_digest(value, label=label)
        _require_aware(self.created_at)
        if self.supersedes_decision_id is not None and (
            _DECISION_ID.fullmatch(self.supersedes_decision_id) is None
        ):
            raise DecisionSnapshotError("supersedes decision id is not canonical")
        payload = _parse_json_object(self.payload_json)
        if _canonical_json(payload) != self.payload_json:
            raise DecisionSnapshotError("decision snapshot payload is not canonical JSON")
        observed_sha256 = hashlib.sha256(self.payload_json.encode("utf-8")).hexdigest()
        if observed_sha256 != self.payload_sha256:
            raise DecisionSnapshotError("decision snapshot payload SHA-256 mismatch")
        indexed = {
            "strategy_session": self.strategy_session.isoformat(),
            "decision_id": self.decision_id,
            "request_fingerprint": self.request_fingerprint,
            "input_fingerprint": self.input_fingerprint,
            "firmquant_commit": self.firmquant_commit,
            "uquant_commit": self.uquant_commit,
            "uquant_code_fingerprint": self.uquant_code_fingerprint,
            "uquant_config_fingerprint": self.uquant_config_fingerprint,
            "data_manifest_sha256": self.data_manifest_sha256,
            "universe_manifest_sha256": self.universe_manifest_sha256,
            "broker_snapshot_sha256": self.broker_snapshot_sha256,
            "account_before_sha256": self.account_before_sha256,
            "account_after_sha256": self.account_after_sha256,
            "created_at": self.created_at.isoformat(),
            "supersedes_decision_id": self.supersedes_decision_id,
        }
        for key, expected in indexed.items():
            if payload.get(key) != expected:
                raise DecisionSnapshotError(f"decision snapshot indexed field mismatch: {key}")

    @classmethod
    def create(
        cls,
        *,
        strategy_session: date,
        request_fingerprint: str,
        input_fingerprint: str,
        firmquant_commit: str,
        identity: StrategyIdentity,
        data_manifest_sha256: str,
        broker_snapshot_sha256: str,
        account_before_sha256: str,
        account_after_sha256: str,
        uquant_payload: Mapping[str, object],
        uquant_decision_digest: str,
        risk_summary: Mapping[str, object],
        created_at: datetime,
        supersedes_decision_id: str | None = None,
    ) -> DecisionSnapshot:
        """Validate exact upstream evidence and seal one canonical snapshot."""

        if type(strategy_session) is not date:
            raise DecisionSnapshotError("strategy_session must be a calendar date")
        _require_aware(created_at)
        _require_digest(request_fingerprint, label="request fingerprint")
        _require_digest(input_fingerprint, label="input fingerprint")
        _require_digest(firmquant_commit, label="firmquant commit", pattern=_GIT_SHA)
        _require_digest(uquant_decision_digest, label="uquant decision digest")
        copied_uquant = _json_copy(uquant_payload, path="uquant_payload")
        copied_risk = _json_copy(risk_summary, path="risk_summary")
        upstream = _mapping(copied_uquant, label="uquant payload")
        risk_evidence = _mapping(copied_risk, label="risk summary")
        observed_decision_digest = _uquant_decision_sha256(upstream)
        if observed_decision_digest != uquant_decision_digest:
            raise DecisionSnapshotError("uquant decision digest does not match canonical payload")
        if upstream.get("date") != strategy_session.isoformat():
            raise DecisionSnapshotError("uquant decision date differs from strategy session")
        if upstream.get("effective_config_sha256") != identity.config_fingerprint:
            raise DecisionSnapshotError("uquant decision config identity mismatch")
        opportunity = upstream.get("opportunity")
        if not isinstance(opportunity, str) or not opportunity:
            raise DecisionSnapshotError("uquant opportunity is missing")
        risk = _mapping(upstream.get("risk"), label="risk")
        targets = _list(upstream.get("targets"), label="targets")
        orders = _list(upstream.get("orders"), label="orders")
        sentinel = {
            key: value
            for key, value in risk_evidence.items()
            if key == "freeze_new_risk" or key.startswith("sentinel_")
        }
        reason_codes = _reason_codes(
            targets=targets,
            orders=orders,
            risk_summary=risk_evidence,
        )
        decision_identity_payload = {
            "schema": "firmquant.decision-id.v1",
            "input_fingerprint": input_fingerprint,
            "uquant_decision_digest": uquant_decision_digest,
            "account_after_sha256": account_after_sha256,
            "uquant_commit": identity.uquant_commit,
            "uquant_code_fingerprint": identity.economic_code_fingerprint,
        }
        decision_id = (
            "decision_"
            + hashlib.sha256(_canonical_json(decision_identity_payload).encode("utf-8")).hexdigest()
        )
        payload = {
            "schema": "firmquant.decision-snapshot.v1",
            "strategy_session": strategy_session.isoformat(),
            "decision_id": decision_id,
            "request_fingerprint": request_fingerprint,
            "input_fingerprint": input_fingerprint,
            "firmquant_commit": firmquant_commit,
            "uquant_commit": identity.uquant_commit,
            "uquant_code_fingerprint": identity.economic_code_fingerprint,
            "uquant_config_fingerprint": identity.config_fingerprint,
            "data_manifest_sha256": data_manifest_sha256,
            "universe_manifest_sha256": identity.canonical_universe_sha256,
            "broker_snapshot_sha256": broker_snapshot_sha256,
            "account_before_sha256": account_before_sha256,
            "account_after_sha256": account_after_sha256,
            "opportunity": opportunity,
            "risk": risk,
            "sentinel": sentinel,
            "targets": targets,
            "pending_orders": orders,
            "reason_codes": reason_codes,
            "uquant_decision_digest": uquant_decision_digest,
            "uquant_payload": upstream,
            "risk_summary": risk_evidence,
            "created_at": created_at.isoformat(),
            "supersedes_decision_id": supersedes_decision_id,
        }
        payload_json = _canonical_json(payload)
        return cls(
            strategy_session=strategy_session,
            decision_id=decision_id,
            request_fingerprint=request_fingerprint,
            input_fingerprint=input_fingerprint,
            firmquant_commit=firmquant_commit,
            uquant_commit=identity.uquant_commit,
            uquant_code_fingerprint=identity.economic_code_fingerprint,
            uquant_config_fingerprint=identity.config_fingerprint,
            data_manifest_sha256=data_manifest_sha256,
            universe_manifest_sha256=identity.canonical_universe_sha256,
            broker_snapshot_sha256=broker_snapshot_sha256,
            account_before_sha256=account_before_sha256,
            account_after_sha256=account_after_sha256,
            payload_json=payload_json,
            payload_sha256=hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
            created_at=created_at,
            supersedes_decision_id=supersedes_decision_id,
        )

    def canonical_json(self) -> str:
        return self.payload_json

    @property
    def uquant_payload(self) -> dict[str, object]:
        payload = _parse_json_object(self.payload_json)
        return _mapping(payload["uquant_payload"], label="uquant payload")


__all__ = ("DecisionSnapshot", "DecisionSnapshotError")
