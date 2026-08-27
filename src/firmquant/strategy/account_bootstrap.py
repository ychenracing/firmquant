"""One-time reviewed bootstrap of the sole production uquant account state."""

from __future__ import annotations

import hashlib
import importlib
import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Protocol, cast

from firmquant.domain.broker_facts import AccountType, BrokerSnapshot
from firmquant.persistence.account_authority import (
    AccountBinding,
    AccountBindingRepository,
    ensure_account_authority_schema,
)
from firmquant.persistence.audit import AuditLedger
from firmquant.persistence.database import Database
from firmquant.persistence.recovery import UquantAccountStateStore

from .account_sync import AccountStateContract
from .identity import StrategyIdentity, StrategyIdentityViolation


class AccountBootstrapDenied(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True, slots=True)
class BootstrapDataIdentity:
    data_hash: str
    as_of: str
    symbols: tuple[str, ...]

    def __post_init__(self) -> None:
        if len(self.data_hash) != 64 or any(
            character not in "0123456789abcdef" for character in self.data_hash
        ):
            raise ValueError("bootstrap data hash must be lowercase SHA-256")
        if not isinstance(self.as_of, str) or not self.as_of:
            raise ValueError("bootstrap data as-of must be non-empty")
        if not isinstance(self.symbols, tuple) or not self.symbols:
            raise ValueError("bootstrap data symbols must be a non-empty tuple")
        if tuple(sorted(set(self.symbols))) != self.symbols:
            raise ValueError("bootstrap data symbols must be sorted and unique")


@dataclass(frozen=True, slots=True)
class AccountBootstrapReceipt:
    binding_id: str
    account_state_sha256: str
    broker_snapshot_sha256: str


@dataclass(frozen=True, slots=True)
class _PendingBootstrap:
    operation_id: str
    stage: str
    account_state_sha256: str
    broker_snapshot_sha256: str
    binding: AccountBinding


class _LoadAccount(Protocol):
    def __call__(
        self,
        path: Path,
        *,
        require_hashes: bool,
        allow_legacy_schema: bool,
    ) -> object: ...


class _AccountStateFactory(Protocol):
    @classmethod
    def empty(cls, cash: float) -> object: ...


class _MutableAccountIdentity(Protocol):
    code_hash: str
    data_hash: str
    data_hash_as_of: str
    data_hash_symbols: list[str]


