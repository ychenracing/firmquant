"""Single-writer local operator controls for market-data and calendar governance."""

from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from firmquant.config import Mode, Settings, load_settings
from firmquant.market_data.calendar import CalendarCoverageState
from firmquant.market_data.calendar_manifest import (
    load_trading_calendar_manifest,
    validate_calendar_update,
)
from firmquant.market_data.generations import DataGenerationError, DataGenerationStore
from firmquant.persistence.audit import AuditLedger
from firmquant.persistence.database import Database
from firmquant.persistence.repositories import canonical_json
from firmquant.persistence.writer_lease import WriterLease

_CALENDAR_FILE = "trading-calendar.json"
_CALENDAR_WARNING_DAYS = 10
_ACTIVE_BROKER_ORDER_STATES = (
    "PENDING_NEW",
    "ACKNOWLEDGED",
    "PARTIALLY_FILLED",
    "PENDING_CANCEL",
)
_ACTIVE_EXECUTION_STATES = (
    "SUBMITTING",
    "ACKNOWLEDGED",
    "PARTIALLY_FILLED",
    "CANCEL_REQUESTED",
)
_CI_KEYS = frozenset(
    {
        "CI",
        "GITHUB_ACTIONS",
        "GITLAB_CI",
        "TF_BUILD",
        "JENKINS_URL",
        "BUILDKITE",
        "CIRCLECI",
    }
)


class DataCalendarControlError(RuntimeError):
    """A local operator data/calendar request failed closed with a stable reason code."""

    def __init__(self, reason_code: str) -> None:
        if (
            not isinstance(reason_code, str)
            or not reason_code
            or reason_code != reason_code.strip()
            or not reason_code.replace("_", "").isalnum()
            or reason_code.upper() != reason_code
        ):
            raise ValueError("data/calendar control reason code is invalid")
        self.reason_code = reason_code
        super().__init__(reason_code)


class ConfirmationReader(Protocol):
    def __call__(self, prompt: str, /) -> str: ...


