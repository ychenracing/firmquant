"""Durable production ownership of the uquant AccountState file."""

from __future__ import annotations

import hashlib
import importlib
import json
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Protocol, cast

from firmquant.domain.broker_facts import BrokerSnapshot
from firmquant.persistence.database import Database
from firmquant.persistence.recovery import (
    AccountOperation,
    RecoveryContradiction,
    UquantAccountStateStore,
)
from firmquant.persistence.repositories import PersistenceConflict, canonical_json
from firmquant.strategy.account_prepare import PreparedAccountSync, prepare_account_sync
from firmquant.strategy.account_sync import AccountStateContract, AccountSyncReceipt


class _LoadAccount(Protocol):
    def __call__(
        self,
        path: Path,
        *,
        require_hashes: bool,
        allow_legacy_schema: bool,
    ) -> object: ...


def _load_account(path: Path) -> AccountStateContract:
    try:
        module = importlib.import_module("uquant.account")
        loader = cast(_LoadAccount, module.load_account)
        account = loader(path, require_hashes=True, allow_legacy_schema=False)
    except Exception as error:
        raise RuntimeError("uquant production account state cannot be loaded") from error
    if not hasattr(account, "to_dict"):
        raise RuntimeError("uquant production account does not satisfy AccountState contract")
    return cast(AccountStateContract, account)


def _path_sha256(path: Path) -> str:
    try:
        canonical = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise RecoveryContradiction("account state path cannot be resolved") from error
    if path.is_symlink() or not canonical.is_file():
        raise RecoveryContradiction("account state must be a regular non-symlink file")
    return hashlib.sha256(str(canonical).encode("utf-8")).hexdigest()