class AccountBootstrapService:
    def __init__(
        self,
        *,
        database: Database,
        account_path: Path,
        data_identity_provider: Callable[[BrokerSnapshot], BootstrapDataIdentity],
        clock: Callable[[], datetime],
    ) -> None:
        if not isinstance(database, Database):
            raise TypeError("account bootstrap requires Database")
        if not isinstance(account_path, Path):
            raise TypeError("account bootstrap path must be Path")
        if not callable(data_identity_provider) or not callable(clock):
            raise TypeError("account bootstrap providers must be callable")
        self._database = database
        self._account_path = account_path
        self._data_identity_provider = data_identity_provider
        self._clock = clock
        self._store = UquantAccountStateStore()
        ensure_account_authority_schema(database)
        self._bindings = AccountBindingRepository(database)

    def _now(self) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise AccountBootstrapDenied("CLOCK_UNAVAILABLE")
        return now

    @staticmethod
    def _count(value: object, *, code: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise AccountBootstrapDenied(code)
        return value

    def _preconditions(self) -> None:
        if self._bindings.load() is not None:
            raise AccountBootstrapDenied("ACCOUNT_ALREADY_BOUND")
        runtime = self._database.query_one("SELECT state FROM runtime_state WHERE singleton_id = 1")
        if runtime is not None and str(runtime["state"]) != "DISARMED":
            raise AccountBootstrapDenied("RUNTIME_NOT_DISARMED")
        if self._count(
            self._database.scalar("SELECT count(*) FROM arm_leases WHERE revoked_at IS NULL"),
            code="DATABASE_STATE_INVALID",
        ):
            raise AccountBootstrapDenied("ACTIVE_ARM_LEASE_PRESENT")
        economic_history = sum(
            self._count(self._database.scalar(query), code="DATABASE_STATE_INVALID")
            for query in (
                "SELECT count(*) FROM decision_snapshots",
                "SELECT count(*) FROM execution_intents",
                "SELECT count(*) FROM broker_orders",
                "SELECT count(*) FROM fills",
            )
        )
        if economic_history:
            raise AccountBootstrapDenied("ACCOUNT_ECONOMIC_HISTORY_PRESENT")
        if self._count(
            self._database.scalar(
                "SELECT count(*) FROM account_operations WHERE stage != 'RECEIPT_COMMITTED'"
            ),
            code="DATABASE_STATE_INVALID",
        ):
            raise AccountBootstrapDenied("ACCOUNT_TRANSACTION_IN_PROGRESS")
        if self._account_path.is_symlink():
            raise AccountBootstrapDenied("ACCOUNT_STATE_PATH_INVALID")
        if self._account_path.exists():
            raise AccountBootstrapDenied("UNBOUND_ACCOUNT_STATE_PRESENT")

    @staticmethod
    def _economic_summary(snapshot: BrokerSnapshot) -> None:
        market_value = sum((position.market_value.value for position in snapshot.positions), Decimal(0))
        if snapshot.account.total_assets.value != snapshot.account.available_cash.value + market_value:
            raise AccountBootstrapDenied("BROKER_ECONOMIC_SUMMARY_INVALID")

    @staticmethod
    def _identity() -> StrategyIdentity:
        try:
            identity = StrategyIdentity.locked()
            identity.verify()
            return identity
        except StrategyIdentityViolation as error:
            raise AccountBootstrapDenied("UQUANT_IDENTITY_UNAVAILABLE") from error

    @staticmethod
    def _strict_load(path: Path) -> AccountStateContract:
        if path.is_symlink() or not path.is_file():
            raise AccountBootstrapDenied("ACCOUNT_STATE_SEED_INVALID")
        try:
            module = importlib.import_module("uquant.account")
            loader = cast(_LoadAccount, module.load_account)
            loaded = loader(path, require_hashes=True, allow_legacy_schema=False)
        except Exception as error:
            raise AccountBootstrapDenied("ACCOUNT_STATE_SEED_INVALID") from error
        if not hasattr(loaded, "to_dict"):
            raise AccountBootstrapDenied("ACCOUNT_STATE_SEED_INVALID")
        return cast(AccountStateContract, loaded)

    @staticmethod
    def _cash_float(value: Decimal) -> float:
        converted = float(value)
        if not math.isfinite(converted) or Decimal(str(converted)) != value:
            raise AccountBootstrapDenied("ACCOUNT_CASH_PRECISION_INVALID")
        if converted <= 0:
            raise AccountBootstrapDenied("ACCOUNT_CASH_INVALID")
        return converted

    @staticmethod
    def _empty_account(cash: Decimal) -> AccountStateContract:
        try:
            module = importlib.import_module("uquant.types")
            factory = cast(type[_AccountStateFactory], module.AccountState)
            account = factory.empty(AccountBootstrapService._cash_float(cash))
        except AccountBootstrapDenied:
            raise
        except Exception as error:
            raise AccountBootstrapDenied("UQUANT_ACCOUNT_CONTRACT_UNAVAILABLE") from error
        return cast(AccountStateContract, account)

    @staticmethod
    def _set_identity(
        account: AccountStateContract,
        *,
        identity: StrategyIdentity,
        data: BootstrapDataIdentity,
    ) -> None:
        try:
            target = cast(_MutableAccountIdentity, account)
            target.code_hash = identity.economic_code_fingerprint
            target.data_hash = data.data_hash
            target.data_hash_as_of = data.as_of
            target.data_hash_symbols = list(data.symbols)
        except (AttributeError, TypeError) as error:
            raise AccountBootstrapDenied("UQUANT_ACCOUNT_CONTRACT_UNAVAILABLE") from error

    @staticmethod
    def _seed_cash(account: object) -> Decimal:
        raw = getattr(account, "cash", None)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(float(raw)):
            raise AccountBootstrapDenied("ACCOUNT_STATE_SEED_INVALID")
        return Decimal(str(raw))

    @staticmethod
    def _validate_seed(
        account: AccountStateContract,
        *,
        snapshot: BrokerSnapshot,
        identity: StrategyIdentity,
        data: BootstrapDataIdentity,
    ) -> None:
        if getattr(account, "code_hash", None) != identity.economic_code_fingerprint:
            raise AccountBootstrapDenied("SEED_CODE_IDENTITY_MISMATCH")
        if (
            getattr(account, "data_hash", None) != data.data_hash
            or getattr(account, "data_hash_as_of", None) != data.as_of
            or tuple(getattr(account, "data_hash_symbols", ())) != data.symbols
        ):
            raise AccountBootstrapDenied("SEED_DATA_IDENTITY_MISMATCH")
        if AccountBootstrapService._seed_cash(account) != snapshot.account.available_cash.value:
            raise AccountBootstrapDenied("SEED_CASH_MISMATCH")
        positions = getattr(account, "positions", None)
        if not isinstance(positions, dict):
            raise AccountBootstrapDenied("ACCOUNT_STATE_SEED_INVALID")
        broker = {position.symbol.canonical: position for position in snapshot.positions}
        if set(positions) != set(broker):
            raise AccountBootstrapDenied("SEED_POSITION_MISMATCH")
        session = snapshot.session_date.isoformat()
        for symbol, broker_position in broker.items():
            seed_position = positions[symbol]
            if getattr(seed_position, "shares", None) != broker_position.total_shares.value:
                raise AccountBootstrapDenied("SEED_POSITION_MISMATCH")
            sellable = getattr(seed_position, "sellable_shares", None)
            if not callable(sellable):
                raise AccountBootstrapDenied("ACCOUNT_STATE_SEED_INVALID")
            if sellable(session) != broker_position.sellable_shares.value:
                raise AccountBootstrapDenied("SEED_SELLABLE_MISMATCH")
        if getattr(account, "pending_orders", None):
            raise AccountBootstrapDenied("SEED_PENDING_ORDER_UNOWNED")

    @staticmethod
    def _operation_id(binding: AccountBinding) -> str:
        return "acctboot_" + hashlib.sha256(binding.binding_id.encode("utf-8")).hexdigest()

    @staticmethod
    def _binding_from_payload(payload_json: str, payload_sha256: str) -> AccountBinding:
        if hashlib.sha256(payload_json.encode("utf-8")).hexdigest() != payload_sha256:
            raise AccountBootstrapDenied("ACCOUNT_BOOTSTRAP_OPERATION_INVALID")
        try:
            payload = json.loads(payload_json)
            if not isinstance(payload, dict):
                raise ValueError
            expected_keys = {
                "schema",
                "account_id_hash",
                "account_type",
                "broker_snapshot_sha256",
                "account_state_sha256",
                "uquant_commit",
                "uquant_code_fingerprint",
                "data_hash",
                "data_as_of",
                "data_symbols",
                "created_at",
            }
            if set(payload) != expected_keys or payload["schema"] != "firmquant.account-binding.v1":
                raise ValueError
            raw_symbols = payload["data_symbols"]
            if not isinstance(raw_symbols, list) or not all(isinstance(item, str) for item in raw_symbols):
                raise ValueError
            binding = AccountBinding.create(
                account_id_hash=str(payload["account_id_hash"]),
                account_type=AccountType(str(payload["account_type"])),
                broker_snapshot_sha256=str(payload["broker_snapshot_sha256"]),
                account_state_sha256=str(payload["account_state_sha256"]),
                uquant_commit=str(payload["uquant_commit"]),
                uquant_code_fingerprint=str(payload["uquant_code_fingerprint"]),
                data_hash=str(payload["data_hash"]),
                data_as_of=str(payload["data_as_of"]),
                data_symbols=tuple(raw_symbols),
                created_at=datetime.fromisoformat(str(payload["created_at"])),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise AccountBootstrapDenied("ACCOUNT_BOOTSTRAP_OPERATION_INVALID") from error
        if binding.payload_json != payload_json or binding.payload_sha256 != payload_sha256:
            raise AccountBootstrapDenied("ACCOUNT_BOOTSTRAP_OPERATION_INVALID")
        return binding

    def _pending_bootstrap(self) -> _PendingBootstrap | None:
        rows = self._database.query_all(
            "SELECT * FROM account_bootstrap_operations WHERE stage != 'BINDING_COMMITTED' "
            "ORDER BY created_at, operation_id"
        )
        if not rows:
            return None
        if len(rows) != 1:
            raise AccountBootstrapDenied("ACCOUNT_BOOTSTRAP_OPERATION_CONFLICT")
        row = rows[0]
        stage = str(row["stage"])
        if stage == "CONTRADICTION":
            raise AccountBootstrapDenied("ACCOUNT_BOOTSTRAP_CONTRADICTION")
        if stage not in {"PREPARED", "FILE_COMMITTED"}:
            raise AccountBootstrapDenied("ACCOUNT_BOOTSTRAP_OPERATION_INVALID")
        payload_json = str(row["binding_payload_json"])
        payload_sha256 = str(row["binding_payload_sha256"])
        binding = self._binding_from_payload(payload_json, payload_sha256)
        operation_id = str(row["operation_id"])
        account_state_sha256 = str(row["account_state_sha256"])
        broker_snapshot_sha256 = str(row["broker_snapshot_sha256"])
        if (
            operation_id != self._operation_id(binding)
            or account_state_sha256 != binding.account_state_sha256
            or broker_snapshot_sha256 != binding.broker_snapshot_sha256
        ):
            raise AccountBootstrapDenied("ACCOUNT_BOOTSTRAP_OPERATION_INVALID")
        return _PendingBootstrap(
            operation_id=operation_id,
            stage=stage,
            account_state_sha256=account_state_sha256,
            broker_snapshot_sha256=broker_snapshot_sha256,
            binding=binding,
        )

    @staticmethod
    def _receipt(binding: AccountBinding) -> AccountBootstrapReceipt:
        return AccountBootstrapReceipt(
            binding_id=binding.binding_id,
            account_state_sha256=binding.account_state_sha256,
            broker_snapshot_sha256=binding.broker_snapshot_sha256,
        )

    def _finalize_bootstrap(
        self,
        pending: _PendingBootstrap,
        *,
        completed: datetime,
    ) -> AccountBootstrapReceipt:
        with self._database.transaction():
            self._bindings.bind_in_transaction(pending.binding)
            cursor = self._database.write(
                "UPDATE account_bootstrap_operations SET stage = 'BINDING_COMMITTED', updated_at = ? "
                "WHERE operation_id = ? AND stage IN ('PREPARED','FILE_COMMITTED')",
                (completed.isoformat(), pending.operation_id),
            )
            if cursor.rowcount != 1:
                raise AccountBootstrapDenied("ACCOUNT_BOOTSTRAP_OPERATION_INVALID")
            AuditLedger(self._database).append(
                audit_event_id="account-bootstrap." + pending.binding.payload_sha256,
                category="account.bootstrap",
                actor="account-bootstrap",
                payload={
                    "schema": "firmquant.account-bootstrap-audit.v1",
                    "binding_id": pending.binding.binding_id,
                    "account_state_sha256": pending.account_state_sha256,
                    "broker_snapshot_sha256": pending.broker_snapshot_sha256,
                    "operation_id": pending.operation_id,
                },
                created_at=completed,
            )
        return self._receipt(pending.binding)

    def _recover_file_applied(
        self,
        pending: _PendingBootstrap | None,
    ) -> AccountBootstrapReceipt | None:
        if pending is None:
            return None
        if self._account_path.is_symlink():
            raise AccountBootstrapDenied("ACCOUNT_STATE_PATH_INVALID")
        if not self._account_path.exists():
            if pending.stage == "FILE_COMMITTED":
                raise AccountBootstrapDenied("ACCOUNT_BOOTSTRAP_CONTRADICTION")
            return None
        if not self._account_path.is_file():
            raise AccountBootstrapDenied("ACCOUNT_STATE_PATH_INVALID")
        try:
            actual = self._store.hash_file(self._account_path)
        except Exception as error:
            raise AccountBootstrapDenied("ACCOUNT_BOOTSTRAP_CONTRADICTION") from error
        if actual != pending.account_state_sha256:
            raise AccountBootstrapDenied("ACCOUNT_BOOTSTRAP_CONTRADICTION")
        return self._finalize_bootstrap(pending, completed=self._now())

    @staticmethod
    def _validate_pending_candidate(
        pending: _PendingBootstrap,
        *,
        snapshot: BrokerSnapshot,
        identity: StrategyIdentity,
        data: BootstrapDataIdentity,
        account_state_sha256: str,
    ) -> None:
        binding = pending.binding
        if (
            pending.account_state_sha256 != account_state_sha256
            or binding.account_id_hash != snapshot.account.account_id_hash
            or binding.account_type is not snapshot.account.account_type
            or binding.uquant_commit != identity.uquant_commit
            or binding.uquant_code_fingerprint != identity.economic_code_fingerprint
            or binding.data_hash != data.data_hash
            or binding.data_as_of != data.as_of
            or binding.data_symbols != data.symbols
        ):
            raise AccountBootstrapDenied("ACCOUNT_BOOTSTRAP_RECOVERY_MISMATCH")

    def bootstrap(
        self,
        snapshot: BrokerSnapshot,
        *,
        seed_path: Path | None = None,
    ) -> AccountBootstrapReceipt:
        if not isinstance(snapshot, BrokerSnapshot) or not snapshot.complete:
            raise AccountBootstrapDenied("BROKER_SNAPSHOT_INVALID")
        if snapshot.account.account_type is not AccountType.CASH:
            raise AccountBootstrapDenied("ACCOUNT_TYPE_UNSUPPORTED")

        pending = self._pending_bootstrap()
        recovered = self._recover_file_applied(pending)
        if recovered is not None:
            return recovered

        if snapshot.orders or snapshot.fills:
            raise AccountBootstrapDenied("BROKER_ACTIVITY_PRESENT")
        self._preconditions()
        self._economic_summary(snapshot)
        identity = self._identity()
        data = self._data_identity_provider(snapshot)
        if not isinstance(data, BootstrapDataIdentity):
            raise AccountBootstrapDenied("DATA_IDENTITY_INVALID")
        if snapshot.positions:
            if seed_path is None:
                raise AccountBootstrapDenied("ACCOUNT_STATE_SEED_REQUIRED")
            candidate = self._strict_load(seed_path)
            self._validate_seed(candidate, snapshot=snapshot, identity=identity, data=data)
        elif seed_path is not None:
            candidate = self._strict_load(seed_path)
            self._validate_seed(candidate, snapshot=snapshot, identity=identity, data=data)
        else:
            candidate = self._empty_account(snapshot.account.available_cash.value)
            self._set_identity(candidate, identity=identity, data=data)
        try:
            account_state_sha256 = self._store.hash_state(candidate)
        except Exception as error:
            raise AccountBootstrapDenied("ACCOUNT_STATE_SEED_INVALID") from error

        if pending is not None:
            self._validate_pending_candidate(
                pending,
                snapshot=snapshot,
                identity=identity,
                data=data,
                account_state_sha256=account_state_sha256,
            )
        else:
            now = self._now()
            binding = AccountBinding.create(
                account_id_hash=snapshot.account.account_id_hash,
                account_type=snapshot.account.account_type,
                broker_snapshot_sha256=snapshot.raw_payload_sha256,
                account_state_sha256=account_state_sha256,
                uquant_commit=identity.uquant_commit,
                uquant_code_fingerprint=identity.economic_code_fingerprint,
                data_hash=data.data_hash,
                data_as_of=data.as_of,
                data_symbols=data.symbols,
                created_at=now,
            )
            pending = _PendingBootstrap(
                operation_id=self._operation_id(binding),
                stage="PREPARED",
                account_state_sha256=account_state_sha256,
                broker_snapshot_sha256=snapshot.raw_payload_sha256,
                binding=binding,
            )
            with self._database.transaction():
                self._database.write(
                    """
                    INSERT INTO account_bootstrap_operations(
                        operation_id, stage, account_state_sha256, broker_snapshot_sha256,
                        binding_payload_json, binding_payload_sha256, created_at, updated_at
                    ) VALUES (?, 'PREPARED', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        pending.operation_id,
                        pending.account_state_sha256,
                        pending.broker_snapshot_sha256,
                        binding.payload_json,
                        binding.payload_sha256,
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )

        try:
            self._account_path.parent.mkdir(parents=True, exist_ok=True)
            self._store.save(candidate, self._account_path)
            if self._store.hash_file(self._account_path) != pending.account_state_sha256:
                raise AccountBootstrapDenied("ACCOUNT_STATE_COMMIT_MISMATCH")
        except Exception as error:
            with self._database.transaction():
                self._database.write(
                    "UPDATE account_bootstrap_operations SET stage = 'CONTRADICTION', updated_at = ? "
                    "WHERE operation_id = ?",
                    (self._now().isoformat(), pending.operation_id),
                )
            if isinstance(error, AccountBootstrapDenied):
                raise
            raise AccountBootstrapDenied("ACCOUNT_STATE_COMMIT_FAILED") from error

        return self._finalize_bootstrap(pending, completed=self._now())


__all__ = (
    "AccountBootstrapDenied",
    "AccountBootstrapReceipt",
    "AccountBootstrapService",
    "BootstrapDataIdentity",
)
