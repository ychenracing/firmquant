"""Recoverable Asia/Shanghai daily-session orchestration."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from firmquant.application.workflows import (
    DecisionPreparation,
    ExecutionPreparation,
    MarketStatusFact,
    WorkflowOutcome,
    WorkflowServices,
)
from firmquant.config import Mode
from firmquant.domain.broker_facts import MarketSessionStatus
from firmquant.domain.states import RuntimeState, RuntimeStatus
from firmquant.market_data.calendar import AuthoritativeTradingCalendar, CalendarCoverageError
from firmquant.market_data.validation import (
    DataValidationError,
    StrategyDataValidator,
    validate_execution_facts,
)
from firmquant.persistence.repositories import canonical_sha256
from firmquant.scheduling.clock import ClockGuard, ClockObservation
from firmquant.scheduling.sessions import (
    EvidenceValue,
    StoredWorkflowState,
    WorkflowReceiptStore,
    WorkflowRunner,
    WorkflowStep,
)
from firmquant.strategy.identity import StrategyIdentity
from firmquant.strategy.snapshots import DecisionSnapshot

_SHANGHAI = ZoneInfo("Asia/Shanghai")


class SessionWorkflowError(RuntimeError):
    """A daily session step failed closed and left the coordinator HALTED."""

    def __init__(self, message: str, *, blocker: str = "SESSION_WORKFLOW_FAILED") -> None:
        super().__init__(message)
        self.blocker = blocker


class SessionCoordinator:
    """Coordinate the only daily strategy path and its operational lifecycle."""

    def __init__(
        self,
        *,
        services: WorkflowServices,
        mode: Mode,
        calendar: AuthoritativeTradingCalendar,
        clock_guard: ClockGuard,
        clock_observer: Callable[[], ClockObservation],
        data_validator: StrategyDataValidator,
        receipts: WorkflowReceiptStore,
        max_quote_age: timedelta,
    ) -> None:
        if not isinstance(mode, Mode):
            raise TypeError("session mode must be typed")
        if not isinstance(calendar, AuthoritativeTradingCalendar):
            raise TypeError("session calendar must be authoritative")
        if not isinstance(clock_guard, ClockGuard):
            raise TypeError("session clock guard must be typed")
        if not callable(clock_observer):
            raise TypeError("session clock observer must be callable")
        if not isinstance(data_validator, StrategyDataValidator):
            raise TypeError("session data validator must be typed")
        if not isinstance(receipts, WorkflowReceiptStore):
            raise TypeError("session workflow receipts must be typed")
        if not isinstance(max_quote_age, timedelta) or max_quote_age <= timedelta(0):
            raise ValueError("maximum quote age must be positive")
        self._services = services
        self._mode = mode
        self._calendar = calendar
        self._clock_guard = clock_guard
        self._clock_observer = clock_observer
        self._data_validator = data_validator
        self._receipts = receipts
        self._runner = WorkflowRunner(receipts=receipts)
        self._max_quote_age = max_quote_age
        self._status = receipts.load_runtime(mode)
        self._startup_completed_in_process = False

    @property
    def status(self) -> RuntimeStatus:
        return self._status

    def _now(self) -> datetime:
        return self._clock_guard.verify(self._clock_observer()).system_time

    def _transition(
        self,
        target: RuntimeState,
        *,
        reason: str,
        at: datetime,
        blockers: tuple[str, ...] = (),
    ) -> None:
        previous = self._status
        current = previous.transition(target, reason=reason, blockers=blockers)
        self._receipts.save_runtime(
            mode=self._mode,
            previous=previous,
            current=current,
            created_at=at,
        )
        self._status = current

    def _halt(self, *, reason: str, blocker: str, at: datetime | None = None) -> None:
        observed_at = datetime.now(UTC) if at is None else at
        if self._status.state is RuntimeState.DISARMED:
            self._transition(
                RuntimeState.STARTING,
                reason="session validation started",
                at=observed_at,
            )
        self._transition(
            RuntimeState.HALTED,
            reason=reason,
            blockers=(blocker,),
            at=observed_at,
        )

    def _require_ready(self) -> None:
        if self._status.state is not RuntimeState.READY:
            raise SessionWorkflowError("session coordinator is not READY")

    def _begin_step(self, reason: str, *, at: datetime) -> None:
        self._require_ready()
        self._transition(RuntimeState.EXECUTING, reason=reason, at=at)

    def _finish_step(self, reason: str, *, at: datetime) -> None:
        self._transition(RuntimeState.READY, reason=reason, at=at)

    def _require_trading_session(self, session: date) -> None:
        if not self._calendar.is_trading_session(session):
            raise SessionWorkflowError(
                "date is not an authoritative trading session",
                blocker="CALENDAR_SESSION_INVALID",
            )

    @staticmethod
    def _identity(value: WorkflowOutcome) -> WorkflowOutcome:
        if not isinstance(value, WorkflowOutcome):
            raise SessionWorkflowError("workflow service returned invalid outcome")
        return value

    def _market_status(
        self,
        *,
        session: date,
        expected: MarketSessionStatus,
        now: datetime,
    ) -> MarketStatusFact:
        fact = self._services.market_status_fact()
        if not isinstance(fact, MarketStatusFact):
            raise SessionWorkflowError("market status fact is not typed")
        age = now - fact.observed_at
        if (
            fact.session_date != session
            or fact.status is not expected
            or age < timedelta(0)
            or age > self._max_quote_age
        ):
            raise SessionWorkflowError(
                "market status fact is stale or bound to another session",
                blocker="MARKET_STATUS_INVALID",
            )
        return fact

    @staticmethod
    def _evidence_digest(evidence: Mapping[str, EvidenceValue], key: str) -> str | None:
        value = evidence.get(key)
        return value if isinstance(value, str) else None

    def _seal_decision(
        self,
        snapshot: DecisionSnapshot,
        *,
        strategy_session: date,
        evidence: Mapping[str, EvidenceValue],
    ) -> WorkflowOutcome:
        if not isinstance(snapshot, DecisionSnapshot):
            raise SessionWorkflowError("strategy service returned invalid decision snapshot")
        if snapshot.strategy_session != strategy_session:
            raise SessionWorkflowError("decision snapshot session mismatch")
        expected = (
            ("data_manifest_sha256", snapshot.data_manifest_sha256),
            ("broker_snapshot_sha256", snapshot.broker_snapshot_sha256),
            ("account_state_sha256", snapshot.account_before_sha256),
        )
        for key, observed in expected:
            required = self._evidence_digest(evidence, key)
            if required is None or observed != required:
                raise SessionWorkflowError(f"decision snapshot {key} premise mismatch")
        identity = StrategyIdentity.locked()
        if (
            snapshot.uquant_commit != identity.uquant_commit
            or snapshot.uquant_code_fingerprint != identity.economic_code_fingerprint
            or snapshot.uquant_config_fingerprint != identity.config_fingerprint
            or snapshot.universe_manifest_sha256 != identity.canonical_universe_sha256
        ):
            raise SessionWorkflowError("decision snapshot uquant identity mismatch")
        return WorkflowOutcome(
            output_sha256=snapshot.payload_sha256,
            reference_id=snapshot.decision_id,
        )

    def _linked_decision(self, strategy_session: date) -> DecisionSnapshot:
        state = self._receipts.inspect(
            step=WorkflowStep.POST_CLOSE_DECISION,
            session=strategy_session,
        )
        if state is None or state.completed_outcome is None:
            raise SessionWorkflowError(
                "completed decision receipt is unavailable",
                blocker="MISSING_DECISION",
            )
        decision = self._services.load_frozen_decision(strategy_session)
        outcome = self._seal_decision(
            decision,
            strategy_session=strategy_session,
            evidence=state.evidence,
        )
        if outcome != state.completed_outcome:
            raise SessionWorkflowError("frozen decision differs from completed decision receipt")
        return decision

    def _recover_state(
        self,
        state: StoredWorkflowState,
        *,
        now: datetime,
    ) -> WorkflowOutcome | DecisionSnapshot:
        if state.step is WorkflowStep.POST_CLOSE_DECISION:
            decision_result = self._runner.recover_existing(
                state=state,
                now=self._now,
                recover=lambda digest: self._services.recover_decision(state.session, digest),
                seal=lambda snapshot: self._seal_decision(
                    snapshot,
                    strategy_session=state.session,
                    evidence=state.evidence,
                ),
                allow_incomplete=True,
            )
            return decision_result.value
        if state.step is WorkflowStep.NEXT_DAY_EXECUTION:
            strategy_session_text = state.evidence.get("strategy_session")
            if not isinstance(strategy_session_text, str):
                raise SessionWorkflowError("execution recovery lacks strategy session evidence")
            decision = self._linked_decision(date.fromisoformat(strategy_session_text))
            if (
                state.evidence.get("decision_id") != decision.decision_id
                or state.evidence.get("decision_payload_sha256") != decision.payload_sha256
            ):
                raise SessionWorkflowError("execution recovery decision linkage changed")
            execution_result = self._runner.recover_existing(
                state=state,
                now=self._now,
                recover=lambda digest: self._services.recover_execution(state.session, digest),
                seal=self._identity,
                allow_incomplete=True,
            )
            return execution_result.value
        if state.step is WorkflowStep.INTRADAY:
            intraday_result = self._runner.recover_existing(
                state=state,
                now=self._now,
                recover=lambda digest: self._services.recover_intraday(state.session, digest),
                seal=self._identity,
                allow_incomplete=True,
            )
            return intraday_result.value
        if state.step is WorkflowStep.EOD:
            eod_result = self._runner.recover_existing(
                state=state,
                now=self._now,
                recover=lambda digest: self._services.recover_eod(state.session, digest),
                seal=self._identity,
                allow_incomplete=True,
            )
            return eod_result.value
        raise SessionWorkflowError("startup workflow cannot be recovered as a pending child step")

    def startup(self) -> WorkflowOutcome:
        if self._startup_completed_in_process:
            raise SessionWorkflowError("session coordinator is already started")
        operational_time = datetime.now(UTC)
        try:
            observation = self._clock_observer()
            operational_time = (
                observation.system_time if isinstance(observation, ClockObservation) else datetime.now(UTC)
            )
            if self._status.state is RuntimeState.DISARMED:
                self._transition(
                    RuntimeState.STARTING,
                    reason="startup requested",
                    at=operational_time,
                )
            if self._status.state is RuntimeState.STOPPING:
                raise SessionWorkflowError("STOPPING runtime cannot restart")
            if self._status.state is not RuntimeState.RECONCILING:
                self._transition(
                    RuntimeState.RECONCILING,
                    reason="startup reconciliation in progress",
                    at=operational_time,
                )
            now = self._clock_guard.verify(observation).system_time
            session = now.astimezone(_SHANGHAI).date()
            input_sha256 = canonical_sha256(
                {
                    "schema": "firmquant.startup-workflow.v2",
                    "calendar_sha256": self._calendar.sha256,
                }
            )
            startup_state = self._receipts.inspect(
                step=WorkflowStep.STARTUP,
                session=session,
            )
            if startup_state is None:
                startup = self._runner.run_new(
                    step=WorkflowStep.STARTUP,
                    session=session,
                    input_sha256=input_sha256,
                    evidence={"calendar_sha256": self._calendar.sha256},
                    now=self._now,
                    action=self._services.startup_reconcile,
                    seal=self._identity,
                )
            else:
                if startup_state.input_sha256 != input_sha256:
                    raise SessionWorkflowError("startup inputs changed during recovery")
                startup = self._runner.recover_existing(
                    state=startup_state,
                    now=self._now,
                    recover=lambda _digest: self._services.recover_startup(),
                    seal=self._identity,
                    allow_incomplete=True,
                )
            for pending in self._receipts.unresolved():
                if pending.step is WorkflowStep.STARTUP:
                    continue
                self._recover_state(pending, now=now)
            self._transition(
                RuntimeState.READY,
                reason="startup reconciliation and pending recovery passed",
                at=self._now(),
            )
            self._startup_completed_in_process = True
            return startup.value
        except Exception as exc:
            with suppress(Exception):
                self._halt(
                    reason="startup failed closed",
                    blocker="STARTUP_RECONCILIATION_FAILED",
                    at=operational_time,
                )
            raise SessionWorkflowError("startup reconciliation failed") from exc

    def post_close_decision(self, strategy_session: date) -> DecisionSnapshot:
        self._require_ready()
        now = self._now()
        self._begin_step("post-close decision started", at=now)
        try:
            self._require_trading_session(strategy_session)
            existing = self._receipts.inspect(
                step=WorkflowStep.POST_CLOSE_DECISION,
                session=strategy_session,
            )
            if existing is not None:
                recovered = self._recover_state(existing, now=now)
                if not isinstance(recovered, DecisionSnapshot):
                    raise SessionWorkflowError("decision recovery returned invalid evidence")
                self._finish_step("post-close decision recovered", at=self._now())
                return recovered
            market = self._market_status(
                session=strategy_session,
                expected=MarketSessionStatus.CLOSED,
                now=now,
            )
            preparation = self._services.prepare_decision(strategy_session)
            if not isinstance(preparation, DecisionPreparation):
                raise SessionWorkflowError("decision preparation is not typed")
            try:
                previous_session = self._calendar.previous_trading_session(strategy_session)
                if preparation.previous_data.latest_common_session != previous_session:
                    raise DataValidationError("previous manifest is not the prior trading session")
                self._data_validator.validate(
                    previous=preparation.previous_data,
                    current=preparation.current_data,
                    target_session=strategy_session,
                    now=now,
                )
            except CalendarCoverageError as exc:
                raise SessionWorkflowError(
                    "previous trading session is outside calendar coverage",
                    blocker="CALENDAR_COVERAGE",
                ) from exc
            except DataValidationError as exc:
                raise SessionWorkflowError(
                    "strategy data validation failed",
                    blocker="STRATEGY_DATA_INVALID",
                ) from exc
            input_sha256 = canonical_sha256(
                {
                    "schema": "firmquant.post-close-workflow.v2",
                    "strategy_session": strategy_session,
                    "preparation_sha256": preparation.input_sha256,
                    "market_status_sha256": market.sha256,
                    "calendar_sha256": self._calendar.sha256,
                }
            )
            evidence: dict[str, EvidenceValue] = {
                "data_manifest_sha256": preparation.current_data.sha256,
                "broker_snapshot_sha256": preparation.broker_snapshot_sha256,
                "account_state_sha256": preparation.account_state_sha256,
                "market_status_sha256": market.sha256,
            }
            result = self._runner.run_new(
                step=WorkflowStep.POST_CLOSE_DECISION,
                session=strategy_session,
                input_sha256=input_sha256,
                evidence=evidence,
                now=self._now,
                action=lambda: self._services.decide_after_close(preparation),
                seal=lambda snapshot: self._seal_decision(
                    snapshot,
                    strategy_session=strategy_session,
                    evidence=evidence,
                ),
            )
            self._finish_step("post-close decision completed", at=self._now())
            return result.value
        except Exception as exc:
            blocker = exc.blocker if isinstance(exc, SessionWorkflowError) else "DECISION_FAILED"
            self._halt(
                reason="post-close decision failed closed",
                blocker=blocker,
                at=now,
            )
            if isinstance(exc, SessionWorkflowError):
                raise
            raise SessionWorkflowError("post-close decision failed") from exc

    def next_day_execute(self, execution_session: date) -> WorkflowOutcome:
        self._require_ready()
        now = self._now()
        self._begin_step("next-day execution started", at=now)
        try:
            self._require_trading_session(execution_session)
            existing = self._receipts.inspect(
                step=WorkflowStep.NEXT_DAY_EXECUTION,
                session=execution_session,
            )
            if existing is not None:
                recovered = self._recover_state(existing, now=now)
                if not isinstance(recovered, WorkflowOutcome):
                    raise SessionWorkflowError("execution recovery returned invalid evidence")
                self._finish_step("next-day execution recovered", at=self._now())
                return recovered
            try:
                strategy_session = self._calendar.previous_trading_session(execution_session)
            except CalendarCoverageError as exc:
                raise SessionWorkflowError(
                    "previous trading session is outside calendar coverage",
                    blocker="CALENDAR_COVERAGE",
                ) from exc
            decision = self._linked_decision(strategy_session)
            market = self._market_status(
                session=execution_session,
                expected=MarketSessionStatus.OPEN,
                now=now,
            )
            preparation = self._services.prepare_execution(execution_session, decision)
            if not isinstance(preparation, ExecutionPreparation):
                raise SessionWorkflowError("execution preparation is not typed")
            if not preparation.premise_matches:
                raise SessionWorkflowError(
                    "account differs from the frozen decision premise",
                    blocker="DECISION_PREMISE_CHANGED",
                )
            if not preparation.reconciliation_healthy:
                raise SessionWorkflowError(
                    "execution reconciliation is unhealthy",
                    blocker="RECONCILIATION_UNHEALTHY",
                )
            try:
                validate_execution_facts(
                    preparation.facts,
                    execution_session=execution_session,
                    now=now,
                    max_age=self._max_quote_age,
                )
            except DataValidationError as exc:
                raise SessionWorkflowError(
                    "execution facts validation failed",
                    blocker="EXECUTION_FACTS_INVALID",
                ) from exc
            input_sha256 = canonical_sha256(
                {
                    "schema": "firmquant.next-day-execution-workflow.v2",
                    "execution_session": execution_session,
                    "decision_id": decision.decision_id,
                    "preparation_sha256": preparation.input_sha256,
                    "market_status_sha256": market.sha256,
                    "calendar_sha256": self._calendar.sha256,
                }
            )
            evidence: dict[str, EvidenceValue] = {
                "strategy_session": strategy_session.isoformat(),
                "decision_id": decision.decision_id,
                "decision_payload_sha256": decision.payload_sha256,
                "market_status_sha256": market.sha256,
                "execution_facts_sha256": preparation.facts.sha256,
            }
            result = self._runner.run_new(
                step=WorkflowStep.NEXT_DAY_EXECUTION,
                session=execution_session,
                input_sha256=input_sha256,
                evidence=evidence,
                now=self._now,
                action=lambda: self._services.execute_frozen(decision, preparation),
                seal=self._identity,
            )
            self._finish_step("next-day execution completed", at=self._now())
            return result.value
        except Exception as exc:
            blocker = exc.blocker if isinstance(exc, SessionWorkflowError) else "EXECUTION_FAILED"
            self._halt(
                reason="next-day execution failed closed",
                blocker=blocker,
                at=now,
            )
            if isinstance(exc, SessionWorkflowError):
                raise
            raise SessionWorkflowError("next-day execution failed") from exc

    def intraday(self, execution_session: date) -> WorkflowOutcome:
        self._require_ready()
        now = self._now()
        self._begin_step("intraday event processing started", at=now)
        try:
            self._require_trading_session(execution_session)
            input_sha256 = canonical_sha256(
                {
                    "schema": "firmquant.intraday-workflow.v2",
                    "execution_session": execution_session,
                    "calendar_sha256": self._calendar.sha256,
                }
            )
            result = self._runner.run(
                step=WorkflowStep.INTRADAY,
                session=execution_session,
                input_sha256=input_sha256,
                evidence={"calendar_sha256": self._calendar.sha256},
                now=self._now,
                action=lambda: self._services.process_intraday(execution_session),
                recover=lambda digest: self._services.recover_intraday(execution_session, digest),
                seal=self._identity,
            )
            self._finish_step("intraday event processing completed", at=self._now())
            return result.value
        except Exception as exc:
            blocker = exc.blocker if isinstance(exc, SessionWorkflowError) else "INTRADAY_FAILED"
            self._halt(
                reason="intraday processing failed closed",
                blocker=blocker,
                at=now,
            )
            if isinstance(exc, SessionWorkflowError):
                raise
            raise SessionWorkflowError("intraday processing failed") from exc

    def eod(self, execution_session: date) -> WorkflowOutcome:
        self._require_ready()
        now = self._now()
        self._begin_step("end-of-day reconciliation started", at=now)
        try:
            self._require_trading_session(execution_session)
            existing = self._receipts.inspect(
                step=WorkflowStep.EOD,
                session=execution_session,
            )
            if existing is not None:
                recovered = self._recover_state(existing, now=now)
                if not isinstance(recovered, WorkflowOutcome):
                    raise SessionWorkflowError("EOD recovery returned invalid evidence")
                self._finish_step("end-of-day reconciliation recovered", at=self._now())
                return recovered
            market = self._market_status(
                session=execution_session,
                expected=MarketSessionStatus.CLOSED,
                now=now,
            )
            input_sha256 = canonical_sha256(
                {
                    "schema": "firmquant.eod-workflow.v2",
                    "execution_session": execution_session,
                    "market_status_sha256": market.sha256,
                    "calendar_sha256": self._calendar.sha256,
                }
            )
            result = self._runner.run(
                step=WorkflowStep.EOD,
                session=execution_session,
                input_sha256=input_sha256,
                evidence={
                    "calendar_sha256": self._calendar.sha256,
                    "market_status_sha256": market.sha256,
                },
                now=self._now,
                action=lambda: self._services.reconcile_eod(execution_session),
                recover=lambda digest: self._services.recover_eod(execution_session, digest),
                seal=self._identity,
            )
            self._finish_step("end-of-day reconciliation completed", at=self._now())
            return result.value
        except Exception as exc:
            blocker = exc.blocker if isinstance(exc, SessionWorkflowError) else "EOD_FAILED"
            self._halt(
                reason="end-of-day reconciliation failed closed",
                blocker=blocker,
                at=now,
            )
            if isinstance(exc, SessionWorkflowError):
                raise
            raise SessionWorkflowError("end-of-day reconciliation failed") from exc


__all__ = ("SessionCoordinator", "SessionWorkflowError")