def _sha256(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise DataCalendarControlError("CONTROL_INPUT_INVALID")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise DataCalendarControlError("CONTROL_INPUT_UNAVAILABLE") from error
    return digest.hexdigest()


def _ci_detected(environment: Mapping[str, str]) -> bool:
    return any(
        (value := environment.get(key)) is not None
        and value.strip().casefold() not in {"", "0", "false", "no"}
        for key in _CI_KEYS
    )


def _count(database: Database, query: str, parameters: tuple[object, ...] = ()) -> int:
    value = database.scalar(query, parameters)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DataCalendarControlError("DATABASE_STATE_INVALID")
    return value


class DataCalendarController:
    """Keep rewrite/calendar mutations behind the existing local writer lease and audit chain."""

    def __init__(
        self,
        config_path: Path,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not isinstance(config_path, Path):
            raise TypeError("data/calendar control config path must be Path")
        if not callable(clock):
            raise TypeError("data/calendar control clock must be callable")
        self._config_path = config_path
        self._clock = clock

    def _now(self) -> datetime:
        observed = self._clock()
        if observed.tzinfo is None or observed.utcoffset() is None:
            raise DataCalendarControlError("CLOCK_UNAVAILABLE")
        return observed

    def _settings(self) -> Settings:
        if self._config_path.is_symlink() or not self._config_path.is_file():
            raise DataCalendarControlError("CONFIGURATION_UNAVAILABLE")
        try:
            return load_settings(self._config_path)
        except Exception as error:
            raise DataCalendarControlError("CONFIGURATION_INVALID") from error

    def _resolved(self, path: Path) -> Path:
        return path if path.is_absolute() else self._config_path.parent / path

    def _state_root(self, settings: Settings) -> Path:
        return self._resolved(settings.paths.state_directory)

    def _database_path(self, settings: Settings) -> Path:
        return self._state_root(settings) / "firmquant.sqlite3"

    def _calendar_path(self, settings: Settings) -> Path:
        return self._resolved(settings.paths.data_directory) / _CALENDAR_FILE

    @staticmethod
    def _runtime_state(database: Database) -> str:
        row = database.query_one("SELECT state FROM runtime_state WHERE singleton_id = 1")
        return "DISARMED" if row is None else str(row["state"])

    @staticmethod
    def _require_idle(database: Database) -> None:
        if DataCalendarController._runtime_state(database) != "DISARMED":
            raise DataCalendarControlError("RUNTIME_MUST_BE_DISARMED")
        if _count(database, "SELECT count(*) FROM arm_leases WHERE revoked_at IS NULL"):
            raise DataCalendarControlError("ACTIVE_ARM_LEASE_PRESENT")
        if _count(
            database,
            "SELECT count(*) FROM broker_orders WHERE status IN (?, ?, ?, ?)",
            _ACTIVE_BROKER_ORDER_STATES,
        ):
            raise DataCalendarControlError("ACTIVE_ORDER_PRESENT")
        if _count(
            database,
            "SELECT count(*) FROM execution_intents WHERE state IN (?, ?, ?, ?)",
            _ACTIVE_EXECUTION_STATES,
        ):
            raise DataCalendarControlError("ACTIVE_ORDER_PRESENT")
        unresolved = (
            _count(database, "SELECT count(*) FROM execution_intents WHERE state = 'UNKNOWN'")
            + _count(database, "SELECT count(*) FROM broker_order_attempts WHERE state = 'UNKNOWN'")
            + _count(database, "SELECT count(*) FROM broker_orders WHERE status = 'UNKNOWN'")
        )
        if unresolved:
            raise DataCalendarControlError("UNRESOLVED_UNKNOWN_PRESENT")

    @staticmethod
    def _operator_event_id(prefix: str, identity: str) -> str:
        digest = hashlib.sha256(f"{prefix}:{identity}".encode()).hexdigest()
        return f"operator:{prefix}:{digest}"

    def list_candidates(self) -> Mapping[str, object]:
        settings = self._settings()
        store = DataGenerationStore(self._state_root(settings))
        items: list[dict[str, object]] = []
        for path in sorted(store.candidates_root.iterdir(), key=lambda item: item.name):
            if not path.name.startswith("candidate-"):
                continue
            try:
                candidate = store.verify_candidate(path.name)
            except DataGenerationError:
                items.append({"candidate_id": path.name, "integrity": "INVALID"})
                continue
            items.append(
                {
                    "candidate_id": candidate.candidate_id,
                    "integrity": "VERIFIED",
                    "candidate_sha256": candidate.candidate_sha256,
                    "active_generation_id": candidate.active_generation_id,
                    "changed_symbols": list(candidate.changed_symbols),
                    "changed_sessions": [item.isoformat() for item in candidate.changed_sessions],
                    "first_difference_session": (
                        None
                        if candidate.first_difference_session is None
                        else candidate.first_difference_session.isoformat()
                    ),
                    "old_digest": candidate.old_digest,
                    "new_digest": candidate.new_digest,
                    "old_row_count": candidate.old_row_count,
                    "new_row_count": candidate.new_row_count,
                    "source": candidate.source,
                    "generated_at": candidate.generated_at.isoformat(),
                }
            )
        return {"count": len(items), "candidates": items}

    def verify_candidate(self, candidate_id: str) -> Mapping[str, object]:
        settings = self._settings()
        try:
            candidate = DataGenerationStore(self._state_root(settings)).verify_candidate(candidate_id)
        except DataGenerationError as error:
            raise DataCalendarControlError("DATA_CANDIDATE_INVALID") from error
        return {
            "candidate_id": candidate.candidate_id,
            "candidate_sha256": candidate.candidate_sha256,
            "active_generation_id": candidate.active_generation_id,
            "changed_symbols": list(candidate.changed_symbols),
            "changed_sessions": [item.isoformat() for item in candidate.changed_sessions],
            "first_difference_session": (
                None
                if candidate.first_difference_session is None
                else candidate.first_difference_session.isoformat()
            ),
            "old_digest": candidate.old_digest,
            "new_digest": candidate.new_digest,
            "old_row_count": candidate.old_row_count,
            "new_row_count": candidate.new_row_count,
            "source": candidate.source,
            "generated_at": candidate.generated_at.isoformat(),
            "verified": True,
        }

    def approve_candidate(
        self,
        candidate_id: str,
        *,
        interactive_terminal: bool,
        confirmation_reader: ConfirmationReader,
        environment: Mapping[str, str],
    ) -> Mapping[str, object]:
        if not interactive_terminal:
            raise DataCalendarControlError("DATA_APPROVAL_INTERACTIVE_TERMINAL_REQUIRED")
        if _ci_detected(environment):
            raise DataCalendarControlError("DATA_APPROVAL_FORBIDDEN_IN_CI")
        settings = self._settings()
        store = DataGenerationStore(self._state_root(settings))
        try:
            preview = store.verify_candidate(candidate_id)
        except DataGenerationError as error:
            raise DataCalendarControlError("DATA_CANDIDATE_INVALID") from error
        phrase = f"APPROVE DATA REWRITE {preview.candidate_id} {preview.candidate_sha256[:12]}"
        if confirmation_reader(f"请输入确认短语: {phrase}") != phrase:
            raise DataCalendarControlError("DATA_APPROVAL_CONFIRMATION_REJECTED")
        with WriterLease.acquire(
            self._database_path(settings),
            owner="operator-approve-data-candidate",
            clock=self._clock,
        ) as writer:
            AuditLedger(writer.database).verify()
            self._require_idle(writer.database)
            try:
                candidate = store.verify_candidate(preview.candidate_id)
            except DataGenerationError as error:
                raise DataCalendarControlError("DATA_CANDIDATE_INVALID") from error
            if candidate.candidate_sha256 != preview.candidate_sha256:
                raise DataCalendarControlError("DATA_CANDIDATE_CHANGED")
            try:
                promoted = store.promote_candidate(
                    candidate.candidate_id,
                    expected_candidate_sha256=candidate.candidate_sha256,
                    promoted_at=self._now(),
                )
            except DataGenerationError as error:
                raise DataCalendarControlError("DATA_PROMOTION_FAILED") from error
            receipt_path = store.promotions_root / f"{candidate.candidate_id}.json"
            receipt_sha256 = _sha256(receipt_path)
            now = self._now()
            event_id = self._operator_event_id("data-promotion", candidate.candidate_sha256)
            with writer.database.transaction():
                existing = writer.database.query_one(
                    "SELECT payload_json FROM audit_events WHERE audit_event_id = ?",
                    (event_id,),
                )
                payload = {
                    "schema": "firmquant.data-source-promotion-operator.v1",
                    "candidate_id": candidate.candidate_id,
                    "candidate_sha256": candidate.candidate_sha256,
                    "previous_generation_id": candidate.active_generation_id,
                    "new_generation_id": promoted.generation_id,
                    "new_data_sha256": promoted.data_sha256,
                    "source": promoted.source,
                    "source_receipt_sha256": receipt_sha256,
                }
                if existing is None:
                    AuditLedger(writer.database).append(
                        audit_event_id=event_id,
                        category="MARKET_DATA_OPERATOR",
                        actor="local-cli",
                        payload=payload,
                        created_at=now,
                    )
                elif str(existing["payload_json"]) != canonical_json(payload):
                    raise DataCalendarControlError("DATA_PROMOTION_AUDIT_CONFLICT")
        return {
            "candidate_id": candidate.candidate_id,
            "candidate_sha256": candidate.candidate_sha256,
            "previous_generation_id": candidate.active_generation_id,
            "active_generation_id": promoted.generation_id,
            "active_data_sha256": promoted.data_sha256,
            "source_receipt_sha256": receipt_sha256,
            "promoted": True,
        }

    @staticmethod
    def _max_date(values: tuple[object | None, ...]) -> date | None:
        observed: list[date] = []
        for value in values:
            if value is None:
                continue
            try:
                observed.append(date.fromisoformat(str(value)))
            except ValueError as error:
                raise DataCalendarControlError("CALENDAR_USED_SESSION_INVALID") from error
        return max(observed) if observed else None

    @staticmethod
    def _used_through(database: Database, *, fallback: date) -> date:
        values = (
            database.scalar("SELECT max(strategy_session) FROM decision_snapshots"),
            database.scalar("SELECT max(strategy_session) FROM execution_intents"),
            database.scalar("SELECT max(session_date) FROM broker_orders"),
            database.scalar("SELECT max(session_date) FROM fills"),
            database.scalar("SELECT max(session_date) FROM broker_snapshots"),
            database.scalar("SELECT max(session_date) FROM broker_events"),
            database.scalar(
                "SELECT max(json_extract(payload_json, '$.session')) FROM audit_events "
                "WHERE category = 'CLOSE_SESSION'"
            ),
        )
        return DataCalendarController._max_date(values) or fallback

    def calendar_status(self) -> Mapping[str, object]:
        settings = self._settings()
        try:
            calendar = load_trading_calendar_manifest(self._calendar_path(settings))
        except Exception as error:
            if settings.mode in {Mode.REPLAY, Mode.PAPER}:
                return {
                    "state": "NOT_REQUIRED",
                    "as_of": self._now().astimezone(ZoneInfo(settings.timezone)).date().isoformat(),
                    "covered_from": None,
                    "covered_through": None,
                    "remaining_days": None,
                    "calendar_sha256": None,
                    "source": None,
                    "source_sha256": None,
                    "warning_threshold_days": _CALENDAR_WARNING_DAYS,
                    "blocker": None,
                }
            raise DataCalendarControlError("CALENDAR_MANIFEST_INVALID") from error
        as_of = self._now().astimezone(ZoneInfo(settings.timezone)).date()
        status = calendar.coverage_status(as_of, warning_days=_CALENDAR_WARNING_DAYS)
        return {
            "state": status.state.value,
            "as_of": status.as_of.isoformat(),
            "covered_from": calendar.covered_from.isoformat(),
            "covered_through": status.covered_through.isoformat(),
            "remaining_days": status.remaining_days,
            "calendar_sha256": calendar.sha256,
            "source": calendar.source,
            "source_sha256": calendar.source_sha256,
            "warning_threshold_days": _CALENDAR_WARNING_DAYS,
            "blocker": (
                "CALENDAR_COVERAGE_EXPIRED"
                if status.state is CalendarCoverageState.EXPIRED
                else "CALENDAR_COVERAGE_WARNING"
                if status.state is CalendarCoverageState.WARNING
                else None
            ),
        }

    def update_calendar(
        self,
        manifest_path: Path,
        *,
        interactive_terminal: bool,
        confirmation_reader: ConfirmationReader,
        environment: Mapping[str, str],
    ) -> Mapping[str, object]:
        if not interactive_terminal:
            raise DataCalendarControlError("CALENDAR_UPDATE_INTERACTIVE_TERMINAL_REQUIRED")
        if _ci_detected(environment):
            raise DataCalendarControlError("CALENDAR_UPDATE_FORBIDDEN_IN_CI")
        candidate_path = Path(manifest_path)
        try:
            proposed = load_trading_calendar_manifest(candidate_path)
        except Exception as error:
            raise DataCalendarControlError("CALENDAR_CANDIDATE_INVALID") from error
        phrase = f"UPDATE TRADING CALENDAR {proposed.sha256[:12]}"
        if confirmation_reader(f"请输入确认短语: {phrase}") != phrase:
            raise DataCalendarControlError("CALENDAR_UPDATE_CONFIRMATION_REJECTED")
        settings = self._settings()
        current_path = self._calendar_path(settings)
        with WriterLease.acquire(
            self._database_path(settings),
            owner="operator-calendar-update",
            clock=self._clock,
        ) as writer:
            AuditLedger(writer.database).verify()
            self._require_idle(writer.database)
            try:
                current = load_trading_calendar_manifest(current_path)
                proposed = load_trading_calendar_manifest(candidate_path)
            except Exception as error:
                raise DataCalendarControlError("CALENDAR_MANIFEST_INVALID") from error
            used_through = self._used_through(writer.database, fallback=current.covered_from)
            if current.sha256 == proposed.sha256:
                update = validate_calendar_update(
                    current=current,
                    proposed=proposed,
                    used_through=used_through,
                )
            else:
                try:
                    update = validate_calendar_update(
                        current=current,
                        proposed=proposed,
                        used_through=used_through,
                    )
                except Exception as error:
                    raise DataCalendarControlError("CALENDAR_UPDATE_REJECTED") from error
                current_raw_sha256 = _sha256(current_path)
                history = self._state_root(settings) / "calendar-history"
                history.mkdir(parents=True, exist_ok=True)
                history_copy = history / f"{current_raw_sha256}.json"
                if not history_copy.exists():
                    shutil.copyfile(current_path, history_copy)
                candidate_bytes = candidate_path.read_bytes()
                temporary = current_path.with_suffix(".json.new")
                try:
                    with temporary.open("wb") as stream:
                        stream.write(candidate_bytes)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, current_path)
                except OSError as error:
                    raise DataCalendarControlError("CALENDAR_UPDATE_PUBLISH_FAILED") from error
                try:
                    published = load_trading_calendar_manifest(current_path)
                except Exception as error:
                    raise DataCalendarControlError("CALENDAR_UPDATE_PUBLISH_INVALID") from error
                if published.sha256 != proposed.sha256 or _sha256(current_path) != _sha256(candidate_path):
                    raise DataCalendarControlError("CALENDAR_UPDATE_IDENTITY_MISMATCH")
            now = self._now()
            event_id = self._operator_event_id("calendar-update", update.proposed_sha256)
            payload = {
                "schema": "firmquant.calendar-update-operator.v1",
                "previous_sha256": update.previous_sha256,
                "proposed_sha256": update.proposed_sha256,
                "used_through": update.used_through.isoformat(),
                "covered_through": update.covered_through.isoformat(),
                "source": proposed.source,
                "source_sha256": proposed.source_sha256,
                "published_raw_sha256": _sha256(current_path),
            }
            with writer.database.transaction():
                existing = writer.database.query_one(
                    "SELECT payload_json FROM audit_events WHERE audit_event_id = ?",
                    (event_id,),
                )
                if existing is None:
                    AuditLedger(writer.database).append(
                        audit_event_id=event_id,
                        category="CALENDAR_OPERATOR",
                        actor="local-cli",
                        payload=payload,
                        created_at=now,
                    )
                elif str(existing["payload_json"]) != canonical_json(payload):
                    raise DataCalendarControlError("CALENDAR_UPDATE_AUDIT_CONFLICT")
        return {
            "previous_sha256": update.previous_sha256,
            "calendar_sha256": update.proposed_sha256,
            "used_through": update.used_through.isoformat(),
            "covered_through": update.covered_through.isoformat(),
            "source": proposed.source,
            "source_sha256": proposed.source_sha256,
            "updated": True,
        }


__all__ = (
    "DataCalendarControlError",
    "DataCalendarController",
)
