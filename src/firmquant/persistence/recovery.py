"""Crash recovery for the uquant account file and durable broker write-ahead state."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast, runtime_checkable

from firmquant.broker.gateway import BrokerGateway
from firmquant.domain.broker_facts import BrokerFillFact, BrokerOrderFact, FillStatus
from firmquant.domain.errors import DomainTypeError, DomainValidationError

from .audit import AuditLedger
from .database import Database, PersistenceError
from .repositories import (
    BrokerAttempt,
    ExecutionLedgerRepository,
    PersistenceConflict,
    canonical_json,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ACCOUNT_OPERATION_ID = re.compile(r"^acctop_[0-9a-f]{64}$")


class RecoveryError(PersistenceError):
    """Recovery could not establish a safe and durable classification."""


class RecoveryContradiction(RecoveryError):
    """Durable stores match neither the recorded before nor expected-after state."""


class AccountRecoveryClassification(StrEnum):
    NOT_APPLIED = "NOT_APPLIED"
    FILE_APPLIED_RECEIPT_MISSING = "FILE_APPLIED_RECEIPT_MISSING"
    CONTRADICTION = "CONTRADICTION"


class OrderRecoveryClassification(StrEnum):
    RESOLVED_FROM_BROKER = "RESOLVED_FROM_BROKER"
    REMAINS_UNKNOWN = "REMAINS_UNKNOWN"
    LATE_FACT_APPLIED = "LATE_FACT_APPLIED"
    CONTRADICTION = "CONTRADICTION"


def _digest(value: str, *, label: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise DomainValidationError(f"{label} must be lowercase SHA-256")


def _aware(value: datetime, *, label: str) -> None:
    if not isinstance(value, datetime):
        raise DomainTypeError(f"{label} must be datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise DomainValidationError(f"{label} must be timezone-aware")


def _canonical_text(value: str, *, label: str, maximum: int = 256) -> None:
    if not isinstance(value, str):
        raise DomainTypeError(f"{label} must be text")
    if not value or value != value.strip() or len(value) > maximum:
        raise DomainValidationError(f"{label} must be canonical non-empty text")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise DomainValidationError(f"{label} contains control characters")


def _count_scalar(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RecoveryError(f"{label} is not a nonnegative database count")
    return value


def _path_sha256(path: Path) -> str:
    try:
        canonical = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise RecoveryError("account state path cannot be resolved") from error
    if path.is_symlink() or not canonical.is_file():
        raise RecoveryError("account state must be a regular non-symlink file")
    return hashlib.sha256(str(canonical).encode("utf-8")).hexdigest()


@runtime_checkable
class AccountStateStore(Protocol):
    """Narrow anti-corruption port over uquant's strict account persistence."""

    def hash_state(self, state: object) -> str: ...

    def hash_file(self, path: Path) -> str: ...

    def save(self, state: object, path: Path) -> None: ...


class UquantAccountStateStore:
    """Use only uquant's public validator, economic identity, and atomic writer."""

    @staticmethod
    def _functions() -> tuple[
        Callable[[object], str],
        Callable[..., object],
        Callable[[object, str | Path], None],
    ]:
        module = importlib.import_module("uquant.account")
        economic = cast(Callable[[object], str], getattr(module, "economic_state_sha256", None))
        load = cast(Callable[..., object], getattr(module, "load_account", None))
        save = cast(Callable[[object, str | Path], None], getattr(module, "save_account", None))
        if not callable(economic) or not callable(load) or not callable(save):
            raise RecoveryError("uquant account persistence contract is unavailable")
        return economic, load, save

    def hash_state(self, state: object) -> str:
        economic, _, _ = self._functions()
        try:
            result = economic(state)
        except (RuntimeError, TypeError, ValueError) as error:
            raise RecoveryError("uquant account economic hash failed") from error
        _digest(result, label="uquant account economic digest")
        return result

    def hash_file(self, path: Path) -> str:
        economic, load, _ = self._functions()
        try:
            state = load(path, require_hashes=True, allow_legacy_schema=False)
            result = economic(state)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            raise RecoveryError("uquant account file is missing or corrupt") from error
        _digest(result, label="uquant account file economic digest")
        return result

    def save(self, state: object, path: Path) -> None:
        _, _, save = self._functions()
        try:
            save(state, path)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            raise RecoveryError("uquant atomic account save failed") from error


