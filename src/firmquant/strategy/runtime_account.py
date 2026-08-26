"""Durable production ownership of the uquant AccountState file."""

from __future__ import annotations

import importlib
from datetime import datetime
from pathlib import Path
from typing import Protocol, cast

from firmquant.domain.broker_facts import BrokerSnapshot
from firmquant.persistence.database import Database
from firmquant.persistence.recovery import AccountOperation, UquantAccountStateStore
from firmquant.strategy.account_sync import (
    AccountStateContract,
    AccountSyncReceipt,
    sync_account,
)


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
        loader = cast(_LoadAccount, getattr(module, "load_account"))
        account = loader(path, require_hashes=True, allow_legacy_schema=False)
    except Exception as error:
        raise RuntimeError("uquant production account state cannot be loaded") from error
    if not hasattr(account, "to_dict"):
        raise RuntimeError("uquant production account does not satisfy AccountState contract")
    return cast(AccountStateContract, account)


class RuntimeAccountRepository:
    """Load, broker-sync, and atomically persist exactly one uquant AccountState."""

    def __init__(self, *, database: Database, path: Path, clock) -> None:
        if not isinstance(database, Database):
            raise TypeError("runtime account repository requires Database")
        if not isinstance(path, Path):
            raise TypeError("runtime account path must be Path")
        if not callable(clock):
            raise TypeError("runtime account clock must be callable")
        self._database = database
        self._path = path
        self._clock = clock
        self._store = UquantAccountStateStore(path)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def store(self) -> UquantAccountStateStore:
        return self._store

    def load(self) -> AccountStateContract:
        return _load_account(self._path)

    def persist_prepared(
        self,
        account: AccountStateContract,
        *,
        expected_before_sha256: str,
        operation_kind: str,
        evidence_sha256: str,
    ) -> str:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise RuntimeError("runtime account clock must be timezone-aware")
        operation = AccountOperation.begin(
            database=self._database,
            state_store=self._store,
            operation_kind=operation_kind,
            expected_before_sha256=expected_before_sha256,
            prepared_state=account,
            evidence_sha256=evidence_sha256,
            created_at=now,
        )
        operation.commit_file(self._store, account, at=now)
        operation.commit_receipt(self._database, state_store=self._store, at=now)
        return operation.expected_account_after_sha256

    def sync_broker_snapshot(
        self,
        snapshot: BrokerSnapshot,
    ) -> tuple[AccountStateContract, AccountSyncReceipt]:
        if not isinstance(snapshot, BrokerSnapshot):
            raise TypeError("runtime broker sync requires BrokerSnapshot")
        account = self.load()
        receipt = sync_account(account, snapshot)
        persisted = self.persist_prepared(
            account,
            expected_before_sha256=receipt.account_before_sha256,
            operation_kind="BROKER_SYNC",
            evidence_sha256=snapshot.raw_payload_sha256,
        )
        if persisted != receipt.account_after_sha256:
            raise RuntimeError("durable broker sync account hash differs from uquant receipt")
        return account, receipt


__all__ = ("RuntimeAccountRepository",)
