from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import firmquant.application.control_channel as channel
from firmquant.application.control_channel import (
    ControlChannelError,
    ControlCommand,
    ControlExecution,
    ControlInbox,
    ControlReceipt,
    ControlRequest,
    ControlRequestRejected,
    ControlStatus,
)
from tests.fixtures.recovery_cases import NOW


def _valid_request(**changes: object) -> ControlRequest:
    values: dict[str, object] = {
        "request_id": "ctrl_" + "1" * 64,
        "command": ControlCommand.HALT,
        "created_at": NOW,
        "expires_at": NOW + timedelta(minutes=1),
        "host_hash": "a" * 64,
        "reason_sha256": "b" * 64,
    }
    values.update(changes)
    return ControlRequest(**values)  # type: ignore[arg-type]


def _valid_receipt(**changes: object) -> ControlReceipt:
    values: dict[str, object] = {
        "request_id": "ctrl_" + "1" * 64,
        "command": ControlCommand.HALT,
        "status": ControlStatus.COMPLETED,
        "request_sha256": "c" * 64,
        "processed_at": NOW,
        "outcome": {"ok": True},
    }
    values.update(changes)
    return ControlReceipt(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (b"\xff", "CONTROL_REQUEST_NOT_UTF8"),
        (b'{"value":NaN}', "CONTROL_REQUEST_NON_STANDARD_JSON"),
        (b'{"value":', "CONTROL_REQUEST_MALFORMED_JSON"),
        (b"[]", "CONTROL_REQUEST_NOT_OBJECT"),
        (b'{"value":1}\n', "CONTROL_REQUEST_NOT_CANONICAL_JSON"),
    ],
)
def test_strict_json_rejects_noncanonical_untrusted_forms(raw: bytes, reason: str) -> None:
    with pytest.raises(ControlRequestRejected, match=reason):
        channel._strict_json(raw)


def test_control_time_requires_aware_datetime() -> None:
    with pytest.raises(TypeError, match="datetime"):
        channel._aware("not-time", label="test")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="timezone-aware"):
        channel._aware(datetime(2026, 8, 27, 1, 0), label="test")


@pytest.mark.parametrize(
    ("reason", "error"),
    [
        (1, TypeError),
        ("   ", ValueError),
        ("x" * 513, ValueError),
        ("line\x00break", ValueError),
    ],
)
def test_reason_digest_rejects_noncanonical_text(reason: object, error: type[Exception]) -> None:
    with pytest.raises(error):
        channel._reason_sha256(reason)  # type: ignore[arg-type]


def test_reason_digest_is_canonical_and_optional() -> None:
    assert channel._reason_sha256(None) is None
    assert channel._reason_sha256("  operator   halt  ") == channel._reason_sha256("operator halt")