class RuntimeAccountRepository:
    """Prepare and explicitly commit exactly one uquant AccountState."""

    def __init__(
        self,
        *,
        database: Database,
        path: Path,
        clock: Callable[[], datetime],
    ) -> None:
        if not isinstance(database, Database):
            raise TypeError("runtime account repository requires Database")
        if not isinstance(path, Path):
            raise TypeError("runtime account path must be Path")
        if not callable(clock):
            raise TypeError("runtime account clock must be callable")
        self._database = database
        self._path = path
        self._clock = clock
        self._store = UquantAccountStateStore()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def store(self) -> UquantAccountStateStore:
        return self._store

    def load(self) -> AccountStateContract:
        return _load_account(self._path)

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise RuntimeError("runtime account clock must be timezone-aware")
        return now

    def persist_prepared(
        self,
        account: AccountStateContract,
        *,
        expected_before_sha256: str,
        operation_kind: str,
        evidence_sha256: str,
    ) -> str:
        now = self._now()
        operation = AccountOperation.begin(
            database=self._database,
            store=self._store,
            account_path=self._path,
            prepared_account=account,
            expected_before_sha256=expected_before_sha256,
            operation_kind=operation_kind,
            evidence_sha256=evidence_sha256,
            now=now,
        )
        operation.commit_file(now=now)
        operation.commit_receipt(now=now)
        return operation.expected_account_after_sha256

    def prepare_broker_snapshot(self, snapshot: BrokerSnapshot) -> PreparedAccountSync:
        if not isinstance(snapshot, BrokerSnapshot):
            raise TypeError("runtime broker sync requires BrokerSnapshot")
        return prepare_account_sync(self.load(), snapshot)

    def sync_broker_snapshot(
        self,
        snapshot: BrokerSnapshot,
    ) -> tuple[AccountStateContract, AccountSyncReceipt]:
        """Compatibility prepare surface; no durable account state is changed."""

        prepared = self.prepare_broker_snapshot(snapshot)
        return prepared.prepared_account, prepared.receipt

    @staticmethod
    def _operation_id(prepared: PreparedAccountSync) -> str:
        return "acctop_" + hashlib.sha256(prepared.preparation_id.encode("utf-8")).hexdigest()

    def _existing_broker_operation(
        self,
        prepared: PreparedAccountSync,
        *,
        operation_id: str,
        finalization_payload: Mapping[str, object] | None = None,
    ) -> AccountOperation | None:
        row = self._database.query_one(
            "SELECT operation_kind, stage, account_before_sha256, "
            "expected_account_after_sha256, actual_account_after_sha256, payload_json "
            "FROM account_operations WHERE operation_id = ?",
            (operation_id,),
        )
        if row is None:
            return None
        if (
            str(row["operation_kind"]) != "BROKER_SYNC"
            or str(row["account_before_sha256"]) != prepared.account_before_sha256
            or str(row["expected_account_after_sha256"]) != prepared.account_after_sha256
        ):
            raise PersistenceConflict("broker account preparation identity collision")
        try:
            payload: object = json.loads(str(row["payload_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise PersistenceConflict("broker account operation payload is invalid") from error
        path_sha256 = _path_sha256(self._path)
        expected_payload: dict[str, object] = {
            "schema": "firmquant.account-operation.v1",
            "operation_kind": "BROKER_SYNC",
            "account_path_sha256": path_sha256,
            "evidence_sha256": prepared.broker_snapshot_sha256,
        }
        if finalization_payload is not None:
            decoded = json.loads(canonical_json(finalization_payload))
            if not isinstance(decoded, dict):
                raise PersistenceConflict("broker account finalization payload is invalid")
            expected_payload["finalization"] = decoded
        if payload != expected_payload:
            raise PersistenceConflict("broker account preparation payload collision")
        stage = str(row["stage"])
        actual = row["actual_account_after_sha256"]
        if stage == "CONTRADICTION":
            raise RecoveryContradiction("broker account operation is already contradictory")
        if stage == "PREPARED":
            if actual is not None:
                raise RecoveryContradiction("prepared broker account operation has unexpected actual hash")
        elif stage in {"FILE_COMMITTED", "RECEIPT_COMMITTED"}:
            if actual is None or str(actual) != prepared.account_after_sha256:
                raise RecoveryContradiction("committed broker account operation has unexpected actual hash")
        else:
            raise RecoveryContradiction("broker account operation stage is invalid")
        return AccountOperation(
            database=self._database,
            store=self._store,
            account_path=self._path,
            prepared_account=prepared.prepared_account,
            operation_id=operation_id,
            operation_kind="BROKER_SYNC",
            account_before_sha256=prepared.account_before_sha256,
            expected_account_after_sha256=prepared.account_after_sha256,
            evidence_sha256=prepared.broker_snapshot_sha256,
            path_sha256=path_sha256,
        )

    def commit_broker_snapshot(
        self,
        prepared: PreparedAccountSync,
        *,
        finalize: Callable[[], None] | None = None,
        finalization_payload: Mapping[str, object] | None = None,
    ) -> str:
        """CAS-commit one reviewed preparation and its SQLite finalization atomically."""

        if not isinstance(prepared, PreparedAccountSync):
            raise TypeError("broker account commit requires PreparedAccountSync")
        if finalize is not None and not callable(finalize):
            raise TypeError("broker account finalizer must be callable or None")
        if self._store.hash_state(prepared.prepared_account) != prepared.account_after_sha256:
            raise RecoveryContradiction("prepared broker account changed before commit")
        operation_id = self._operation_id(prepared)
        operation = self._existing_broker_operation(
            prepared,
            operation_id=operation_id,
            finalization_payload=finalization_payload,
        )
        now = self._now()
        if operation is None:
            operation = AccountOperation.begin(
                database=self._database,
                store=self._store,
                account_path=self._path,
                prepared_account=prepared.prepared_account,
                expected_before_sha256=prepared.account_before_sha256,
                operation_kind="BROKER_SYNC",
                evidence_sha256=prepared.broker_snapshot_sha256,
                now=now,
                operation_id=operation_id,
                finalization_payload=finalization_payload,
            )
        operation.commit_file(now=now)
        operation.commit_receipt(now=now, finalize=finalize)
        if self._store.hash_file(self._path) != prepared.account_after_sha256:
            raise RecoveryContradiction("durable broker sync account hash differs from preparation")
        return prepared.account_after_sha256


__all__ = ("RuntimeAccountRepository",)
