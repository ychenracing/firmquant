from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

import tests.unit.application.test_production_services_acceptance as base
from firmquant.application.data_calendar_control import (
    DataCalendarControlError,
    DataCalendarController,
)
from firmquant.config import Mode, Settings
from firmquant.market_data.generations import DataGenerationStore, RewriteCandidate
from firmquant.persistence.database import Database

NOW = datetime(2026, 8, 27, 8, tzinfo=UTC)


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, separators=(",", ":"), sort_keys=True), encoding="utf-8")


def _candidate_case(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Settings, DataGenerationStore, RewriteCandidate, DataCalendarController]:
    settings, config_path = base.settings_for(root, Mode.SHADOW)
    store = DataGenerationStore(settings.paths.state_directory)
    active = store.ensure_active(settings.paths.data_directory, source="xtquant", created_at=NOW)
    candidate = store.create_candidate(
        active_generation_id=active.generation_id,
        replacement_rows={
            "sz300308": (
                b"date,open,high,low,close,volume,amount\n"
                b"2026-08-24,9,9,9,9,1000,9000\n"
                b"2026-08-25,11,11,11,11,1000,11000\n"
            )
        },
        source="xtquant",
        generated_at=NOW,
    )
    controller = DataCalendarController(config_path, clock=lambda: NOW)
    monkeypatch.setattr(controller, "_settings", lambda: settings)
    return settings, store, candidate, controller


def test_operator_can_inspect_verify_and_explicitly_promote_rewrite_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings, store, candidate, controller = _candidate_case(tmp_path, monkeypatch)

    listing = controller.list_candidates()
    assert listing["count"] == 1
    assert listing["candidates"][0]["candidate_id"] == candidate.candidate_id  # type: ignore[index]
    verified = controller.verify_candidate(candidate.candidate_id)
    assert verified["verified"] is True
    assert verified["candidate_sha256"] == candidate.candidate_sha256

    with pytest.raises(DataCalendarControlError, match="INTERACTIVE_TERMINAL_REQUIRED"):
        controller.approve_candidate(
            candidate.candidate_id,
            interactive_terminal=False,
            confirmation_reader=lambda _prompt: "",
            environment={},
        )
    with pytest.raises(DataCalendarControlError, match="FORBIDDEN_IN_CI"):
        controller.approve_candidate(
            candidate.candidate_id,
            interactive_terminal=True,
            confirmation_reader=lambda _prompt: "",
            environment={"CI": "true"},
        )
    with pytest.raises(DataCalendarControlError, match="CONFIRMATION_REJECTED"):
        controller.approve_candidate(
            candidate.candidate_id,
            interactive_terminal=True,
            confirmation_reader=lambda _prompt: "wrong",
            environment={},
        )

    phrase = f"APPROVE DATA REWRITE {candidate.candidate_id} {candidate.candidate_sha256[:12]}"
    result = controller.approve_candidate(
        candidate.candidate_id,
        interactive_terminal=True,
        confirmation_reader=lambda _prompt: phrase,
        environment={},
    )
    assert result["promoted"] is True
    assert result["active_generation_id"] == store.active().generation_id
    assert result["active_generation_id"] != candidate.active_generation_id
    database = Database.open_read_only(tmp_path / "state" / "firmquant.sqlite3")
    try:
        assert (
            database.scalar(
                "SELECT count(*) FROM audit_events WHERE category = 'MARKET_DATA_OPERATOR'"
            )
            == 1
        )
    finally:
        database.close()


def test_rewrite_approval_requires_disarmed_runtime_and_unchanged_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, _store, candidate, controller = _candidate_case(tmp_path, monkeypatch)
    database = Database.open(settings.paths.state_directory / "firmquant.sqlite3")
    try:
        with database.transaction():
            database.write(
                """
                INSERT INTO runtime_state(
                    singleton_id, mode, state, revision, reason, blockers_json, updated_at
                ) VALUES (1, 'SHADOW', 'READY', 1, 'test ready', '[]', ?)
                """,
                (NOW.isoformat(),),
            )
    finally:
        database.close()

    phrase = f"APPROVE DATA REWRITE {candidate.candidate_id} {candidate.candidate_sha256[:12]}"
    with pytest.raises(DataCalendarControlError, match="RUNTIME_MUST_BE_DISARMED"):
        controller.approve_candidate(
            candidate.candidate_id,
            interactive_terminal=True,
            confirmation_reader=lambda _prompt: phrase,
            environment={},
        )

    database = Database.open(settings.paths.state_directory / "firmquant.sqlite3")
    try:
        with database.transaction():
            database.write("DELETE FROM runtime_state WHERE singleton_id = 1")
    finally:
        database.close()
    (candidate.path / "sz300308.csv").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(DataCalendarControlError, match="DATA_CANDIDATE_INVALID"):
        controller.approve_candidate(
            candidate.candidate_id,
            interactive_terminal=True,
            confirmation_reader=lambda _prompt: phrase,
            environment={},
        )


