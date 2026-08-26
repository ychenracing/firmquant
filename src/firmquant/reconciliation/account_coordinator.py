"""Gate broker-to-uquant adoption behind preflight and final reconciliation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from firmquant.domain.broker_facts import BrokerSnapshot
from firmquant.domain.errors import DomainTypeError, DomainValidationError
from firmquant.domain.values import Money
from firmquant.persistence.account_authority import AccountBinding
from firmquant.persistence.recovery import RecoveryContradiction
from firmquant.strategy.account_prepare import PreparedAccountSync
from firmquant.strategy.account_sync import AccountStateContract

from .account_preflight import AccountPreflightResult, evaluate_account_preflight
from .models import OperationalLedgerView, ReconciliationFacts, ReconciliationKind


class _AccountStore(Protocol):
    def hash_state(self, state: object) -> str: ...


class _AccountRepository(Protocol):
    store: _AccountStore

    def load(self) -> AccountStateContract: ...

    def prepare_broker_snapshot(self, snapshot: BrokerSnapshot) -> PreparedAccountSync: ...

    def commit_broker_snapshot(self, prepared: PreparedAccountSync) -> str: ...


class _ReconciliationReceipt(Protocol):
    reconciliation_id: str
    passed: bool
    blockers: tuple[str, ...]


class _Reconciler(Protocol):
    def run(
        self,
        kind: ReconciliationKind,
        facts: ReconciliationFacts,
    ) -> _ReconciliationReceipt: ...


class AccountReconciliationBlocked(RuntimeError):
    """Raised before commit whenever broker adoption lacks authority."""

    def __init__(self, blockers: tuple[str, ...]) -> None:
        normalized = tuple(sorted(set(blockers)))
        if not normalized or normalized != blockers:
            raise DomainValidationError("account reconciliation blockers must be sorted and unique")
        if any(not isinstance(item, str) or not item or item != item.strip() for item in normalized):
            raise DomainValidationError("account reconciliation blockers must be canonical text")
        self.blockers = normalized
        super().__init__(",".join(normalized))


@dataclass(frozen=True, slots=True)
class AccountReconciliationResult:
    """Outcome of one authoritative broker/account reconciliation cycle."""

    receipt: _ReconciliationReceipt
    account: AccountStateContract
    preflight: AccountPreflightResult
    account_before_sha256: str
    account_after_sha256: str
    committed: bool

    def __post_init__(self) -> None:
        if not isinstance(self.committed, bool):
            raise DomainTypeError("account reconciliation committed must be bool")
        for label, digest in (
            ("before", self.account_before_sha256),
            ("after", self.account_after_sha256),
        ):
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise DomainValidationError(f"account reconciliation {label} hash must be SHA-256")
        if self.committed and self.account_before_sha256 == self.account_after_sha256:
            raise DomainValidationError("account reconciliation cannot commit an economic no-op")


class AccountReconciliationCoordinator:
    """Own the sole order in which broker facts may become strategy state."""

    def __init__(
        self,
        *,
        account_repository: _AccountRepository,
        reconciler: _Reconciler,
        cash_tolerance: Decimal,
    ) -> None:
        if not isinstance(cash_tolerance, Decimal):
            raise DomainTypeError("account reconciliation cash tolerance must be Decimal")
        if not cash_tolerance.is_finite() or cash_tolerance < 0:
            raise DomainValidationError(
                "account reconciliation cash tolerance must be finite and nonnegative"
            )
        self._accounts = account_repository
        self._reconciler = reconciler
        self._cash_tolerance = Money(cash_tolerance)

    def reconcile(
        self,
        *,
        kind: ReconciliationKind,
        snapshot: BrokerSnapshot,
        operational_ledger: OperationalLedgerView,
        binding: AccountBinding,
        final_facts: Callable[[AccountStateContract], ReconciliationFacts],
    ) -> AccountReconciliationResult:
        if not isinstance(kind, ReconciliationKind):
            raise DomainTypeError("account reconciliation kind must be typed")
        if not isinstance(snapshot, BrokerSnapshot):
            raise DomainTypeError("account reconciliation snapshot must be BrokerSnapshot")
        if not isinstance(operational_ledger, OperationalLedgerView):
            raise DomainTypeError("account reconciliation ledger must be OperationalLedgerView")
        if not isinstance(binding, AccountBinding):
            raise DomainTypeError("account reconciliation binding must be AccountBinding")
        if not callable(final_facts):
            raise DomainTypeError("account reconciliation final facts must be callable")

        current = self._accounts.load()
        expected_before = self._accounts.store.hash_state(current)
        preflight = evaluate_account_preflight(
            snapshot=snapshot,
            account=current,
            operational_ledger=operational_ledger,
            binding=binding,
            cash_tolerance=self._cash_tolerance,
        )
        if not preflight.passed:
            raise AccountReconciliationBlocked(preflight.blockers)

        prepared = self._accounts.prepare_broker_snapshot(snapshot)
        if prepared.account_before_sha256 != expected_before:
            raise RecoveryContradiction("account state changed between preflight and preparation")

        facts = final_facts(prepared.prepared_account)
        if not isinstance(facts, ReconciliationFacts):
            raise DomainTypeError("account reconciliation final facts must be ReconciliationFacts")
        if facts.broker_snapshot != snapshot:
            raise RecoveryContradiction("final reconciliation broker snapshot changed after preflight")
        if facts.operational_ledger != operational_ledger:
            raise RecoveryContradiction("final reconciliation ledger changed after preflight")

        receipt = self._reconciler.run(kind, facts)
        if not receipt.passed:
            raise AccountReconciliationBlocked(tuple(receipt.blockers))

        if prepared.account_after_sha256 == prepared.account_before_sha256:
            return AccountReconciliationResult(
                receipt=receipt,
                account=prepared.prepared_account,
                preflight=preflight,
                account_before_sha256=prepared.account_before_sha256,
                account_after_sha256=prepared.account_after_sha256,
                committed=False,
            )

        committed = self._accounts.commit_broker_snapshot(prepared)
        if committed != prepared.account_after_sha256:
            raise RecoveryContradiction("committed account hash differs from reviewed preparation")
        return AccountReconciliationResult(
            receipt=receipt,
            account=prepared.prepared_account,
            preflight=preflight,
            account_before_sha256=prepared.account_before_sha256,
            account_after_sha256=prepared.account_after_sha256,
            committed=True,
        )


__all__ = (
    "AccountReconciliationBlocked",
    "AccountReconciliationCoordinator",
    "AccountReconciliationResult",
)