@pytest.mark.parametrize(
    "changes",
    [
        {"request_id": "../escape"},
        {"command": "HALT"},
        {"expires_at": NOW},
        {"expires_at": NOW + timedelta(minutes=16)},
        {"host_hash": "not-a-hash"},
        {"reason_sha256": "not-a-hash"},
    ],
)
def test_control_request_rejects_invalid_identity_and_ttl(changes: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        _valid_request(**changes)


def test_control_request_rejects_naive_timestamps() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _valid_request(created_at=datetime(2026, 8, 27, 1, 0))
    with pytest.raises(ValueError, match="timezone-aware"):
        _valid_request(expires_at=datetime(2026, 8, 27, 1, 1))


def test_control_request_payload_omits_absent_reason() -> None:
    request = _valid_request(reason_sha256=None)
    assert "reason_sha256" not in request.payload()
    assert len(request.payload_sha256) == 64


@pytest.mark.parametrize(
    "kwargs",
    [
        {"outcome": "not-a-mapping"},
        {"outcome": {}, "halted": 1},
        {"outcome": {}, "stop": 1},
    ],
)
def test_control_execution_rejects_invalid_shape(kwargs: dict[str, object]) -> None:
    with pytest.raises(TypeError):
        ControlExecution(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "changes",
    [
        {"request_id": "bad"},
        {"command": "HALT"},
        {"status": ControlStatus.QUEUED},
        {"request_sha256": "bad"},
        {"processed_at": datetime(2026, 8, 27, 1, 0)},
    ],
)
def test_control_receipt_rejects_invalid_terminal_evidence(changes: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        _valid_receipt(**changes)


def test_control_receipt_allows_rejected_request_without_command() -> None:
    receipt = _valid_receipt(
        request_id="invalid_" + "2" * 64,
        command=None,
        status=ControlStatus.REJECTED,
    )
    assert receipt.payload()["command"] is None
    assert receipt.payload()["status"] == "REJECTED"


def test_control_inbox_rejects_invalid_state_root_and_host_hash(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(ControlChannelError, match="CONTROL_STATE_DIRECTORY_INVALID"):
        ControlInbox(missing)

    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "state-link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        link = target / "not-a-directory"
        link.write_text("x", encoding="utf-8")
    with pytest.raises(ControlChannelError, match="CONTROL_STATE_DIRECTORY_INVALID"):
        ControlInbox(link)

    with pytest.raises(ValueError, match="host hash"):
        ControlInbox(target, host_hash="bad")


def test_control_inbox_status_and_enqueue_validation(tmp_path: Path) -> None:
    inbox = ControlInbox(tmp_path, host_hash="d" * 64, clock=lambda: NOW)
    unknown_id = "ctrl_" + "3" * 64
    assert inbox.status(unknown_id).status is ControlStatus.UNKNOWN
    with pytest.raises(ValueError, match="request id"):
        inbox.status("bad")
    with pytest.raises(TypeError, match="typed"):
        inbox.enqueue("HALT")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="ttl"):
        inbox.enqueue(ControlCommand.HALT, ttl=timedelta(0))
    with pytest.raises(ValueError, match="ttl"):
        inbox.enqueue(ControlCommand.HALT, ttl=timedelta(minutes=16))


def test_control_request_id_collision_and_completed_idempotency(tmp_path: Path) -> None:
    def clock() -> datetime:
        return NOW

    request_id = "ctrl_" + "4" * 64
    inbox = ControlInbox(tmp_path, host_hash="e" * 64, clock=clock)
    original = inbox.enqueue(ControlCommand.DISARM, request_id=request_id)
    with pytest.raises(ControlChannelError, match="CONTROL_REQUEST_ALREADY_QUEUED"):
        inbox.enqueue(ControlCommand.DISARM, request_id=request_id)

    batch = inbox.process_pending(lambda _request: ControlExecution(outcome={"done": True}))
    assert len(batch.receipts) == 1
    repeated = inbox.enqueue(ControlCommand.DISARM, request_id=request_id)
    assert repeated.payload_sha256 == original.payload_sha256
    with pytest.raises(ControlChannelError, match="CONTROL_REQUEST_ID_COLLISION"):
        inbox.enqueue(ControlCommand.HALT, request_id=request_id)


def test_control_handler_contract_is_fail_closed(tmp_path: Path) -> None:
    inbox = ControlInbox(tmp_path, host_hash="f" * 64, clock=lambda: NOW)
    with pytest.raises(TypeError, match="callable"):
        inbox.process_pending(None)  # type: ignore[arg-type]

    inbox.enqueue(ControlCommand.HALT, request_id="ctrl_" + "5" * 64)
    with pytest.raises(TypeError, match="ControlExecution"):
        inbox.process_pending(lambda _request: object())  # type: ignore[arg-type,return-value]


def test_control_receipt_reader_rejects_symlink_and_oversize(tmp_path: Path) -> None:
    inbox = ControlInbox(tmp_path, host_hash="0" * 64, clock=lambda: NOW)
    request_id = "ctrl_" + "6" * 64
    receipt = inbox.receipt_directory / f"{request_id}.json"
    receipt.write_bytes(b"x" * (channel.MAX_CONTROL_REQUEST_BYTES * 2 + 1))
    if os.name != "nt":
        receipt.chmod(0o600)
    with pytest.raises(ControlChannelError, match="CONTROL_RECEIPT_TOO_LARGE"):
        inbox.status(request_id)

    receipt.unlink()
    outside = tmp_path / "outside"
    outside.write_text("{}", encoding="utf-8")
    try:
        receipt.symlink_to(outside)
    except (OSError, NotImplementedError):
        return
    assert inbox.status(request_id).status is ControlStatus.UNKNOWN
