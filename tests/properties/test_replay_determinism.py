from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from hypothesis import given, settings
from hypothesis import strategies as st

from firmquant.broker.replay import RecordedReplayBroker
from tests.fixtures.broker_contract import order_event, write_recording


@settings(max_examples=20, deadline=None)
@given(ordering=st.permutations((1, 2, 3, 4, 5)))
def test_replay_result_is_independent_of_recording_line_order(
    ordering: list[int],
) -> None:
    with TemporaryDirectory(prefix="firmquant-replay-property-") as directory:
        root = Path(directory)
        left_path = root / "left.jsonl"
        right_path = root / "right.jsonl"
        events = {
            sequence: order_event(event_id=f"event-{sequence}", sequence=sequence)
            for sequence in ordering
        }
        write_recording(left_path, [events[sequence] for sequence in ordering])
        write_recording(right_path, [events[sequence] for sequence in reversed(ordering)])

        left = RecordedReplayBroker.from_jsonl(left_path)
        right = RecordedReplayBroker.from_jsonl(right_path)
        left_events: list[dict[str, object]] = []
        right_events: list[dict[str, object]] = []
        left.connect()
        right.connect()
        left.subscribe(left_events.append)
        right.subscribe(right_events.append)

        assert left_events == right_events
        assert left.state_sha256 == right.state_sha256
