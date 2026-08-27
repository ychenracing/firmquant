from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from firmquant.application.control_channel import (
    MAX_CONTROL_REQUEST_BYTES,
    ControlChannelError,
    ControlCommand,
    ControlExecution,
    ControlInbox,
    ControlReceipt,
    ControlRequest,
    ControlRequestRejected,
    ControlStatus,
    _reason_sha256,
    _strict_json,
    local_host_hash,
)

NOW = datetime(2026, 8, 27, 6, 40, tzinfo=UTC)
HOST = "a" * 64
REQUEST_ID = "ctrl_" + "1" * 64


def _canonical(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _request(**changes: object) -> ControlRequest:
    values: dict[str, object] = {
        "request_id": REQUEST_ID,
        "command": ControlCommand.HALT,
        "created_at": NOW,
        "expires_at": NOW + timedelta(minutes=5),
        "host_hash": HOST,
        "reason_sha256": None,
    }
    values.update(changes)
    return ControlRequest(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (b"\xff", "CONTROL_REQUEST_NOT_UTF8"),
        (b'{"a":1,"a":2}', "CONTROL_REQUEST_DUPLICATE_KEY"),
        (b'{"a":NaN}', "CONTROL_REQUEST_NON_STANDARD_JSON"),
        (b"{", "CONTROL_REQUEST_MALFORMED_JSON"),
        (b"[]", "CONTROL_REQUEST_NOT_OBJECT"),
        (b'{"b":1,"a":2}', "CONTROL_REQUEST_NOT_CANONICAL_JSON"),
    ],
)
def test_strict_json_rejects_noncanonical_or_ambiguous_input(raw: bytes, reason: str) -> None:
    with pytest.raises(ControlRequestRejected, match=reason):
        _strict_json(raw)

    assert _strict_json(b'{"a":1}') == {"a": 1}


def test_reason_digest_is_bounded_canonical_and_never_stores_plaintext() -> None:
    expected = hashlib.sha256(b"operator emergency halt").hexdigest()
    assert _reason_sha256("  operator   emergency halt  ") == expected
    assert _reason_sha256(None) is None

    with pytest.raises(TypeError, match="text or null"):
        _reason_sha256(1)  # type: ignore[arg-type]
    for invalid in ("", " " * 3, "x" * 513, "bad\x00reason"):
        with pytest.raises(ValueError, match="control reason"):
            _reason_sha256(invalid)


def test_request_execution_and_receipt_models_fail_closed_on_invalid_types() -> None:
    with pytest.raises(ValueError, match="request id"):
        _request(request_id="bad")
    with pytest.raises(TypeError, match="command"):
        _request(command="HALT")
    with pytest.raises(ValueError, match="timezone-aware"):
        _request(created_at=datetime(2026, 8, 27, 6, 40))
    with pytest.raises(ValueError, match="expire after creation"):
        _request(expires_at=NOW)
    with pytest.raises(ValueError, match="ttl exceeds"):
        _request(expires_at=NOW + timedelta(minutes=16))
    with pytest.raises(ValueError, match="host binding"):
        _request(host_hash="bad")
    with pytest.raises(ValueError, match="reason digest"):
        _request(reason_sha256="bad")

    with pytest.raises(TypeError, match="outcome"):
        ControlExecution(outcome=[])  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="flags"):
        ControlExecution(outcome={}, halted=1)  # type: ignore[arg-type]

    valid_hash = "b" * 64
    with pytest.raises(ValueError, match="receipt id"):
        ControlReceipt("bad", None, ControlStatus.REJECTED, valid_hash, NOW, {})
    with pytest.raises(TypeError, match="receipt command"):
        ControlReceipt("invalid_" + "1" * 64, "HALT", ControlStatus.REJECTED, valid_hash, NOW, {})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="terminal"):
        ControlReceipt("invalid_" + "1" * 64, None, ControlStatus.QUEUED, valid_hash, NOW, {})
    with pytest.raises(ValueError, match="request hash"):
        ControlReceipt("invalid_" + "1" * 64, None, ControlStatus.REJECTED, "bad", NOW, {})
    with pytest.raises(ValueError, match="timezone-aware"):
        ControlReceipt(
            "invalid_" + "1" * 64,
            None,
            ControlStatus.REJECTED,
            valid_hash,
            datetime(2026, 8, 27, 6, 40),
            {},
        )


