"""Typed application ports for one recoverable daily trading workflow."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from firmquant.domain.broker_facts import MarketSessionStatus
from firmquant.execution.planner import ExecutionBrokerSnapshot
from firmquant.market_data.validation import DataManifest
from firmquant.persistence.repositories import canonical_sha256
from firmquant.strategy.snapshots import DecisionSnapshot

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class WorkflowPortError(ValueError):
    """Raised when an application service returns ambiguous workflow evidence."""


def _digest(value: str, *, label: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise WorkflowPortError(f"{label} must be lowercase SHA-256")


def _aware(value: datetime, *, label: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise WorkflowPortError(f"{label} must be timezone-aware datetime")


def _text(value: str, *, label: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise WorkflowPortError(f"{label} must be canonical text")


@dataclass(frozen=True, slots=True)
class WorkflowOutcome:
    """Safe digest and stable reference returned by an idempotent workflow action."""

    output_sha256: str
    reference_id: str

    def __post_init__(self) -> None:
        _digest(self.output_sha256, label="workflow output digest")
        _text(self.reference_id, label="workflow reference id")


@dataclass(frozen=True, slots=True)
class MarketStatusFact:
    """Broker market status bound to one session and fresh observation."""

    session_date: date
    status: MarketSessionStatus
    observed_at: datetime
    source: str
    raw_payload_sha256: str

    def __post_init__(self) -> None:
        if type(self.session_date) is not date:
            raise WorkflowPortError("market status session must be a date")
        if not isinstance(self.status, MarketSessionStatus):
            raise WorkflowPortError("market status must be typed")
        _aware(self.observed_at, label="market status observed_at")
        _text(self.source, label="market status source")
        _digest(self.raw_payload_sha256, label="market status raw payload digest")

    @property
    def sha256(self) -> str:
        return canonical_sha256(
            {
                "schema": "firmquant.market-status-fact.v1",
                "session_date": self.session_date,
                "status": self.status,
                "observed_at": self.observed_at,
                "source": self.source,
                "raw_payload_sha256": self.raw_payload_sha256,
            }
        )


@dataclass(frozen=True, slots=True)
class DecisionPreparation:
    """All sealed inputs collected before the only ProductionEngine.decide call."""

    current_data: DataManifest
    previous_data: DataManifest
    broker_snapshot_sha256: str
    account_state_sha256: str
    prepared_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.current_data, DataManifest) or not isinstance(
            self.previous_data, DataManifest
        ):
            raise WorkflowPortError("decision data manifests must be typed")
        _digest(self.broker_snapshot_sha256, label="broker snapshot digest")
        _digest(self.account_state_sha256, label="account state digest")
        _aware(self.prepared_at, label="decision prepared_at")

    @property
    def input_sha256(self) -> str:
        return canonical_sha256(
            {
                "schema": "firmquant.decision-preparation.v1",
                "current_data_sha256": self.current_data.sha256,
                "previous_data_sha256": self.previous_data.sha256,
                "broker_snapshot_sha256": self.broker_snapshot_sha256,
                "account_state_sha256": self.account_state_sha256,
            }
        )


@dataclass(frozen=True, slots=True)
class ExecutionPreparation:
    """Fresh pre-open facts plus explicit reconciliation/premise verdicts."""

    facts: ExecutionBrokerSnapshot
    premise_matches: bool
    reconciliation_healthy: bool
    prepared_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.facts, ExecutionBrokerSnapshot):
            raise WorkflowPortError("execution preparation facts must be typed")
        if not isinstance(self.premise_matches, bool):
            raise WorkflowPortError("execution premise verdict must be bool")
        if not isinstance(self.reconciliation_healthy, bool):
            raise WorkflowPortError("execution reconciliation verdict must be bool")
        _aware(self.prepared_at, label="execution prepared_at")

    @property
    def input_sha256(self) -> str:
        return canonical_sha256(
            {
                "schema": "firmquant.execution-preparation.v1",
                "facts_sha256": self.facts.sha256,
                "premise_matches": self.premise_matches,
                "reconciliation_healthy": self.reconciliation_healthy,
            }
        )


class WorkflowServices(Protocol):
    """Side-effect ports; recovery methods must reconcile rather than blindly retry."""

    def startup_reconcile(self) -> WorkflowOutcome: ...

    def recover_startup(self) -> WorkflowOutcome: ...

    def market_status_fact(self) -> MarketStatusFact: ...

    def prepare_decision(self, strategy_session: date) -> DecisionPreparation: ...

    def decide_after_close(self, preparation: DecisionPreparation) -> DecisionSnapshot: ...

    def recover_decision(self, strategy_session: date, input_sha256: str) -> DecisionSnapshot: ...

    def load_frozen_decision(self, strategy_session: date) -> DecisionSnapshot: ...

    def prepare_execution(
        self, execution_session: date, decision: DecisionSnapshot
    ) -> ExecutionPreparation: ...

    def execute_frozen(
        self, decision: DecisionSnapshot, preparation: ExecutionPreparation
    ) -> WorkflowOutcome: ...

    def recover_execution(
        self,
        execution_session: date,
        input_sha256: str,
    ) -> WorkflowOutcome: ...

    def process_intraday(self, execution_session: date) -> WorkflowOutcome: ...

    def recover_intraday(self, execution_session: date, input_sha256: str) -> WorkflowOutcome: ...

    def reconcile_eod(self, execution_session: date) -> WorkflowOutcome: ...

    def recover_eod(self, execution_session: date, input_sha256: str) -> WorkflowOutcome: ...


__all__ = (
    "DecisionPreparation",
    "ExecutionPreparation",
    "MarketStatusFact",
    "WorkflowOutcome",
    "WorkflowPortError",
    "WorkflowServices",
)