@dataclass(frozen=True, slots=True)
class AccountRecoveryReceipt:
    operation_id: str
    classification: AccountRecoveryClassification
    actual_account_sha256: str | None


@dataclass(frozen=True, slots=True)
class OrderRecoveryReceipt:
    execution_id: str
    classification: OrderRecoveryClassification
    reason_code: str


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    account_receipts: tuple[AccountRecoveryReceipt, ...]
    order_receipts: tuple[OrderRecoveryReceipt, ...]
    unresolved_order_ids: tuple[str, ...]
    blockers: tuple[str, ...]
    duplicate_orders: int
    duplicate_fills: int

    @property
    def halt_required(self) -> bool:
        return bool(self.blockers or self.unresolved_order_ids)


@dataclass(frozen=True, slots=True)
class AccountOperation:
    """In-process handle for a database-before-file-before-receipt protocol."""

    database: Database
    store: AccountStateStore
    account_path: Path
    prepared_account: object
    operation_id: str
    operation_kind: str
    account_before_sha256: str
    expected_account_after_sha256: str
    evidence_sha256: str
    path_sha256: str

    @classmethod
    def begin(
        cls,
        *,
        database: Database,
        store: AccountStateStore,
        account_path: Path,
        prepared_account: object,
        expected_before_sha256: str,
        operation_kind: str,
        evidence_sha256: str,
        now: datetime,
        operation_id: str | None = None,
        finalization_payload: Mapping[str, object] | None = None,
    ) -> AccountOperation:
        if not isinstance(database, Database):
            raise DomainTypeError("account operation database must be Database")
        if not isinstance(store, AccountStateStore):
            raise DomainTypeError("account operation store must satisfy AccountStateStore")
        path = Path(account_path)
        path_digest = _path_sha256(path)
        _digest(expected_before_sha256, label="expected account before digest")
        _digest(evidence_sha256, label="account operation evidence digest")
        _canonical_text(operation_kind, label="account operation kind", maximum=64)
        _aware(now, label="account operation begin time")
        current = store.hash_file(path)
        if current != expected_before_sha256:
            raise RecoveryContradiction("account file does not match the recorded operation precondition")
        expected_after = store.hash_state(prepared_account)
        _digest(expected_after, label="expected account after digest")
        identity = operation_id or "acctop_" + os.urandom(32).hex()
        if not isinstance(identity, str) or _ACCOUNT_OPERATION_ID.fullmatch(identity) is None:
            raise DomainValidationError("account operation id is not canonical")
        payload: dict[str, object] = {
            "schema": "firmquant.account-operation.v1",
            "operation_kind": operation_kind,
            "account_path_sha256": path_digest,
            "evidence_sha256": evidence_sha256,
        }
        if finalization_payload is not None:
            if not isinstance(finalization_payload, Mapping):
                raise DomainTypeError("account operation finalization payload must be a mapping")
            canonical_finalization = canonical_json(finalization_payload)
            decoded_finalization = json.loads(canonical_finalization)
            if not isinstance(decoded_finalization, dict):
                raise DomainValidationError("account operation finalization payload must be an object")
            payload["finalization"] = decoded_finalization
        payload_json = canonical_json(payload)
        payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        stable = (
            operation_kind,
            "PREPARED",
            expected_before_sha256,
            expected_after,
            None,
            payload_json,
            payload_sha256,
            now.isoformat(),
            now.isoformat(),
        )
        with database.transaction():
            existing = database.query_one(
                "SELECT operation_kind, stage, account_before_sha256, "
                "expected_account_after_sha256, actual_account_after_sha256, "
                "payload_json, payload_sha256, created_at, updated_at "
                "FROM account_operations WHERE operation_id = ?",
                (identity,),
            )
            if existing is None:
                database.write(
                    """
                    INSERT INTO account_operations(
                        operation_id, operation_kind, stage, account_before_sha256,
                        expected_account_after_sha256, actual_account_after_sha256,
                        payload_json, payload_sha256, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (identity, *stable),
                )
            elif tuple(existing) != stable:
                raise PersistenceConflict("account operation identity collision")
        return cls(
            database=database,
            store=store,
            account_path=path,
            prepared_account=prepared_account,
            operation_id=identity,
            operation_kind=operation_kind,
            account_before_sha256=expected_before_sha256,
            expected_account_after_sha256=expected_after,
            evidence_sha256=evidence_sha256,
            path_sha256=path_digest,
        )

    def _stage(self) -> str:
        row = self.database.query_one(
            "SELECT stage FROM account_operations WHERE operation_id = ?",
            (self.operation_id,),
        )
        if row is None:
            raise RecoveryError("account operation receipt disappeared")
        return str(row["stage"])

    def _mark_contradiction(self, actual: str | None, *, now: datetime) -> None:
        with self.database.transaction():
            self.database.write(
                "UPDATE account_operations SET stage = 'CONTRADICTION', "
                "actual_account_after_sha256 = ?, updated_at = ? WHERE operation_id = ?",
                (actual, now.isoformat(), self.operation_id),
            )

    def commit_file(self, *, now: datetime) -> None:
        _aware(now, label="account file commit time")
        if _path_sha256(self.account_path) != self.path_sha256:
            self._mark_contradiction(None, now=now)
            raise RecoveryContradiction("account state path identity changed")
        stage = self._stage()
        if stage in {"FILE_COMMITTED", "RECEIPT_COMMITTED"}:
            if self.store.hash_file(self.account_path) != self.expected_account_after_sha256:
                self._mark_contradiction(None, now=now)
                raise RecoveryContradiction("committed account file identity changed")
            return
        if stage == "CONTRADICTION":
            raise RecoveryContradiction("account operation is already contradictory")
        actual_before = self.store.hash_file(self.account_path)
        if actual_before == self.account_before_sha256:
            self.store.save(self.prepared_account, self.account_path)
        elif actual_before != self.expected_account_after_sha256:
            self._mark_contradiction(actual_before, now=now)
            raise RecoveryContradiction("account file changed before atomic commit")
        actual_after = self.store.hash_file(self.account_path)
        if actual_after != self.expected_account_after_sha256:
            self._mark_contradiction(actual_after, now=now)
            raise RecoveryContradiction("atomic account file has unexpected identity")
        with self.database.transaction():
            self.database.write(
                "UPDATE account_operations SET stage = 'FILE_COMMITTED', "
                "actual_account_after_sha256 = ?, updated_at = ? "
                "WHERE operation_id = ? AND stage = 'PREPARED'",
                (actual_after, now.isoformat(), self.operation_id),
            )

    def commit_receipt(
        self,
        *,
        now: datetime,
        finalize: Callable[[], None] | None = None,
    ) -> None:
        _aware(now, label="account receipt commit time")
        if finalize is not None and not callable(finalize):
            raise DomainTypeError("account receipt finalizer must be callable or None")
        if _path_sha256(self.account_path) != self.path_sha256:
            self._mark_contradiction(None, now=now)
            raise RecoveryContradiction("account state path identity changed")
        stage = self._stage()
        if stage == "RECEIPT_COMMITTED":
            return
        if stage != "FILE_COMMITTED":
            raise RecoveryError("account receipt requires a committed file")
        actual = self.store.hash_file(self.account_path)
        if actual != self.expected_account_after_sha256:
            self._mark_contradiction(actual, now=now)
            raise RecoveryContradiction("account file changed before receipt commit")
        with self.database.transaction():
            self.database.write(
                "UPDATE account_operations SET stage = 'RECEIPT_COMMITTED', "
                "actual_account_after_sha256 = ?, updated_at = ? "
                "WHERE operation_id = ? AND stage = 'FILE_COMMITTED'",
                (actual, now.isoformat(), self.operation_id),
            )
            AuditLedger(self.database).append(
                audit_event_id="account-operation."
                + hashlib.sha256(self.operation_id.encode("utf-8")).hexdigest(),
                category="account.operation.committed",
                actor="firmquant",
                payload={
                    "operation_id": self.operation_id,
                    "operation_kind": self.operation_kind,
                    "account_path_sha256": self.path_sha256,
                    "evidence_sha256": self.evidence_sha256,
                    "account_before_sha256": self.account_before_sha256,
                    "account_after_sha256": self.expected_account_after_sha256,
                },
                created_at=now,
            )
            if finalize is not None:
                finalize()


class RecoveryService:
    """Classify durable crash state and absorb queried broker facts without writes."""

    def __init__(
        self,
        *,
        database: Database,
        account_store: AccountStateStore | None,
        account_path: Path | None,
        gateway: BrokerGateway | None,
        clock: Callable[[], datetime],
    ) -> None:
        if not isinstance(database, Database):
            raise DomainTypeError("recovery database must be Database")
        if (account_store is None) != (account_path is None):
            raise DomainValidationError("recovery account store and path must be configured together")
        if account_store is not None and not isinstance(account_store, AccountStateStore):
            raise DomainTypeError("recovery account store must satisfy AccountStateStore")
        if gateway is not None and not isinstance(gateway, BrokerGateway):
            raise DomainTypeError("recovery gateway must satisfy BrokerGateway")
        if not callable(clock):
            raise DomainTypeError("recovery clock must be callable")
        self._database = database
        self._account_store = account_store
        self._account_path = None if account_path is None else Path(account_path)
        self._gateway = gateway
        self._clock = clock
        self._orders = ExecutionLedgerRepository(database)

    def recover(self) -> RecoveryReport:
        now = self._clock()
        _aware(now, label="recovery time")
        account_receipts, account_blockers = self._recover_accounts(now)
        order_receipts, order_blockers = self._recover_orders(now)
        unresolved = tuple(
            str(row["execution_id"])
            for row in self._database.query_all(
                "SELECT execution_id FROM execution_intents "
                "WHERE state IN ('SUBMITTING','CANCEL_REQUESTED','UNKNOWN') "
                "ORDER BY execution_id"
            )
        )
        blockers = set(account_blockers) | set(order_blockers)
        if unresolved:
            blockers.add("UNRESOLVED_ORDER_STATE")
        duplicate_orders = _count_scalar(
            self._database.scalar(
                "SELECT count(*) FROM (SELECT decision_id, uquant_order_id "
                "FROM execution_intents GROUP BY decision_id, uquant_order_id "
                "HAVING count(*) > 1)"
            ),
            label="duplicate economic order count",
        )
        duplicate_fills = _count_scalar(
            self._database.scalar(
                "SELECT count(*) FROM (SELECT broker_fill_id FROM fills "
                "GROUP BY broker_fill_id HAVING count(*) > 1)"
            ),
            label="duplicate broker fill count",
        )
        if duplicate_orders:
            blockers.add("DUPLICATE_ECONOMIC_ORDER")
        if duplicate_fills:
            blockers.add("DUPLICATE_BROKER_FILL")
        report = RecoveryReport(
            account_receipts=account_receipts,
            order_receipts=order_receipts,
            unresolved_order_ids=unresolved,
            blockers=tuple(sorted(blockers)),
            duplicate_orders=duplicate_orders,
            duplicate_fills=duplicate_fills,
        )
        self._append_report_audit(report, now=now)
        return report

    @staticmethod
    def _operation_payload(payload_json: str) -> dict[str, object] | None:
        try:
            payload: object = json.loads(payload_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _recover_broker_sync_finalization(
        self,
        *,
        row: sqlite3.Row,
        operation_id: str,
        actual: str,
        now: datetime,
    ) -> bool:
        payload = self._operation_payload(str(row["payload_json"]))
        if payload is None or "finalization" not in payload:
            return False
        finalization = payload["finalization"]
        evidence_sha256 = payload.get("evidence_sha256")
        if not isinstance(evidence_sha256, str):
            return False
        try:
            from firmquant.reconciliation.service import commit_reconciliation_finalization

            with self._database.transaction():
                self._database.write(
                    "UPDATE account_operations SET stage = 'RECEIPT_COMMITTED', "
                    "actual_account_after_sha256 = ?, updated_at = ? "
                    "WHERE operation_id = ? AND stage IN ('PREPARED','FILE_COMMITTED')",
                    (actual, now.isoformat(), operation_id),
                )
                AuditLedger(self._database).append(
                    audit_event_id="account-operation."
                    + hashlib.sha256(operation_id.encode("utf-8")).hexdigest(),
                    category="account.operation.committed",
                    actor="firmquant",
                    payload={
                        "operation_id": operation_id,
                        "operation_kind": "BROKER_SYNC",
                        "account_path_sha256": payload.get("account_path_sha256"),
                        "evidence_sha256": evidence_sha256,
                        "account_before_sha256": str(row["account_before_sha256"]),
                        "account_after_sha256": str(row["expected_account_after_sha256"]),
                    },
                    created_at=now,
                )
                commit_reconciliation_finalization(self._database, finalization)
                self._append_account_recovery_audit(
                    operation_id=operation_id,
                    classification=AccountRecoveryClassification.FILE_APPLIED_RECEIPT_MISSING,
                    actual=actual,
                    now=now,
                )
            return True
        except (
            DomainTypeError,
            DomainValidationError,
            PersistenceConflict,
            sqlite3.DatabaseError,
            ValueError,
        ):
            return False

    def _recover_accounts(self, now: datetime) -> tuple[tuple[AccountRecoveryReceipt, ...], tuple[str, ...]]:
        rows = self._database.query_all(
            "SELECT * FROM account_operations WHERE stage != 'RECEIPT_COMMITTED' "
            "ORDER BY created_at, operation_id"
        )
        if not rows:
            return (), ()
        receipts: list[AccountRecoveryReceipt] = []
        blockers: set[str] = set()
        for row in rows:
            operation_id = str(row["operation_id"])
            before = str(row["account_before_sha256"])
            expected_after = str(row["expected_account_after_sha256"])
            if str(row["stage"]) == "CONTRADICTION":
                retained_actual = row["actual_account_after_sha256"]
                receipts.append(
                    AccountRecoveryReceipt(
                        operation_id=operation_id,
                        classification=AccountRecoveryClassification.CONTRADICTION,
                        actual_account_sha256=(None if retained_actual is None else str(retained_actual)),
                    )
                )
                blockers.add("ACCOUNT_OPERATION_CONTRADICTION")
                continue
            actual: str | None = None
            classification = AccountRecoveryClassification.CONTRADICTION
            if (
                self._account_store is not None
                and self._account_path is not None
                and self._operation_path_matches(str(row["payload_json"]))
            ):
                try:
                    actual = self._account_store.hash_file(self._account_path)
                except Exception:
                    actual = None
                if actual == before:
                    classification = AccountRecoveryClassification.NOT_APPLIED
                elif actual == expected_after:
                    classification = AccountRecoveryClassification.FILE_APPLIED_RECEIPT_MISSING
            operation_kind = str(row["operation_kind"])
            original_stage = str(row["stage"])
            persisted_actual = actual
            if classification is AccountRecoveryClassification.CONTRADICTION:
                target_stage = "CONTRADICTION"
            elif operation_kind == "BROKER_SYNC":
                if (
                    classification is AccountRecoveryClassification.NOT_APPLIED
                    and original_stage == "PREPARED"
                ):
                    target_stage = "PREPARED"
                    persisted_actual = None
                    blockers.add("ACCOUNT_COMMIT_RETRY_REQUIRED")
                elif (
                    classification is AccountRecoveryClassification.FILE_APPLIED_RECEIPT_MISSING
                    and original_stage in {"PREPARED", "FILE_COMMITTED"}
                ):
                    if actual is not None and self._recover_broker_sync_finalization(
                        row=row,
                        operation_id=operation_id,
                        actual=actual,
                        now=now,
                    ):
                        receipts.append(
                            AccountRecoveryReceipt(
                                operation_id=operation_id,
                                classification=classification,
                                actual_account_sha256=actual,
                            )
                        )
                        continue
                    target_stage = "FILE_COMMITTED"
                    blockers.add("ACCOUNT_FINALIZATION_REQUIRED")
                else:
                    target_stage = "CONTRADICTION"
                    classification = AccountRecoveryClassification.CONTRADICTION
            else:
                target_stage = "RECEIPT_COMMITTED"
            with self._database.transaction():
                self._database.write(
                    "UPDATE account_operations SET stage = ?, "
                    "actual_account_after_sha256 = ?, updated_at = ? "
                    "WHERE operation_id = ?",
                    (target_stage, persisted_actual, now.isoformat(), operation_id),
                )
                self._append_account_recovery_audit(
                    operation_id=operation_id,
                    classification=classification,
                    actual=actual,
                    now=now,
                )
            receipts.append(
                AccountRecoveryReceipt(
                    operation_id=operation_id,
                    classification=classification,
                    actual_account_sha256=actual,
                )
            )
            if classification is AccountRecoveryClassification.CONTRADICTION:
                blockers.add("ACCOUNT_OPERATION_CONTRADICTION")
        return tuple(receipts), tuple(sorted(blockers))

    def _operation_path_matches(self, payload_json: str) -> bool:
        if self._account_path is None:
            return False
        try:
            payload: object = json.loads(payload_json)
            if not isinstance(payload, dict):
                return False
            expected = payload.get("account_path_sha256")
            return isinstance(expected, str) and expected == _path_sha256(self._account_path)
        except (OSError, RuntimeError, TypeError, ValueError):
            return False

    def _append_account_recovery_audit(
        self,
        *,
        operation_id: str,
        classification: AccountRecoveryClassification,
        actual: str | None,
        now: datetime,
    ) -> None:
        event_digest = hashlib.sha256(
            canonical_json(
                {
                    "operation_id": operation_id,
                    "classification": classification,
                    "actual_account_sha256": actual,
                }
            ).encode("utf-8")
        ).hexdigest()
        AuditLedger(self._database).append(
            audit_event_id="account-recovery." + event_digest,
            category="account.operation.recovery",
            actor="firmquant",
            payload={
                "operation_id": operation_id,
                "classification": classification,
                "actual_account_sha256": actual,
            },
            created_at=now,
        )

    def _recover_orders(self, now: datetime) -> tuple[tuple[OrderRecoveryReceipt, ...], tuple[str, ...]]:
        attempts = self._pending_attempts()
        known_mapping_count = _count_scalar(
            self._database.scalar("SELECT count(*) FROM broker_orders WHERE ownership = 'SYSTEM'"),
            label="known broker mapping count",
        )
        if not attempts and known_mapping_count == 0:
            return (), ()
        if self._gateway is None:
            unavailable_receipts = self._mark_attempts_unknown(
                attempts,
                now=now,
                reason="BROKER_RECOVERY_UNAVAILABLE",
            )
            unavailable_blockers = ("BROKER_RECOVERY_UNAVAILABLE",) if attempts else ()
            return unavailable_receipts, unavailable_blockers
        try:
            health = self._gateway.health()
            if not health.connected or not health.read_healthy:
                raise RecoveryError("broker is not readable during recovery")
            broker_orders = self._gateway.query_orders()
            broker_fills = self._gateway.query_fills()
        except Exception:
            unavailable_receipts = self._mark_attempts_unknown(
                attempts,
                now=now,
                reason="BROKER_RECOVERY_UNAVAILABLE",
            )
            unavailable_blockers = ("BROKER_RECOVERY_UNAVAILABLE",) if attempts else ()
            return unavailable_receipts, unavailable_blockers

        receipts: list[OrderRecoveryReceipt] = []
        blockers: set[str] = set()
        for attempt in attempts:
            aggregate = self._orders.load(attempt.execution_id)
            if aggregate is None:
                blockers.add("RECOVERY_AGGREGATE_MISSING")
                receipts.append(
                    OrderRecoveryReceipt(
                        execution_id=attempt.execution_id,
                        classification=OrderRecoveryClassification.CONTRADICTION,
                        reason_code="RECOVERY_AGGREGATE_MISSING",
                    )
                )
                continue
            if attempt.command_kind == "SUBMIT":
                candidates = tuple(
                    order
                    for order in broker_orders
                    if order.client_order_id == aggregate.intent.uquant_order_id
                )
            else:
                candidates = tuple(
                    order for order in broker_orders if order.broker_order_id == aggregate.broker_order_id
                )
            if not candidates:
                receipts.extend(
                    self._mark_attempts_unknown(
                        (attempt,),
                        now=now,
                        reason="BROKER_ACCEPTANCE_UNPROVEN",
                    )
                )
                continue
            if len(candidates) != 1:
                blockers.add("MULTIPLE_BROKER_ORDER_MATCHES")
                receipts.append(
                    OrderRecoveryReceipt(
                        execution_id=attempt.execution_id,
                        classification=OrderRecoveryClassification.CONTRADICTION,
                        reason_code="MULTIPLE_BROKER_ORDER_MATCHES",
                    )
                )
                continue
            fact = candidates[0]
            related_fills = tuple(
                fill for fill in broker_fills if fill.broker_order_id == fact.broker_order_id
            )
            if not self._broker_evidence_matches(
                aggregate,
                fact,
                related_fills,
            ):
                blockers.add("BROKER_RECOVERY_CONTRADICTION")
                receipts.append(
                    OrderRecoveryReceipt(
                        execution_id=attempt.execution_id,
                        classification=OrderRecoveryClassification.CONTRADICTION,
                        reason_code="BROKER_RECOVERY_CONTRADICTION",
                    )
                )
                continue
            try:
                with self._database.transaction():
                    if attempt.command_kind == "SUBMIT":
                        self._orders.record_submit_result(
                            aggregate,
                            attempt,
                            fact,
                            related_fills,
                            received_at=now,
                        )
                    else:
                        self._orders.record_cancel_result(
                            aggregate,
                            attempt,
                            fact,
                            related_fills,
                            received_at=now,
                        )
                receipts.append(
                    OrderRecoveryReceipt(
                        execution_id=attempt.execution_id,
                        classification=OrderRecoveryClassification.RESOLVED_FROM_BROKER,
                        reason_code="QUERIED_BROKER_FACT_APPLIED",
                    )
                )
            except (PersistenceConflict, sqlite3.DatabaseError, ValueError):
                blockers.add("BROKER_RECOVERY_CONTRADICTION")
                receipts.append(
                    OrderRecoveryReceipt(
                        execution_id=attempt.execution_id,
                        classification=OrderRecoveryClassification.CONTRADICTION,
                        reason_code="BROKER_RECOVERY_CONTRADICTION",
                    )
                )
        late_receipts, late_blockers = self._apply_late_facts(
            broker_orders,
            broker_fills,
            now=now,
        )
        receipts.extend(late_receipts)
        blockers.update(late_blockers)
        return tuple(receipts), tuple(sorted(blockers))

    @staticmethod
    def _broker_evidence_matches(
        aggregate: object,
        fact: BrokerOrderFact,
        fills: tuple[BrokerFillFact, ...],
    ) -> bool:
        from firmquant.domain.orders import OrderAggregate

        if not isinstance(aggregate, OrderAggregate):
            return False
        intent = aggregate.intent
        if (
            fact.client_order_id != intent.uquant_order_id
            or fact.symbol != intent.symbol
            or fact.side is not intent.side
            or fact.requested_shares != intent.requested_shares
        ):
            return False
        if any(
            fill.broker_order_id != fact.broker_order_id
            or fill.symbol != fact.symbol
            or fill.side is not fact.side
            or fill.status is not FillStatus.CONFIRMED
            for fill in fills
        ):
            return False
        return sum(fill.shares.value for fill in fills) == fact.filled_shares.value

    def _pending_attempts(self) -> tuple[BrokerAttempt, ...]:
        rows = self._database.query_all(
            """
            SELECT boa.attempt_id, boa.execution_id, boa.attempt_number, oc.command_kind
            FROM broker_order_attempts boa
            JOIN order_commands oc ON oc.attempt_id = boa.attempt_id
            WHERE boa.state IN ('SUBMITTING','UNKNOWN')
            ORDER BY boa.started_at, boa.attempt_id
            """
        )
        return tuple(
            BrokerAttempt(
                attempt_id=str(row["attempt_id"]),
                execution_id=str(row["execution_id"]),
                attempt_number=int(row["attempt_number"]),
                command_kind=str(row["command_kind"]),
            )
            for row in rows
        )

    def _mark_attempts_unknown(
        self,
        attempts: tuple[BrokerAttempt, ...],
        *,
        now: datetime,
        reason: str,
    ) -> tuple[OrderRecoveryReceipt, ...]:
        receipts: list[OrderRecoveryReceipt] = []
        for attempt in attempts:
            aggregate = self._orders.load(attempt.execution_id)
            if aggregate is None:
                receipts.append(
                    OrderRecoveryReceipt(
                        execution_id=attempt.execution_id,
                        classification=OrderRecoveryClassification.CONTRADICTION,
                        reason_code="RECOVERY_AGGREGATE_MISSING",
                    )
                )
                continue
            with self._database.transaction():
                self._orders.mark_attempt_unknown(
                    aggregate,
                    attempt,
                    diagnostic_code=reason,
                    occurred_at=now,
                )
            receipts.append(
                OrderRecoveryReceipt(
                    execution_id=attempt.execution_id,
                    classification=OrderRecoveryClassification.REMAINS_UNKNOWN,
                    reason_code=reason,
                )
            )
        return tuple(receipts)

    def _apply_late_facts(
        self,
        broker_orders: tuple[BrokerOrderFact, ...],
        broker_fills: tuple[BrokerFillFact, ...],
        *,
        now: datetime,
    ) -> tuple[tuple[OrderRecoveryReceipt, ...], tuple[str, ...]]:
        order_by_id = {order.broker_order_id: order for order in broker_orders}
        local_rows = self._database.query_all(
            "SELECT broker_order_id, execution_id FROM broker_orders "
            "WHERE ownership = 'SYSTEM' ORDER BY broker_order_id"
        )
        local_ids = {str(row["broker_order_id"]) for row in local_rows}
        blockers: set[str] = set()
        receipts: list[OrderRecoveryReceipt] = []
        if any(order.broker_order_id not in local_ids for order in broker_orders):
            blockers.add("EXTERNAL_BROKER_ORDER")
        if any(fill.broker_order_id not in local_ids for fill in broker_fills):
            blockers.add("UNMAPPED_BROKER_FILL")
        for row in local_rows:
            broker_order_id = str(row["broker_order_id"])
            fact = order_by_id.get(broker_order_id)
            if fact is None:
                continue
            aggregate = self._orders.load(str(row["execution_id"]))
            if aggregate is None:
                blockers.add("RECOVERY_AGGREGATE_MISSING")
                continue
            related_fills = tuple(fill for fill in broker_fills if fill.broker_order_id == broker_order_id)
            before_version = aggregate.version
            try:
                with self._database.transaction():
                    recovered = self._orders.reconcile_broker_fact(
                        aggregate,
                        fact,
                        related_fills,
                        received_at=now,
                    )
            except (PersistenceConflict, RuntimeError, sqlite3.DatabaseError, ValueError):
                blockers.add("BROKER_RECOVERY_CONTRADICTION")
                continue
            if recovered.version != before_version:
                receipts.append(
                    OrderRecoveryReceipt(
                        execution_id=recovered.intent.execution_id,
                        classification=OrderRecoveryClassification.LATE_FACT_APPLIED,
                        reason_code="QUERIED_LATE_BROKER_FACT_APPLIED",
                    )
                )
            if recovered.late_fill_investigation_required:
                blockers.add("LATE_FILL_INVESTIGATION_REQUIRED")
        return tuple(receipts), tuple(sorted(blockers))

    def _append_report_audit(self, report: RecoveryReport, *, now: datetime) -> None:
        payload = {
            "schema": "firmquant.recovery-report.v1",
            "observed_at": now,
            "account_classifications": [
                {
                    "operation_id": item.operation_id,
                    "classification": item.classification,
                    "actual_account_sha256": item.actual_account_sha256,
                }
                for item in report.account_receipts
            ],
            "order_classifications": [
                {
                    "execution_id": item.execution_id,
                    "classification": item.classification,
                    "reason_code": item.reason_code,
                }
                for item in report.order_receipts
            ],
            "unresolved_order_ids": report.unresolved_order_ids,
            "blockers": report.blockers,
            "duplicate_orders": report.duplicate_orders,
            "duplicate_fills": report.duplicate_fills,
        }
        digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
        with self._database.transaction():
            AuditLedger(self._database).append(
                audit_event_id="recovery-report." + digest,
                category="recovery.report",
                actor="firmquant",
                payload=payload,
                created_at=now,
            )


__all__ = (
    "AccountOperation",
    "AccountRecoveryClassification",
    "AccountRecoveryReceipt",
    "AccountStateStore",
    "OrderRecoveryClassification",
    "OrderRecoveryReceipt",
    "RecoveryContradiction",
    "RecoveryError",
    "RecoveryReport",
    "RecoveryService",
    "UquantAccountStateStore",
)