def test_inbox_validation_idempotency_collision_and_status_contract(tmp_path: Path) -> None:
    with pytest.raises(ControlChannelError, match="STATE_DIRECTORY_INVALID"):
        ControlInbox(tmp_path / "missing")

    state = tmp_path / "state"
    state.mkdir()
    with pytest.raises(ValueError, match="host hash"):
        ControlInbox(state, host_hash="bad")

    inbox = ControlInbox(state, clock=lambda: NOW, host_hash=HOST)
    with pytest.raises(TypeError, match="command"):
        inbox.enqueue("HALT")  # type: ignore[arg-type]
    for ttl in (timedelta(0), timedelta(minutes=16)):
        with pytest.raises(ValueError, match="control ttl"):
            inbox.enqueue(ControlCommand.HALT, ttl=ttl)
    with pytest.raises(ValueError, match="status request id"):
        inbox.status("bad")
    assert inbox.status("ctrl_" + "9" * 64).status is ControlStatus.UNKNOWN
    with pytest.raises(TypeError, match="handler"):
        inbox.process_pending(None)  # type: ignore[arg-type]

    request = inbox.enqueue(ControlCommand.HALT, request_id=REQUEST_ID, reason="risk reduction")
    assert inbox.status(request.request_id).status is ControlStatus.QUEUED
    with pytest.raises(ControlChannelError, match="ALREADY_QUEUED"):
        inbox.enqueue(ControlCommand.HALT, request_id=REQUEST_ID, reason="risk reduction")
    with pytest.raises(TypeError, match="return ControlExecution"):
        inbox.process_pending(lambda _request: object())  # type: ignore[arg-type,return-value]

    batch = inbox.process_pending(lambda _request: ControlExecution({"ok": True}, halted=True, stop=True))
    assert batch.halted is True and batch.stop is True
    assert inbox.status(request.request_id).status is ControlStatus.COMPLETED

    same = inbox.enqueue(ControlCommand.HALT, request_id=REQUEST_ID, reason="risk reduction")
    assert same.payload_sha256 == request.payload_sha256

    later = ControlInbox(state, clock=lambda: NOW + timedelta(seconds=1), host_hash=HOST)
    with pytest.raises(ControlChannelError, match="REQUEST_ID_COLLISION"):
        later.enqueue(ControlCommand.HALT, request_id=REQUEST_ID, reason="risk reduction")


def test_inbox_parse_request_checks_fields_host_future_and_expiry(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    inbox = ControlInbox(state, clock=lambda: NOW, host_hash=HOST)
    valid = _request().payload()

    missing = dict(valid)
    missing.pop("command")
    with pytest.raises(ControlRequestRejected, match="FIELDS_INVALID"):
        inbox._parse_request(missing, now=NOW)

    bad_command = dict(valid)
    bad_command["command"] = "SUBMIT"
    with pytest.raises(ControlRequestRejected, match="FIELDS_INVALID"):
        inbox._parse_request(bad_command, now=NOW)

    foreign_host = dict(valid)
    foreign_host["host_hash"] = "b" * 64
    with pytest.raises(ControlRequestRejected, match="HOST_MISMATCH"):
        inbox._parse_request(foreign_host, now=NOW)

    future = dict(valid)
    future["created_at"] = (NOW + timedelta(seconds=1)).isoformat()
    future["expires_at"] = (NOW + timedelta(minutes=1)).isoformat()
    with pytest.raises(ControlRequestRejected, match="FROM_FUTURE"):
        inbox._parse_request(future, now=NOW)

    expired = dict(valid)
    expired["created_at"] = (NOW - timedelta(seconds=2)).isoformat()
    expired["expires_at"] = (NOW - timedelta(seconds=1)).isoformat()
    with pytest.raises(ControlRequestRejected, match="EXPIRED"):
        inbox._parse_request(expired, now=NOW)


def test_receipt_reader_rejects_nonregular_oversize_schema_and_outcome(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    inbox = ControlInbox(state, clock=lambda: NOW, host_hash=HOST)

    directory = inbox.receipt_directory / ("invalid_" + "2" * 64 + ".json")
    directory.mkdir()
    with pytest.raises(ControlChannelError, match="RECEIPT_NOT_REGULAR"):
        inbox._read_receipt(directory)

    oversized = inbox.receipt_directory / ("invalid_" + "3" * 64 + ".json")
    oversized.write_bytes(b"x" * (MAX_CONTROL_REQUEST_BYTES * 2 + 1))
    with pytest.raises(ControlChannelError, match="RECEIPT_TOO_LARGE"):
        inbox._read_receipt(oversized)

    base = {
        "command": None,
        "outcome": {},
        "processed_at": NOW.isoformat(),
        "request_id": "invalid_" + "4" * 64,
        "request_sha256": "b" * 64,
        "schema": "wrong",
        "status": "REJECTED",
    }
    invalid_schema = inbox.receipt_directory / ("invalid_" + "4" * 64 + ".json")
    invalid_schema.write_bytes(_canonical(base))
    with pytest.raises(ControlChannelError, match="RECEIPT_INVALID"):
        inbox._read_receipt(invalid_schema)

    base["schema"] = "firmquant.control-receipt.v1"
    base["outcome"] = []
    invalid_outcome = inbox.receipt_directory / ("invalid_" + "5" * 64 + ".json")
    base["request_id"] = "invalid_" + "5" * 64
    invalid_outcome.write_bytes(_canonical(base))
    with pytest.raises(ControlChannelError, match="OUTCOME_INVALID"):
        inbox._read_receipt(invalid_outcome)


def test_receipt_write_is_idempotent_but_rejects_conflicting_content(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    inbox = ControlInbox(state, clock=lambda: NOW, host_hash=HOST)
    receipt = ControlReceipt(
        request_id="invalid_" + "6" * 64,
        command=None,
        status=ControlStatus.REJECTED,
        request_sha256="b" * 64,
        processed_at=NOW,
        outcome={"reason": "test"},
    )
    inbox._write_receipt(receipt)
    inbox._write_receipt(receipt)

    conflict = ControlReceipt(
        request_id=receipt.request_id,
        command=None,
        status=ControlStatus.REJECTED,
        request_sha256=receipt.request_sha256,
        processed_at=NOW,
        outcome={"reason": "different"},
    )
    with pytest.raises(ControlChannelError, match="RECEIPT_COLLISION"):
        inbox._write_receipt(conflict)


def test_local_host_hash_fails_closed_when_hostname_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("firmquant.application.control_channel.socket.gethostname", lambda: "")
    with pytest.raises(ControlChannelError, match="HOSTNAME_UNAVAILABLE"):
        local_host_hash()