def _calendar_payload(*, end: str, sessions: list[str], digest: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "source_name": "reviewed-calendar",
        "source_sha256": digest,
        "covered_start": "2026-08-20",
        "covered_end": end,
        "sessions": sessions,
    }


def test_calendar_status_warns_expires_and_controlled_update_is_audited(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, config_path = base.settings_for(tmp_path, Mode.SHADOW)
    current_path = settings.paths.data_directory / "trading-calendar.json"
    current_sessions = [
        "2026-08-21",
        "2026-08-24",
        "2026-08-25",
        "2026-08-26",
        "2026-08-27",
        "2026-08-28",
    ]
    _write_json(
        current_path,
        _calendar_payload(end="2026-08-28", sessions=current_sessions, digest="a" * 64),
    )
    controller = DataCalendarController(config_path, clock=lambda: NOW)
    monkeypatch.setattr(controller, "_settings", lambda: settings)
    warning = controller.calendar_status()
    assert warning["state"] == "WARNING"
    assert warning["blocker"] == "CALENDAR_COVERAGE_WARNING"

    expired_controller = DataCalendarController(
        config_path,
        clock=lambda: datetime(2026, 8, 29, 8, tzinfo=UTC),
    )
    monkeypatch.setattr(expired_controller, "_settings", lambda: settings)
    expired = expired_controller.calendar_status()
    assert expired["state"] == "EXPIRED"
    assert expired["blocker"] == "CALENDAR_COVERAGE_EXPIRED"

    proposed_path = tmp_path / "calendar-proposed.json"
    proposed_sessions = [
        *current_sessions,
        "2026-08-31",
        "2026-09-01",
        "2026-09-02",
        "2026-09-03",
        "2026-09-04",
    ]
    _write_json(
        proposed_path,
        _calendar_payload(end="2026-09-04", sessions=proposed_sessions, digest="b" * 64),
    )
    phrase_holder: list[str] = []

    def confirmation(prompt: str) -> str:
        phrase = prompt.split(": ", 1)[1]
        phrase_holder.append(phrase)
        return phrase

    result = controller.update_calendar(
        proposed_path,
        interactive_terminal=True,
        confirmation_reader=confirmation,
        environment={},
    )
    assert phrase_holder and result["updated"] is True
    assert result["covered_through"] == "2026-09-04"
    assert json.loads(current_path.read_text(encoding="utf-8"))["source_sha256"] == "b" * 64
    history = settings.paths.state_directory / "calendar-history"
    assert len(tuple(history.glob("*.json"))) == 1
    database = Database.open_read_only(settings.paths.state_directory / "firmquant.sqlite3")
    try:
        assert database.scalar("SELECT count(*) FROM audit_events WHERE category = 'CALENDAR_OPERATOR'") == 1
    finally:
        database.close()


def test_calendar_update_rejects_noninteractive_ci_and_past_session_rewrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, config_path = base.settings_for(tmp_path, Mode.SHADOW)
    current_path = settings.paths.data_directory / "trading-calendar.json"
    current_sessions = ["2026-08-21", "2026-08-24", "2026-08-25", "2026-08-26"]
    _write_json(
        current_path,
        _calendar_payload(end="2026-08-31", sessions=current_sessions, digest="c" * 64),
    )
    proposed_path = tmp_path / "calendar-invalid.json"
    _write_json(
        proposed_path,
        _calendar_payload(
            end="2026-09-04",
            sessions=["2026-08-21", "2026-08-25", "2026-08-26", "2026-09-01"],
            digest="d" * 64,
        ),
    )
    controller = DataCalendarController(config_path, clock=lambda: NOW)
    monkeypatch.setattr(controller, "_settings", lambda: settings)
    with pytest.raises(DataCalendarControlError, match="INTERACTIVE_TERMINAL_REQUIRED"):
        controller.update_calendar(
            proposed_path,
            interactive_terminal=False,
            confirmation_reader=lambda _prompt: "",
            environment={},
        )
    with pytest.raises(DataCalendarControlError, match="FORBIDDEN_IN_CI"):
        controller.update_calendar(
            proposed_path,
            interactive_terminal=True,
            confirmation_reader=lambda _prompt: "",
            environment={"GITHUB_ACTIONS": "1"},
        )

    database = Database.open(settings.paths.state_directory / "firmquant.sqlite3")
    try:
        with database.transaction():
            database.write(
                """
                INSERT INTO broker_snapshots(
                    snapshot_id, account_id_hash, account_type, session_date, captured_at,
                    broker_event_watermark, raw_payload_sha256, complete
                ) VALUES (?, ?, 'CASH', '2026-08-25', ?, 0, ?, 1)
                """,
                ("calendar-used", "a" * 64, NOW.isoformat(), "e" * 64),
            )
    finally:
        database.close()

    def confirmation(prompt: str) -> str:
        return prompt.split(": ", 1)[1]

    with pytest.raises(DataCalendarControlError, match="CALENDAR_UPDATE_REJECTED"):
        controller.update_calendar(
            proposed_path,
            interactive_terminal=True,
            confirmation_reader=confirmation,
            environment={},
        )
