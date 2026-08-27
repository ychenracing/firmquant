"""Single-call, append-only adapter around uquant ProductionEngine.decide()."""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Final, Protocol, cast

from firmquant.build_identity import (
    SourceIdentityError,
    load_locked_source_identity,
    verify_uquant_source_checkout,
)
from firmquant.domain.errors import DomainTypeError, DomainValidationError
from firmquant.domain.values import Symbol
from firmquant.persistence.audit import AuditLedger
from firmquant.persistence.database import Database, PersistenceError
from firmquant.persistence.repositories import DecisionSnapshotRepository

from .account_sync import (
    AccountStateContract,
    StrategySyncError,
    commit_prepared_account,
)
from .identity import StrategyIdentity, StrategyIdentityViolation
from .snapshots import DecisionSnapshot, DecisionSnapshotError
from .universe import UniversePolicy

_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA: Final = re.compile(r"^[0-9a-f]{40}$")


class StrategyAdapterError(RuntimeError):
    """Raised when the only authorized strategy path cannot be proven safe."""


class DecisionConflict(StrategyAdapterError):
    """Raised after recording a changed input for an already-decided session."""


class DecisionRecoveryRequired(StrategyAdapterError):
    """Raised when snapshot and AccountState durable application may be incomplete."""


class _DecisionContract(Protocol):
    decision_digest: str
    risk_summary: dict[str, object]

    def canonical_payload(self, *, effective_config_sha256: str) -> dict[str, object]: ...


class ProductionEngineContract(Protocol):
    cfg: object
    data: object
    _code_hash: str | None

    def decide(
        self,
        *,
        symbols: Iterable[str],
        as_of: str,
        account: object,
    ) -> _DecisionContract: ...


def _require_digest(value: str, *, label: str, pattern: re.Pattern[str] = _SHA256) -> None:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise StrategyAdapterError(f"{label} must be a canonical lowercase digest")


@dataclass(frozen=True, slots=True)
class DecisionRequest:
    strategy_session: date
    symbols: tuple[str, ...]
    account: AccountStateContract
    firmquant_commit: str
    data_manifest_sha256: str
    broker_snapshot_sha256: str
    created_at: datetime

    def __post_init__(self) -> None:
        if type(self.strategy_session) is not date:
            raise StrategyAdapterError("strategy session must be a calendar date")
        if not isinstance(self.symbols, tuple) or not self.symbols:
            raise StrategyAdapterError("decision symbols must be a non-empty tuple")
        if any(not isinstance(symbol, str) or not symbol for symbol in self.symbols):
            raise StrategyAdapterError("decision symbols must be non-empty text")
        _require_digest(self.firmquant_commit, label="firmquant commit", pattern=_GIT_SHA)
        _require_digest(self.data_manifest_sha256, label="data manifest SHA-256")
        _require_digest(self.broker_snapshot_sha256, label="broker snapshot SHA-256")
        if (
            not isinstance(self.created_at, datetime)
            or self.created_at.tzinfo is None
            or self.created_at.utcoffset() is None
        ):
            raise StrategyAdapterError("decision created_at must be timezone-aware")


def _account_sha256(account: object) -> str:
    module = importlib.import_module("uquant.account")
    function = cast(object, module.economic_state_sha256)
    if not callable(function):
        raise StrategyAdapterError("uquant economic account identity is unavailable")
    try:
        result = function(account)
    except (TypeError, ValueError) as exc:
        raise StrategyAdapterError("uquant economic account identity failed") from exc
    if not isinstance(result, str) or _SHA256.fullmatch(result) is None:
        raise StrategyAdapterError("uquant economic account identity is malformed")
    return result


def _config_fingerprint(config: object) -> str:
    module = importlib.import_module("uquant.config")
    function = cast(object, module.config_fingerprint)
    if not callable(function):
        raise StrategyAdapterError("uquant config fingerprint is unavailable")
    result = function(config)
    if not isinstance(result, str) or _SHA256.fullmatch(result) is None:
        raise StrategyAdapterError("uquant config fingerprint is malformed")
    return result


