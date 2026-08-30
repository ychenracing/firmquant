"""Typed fail-closed storage for operational authority epochs and staged operations."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Never, cast

from firmquant.config import Mode

from .database import Database, TransactionRequired
from .repositories import PersistenceConflict, canonical_json

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PRODUCTION_MODES = frozenset({Mode.PAPER, Mode.SHADOW, Mode.CANARY, Mode.LIVE})


class OperationStage(StrEnum):
    PREPARED = "PREPARED"
    FILE_COMMITTED = "FILE_COMMITTED"
    EPOCH_COMMITTED = "EPOCH_COMMITTED"
    RECEIPT_COMMITTED = "RECEIPT_COMMITTED"
    CONTRADICTION = "CONTRADICTION"


def _sha256(value: str, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be lowercase SHA-256")
    return value


def _text(value: str, *, label: str, maximum: int = 256) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value)
    ):
        raise ValueError(f"{label} must be canonical non-empty text")
    return value


def _positive(value: int, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    if value <= 0:
        raise ValueError(f"{label} must be positive")
    return value


def _adjacent(source: int, target: int) -> None:
    _positive(source, label="source epoch")
    _positive(target, label="target epoch")
    if target != source + 1:
        raise ValueError("source and target epochs must be adjacent")


def _utc(value: datetime, *, label: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{label} must be datetime")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{label} must be UTC")
    return value


def _reject_float(_value: str) -> Never:
    raise PersistenceConflict("stored authority payload contains a binary float")


def _reject_constant(_value: str) -> Never:
    raise PersistenceConflict("stored authority payload contains a non-standard constant")


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PersistenceConflict("stored authority payload contains a duplicate field")
        result[key] = value
    return result


def _payload(payload_json: str, payload_sha256: str, *, label: str) -> dict[str, object]:
    try:
        decoded = json.loads(
            payload_json,
            object_pairs_hook=_object,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, UnicodeError) as error:
        raise PersistenceConflict(f"{label} payload is malformed") from error
    if not isinstance(decoded, dict) or canonical_json(decoded) != payload_json:
        raise PersistenceConflict(f"{label} payload is not canonical")
    actual = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    if not secrets.compare_digest(actual, payload_sha256):
        raise PersistenceConflict(f"{label} payload SHA-256 mismatch")
    return cast(dict[str, object], decoded)


def _stored_datetime(value: object, *, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as error:
        raise PersistenceConflict(f"stored {label} is malformed") from error
    if parsed.isoformat() != str(value) or parsed.utcoffset() != timedelta(0):
        raise PersistenceConflict(f"stored {label} is not canonical UTC")
    return parsed


@dataclass(frozen=True, slots=True)
class AccountAuthorityEpoch:
    epoch: int
    account_id_hash: str
    account_state_sha256: str
    deployment_identity_sha256: str | None
    source_binding_id: str | None
    payload_json: str
    payload_sha256: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ModeEpoch:
    epoch: int
    mode: Mode
    deployment_identity_sha256: str | None
    caps_sha256: str | None
    payload_json: str
    payload_sha256: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RebaselineOperation:
    operation_id: str
    stage: OperationStage
    source_epoch: int
    target_epoch: int
    account_id_hash: str
    account_before_sha256: str
    candidate_account_state_sha256: str
    deployment_identity_sha256: str
    broker_snapshot_id: str
    broker_snapshot_sha256: str
    backup_id: str
    reviewed_evidence_sha256: str
    account_path_sha256: str
    actual_account_after_sha256: str | None
    reason: str
    created_at: datetime
    updated_at: datetime
    payload_json: str
    payload_sha256: str

    @classmethod
    def create(
        cls,
        *,
        operation_id: str,
        source_epoch: int,
        target_epoch: int,
        account_id_hash: str,
        account_before_sha256: str,
        candidate_account_state_sha256: str,
        deployment_identity_sha256: str,
        broker_snapshot_id: str,
        broker_snapshot_sha256: str,
        backup_id: str,
        reviewed_evidence_sha256: str,
        account_path_sha256: str,
        reason: str,
        created_at: datetime,
    ) -> RebaselineOperation:
        _text(operation_id, label="rebaseline operation id")
        _adjacent(source_epoch, target_epoch)
        _sha256(account_id_hash, label="account identity")
        _sha256(account_before_sha256, label="account before")
        _sha256(candidate_account_state_sha256, label="candidate account state")
        _sha256(deployment_identity_sha256, label="deployment identity")
        _text(broker_snapshot_id, label="broker snapshot id")
        _sha256(broker_snapshot_sha256, label="broker snapshot")
        _text(backup_id, label="rebaseline backup id")
        _sha256(reviewed_evidence_sha256, label="reviewed evidence")
        _sha256(account_path_sha256, label="account path identity")
        _text(reason, label="rebaseline reason", maximum=1024)
        _utc(created_at, label="rebaseline created_at")
        payload_json = canonical_json(
            {
                "schema": "firmquant.account-rebaseline-operation.v1",
                "operation_id": operation_id,
                "source_epoch": source_epoch,
                "target_epoch": target_epoch,
                "account_id_hash": account_id_hash,
                "account_before_sha256": account_before_sha256,
                "candidate_account_state_sha256": candidate_account_state_sha256,
                "deployment_identity_sha256": deployment_identity_sha256,
                "broker_snapshot_id": broker_snapshot_id,
                "broker_snapshot_sha256": broker_snapshot_sha256,
                "backup_id": backup_id,
                "reviewed_evidence_sha256": reviewed_evidence_sha256,
                "account_path_sha256": account_path_sha256,
                "actual_account_after_sha256": None,
                "reason": reason,
                "created_at": created_at,
            }
        )
        return cls(
            operation_id=operation_id,
            stage=OperationStage.PREPARED,
            source_epoch=source_epoch,
            target_epoch=target_epoch,
            account_id_hash=account_id_hash,
            account_before_sha256=account_before_sha256,
            candidate_account_state_sha256=candidate_account_state_sha256,
            deployment_identity_sha256=deployment_identity_sha256,
            broker_snapshot_id=broker_snapshot_id,
            broker_snapshot_sha256=broker_snapshot_sha256,
            backup_id=backup_id,
            reviewed_evidence_sha256=reviewed_evidence_sha256,
            account_path_sha256=account_path_sha256,
            actual_account_after_sha256=None,
            reason=reason,
            created_at=created_at,
            updated_at=created_at,
            payload_json=payload_json,
            payload_sha256=hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
        )


@dataclass(frozen=True, slots=True)
class ModeTransitionOperation:
    operation_id: str
    stage: OperationStage
    source_epoch: int
    target_epoch: int
    source_mode: Mode
    target_mode: Mode
    deployment_identity_sha256: str
    backup_id: str
    evidence_sha256: str
    created_at: datetime
    updated_at: datetime
    payload_json: str
    payload_sha256: str

    @classmethod
    def create(
        cls,
        *,
        operation_id: str,
        source_epoch: int,
        target_epoch: int,
        source_mode: Mode,
        target_mode: Mode,
        deployment_identity_sha256: str,
        backup_id: str,
        evidence_sha256: str,
        created_at: datetime,
    ) -> ModeTransitionOperation:
        _text(operation_id, label="mode transition operation id")
        _adjacent(source_epoch, target_epoch)
        if source_mode not in _PRODUCTION_MODES or target_mode not in _PRODUCTION_MODES:
            raise ValueError("mode transition requires production modes")
        _sha256(deployment_identity_sha256, label="deployment identity")
        _text(backup_id, label="mode transition backup id")
        _sha256(evidence_sha256, label="mode transition evidence")
        _utc(created_at, label="mode transition created_at")
        payload_json = canonical_json(
            {
                "schema": "firmquant.mode-transition-operation.v1",
                "operation_id": operation_id,
                "source_epoch": source_epoch,
                "target_epoch": target_epoch,
                "source_mode": source_mode,
                "target_mode": target_mode,
                "deployment_identity_sha256": deployment_identity_sha256,
                "backup_id": backup_id,
                "evidence_sha256": evidence_sha256,
                "created_at": created_at,
            }
        )
        return cls(
            operation_id=operation_id,
            stage=OperationStage.PREPARED,
            source_epoch=source_epoch,
            target_epoch=target_epoch,
            source_mode=source_mode,
            target_mode=target_mode,
            deployment_identity_sha256=deployment_identity_sha256,
            backup_id=backup_id,
            evidence_sha256=evidence_sha256,
            created_at=created_at,
            updated_at=created_at,
            payload_json=payload_json,
            payload_sha256=hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
        )


class OperationalAuthorityStore:
    def __init__(self, database: Database) -> None:
        if not isinstance(database, Database):
            raise TypeError("operational authority store requires Database")
        self._database = database

    @staticmethod
    def _account_epoch(row: object) -> AccountAuthorityEpoch:
        if not hasattr(row, "__getitem__"):
            raise PersistenceConflict("active account authority row is malformed")
        try:
            epoch = _positive(int(row["epoch"]), label="account authority epoch")
            account_id_hash = _sha256(str(row["account_id_hash"]), label="account identity")
            account_state_sha256 = _sha256(str(row["account_state_sha256"]), label="account state")
            deployment = row["deployment_identity_sha256"]
            if deployment is not None:
                deployment = _sha256(str(deployment), label="deployment identity")
            source_binding = row["source_binding_id"]
            if source_binding is not None:
                source_binding = _text(str(source_binding), label="source binding id")
            payload_json = str(row["payload_json"])
            payload_sha256 = _sha256(str(row["payload_sha256"]), label="authority payload")
            created_at = _stored_datetime(row["created_at"], label="account authority created_at")
            payload = _payload(payload_json, payload_sha256, label="account authority epoch")
        except (KeyError, TypeError, ValueError) as error:
            raise PersistenceConflict("active account authority row is malformed") from error
        if source_binding is not None:
            if (
                payload.get("schema") != "firmquant.account-binding.v1"
                or payload.get("account_id_hash") != account_id_hash
                or payload.get("account_state_sha256") != account_state_sha256
                or payload.get("created_at") != created_at.isoformat()
                or deployment is not None
                or epoch != 1
            ):
                raise PersistenceConflict("account authority epoch payload identity mismatch")
        else:
            expected = {
                "schema": "firmquant.account-authority-epoch.v1",
                "epoch": epoch,
                "account_id_hash": account_id_hash,
                "account_state_sha256": account_state_sha256,
                "deployment_identity_sha256": deployment,
                "created_at": created_at,
            }
            if deployment is None or canonical_json(expected) != payload_json:
                raise PersistenceConflict("account authority epoch payload identity mismatch")
        return AccountAuthorityEpoch(
            epoch=epoch,
            account_id_hash=account_id_hash,
            account_state_sha256=account_state_sha256,
            deployment_identity_sha256=deployment,
            source_binding_id=source_binding,
            payload_json=payload_json,
            payload_sha256=payload_sha256,
            created_at=created_at,
        )

    @staticmethod
    def _mode_epoch(row: object) -> ModeEpoch:
        if not hasattr(row, "__getitem__"):
            raise PersistenceConflict("active mode epoch row is malformed")
        try:
            epoch = _positive(int(row["epoch"]), label="mode epoch")
            mode = Mode(str(row["mode"]))
            deployment = row["deployment_identity_sha256"]
            if deployment is not None:
                deployment = _sha256(str(deployment), label="deployment identity")
            caps = row["caps_sha256"]
            if caps is not None:
                caps = _sha256(str(caps), label="deployment caps")
            payload_json = str(row["payload_json"])
            payload_sha256 = _sha256(str(row["payload_sha256"]), label="mode payload")
            created_at = _stored_datetime(row["created_at"], label="mode epoch created_at")
            _payload(payload_json, payload_sha256, label="mode epoch")
        except (KeyError, TypeError, ValueError) as error:
            raise PersistenceConflict("active mode epoch row is malformed") from error
        legacy = {"schema": "firmquant.mode-epoch.v1", "mode": mode}
        current = {
            "schema": "firmquant.mode-epoch.v1",
            "epoch": epoch,
            "mode": mode,
            "deployment_identity_sha256": deployment,
            "caps_sha256": caps,
            "created_at": created_at,
        }
        expected = legacy if epoch == 1 and deployment is None and caps is None else current
        if (
            ((deployment is None) != (caps is None))
            or ((deployment is None or caps is None) and epoch != 1)
            or canonical_json(expected) != payload_json
        ):
            raise PersistenceConflict("mode epoch payload identity mismatch")
        return ModeEpoch(
            epoch=epoch,
            mode=mode,
            deployment_identity_sha256=deployment,
            caps_sha256=caps,
            payload_json=payload_json,
            payload_sha256=payload_sha256,
            created_at=created_at,
        )

    def active_account_epoch(self) -> AccountAuthorityEpoch:
        row = self._database.query_one(
            """
            SELECT e.* FROM account_authority_active a
            JOIN account_authority_epochs e ON e.epoch=a.epoch
            WHERE a.singleton_id=1
            """
        )
        if row is None:
            raise PersistenceConflict("active account authority epoch is unavailable")
        epoch = self._account_epoch(row)
        if epoch.source_binding_id is not None:
            binding_row = self._database.query_one(
                "SELECT * FROM account_bindings WHERE binding_id=? AND singleton_id=1",
                (epoch.source_binding_id,),
            )
            if binding_row is None:
                raise PersistenceConflict("account authority source binding is unavailable")
            try:
                from .account_authority import AccountBindingRepository

                binding = AccountBindingRepository._from_row(binding_row)
            except PersistenceConflict as error:
                raise PersistenceConflict("account authority source binding is invalid") from error
            if (
                binding.binding_id != epoch.source_binding_id
                or binding.account_id_hash != epoch.account_id_hash
                or binding.account_state_sha256 != epoch.account_state_sha256
                or binding.payload_json != epoch.payload_json
                or binding.payload_sha256 != epoch.payload_sha256
                or binding.created_at != epoch.created_at
            ):
                raise PersistenceConflict("account authority source binding identity mismatch")
        return epoch

    def active_mode_epoch(self) -> ModeEpoch:
        row = self._database.query_one(
            """
            SELECT e.* FROM mode_epoch_active a
            JOIN mode_epochs e ON e.epoch=a.epoch
            WHERE a.singleton_id=1
            """
        )
        if row is None:
            raise PersistenceConflict("active mode epoch is unavailable")
        return self._mode_epoch(row)

    @staticmethod
    def _require_prepared_rebaseline(operation: RebaselineOperation) -> None:
        try:
            canonical = RebaselineOperation.create(
                operation_id=operation.operation_id,
                source_epoch=operation.source_epoch,
                target_epoch=operation.target_epoch,
                account_id_hash=operation.account_id_hash,
                account_before_sha256=operation.account_before_sha256,
                candidate_account_state_sha256=operation.candidate_account_state_sha256,
                deployment_identity_sha256=operation.deployment_identity_sha256,
                broker_snapshot_id=operation.broker_snapshot_id,
                broker_snapshot_sha256=operation.broker_snapshot_sha256,
                backup_id=operation.backup_id,
                reviewed_evidence_sha256=operation.reviewed_evidence_sha256,
                account_path_sha256=operation.account_path_sha256,
                reason=operation.reason,
                created_at=operation.created_at,
            )
        except (TypeError, ValueError, UnicodeError) as error:
            raise PersistenceConflict("rebaseline operation is not canonical PREPARED payload") from error
        if canonical != operation:
            raise PersistenceConflict("rebaseline operation is not canonical PREPARED payload")

    @staticmethod
    def _require_prepared_transition(operation: ModeTransitionOperation) -> None:
        try:
            canonical = ModeTransitionOperation.create(
                operation_id=operation.operation_id,
                source_epoch=operation.source_epoch,
                target_epoch=operation.target_epoch,
                source_mode=operation.source_mode,
                target_mode=operation.target_mode,
                deployment_identity_sha256=operation.deployment_identity_sha256,
                backup_id=operation.backup_id,
                evidence_sha256=operation.evidence_sha256,
                created_at=operation.created_at,
            )
        except (TypeError, ValueError, UnicodeError) as error:
            raise PersistenceConflict(
                "mode transition operation is not canonical PREPARED payload"
            ) from error
        if canonical != operation:
            raise PersistenceConflict("mode transition operation is not canonical PREPARED payload")

    @staticmethod
    def _rebaseline(row: object) -> RebaselineOperation:
        if not hasattr(row, "__getitem__"):
            raise PersistenceConflict("rebaseline operation row is malformed")
        try:
            created_at = _stored_datetime(row["created_at"], label="rebaseline created_at")
            stored = RebaselineOperation.create(
                operation_id=str(row["operation_id"]),
                source_epoch=int(row["source_epoch"]),
                target_epoch=int(row["target_epoch"]),
                account_id_hash=str(row["account_id_hash"]),
                account_before_sha256=str(row["account_before_sha256"]),
                candidate_account_state_sha256=str(row["candidate_account_state_sha256"]),
                deployment_identity_sha256=str(row["deployment_identity_sha256"]),
                broker_snapshot_id=str(row["broker_snapshot_id"]),
                broker_snapshot_sha256=str(row["broker_snapshot_sha256"]),
                backup_id=str(row["backup_id"]),
                reviewed_evidence_sha256=str(row["reviewed_evidence_sha256"]),
                account_path_sha256=str(row["account_path_sha256"]),
                reason=str(row["reason"]),
                created_at=created_at,
            )
            stage = OperationStage(str(row["stage"]))
            updated_at = _stored_datetime(row["updated_at"], label="rebaseline updated_at")
            actual = row["actual_account_after_sha256"]
            if actual is not None:
                actual = _sha256(str(actual), label="actual account after")
        except (KeyError, TypeError, ValueError) as error:
            raise PersistenceConflict("rebaseline operation row is malformed") from error
        if stored.payload_json != str(row["payload_json"]) or stored.payload_sha256 != str(
            row["payload_sha256"]
        ):
            raise PersistenceConflict("rebaseline operation payload identity mismatch")
        return replace(stored, stage=stage, updated_at=updated_at, actual_account_after_sha256=actual)

    @staticmethod
    def _transition(row: object) -> ModeTransitionOperation:
        if not hasattr(row, "__getitem__"):
            raise PersistenceConflict("mode transition operation row is malformed")
        try:
            created_at = _stored_datetime(row["created_at"], label="mode transition created_at")
            stored = ModeTransitionOperation.create(
                operation_id=str(row["operation_id"]),
                source_epoch=int(row["source_epoch"]),
                target_epoch=int(row["target_epoch"]),
                source_mode=Mode(str(row["source_mode"])),
                target_mode=Mode(str(row["target_mode"])),
                deployment_identity_sha256=str(row["deployment_identity_sha256"]),
                backup_id=str(row["backup_id"]),
                evidence_sha256=str(row["evidence_sha256"]),
                created_at=created_at,
            )
            stage = OperationStage(str(row["stage"]))
            updated_at = _stored_datetime(row["updated_at"], label="mode transition updated_at")
        except (KeyError, TypeError, ValueError) as error:
            raise PersistenceConflict("mode transition operation row is malformed") from error
        if stored.payload_json != str(row["payload_json"]) or stored.payload_sha256 != str(
            row["payload_sha256"]
        ):
            raise PersistenceConflict("mode transition operation payload identity mismatch")
        return replace(stored, stage=stage, updated_at=updated_at)

    def prepare_rebaseline_in_transaction(self, operation: RebaselineOperation) -> RebaselineOperation:
        if not isinstance(operation, RebaselineOperation):
            raise TypeError("rebaseline operation must be typed")
        self._require_prepared_rebaseline(operation)
        if not self._database.in_transaction:
            raise TransactionRequired("rebaseline preparation requires an active SQLite transaction")
        row = self._database.query_one(
            "SELECT * FROM account_rebaseline_operations WHERE operation_id=?",
            (operation.operation_id,),
        )
        if row is not None:
            stored = self._rebaseline(row)
            if (
                stored.payload_json == operation.payload_json
                and stored.payload_sha256 == operation.payload_sha256
            ):
                return stored
            raise PersistenceConflict("rebaseline operation identity collision")
        self._database.write(
            """
            INSERT INTO account_rebaseline_operations(
                operation_id,stage,source_epoch,target_epoch,account_id_hash,
                account_before_sha256,candidate_account_state_sha256,
                deployment_identity_sha256,broker_snapshot_id,broker_snapshot_sha256,backup_id,
                reviewed_evidence_sha256,account_path_sha256,actual_account_after_sha256,
                reason,payload_json,payload_sha256,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                operation.operation_id,
                operation.stage.value,
                operation.source_epoch,
                operation.target_epoch,
                operation.account_id_hash,
                operation.account_before_sha256,
                operation.candidate_account_state_sha256,
                operation.deployment_identity_sha256,
                operation.broker_snapshot_id,
                operation.broker_snapshot_sha256,
                operation.backup_id,
                operation.reviewed_evidence_sha256,
                operation.account_path_sha256,
                operation.actual_account_after_sha256,
                operation.reason,
                operation.payload_json,
                operation.payload_sha256,
                operation.created_at.isoformat(),
                operation.updated_at.isoformat(),
            ),
        )
        return operation

    def prepare_rebaseline(self, operation: RebaselineOperation) -> RebaselineOperation:
        with self._database.transaction():
            return self.prepare_rebaseline_in_transaction(operation)

    def prepare_transition_in_transaction(
        self, operation: ModeTransitionOperation
    ) -> ModeTransitionOperation:
        if not isinstance(operation, ModeTransitionOperation):
            raise TypeError("mode transition operation must be typed")
        self._require_prepared_transition(operation)
        if not self._database.in_transaction:
            raise TransactionRequired("mode transition preparation requires an active SQLite transaction")
        row = self._database.query_one(
            "SELECT * FROM mode_transition_operations WHERE operation_id=?",
            (operation.operation_id,),
        )
        if row is not None:
            stored = self._transition(row)
            if (
                stored.payload_json == operation.payload_json
                and stored.payload_sha256 == operation.payload_sha256
            ):
                return stored
            raise PersistenceConflict("mode transition operation identity collision")
        self._database.write(
            """
            INSERT INTO mode_transition_operations(
                operation_id,stage,source_epoch,target_epoch,source_mode,target_mode,
                deployment_identity_sha256,backup_id,evidence_sha256,payload_json,
                payload_sha256,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                operation.operation_id,
                operation.stage.value,
                operation.source_epoch,
                operation.target_epoch,
                operation.source_mode.value,
                operation.target_mode.value,
                operation.deployment_identity_sha256,
                operation.backup_id,
                operation.evidence_sha256,
                operation.payload_json,
                operation.payload_sha256,
                operation.created_at.isoformat(),
                operation.updated_at.isoformat(),
            ),
        )
        return operation

    def prepare_transition(self, operation: ModeTransitionOperation) -> ModeTransitionOperation:
        with self._database.transaction():
            return self.prepare_transition_in_transaction(operation)


__all__ = (
    "AccountAuthorityEpoch",
    "ModeEpoch",
    "ModeTransitionOperation",
    "OperationStage",
    "OperationalAuthorityStore",
    "RebaselineOperation",
)
