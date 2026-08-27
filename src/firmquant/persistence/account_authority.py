"""Durable single-account authority binding and reviewed adjustment evidence."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from firmquant.domain.broker_facts import AccountType
from firmquant.domain.values import Symbol

from .audit import AuditLedger
from .database import Database, TransactionRequired
from .repositories import PersistenceConflict, canonical_json

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SHA1 = re.compile(r"^[0-9a-f]{40}$")


class AdjustmentCoverage(StrEnum):
    AVAILABLE_CASH = "AVAILABLE_CASH"
    TOTAL_ASSETS = "TOTAL_ASSETS"
    POSITION_TOTAL_SHARES = "POSITION_TOTAL_SHARES"
    POSITION_SELLABLE_SHARES = "POSITION_SELLABLE_SHARES"


def _sha256(value: str, *, label: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be lowercase SHA-256")


def _sha1(value: str, *, label: str) -> None:
    if not isinstance(value, str) or _SHA1.fullmatch(value) is None:
        raise ValueError(f"{label} must be lowercase SHA-1")


def _text(value: str, *, label: str, maximum: int = 256) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{label} must be canonical non-empty text")


def _aware(value: datetime, *, label: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{label} must be datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


def ensure_account_authority_schema(database: Database) -> None:
    """Verify the centrally checksummed authority schema is applied."""

    if not isinstance(database, Database):
        raise TypeError("account authority schema requires Database")
    from .schema import apply_migrations

    apply_migrations(database)


@dataclass(frozen=True, slots=True)
class AccountBinding:
    binding_id: str
    account_id_hash: str
    account_type: AccountType
    broker_snapshot_sha256: str
    account_state_sha256: str
    uquant_commit: str
    uquant_code_fingerprint: str
    data_hash: str
    data_as_of: str
    data_symbols: tuple[str, ...]
    created_at: datetime
    payload_json: str
    payload_sha256: str

    @classmethod
    def create(
        cls,
        *,
        account_id_hash: str,
        account_type: AccountType,
        broker_snapshot_sha256: str,
        account_state_sha256: str,
        uquant_commit: str,
        uquant_code_fingerprint: str,
        data_hash: str,
        data_as_of: str,
        data_symbols: tuple[str, ...],
        created_at: datetime,
    ) -> AccountBinding:
        _sha256(account_id_hash, label="account identity hash")
        if account_type is not AccountType.CASH:
            raise ValueError("account binding requires CASH account")
        _sha256(broker_snapshot_sha256, label="broker snapshot hash")
        _sha256(account_state_sha256, label="account state hash")
        _sha1(uquant_commit, label="uquant commit")
        _sha256(uquant_code_fingerprint, label="uquant code fingerprint")
        _sha256(data_hash, label="data identity hash")
        _text(data_as_of, label="data as-of")
        if not isinstance(data_symbols, tuple) or not data_symbols:
            raise ValueError("data symbols must be a non-empty tuple")
        if tuple(sorted(set(data_symbols))) != data_symbols:
            raise ValueError("data symbols must be sorted and unique")
        for symbol in data_symbols:
            _text(symbol, label="data symbol")
            Symbol.parse(symbol)
        _aware(created_at, label="account binding created_at")
        payload = {
            "schema": "firmquant.account-binding.v1",
            "account_id_hash": account_id_hash,
            "account_type": account_type,
            "broker_snapshot_sha256": broker_snapshot_sha256,
            "account_state_sha256": account_state_sha256,
            "uquant_commit": uquant_commit,
            "uquant_code_fingerprint": uquant_code_fingerprint,
            "data_hash": data_hash,
            "data_as_of": data_as_of,
            "data_symbols": data_symbols,
            "created_at": created_at,
        }
        payload_json = canonical_json(payload)
        payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        return cls(
            binding_id="acctbind_" + payload_sha256,
            account_id_hash=account_id_hash,
            account_type=account_type,
            broker_snapshot_sha256=broker_snapshot_sha256,
            account_state_sha256=account_state_sha256,
            uquant_commit=uquant_commit,
            uquant_code_fingerprint=uquant_code_fingerprint,
            data_hash=data_hash,
            data_as_of=data_as_of,
            data_symbols=data_symbols,
            created_at=created_at,
            payload_json=payload_json,
            payload_sha256=payload_sha256,
        )


class AccountBindingRepository:
    def __init__(self, database: Database) -> None:
        if not isinstance(database, Database):
            raise TypeError("account binding repository requires Database")
        self._database = database
        ensure_account_authority_schema(database)

    @staticmethod
    def _from_row(row: object) -> AccountBinding:
        if not hasattr(row, "__getitem__"):
            raise PersistenceConflict("account binding row is invalid")
        try:
            raw_symbols = json.loads(str(row["data_symbols_json"]))
            if not isinstance(raw_symbols, list) or not all(isinstance(item, str) for item in raw_symbols):
                raise ValueError
            binding = AccountBinding.create(
                account_id_hash=str(row["account_id_hash"]),
                account_type=AccountType(str(row["account_type"])),
                broker_snapshot_sha256=str(row["broker_snapshot_sha256"]),
                account_state_sha256=str(row["account_state_sha256"]),
                uquant_commit=str(row["uquant_commit"]),
                uquant_code_fingerprint=str(row["uquant_code_fingerprint"]),
                data_hash=str(row["data_hash"]),
                data_as_of=str(row["data_as_of"]),
                data_symbols=tuple(raw_symbols),
                created_at=datetime.fromisoformat(str(row["created_at"])),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise PersistenceConflict("account binding row is invalid") from error
        if (
            binding.binding_id != str(row["binding_id"])
            or binding.payload_json != str(row["payload_json"])
            or binding.payload_sha256 != str(row["payload_sha256"])
        ):
            raise PersistenceConflict("account binding payload identity mismatch")
        return binding

    def load(self) -> AccountBinding | None:
        row = self._database.query_one("SELECT * FROM account_bindings WHERE singleton_id = 1")
        return None if row is None else self._from_row(row)

    def bind_in_transaction(self, binding: AccountBinding) -> AccountBinding:
        """Bind the singleton account inside the caller's existing SQLite transaction."""

        if not isinstance(binding, AccountBinding):
            raise TypeError("account binding must be typed")
        if not self._database.in_transaction:
            raise TransactionRequired("account binding requires an active SQLite transaction")
        existing = self.load()
        if existing is not None:
            if existing == binding:
                return existing
            raise PersistenceConflict("account is already bound to a different authority identity")
        self._database.write(
            """
            INSERT INTO account_bindings(
                binding_id, singleton_id, account_id_hash, account_type,
                broker_snapshot_sha256, account_state_sha256, uquant_commit,
                uquant_code_fingerprint, data_hash, data_as_of, data_symbols_json,
                payload_json, payload_sha256, created_at
            ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                binding.binding_id,
                binding.account_id_hash,
                binding.account_type.value,
                binding.broker_snapshot_sha256,
                binding.account_state_sha256,
                binding.uquant_commit,
                binding.uquant_code_fingerprint,
                binding.data_hash,
                binding.data_as_of,
                canonical_json(binding.data_symbols),
                binding.payload_json,
                binding.payload_sha256,
                binding.created_at.isoformat(),
            ),
        )
        AuditLedger(self._database).append(
            audit_event_id="account-binding." + binding.payload_sha256,
            category="account.binding",
            actor="account-bootstrap",
            payload={
                "schema": "firmquant.account-binding-audit.v1",
                "binding_id": binding.binding_id,
                "account_hash": binding.account_id_hash,
                "broker_snapshot_sha256": binding.broker_snapshot_sha256,
                "account_state_sha256": binding.account_state_sha256,
                "uquant_commit": binding.uquant_commit,
                "uquant_code_fingerprint": binding.uquant_code_fingerprint,
                "data_hash": binding.data_hash,
            },
            created_at=binding.created_at,
        )
        return binding

    def bind(self, binding: AccountBinding) -> AccountBinding:
        with self._database.transaction():
            return self.bind_in_transaction(binding)



@dataclass(frozen=True, slots=True)
class ReviewedAccountAdjustment:
    adjustment_id: str
    account_id_hash: str
    symbol: Symbol
    session: date
    adjustment_type: str
    coverage: AdjustmentCoverage
    broker_snapshot_sha256: str
    difference_sha256: str
    audit_summary_sha256: str
    created_at: datetime
    payload_json: str
    payload_sha256: str

    @classmethod
    def create(
        cls,
        *,
        account_id_hash: str,
        symbol: Symbol,
        session: date,
        adjustment_type: str,
        coverage: AdjustmentCoverage,
        broker_snapshot_sha256: str,
        difference_sha256: str,
        audit_summary_sha256: str,
        created_at: datetime,
    ) -> ReviewedAccountAdjustment:
        _sha256(account_id_hash, label="adjustment account identity")
        if not isinstance(symbol, Symbol):
            raise TypeError("adjustment symbol must be Symbol")
        if type(session) is not date:
            raise TypeError("adjustment session must be date")
        _text(adjustment_type, label="adjustment type", maximum=64)
        if not isinstance(coverage, AdjustmentCoverage):
            raise TypeError("adjustment coverage must be typed")
        _sha256(broker_snapshot_sha256, label="adjustment broker snapshot")
        _sha256(difference_sha256, label="adjustment difference")
        _sha256(audit_summary_sha256, label="adjustment audit summary")
        _aware(created_at, label="adjustment created_at")
        payload = {
            "schema": "firmquant.reviewed-account-adjustment.v1",
            "account_id_hash": account_id_hash,
            "symbol": symbol,
            "session": session,
            "adjustment_type": adjustment_type,
            "coverage": coverage,
            "broker_snapshot_sha256": broker_snapshot_sha256,
            "difference_sha256": difference_sha256,
            "audit_summary_sha256": audit_summary_sha256,
            "created_at": created_at,
        }
        payload_json = canonical_json(payload)
        payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        return cls(
            adjustment_id="acctadj_" + payload_sha256,
            account_id_hash=account_id_hash,
            symbol=symbol,
            session=session,
            adjustment_type=adjustment_type,
            coverage=coverage,
            broker_snapshot_sha256=broker_snapshot_sha256,
            difference_sha256=difference_sha256,
            audit_summary_sha256=audit_summary_sha256,
            created_at=created_at,
            payload_json=payload_json,
            payload_sha256=payload_sha256,
        )


class ReviewedAccountAdjustmentRepository:
    def __init__(self, database: Database) -> None:
        if not isinstance(database, Database):
            raise TypeError("reviewed adjustment repository requires Database")
        self._database = database
        ensure_account_authority_schema(database)

    def append(self, adjustment: ReviewedAccountAdjustment) -> bool:
        if not isinstance(adjustment, ReviewedAccountAdjustment):
            raise TypeError("reviewed account adjustment must be typed")
        row = self._database.query_one(
            "SELECT payload_json, payload_sha256 FROM reviewed_account_adjustments WHERE adjustment_id = ?",
            (adjustment.adjustment_id,),
        )
        if row is not None:
            if tuple(row) == (adjustment.payload_json, adjustment.payload_sha256):
                return False
            raise PersistenceConflict("reviewed adjustment identity collision")
        with self._database.transaction():
            self._database.write(
                """
                INSERT INTO reviewed_account_adjustments(
                    adjustment_id, account_id_hash, symbol, session_date, adjustment_type,
                    coverage_kind, broker_snapshot_sha256, difference_sha256,
                    audit_summary_sha256, payload_json, payload_sha256, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    adjustment.adjustment_id,
                    adjustment.account_id_hash,
                    adjustment.symbol.canonical,
                    adjustment.session.isoformat(),
                    adjustment.adjustment_type,
                    adjustment.coverage.value,
                    adjustment.broker_snapshot_sha256,
                    adjustment.difference_sha256,
                    adjustment.audit_summary_sha256,
                    adjustment.payload_json,
                    adjustment.payload_sha256,
                    adjustment.created_at.isoformat(),
                ),
            )
            AuditLedger(self._database).append(
                audit_event_id="account-adjustment." + adjustment.payload_sha256,
                category="account.adjustment.reviewed",
                actor="operator-review",
                payload={
                    "schema": "firmquant.reviewed-account-adjustment-audit.v1",
                    "adjustment_id": adjustment.adjustment_id,
                    "account_hash": adjustment.account_id_hash,
                    "symbol": adjustment.symbol,
                    "session": adjustment.session,
                    "coverage": adjustment.coverage,
                    "broker_snapshot_sha256": adjustment.broker_snapshot_sha256,
                    "difference_sha256": adjustment.difference_sha256,
                    "audit_summary_sha256": adjustment.audit_summary_sha256,
                },
                created_at=adjustment.created_at,
            )
        return True

    def matching_ids(
        self,
        *,
        account_id_hash: str,
        symbol: Symbol | None,
        session: date,
        coverage: AdjustmentCoverage,
        broker_snapshot_sha256: str,
        difference_sha256: str,
    ) -> tuple[str, ...]:
        """Return exact reviewed evidence identities after re-verifying stored payload hashes."""

        _sha256(account_id_hash, label="adjustment lookup account identity")
        if symbol is not None and not isinstance(symbol, Symbol):
            raise TypeError("adjustment lookup symbol must be Symbol or None")
        if type(session) is not date:
            raise TypeError("adjustment lookup session must be date")
        if not isinstance(coverage, AdjustmentCoverage):
            raise TypeError("adjustment lookup coverage must be typed")
        _sha256(broker_snapshot_sha256, label="adjustment lookup broker snapshot")
        _sha256(difference_sha256, label="adjustment lookup difference")
        parameters: tuple[object, ...] = (
            account_id_hash,
            session.isoformat(),
            coverage.value,
            broker_snapshot_sha256,
            difference_sha256,
        )
        if symbol is None:
            rows = self._database.query_all(
                """
                SELECT adjustment_id, payload_json, payload_sha256
                FROM reviewed_account_adjustments
                WHERE account_id_hash = ? AND session_date = ?
                  AND coverage_kind = ? AND broker_snapshot_sha256 = ? AND difference_sha256 = ?
                ORDER BY adjustment_id
                """,
                parameters,
            )
        else:
            rows = self._database.query_all(
                """
                SELECT adjustment_id, payload_json, payload_sha256
                FROM reviewed_account_adjustments
                WHERE account_id_hash = ? AND session_date = ?
                  AND coverage_kind = ? AND broker_snapshot_sha256 = ? AND difference_sha256 = ?
                  AND symbol = ?
                ORDER BY adjustment_id
                """,
                (*parameters, symbol.canonical),
            )
        identities: list[str] = []
        for row in rows:
            payload_json = str(row["payload_json"])
            payload_sha256 = str(row["payload_sha256"])
            actual = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
            adjustment_id = str(row["adjustment_id"])
            if actual != payload_sha256 or adjustment_id != "acctadj_" + payload_sha256:
                raise PersistenceConflict("reviewed adjustment stored identity is corrupt")
            identities.append(adjustment_id)
        return tuple(identities)

    def covers(
        self,
        *,
        account_id_hash: str,
        symbol: Symbol,
        session: date,
        coverage: AdjustmentCoverage,
        broker_snapshot_sha256: str,
        difference_sha256: str,
    ) -> bool:
        return bool(
            self.matching_ids(
                account_id_hash=account_id_hash,
                symbol=symbol,
                session=session,
                coverage=coverage,
                broker_snapshot_sha256=broker_snapshot_sha256,
                difference_sha256=difference_sha256,
            )
        )


__all__ = (
    "AccountBinding",
    "AccountBindingRepository",
    "AdjustmentCoverage",
    "ReviewedAccountAdjustment",
    "ReviewedAccountAdjustmentRepository",
    "ensure_account_authority_schema",
)