def _canonical_sha256(value: Mapping[str, object]) -> str:
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise StrategyAdapterError("decision input is not canonical JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


def _normalized_symbols(
    symbols: tuple[str, ...],
    *,
    session: date,
    policy: UniversePolicy,
) -> tuple[str, ...]:
    normalized: set[str] = set()
    for raw in symbols:
        try:
            symbol = Symbol.parse(raw).canonical
        except (DomainTypeError, DomainValidationError) as exc:
            raise StrategyAdapterError(f"invalid decision symbol: {raw!r}") from exc
        if not policy.allowed(symbol, session):
            raise StrategyAdapterError(f"decision symbol is outside deployment AI universe: {symbol}")
        normalized.add(symbol)
    if not normalized:
        raise StrategyAdapterError("decision universe is empty after normalization")
    return tuple(sorted(normalized))


class StrategyAdapter:
    """Persist exactly one uquant decision for a session and never derive economics."""

    def __init__(
        self,
        *,
        engine: ProductionEngineContract,
        database: Database,
        source_checkout: Path,
        universe_policy: UniversePolicy,
    ) -> None:
        self._engine = engine
        self._database = database
        self._source_checkout = Path(source_checkout).resolve()
        self._universe_policy = universe_policy
        self._snapshots = DecisionSnapshotRepository(database)

    def _verified_identity(self) -> StrategyIdentity:
        identity = StrategyIdentity.locked()
        try:
            identity.verify()
            verify_uquant_source_checkout(
                load_locked_source_identity(),
                self._source_checkout,
            )
        except (SourceIdentityError, StrategyIdentityViolation) as exc:
            raise StrategyAdapterError("uquant source identity is not verified") from exc
        module = importlib.import_module(type(self._engine).__module__)
        module_file = getattr(module, "__file__", None)
        expected_engine = (self._source_checkout / "uquant/engine.py").resolve()
        if not isinstance(module_file, str) or Path(module_file).resolve() != expected_engine:
            raise StrategyAdapterError("ProductionEngine is not loaded from the verified checkout")
        observed_config = _config_fingerprint(self._engine.cfg)
        if observed_config != identity.config_fingerprint:
            raise StrategyAdapterError("ProductionEngine config differs from locked uquant config")
        if self._universe_policy.manifest_sha256 != identity.canonical_universe_sha256:
            raise StrategyAdapterError("deployment universe identity differs from locked uquant")
        return identity

    @staticmethod
    def _fingerprints(
        request: DecisionRequest,
        *,
        symbols: tuple[str, ...],
        identity: StrategyIdentity,
        account_before_sha256: str,
    ) -> tuple[str, str]:
        context = {
            "schema": "firmquant.decision-request.v1",
            "strategy_session": request.strategy_session.isoformat(),
            "symbols": list(symbols),
            "firmquant_commit": request.firmquant_commit,
            "uquant_commit": identity.uquant_commit,
            "uquant_code_fingerprint": identity.economic_code_fingerprint,
            "uquant_config_fingerprint": identity.config_fingerprint,
            "data_manifest_sha256": request.data_manifest_sha256,
            "universe_manifest_sha256": identity.canonical_universe_sha256,
            "broker_snapshot_sha256": request.broker_snapshot_sha256,
        }
        request_fingerprint = _canonical_sha256(context)
        input_fingerprint = _canonical_sha256(
            {
                "schema": "firmquant.decision-input.v1",
                "request_fingerprint": request_fingerprint,
                "account_before_sha256": account_before_sha256,
            }
        )
        return request_fingerprint, input_fingerprint

    def _record_conflict(
        self,
        request: DecisionRequest,
        *,
        request_fingerprint: str,
        input_fingerprint: str,
        existing: tuple[DecisionSnapshot, ...],
    ) -> None:
        event_id = (
            "decision-conflict-"
            + hashlib.sha256(
                (request.strategy_session.isoformat() + input_fingerprint).encode("utf-8")
            ).hexdigest()
        )
        with self._database.transaction():
            already_recorded = self._database.query_one(
                "SELECT 1 FROM audit_events WHERE audit_event_id = ?",
                (event_id,),
            )
            if already_recorded is None:
                AuditLedger(self._database).append(
                    audit_event_id=event_id,
                    category="DECISION_CONFLICT",
                    actor="strategy-adapter",
                    payload={
                        "strategy_session": request.strategy_session.isoformat(),
                        "request_fingerprint": request_fingerprint,
                        "input_fingerprint": input_fingerprint,
                        "existing_decision_ids": [item.decision_id for item in existing],
                    },
                    created_at=request.created_at,
                )

    def _existing_or_conflict(
        self,
        request: DecisionRequest,
        *,
        request_fingerprint: str,
        input_fingerprint: str,
        account_sha256: str,
    ) -> DecisionSnapshot | None:
        exact = self._snapshots.find_by_input(
            strategy_session=request.strategy_session,
            input_fingerprint=input_fingerprint,
        )
        existing = self._snapshots.for_session(request.strategy_session)
        candidate = exact or next(
            (item for item in existing if item.request_fingerprint == request_fingerprint),
            None,
        )
        if candidate is not None:
            if account_sha256 == candidate.account_after_sha256:
                return candidate
            if account_sha256 == candidate.account_before_sha256:
                raise DecisionRecoveryRequired(
                    "decision snapshot exists but uquant AccountState is not durably advanced"
                )
            raise DecisionRecoveryRequired(
                "uquant AccountState matches neither decision pre-state nor post-state"
            )
        if existing:
            self._record_conflict(
                request,
                request_fingerprint=request_fingerprint,
                input_fingerprint=input_fingerprint,
                existing=existing,
            )
            raise DecisionConflict("strategy session already has an immutable decision with different inputs")
        return None

    def recover_existing_decision(
        self,
        request: DecisionRequest,
        snapshot: DecisionSnapshot,
    ) -> DecisionSnapshot:
        """Recompute a durable decision after-state and apply it only if every identity is exact."""

        if not isinstance(snapshot, DecisionSnapshot):
            raise StrategyAdapterError("decision recovery requires DecisionSnapshot")
        identity = self._verified_identity()
        symbols = _normalized_symbols(
            request.symbols,
            session=request.strategy_session,
            policy=self._universe_policy,
        )
        account_before_sha256 = _account_sha256(request.account)
        if account_before_sha256 != snapshot.account_before_sha256:
            raise DecisionRecoveryRequired("decision recovery requires the immutable before-state")
        request_fingerprint, input_fingerprint = self._fingerprints(
            request,
            symbols=symbols,
            identity=identity,
            account_before_sha256=account_before_sha256,
        )
        if (
            request_fingerprint != snapshot.request_fingerprint
            or input_fingerprint != snapshot.input_fingerprint
        ):
            raise DecisionRecoveryRequired("decision recovery input identity differs from durable snapshot")
        current_code_hash = self._engine._code_hash
        if current_code_hash not in {None, identity.economic_code_fingerprint}:
            raise StrategyAdapterError("ProductionEngine instance has an unexpected code hash")
        self._engine._code_hash = identity.economic_code_fingerprint
        working = copy.deepcopy(request.account)
        try:
            decision = self._engine.decide(
                symbols=symbols,
                as_of=request.strategy_session.isoformat(),
                account=working,
            )
        except (RuntimeError, TypeError, ValueError) as exc:
            raise DecisionRecoveryRequired("uquant decision recovery recomputation failed") from exc
        uquant_payload = decision.canonical_payload(effective_config_sha256=identity.config_fingerprint)
        if decision.decision_digest != _canonical_sha256(uquant_payload):
            raise DecisionRecoveryRequired("recomputed uquant decision digest is not canonical")
        account_after_sha256 = _account_sha256(working)
        if getattr(working, "code_hash", None) != identity.economic_code_fingerprint:
            raise DecisionRecoveryRequired("recomputed account code identity differs")
        if getattr(working, "data_hash", None) != request.data_manifest_sha256:
            raise DecisionRecoveryRequired("recomputed account data identity differs")
        if getattr(working, "data_hash_as_of", None) != request.strategy_session.isoformat():
            raise DecisionRecoveryRequired("recomputed account data session differs")
        candidate = DecisionSnapshot.create(
            strategy_session=request.strategy_session,
            request_fingerprint=request_fingerprint,
            input_fingerprint=input_fingerprint,
            firmquant_commit=request.firmquant_commit,
            identity=identity,
            data_manifest_sha256=request.data_manifest_sha256,
            broker_snapshot_sha256=request.broker_snapshot_sha256,
            account_before_sha256=account_before_sha256,
            account_after_sha256=account_after_sha256,
            uquant_payload=uquant_payload,
            uquant_decision_digest=decision.decision_digest,
            risk_summary=decision.risk_summary,
            created_at=snapshot.created_at,
            supersedes_decision_id=snapshot.supersedes_decision_id,
        )
        if candidate != snapshot:
            raise DecisionRecoveryRequired("recomputed decision differs from immutable durable snapshot")
        try:
            commit_prepared_account(
                request.account,
                working,
                expected_sha256=snapshot.account_after_sha256,
            )
        except StrategySyncError as exc:
            raise DecisionRecoveryRequired("recomputed AccountState could not be applied") from exc
        return snapshot

    def decide_once(self, request: DecisionRequest) -> DecisionSnapshot:
        """Call ProductionEngine.decide() once or fail closed before another economic path."""

        identity = self._verified_identity()
        symbols = _normalized_symbols(
            request.symbols,
            session=request.strategy_session,
            policy=self._universe_policy,
        )
        account_before_sha256 = _account_sha256(request.account)
        request_fingerprint, input_fingerprint = self._fingerprints(
            request,
            symbols=symbols,
            identity=identity,
            account_before_sha256=account_before_sha256,
        )
        existing = self._existing_or_conflict(
            request,
            request_fingerprint=request_fingerprint,
            input_fingerprint=input_fingerprint,
            account_sha256=account_before_sha256,
        )
        if existing is not None:
            return existing

        current_code_hash = self._engine._code_hash
        if current_code_hash not in {None, identity.economic_code_fingerprint}:
            raise StrategyAdapterError("ProductionEngine instance has an unexpected code hash")
        self._engine._code_hash = identity.economic_code_fingerprint
        working = copy.deepcopy(request.account)
        try:
            decision = self._engine.decide(
                symbols=symbols,
                as_of=request.strategy_session.isoformat(),
                account=working,
            )
        except (RuntimeError, TypeError, ValueError) as exc:
            raise StrategyAdapterError("uquant ProductionEngine.decide() failed") from exc
        uquant_payload = decision.canonical_payload(effective_config_sha256=identity.config_fingerprint)
        observed_digest = _canonical_sha256(uquant_payload)
        if decision.decision_digest != observed_digest:
            raise StrategyAdapterError("uquant decision digest differs from canonical payload")
        account_after_sha256 = _account_sha256(working)
        if getattr(working, "code_hash", None) != identity.economic_code_fingerprint:
            raise StrategyAdapterError("uquant account code fingerprint was not advanced exactly")
        if getattr(working, "data_hash", None) != request.data_manifest_sha256:
            raise StrategyAdapterError("uquant account data manifest differs from verified input")
        if getattr(working, "data_hash_as_of", None) != request.strategy_session.isoformat():
            raise StrategyAdapterError("uquant account data as_of differs from strategy session")
        snapshot = DecisionSnapshot.create(
            strategy_session=request.strategy_session,
            request_fingerprint=request_fingerprint,
            input_fingerprint=input_fingerprint,
            firmquant_commit=request.firmquant_commit,
            identity=identity,
            data_manifest_sha256=request.data_manifest_sha256,
            broker_snapshot_sha256=request.broker_snapshot_sha256,
            account_before_sha256=account_before_sha256,
            account_after_sha256=account_after_sha256,
            uquant_payload=uquant_payload,
            uquant_decision_digest=decision.decision_digest,
            risk_summary=decision.risk_summary,
            created_at=request.created_at,
        )
        try:
            with self._database.transaction():
                self._snapshots.append(snapshot)
        except (DecisionSnapshotError, PersistenceError) as exc:
            raise StrategyAdapterError("decision snapshot could not be durably appended") from exc
        try:
            commit_prepared_account(
                request.account,
                working,
                expected_sha256=account_after_sha256,
            )
        except StrategySyncError as exc:
            raise DecisionRecoveryRequired(
                "decision snapshot is durable but uquant AccountState commit requires recovery"
            ) from exc
        return snapshot


__all__ = (
    "DecisionConflict",
    "DecisionRecoveryRequired",
    "DecisionRequest",
    "ProductionEngineContract",
    "StrategyAdapter",
    "StrategyAdapterError",
)
