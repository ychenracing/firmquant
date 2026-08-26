from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from firmquant.broker.gateway import BrokerWriteForbidden
from firmquant.broker.replay import RecordedReplayBroker, ReplayFormatError
from tests.fixtures.broker_contract import (
    NOW,
    assert_read_gateway_contract,
    order_command,
    order_event,
    write_recording,
)


def test_replay_broker_passes_shared_read_contract(tmp_path: Path) -> None:
    path = tmp_path / "broker.jsonl"
    write_recording(path, [])
    broker = RecordedReplayBroker.from_jsonl(path)
    assert_read_gateway_contract(broker)


def test_replay_orders_callbacks_by_event_time_sequence_and_id(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    events = [
        order_event(event_id="event-c", sequence=2, event_time=NOW + timedelta(seconds=1)),
        order_event(event_id="event-b", sequence=2, event_time=NOW),
        order_event(event_id="event-a", sequence=2, event_time=NOW),
    ]
    write_recording(path, events)
    broker = RecordedReplayBroker.from_jsonl(path)
    received: list[dict[str, object]] = []
    broker.connect()
    broker.subscribe(received.append)

    assert [event["event_id"] for event in received] == [
        "event-a",
        "event-b",
        "event-c",
    ]


def test_replay_allows_exact_duplicate_but_rejects_event_id_collision(
    tmp_path: Path,
) -> None:
    duplicate_path = tmp_path / "duplicates.jsonl"
    duplicate = order_event(event_id="event-duplicate")
    write_recording(duplicate_path, [duplicate, duplicate])
    broker = RecordedReplayBroker.from_jsonl(duplicate_path)
    received: list[dict[str, object]] = []
    broker.connect()
    broker.subscribe(received.append)
    assert len(received) == 2

    conflict_path = tmp_path / "conflict.jsonl"
    conflict = order_event(event_id="event-duplicate", sequence=999)
    write_recording(conflict_path, [duplicate, conflict])
    with pytest.raises(ReplayFormatError, match="identity collision"):
        RecordedReplayBroker.from_jsonl(conflict_path)


def test_replay_never_accepts_submit_or_cancel(tmp_path: Path) -> None:
    path = tmp_path / "readonly.jsonl"
    write_recording(path, [])
    broker = RecordedReplayBroker.from_jsonl(path)
    broker.connect()

    with pytest.raises(BrokerWriteForbidden):
        broker.submit_order(order_command())
    with pytest.raises(BrokerWriteForbidden):
        broker.cancel_order("broker-order-1")
    assert broker.write_attempts == (
        ("SUBMIT", order_command().execution_id),
        ("CANCEL", "broker-order-1"),
    )


def test_same_recording_has_stable_state_digest(tmp_path: Path) -> None:
    path = tmp_path / "stable.jsonl"
    write_recording(path, [order_event()])

    assert (
        RecordedReplayBroker.from_jsonl(path).state_sha256
        == RecordedReplayBroker.from_jsonl(path).state_sha256
    )
