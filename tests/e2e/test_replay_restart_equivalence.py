from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path

import pytest

from firmquant.broker.normalization import normalize_broker_event
from firmquant.broker.replay import RecordedReplayBroker
from firmquant.persistence.database import Database
from firmquant.persistence.repositories import BrokerEventRepository, canonical_sha256
from tests.fixtures.broker_contract import NOW, order_event, write_recording


class InjectedProcessCrash(RuntimeError):
    """Test-only crash after a durable broker event commit."""


def _event_rows(database: Database) -> tuple[tuple[object, ...], ...]:
    rows = database.query_all(
        "SELECT broker_event_id, event_type, broker_sequence, session_date, "
        "event_time, received_at, safe_payload_json, safe_payload_sha256, "
        "raw_payload_sha256 FROM broker_events "
        "ORDER BY event_time, broker_sequence, broker_event_id"
    )
    return tuple(tuple(row) for row in rows)


def _subscribe_into(
    broker: RecordedReplayBroker,
    database: Database,
    *,
    crash_after_commit: int | None = None,
) -> None:
    observed = 0

    def persist(raw_event: Mapping[str, object]) -> None:
        nonlocal observed
        envelope = normalize_broker_event(raw_event, received_at=NOW)
        with database.transaction():
            BrokerEventRepository(database).append(
                broker_event_id=envelope.broker_event_id,
                event_type=envelope.event_type.value,
                broker_sequence=envelope.broker_sequence,
                session_date=envelope.session_date,
                event_time=envelope.event_time,
                received_at=envelope.received_at,
                safe_payload=envelope.safe_payload,
                raw_payload_sha256=envelope.raw_payload_sha256,
            )
        observed += 1
        if crash_after_commit is not None and observed == crash_after_commit:
            raise InjectedProcessCrash("crash immediately after durable event commit")

    broker.subscribe(persist)


def test_replay_after_restart_matches_uninterrupted_durable_result(
    tmp_path: Path,
) -> None:
    recording = tmp_path / "incident.jsonl"
    duplicate = order_event(
        event_id="event-2",
        sequence=2,
        event_time=NOW + timedelta(seconds=1),
    )
    write_recording(
        recording,
        [
            order_event(
                event_id="event-4",
                sequence=4,
                event_time=NOW + timedelta(seconds=3),
            ),
            duplicate,
            order_event(event_id="event-1", sequence=1, event_time=NOW),
            duplicate,
            order_event(
                event_id="event-3",
                sequence=3,
                event_time=NOW + timedelta(seconds=2),
            ),
        ],
    )
    uninterrupted_broker = RecordedReplayBroker.from_jsonl(recording)
    restarted_broker = RecordedReplayBroker.from_jsonl(recording)
    assert uninterrupted_broker.state_sha256 == restarted_broker.state_sha256

    uninterrupted_path = tmp_path / "uninterrupted.sqlite3"
    uninterrupted = Database.open(uninterrupted_path)
    uninterrupted_broker.connect()
    try:
        _subscribe_into(uninterrupted_broker, uninterrupted)
        expected_rows = _event_rows(uninterrupted)
    finally:
        uninterrupted.close()

    restarted_path = tmp_path / "restarted.sqlite3"
    before_crash = Database.open(restarted_path)
    restarted_broker.connect()
    try:
        with pytest.raises(InjectedProcessCrash, match="durable event commit"):
            _subscribe_into(restarted_broker, before_crash, crash_after_commit=3)
    finally:
        before_crash.close()

    after_restart = Database.open(restarted_path)
    try:
        _subscribe_into(restarted_broker, after_restart)
        actual_rows = _event_rows(after_restart)
        assert actual_rows == expected_rows
        assert canonical_sha256(actual_rows) == canonical_sha256(expected_rows)
        assert after_restart.scalar("SELECT count(*) FROM broker_events") == 4
    finally:
        after_restart.close()

    assert uninterrupted_broker.write_attempts == ()
    assert restarted_broker.write_attempts == ()
