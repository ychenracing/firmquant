"""Local filesystem control inbox for risk-reducing production commands."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import socket
import stat
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Final, cast

MAX_CONTROL_REQUEST_BYTES: Final = 4_096
_DEFAULT_TTL: Final = timedelta(minutes=5)
_MAX_TTL: Final = timedelta(minutes=15)
_REQUEST_ID = re.compile(r"^ctrl_[0-9a-f]{64}$")
_RECEIPT_ID = re.compile(r"^(?:ctrl|invalid)_[0-9a-f]{64}$")
_REQUIRED_FIELDS = frozenset({"request_id", "command", "created_at", "expires_at", "host_hash"})
_OPTIONAL_FIELDS = frozenset({"reason_sha256"})
_PRIORITY: Final = {"HALT": 0, "DISARM": 1, "CANCEL_SYSTEM_ORDERS": 2, "STOP": 3}


class ControlChannelError(RuntimeError):
    """Base error for the local production control channel."""


class ControlRequestRejected(ControlChannelError):
    """Raised when an untrusted request cannot be accepted."""


class ControlCommand(StrEnum):
    HALT = "HALT"
    DISARM = "DISARM"
    CANCEL_SYSTEM_ORDERS = "CANCEL_SYSTEM_ORDERS"
    STOP = "STOP"


class ControlStatus(StrEnum):
    UNKNOWN = "UNKNOWN"
    QUEUED = "QUEUED"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"


def _aware(value: datetime, *, label: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{label} must be datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value


def _canonical_json(payload: Mapping[str, object]) -> bytes:
    return json.dumps(
        dict(payload), ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False
    ).encode()


def _strict_json(raw: bytes) -> dict[str, object]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ControlRequestRejected("CONTROL_REQUEST_NOT_UTF8") from error

    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                raise ControlRequestRejected("CONTROL_REQUEST_DUPLICATE_KEY")
            result[key] = value
        return result

    def constant(_value: str) -> object:
        raise ControlRequestRejected("CONTROL_REQUEST_NON_STANDARD_JSON")

    try:
        parsed = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except ControlRequestRejected:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise ControlRequestRejected("CONTROL_REQUEST_MALFORMED_JSON") from error
    if not isinstance(parsed, dict):
        raise ControlRequestRejected("CONTROL_REQUEST_NOT_OBJECT")
    payload = cast(dict[str, object], parsed)
    if _canonical_json(payload) != raw:
        raise ControlRequestRejected("CONTROL_REQUEST_NOT_CANONICAL_JSON")
    return payload


def local_host_hash() -> str:
    hostname = socket.gethostname().strip().lower()
    if not hostname:
        raise ControlChannelError("CONTROL_HOSTNAME_UNAVAILABLE")
    return hashlib.sha256(hostname.encode()).hexdigest()


def _reason_sha256(reason: str | None) -> str | None:
    if reason is None:
        return None
    if not isinstance(reason, str):
        raise TypeError("control reason must be text or null")
    canonical = " ".join(reason.strip().split())
    if not canonical or len(canonical) > 512:
        raise ValueError("control reason must contain 1..512 canonical characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in canonical):
        raise ValueError("control reason contains control characters")
    return hashlib.sha256(canonical.encode()).hexdigest()


def _new_request_id() -> str:
    return "ctrl_" + hashlib.sha256(secrets.token_bytes(32)).hexdigest()


def _ensure_private_directory(path: Path) -> None:
    try:
        if path.is_symlink():
            raise ControlChannelError("CONTROL_DIRECTORY_SYMLINK")
        path.mkdir(mode=0o700, parents=False, exist_ok=True)
        metadata = path.lstat()
    except OSError as error:
        raise ControlChannelError("CONTROL_DIRECTORY_UNAVAILABLE") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ControlChannelError("CONTROL_DIRECTORY_NOT_FIXED")
    if os.name != "nt":
        try:
            path.chmod(0o700)
        except OSError as error:
            raise ControlChannelError("CONTROL_DIRECTORY_PERMISSIONS") from error
        if path.stat().st_mode & 0o077:
            raise ControlChannelError("CONTROL_DIRECTORY_NOT_PRIVATE")


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(directory: Path, target: Path, payload: bytes) -> None:
    temporary = directory / f".tmp-{os.getpid()}-{secrets.token_hex(16)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if target.is_symlink():
            raise ControlChannelError("CONTROL_TARGET_SYMLINK")
        os.replace(temporary, target)
        if os.name != "nt":
            target.chmod(0o600)
        _fsync_directory(directory)
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise


@dataclass(frozen=True, slots=True)
class ControlRequest:
    request_id: str
    command: ControlCommand
    created_at: datetime
    expires_at: datetime
    host_hash: str
    reason_sha256: str | None = None

    def __post_init__(self) -> None:
        if _REQUEST_ID.fullmatch(self.request_id) is None:
            raise ValueError("control request id is invalid")
        if not isinstance(self.command, ControlCommand):
            raise TypeError("control command must be typed")
        _aware(self.created_at, label="control created_at")
        _aware(self.expires_at, label="control expires_at")
        if self.expires_at <= self.created_at:
            raise ValueError("control request must expire after creation")
        if self.expires_at - self.created_at > _MAX_TTL:
            raise ValueError("control request ttl exceeds maximum")
        if not isinstance(self.host_hash, str) or re.fullmatch(r"[0-9a-f]{64}", self.host_hash) is None:
            raise ValueError("control host binding must be SHA-256")
        if self.reason_sha256 is not None and re.fullmatch(r"[0-9a-f]{64}", self.reason_sha256) is None:
            raise ValueError("control reason digest must be SHA-256")

    def payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "command": self.command.value,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "host_hash": self.host_hash,
            "request_id": self.request_id,
        }
        if self.reason_sha256 is not None:
            payload["reason_sha256"] = self.reason_sha256
        return payload

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.payload())

    @property
    def payload_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class ControlExecution:
    outcome: Mapping[str, object]
    halted: bool = False
    stop: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, Mapping):
            raise TypeError("control outcome must be a mapping")
        if not isinstance(self.halted, bool) or not isinstance(self.stop, bool):
            raise TypeError("control execution flags must be bool")
        object.__setattr__(self, "outcome", MappingProxyType(dict(self.outcome)))


@dataclass(frozen=True, slots=True)
class ControlReceipt:
    request_id: str
    command: ControlCommand | None
    status: ControlStatus
    request_sha256: str
    processed_at: datetime
    outcome: Mapping[str, object]

    def __post_init__(self) -> None:
        if _RECEIPT_ID.fullmatch(self.request_id) is None:
            raise ValueError("control receipt id is invalid")
        if self.command is not None and not isinstance(self.command, ControlCommand):
            raise TypeError("control receipt command must be typed or null")
        if self.status not in {ControlStatus.COMPLETED, ControlStatus.REJECTED}:
            raise ValueError("control receipt status must be terminal")
        if re.fullmatch(r"[0-9a-f]{64}", self.request_sha256) is None:
            raise ValueError("control receipt request hash must be SHA-256")
        _aware(self.processed_at, label="control processed_at")
        object.__setattr__(self, "outcome", MappingProxyType(dict(self.outcome)))

    def payload(self) -> dict[str, object]:
        return {
            "command": None if self.command is None else self.command.value,
            "outcome": dict(self.outcome),
            "processed_at": self.processed_at.isoformat(),
            "request_id": self.request_id,
            "request_sha256": self.request_sha256,
            "schema": "firmquant.control-receipt.v1",
            "status": self.status.value,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.payload())


@dataclass(frozen=True, slots=True)
class ControlStatusView:
    request_id: str
    status: ControlStatus
    command: ControlCommand | None = None
    processed_at: datetime | None = None
    outcome: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class ControlBatch:
    receipts: tuple[ControlReceipt, ...]
    halted: bool
    stop: bool


class ControlInbox:
    """Fixed local inbox with canonical requests, durable receipts, and idempotent consumption."""

    def __init__(
        self,
        state_directory: Path,
        *,
        clock: Callable[[], datetime] | None = None,
        request_id_factory: Callable[[], str] | None = None,
        host_hash: str | None = None,
    ) -> None:
        root = Path(state_directory)
        if root.is_symlink() or not root.is_dir():
            raise ControlChannelError("CONTROL_STATE_DIRECTORY_INVALID")
        self._clock = clock or (lambda: datetime.now(UTC))
        self._request_id_factory = request_id_factory or _new_request_id
        self._host_hash = host_hash or local_host_hash()
        if re.fullmatch(r"[0-9a-f]{64}", self._host_hash) is None:
            raise ValueError("control host hash must be SHA-256")
        control = root / "control"
        _ensure_private_directory(control)
        self._inbox = control / "inbox"
        self._receipts = control / "receipts"
        _ensure_private_directory(self._inbox)
        _ensure_private_directory(self._receipts)

    @property
    def inbox_directory(self) -> Path:
        return self._inbox

    @property
    def receipt_directory(self) -> Path:
        return self._receipts

    @property
    def host_hash(self) -> str:
        return self._host_hash

    def enqueue(
        self,
        command: ControlCommand,
        *,
        reason: str | None = None,
        ttl: timedelta = _DEFAULT_TTL,
        request_id: str | None = None,
    ) -> ControlRequest:
        if not isinstance(command, ControlCommand):
            raise TypeError("control command must be typed")
        if not isinstance(ttl, timedelta) or ttl <= timedelta(0) or ttl > _MAX_TTL:
            raise ValueError("control ttl must be within (0, 15 minutes]")
        created_at = _aware(self._clock(), label="control clock")
        request = ControlRequest(
            request_id=request_id or self._request_id_factory(),
            command=command,
            created_at=created_at,
            expires_at=created_at + ttl,
            host_hash=self._host_hash,
            reason_sha256=_reason_sha256(reason),
        )
        payload = request.canonical_bytes()
        if len(payload) > MAX_CONTROL_REQUEST_BYTES:
            raise ControlChannelError("CONTROL_REQUEST_TOO_LARGE")
        receipt_path = self._receipt_path(request.request_id)
        if receipt_path.exists():
            existing = self._read_receipt(receipt_path)
            if existing.request_sha256 != request.payload_sha256:
                raise ControlChannelError("CONTROL_REQUEST_ID_COLLISION")
            return request
        target = self._request_path(request.request_id)
        if target.exists() or target.is_symlink():
            raise ControlChannelError("CONTROL_REQUEST_ALREADY_QUEUED")
        _atomic_write(self._inbox, target, payload)
        return request

    def status(self, request_id: str) -> ControlStatusView:
        if _REQUEST_ID.fullmatch(request_id) is None:
            raise ValueError("control status request id is invalid")
        receipt_path = self._receipt_path(request_id)
        if receipt_path.exists() and not receipt_path.is_symlink():
            receipt = self._read_receipt(receipt_path)
            return ControlStatusView(
                request_id=request_id,
                status=receipt.status,
                command=receipt.command,
                processed_at=receipt.processed_at,
                outcome=receipt.outcome,
            )
        request_path = self._request_path(request_id)
        if request_path.exists() and not request_path.is_symlink():
            return ControlStatusView(request_id=request_id, status=ControlStatus.QUEUED)
        return ControlStatusView(request_id=request_id, status=ControlStatus.UNKNOWN)

    def process_pending(
        self, handler: Callable[[ControlRequest], ControlExecution]
    ) -> ControlBatch:
        if not callable(handler):
            raise TypeError("control handler must be callable")
        now = _aware(self._clock(), label="control clock")
        receipts: list[ControlReceipt] = []
        pending: list[tuple[ControlRequest, Path]] = []
        try:
            entries = tuple(self._inbox.iterdir())
        except OSError as error:
            raise ControlChannelError("CONTROL_INBOX_UNREADABLE") from error
        for path in entries:
            loaded = self._load_path(path, now=now)
            if isinstance(loaded, ControlReceipt):
                receipts.append(loaded)
                continue
            if loaded is None:
                continue
            request, request_path = loaded
            receipt_path = self._receipt_path(request.request_id)
            if receipt_path.exists() and not receipt_path.is_symlink():
                existing = self._read_receipt(receipt_path)
                if existing.request_sha256 == request.payload_sha256:
                    request_path.unlink(missing_ok=True)
                    _fsync_directory(self._inbox)
                    continue
                receipts.append(
                    self._reject(
                        request_path,
                        reason="CONTROL_REQUEST_ID_COLLISION",
                        raw_hash=request.payload_sha256,
                    )
                )
                continue
            pending.append((request, request_path))
        pending.sort(
            key=lambda item: (
                _PRIORITY[item[0].command.value], item[0].created_at, item[0].request_id
            )
        )
        halted = False
        stop = False
        for request, path in pending:
            execution = handler(request)
            if not isinstance(execution, ControlExecution):
                raise TypeError("control handler must return ControlExecution")
            receipt = ControlReceipt(
                request_id=request.request_id,
                command=request.command,
                status=ControlStatus.COMPLETED,
                request_sha256=request.payload_sha256,
                processed_at=_aware(self._clock(), label="control clock"),
                outcome=execution.outcome,
            )
            self._write_receipt(receipt)
            path.unlink(missing_ok=True)
            _fsync_directory(self._inbox)
            receipts.append(receipt)
            halted = halted or execution.halted
            stop = stop or execution.stop
        return ControlBatch(receipts=tuple(receipts), halted=halted, stop=stop)

    def _load_path(
        self, path: Path, *, now: datetime
    ) -> tuple[ControlRequest, Path] | ControlReceipt | None:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise ControlChannelError("CONTROL_REQUEST_STAT_FAILED") from error
        if stat.S_ISLNK(metadata.st_mode):
            return self._reject(path, reason="CONTROL_REQUEST_SYMLINK", raw_hash=None)
        if not stat.S_ISREG(metadata.st_mode):
            return self._reject(path, reason="CONTROL_REQUEST_NOT_REGULAR", raw_hash=None)
        if os.name != "nt" and metadata.st_mode & 0o077:
            return self._reject(path, reason="CONTROL_REQUEST_NOT_PRIVATE", raw_hash=None)
        if metadata.st_size <= 0 or metadata.st_size > MAX_CONTROL_REQUEST_BYTES:
            return self._reject(path, reason="CONTROL_REQUEST_SIZE_INVALID", raw_hash=None)
        try:
            raw = path.read_bytes()
        except OSError as error:
            raise ControlChannelError("CONTROL_REQUEST_READ_FAILED") from error
        raw_hash = hashlib.sha256(raw).hexdigest()
        try:
            request = self._parse_request(_strict_json(raw), now=now)
            expected = self._request_path(request.request_id)
            if path.name != expected.name or path.parent != expected.parent:
                raise ControlRequestRejected("CONTROL_REQUEST_PATH_MISMATCH")
        except (ControlRequestRejected, TypeError, ValueError) as error:
            return self._reject(path, reason=str(error), raw_hash=raw_hash)
        return request, path

    def _parse_request(self, payload: Mapping[str, object], *, now: datetime) -> ControlRequest:
        keys = frozenset(payload)
        if not _REQUIRED_FIELDS.issubset(keys) or not keys.issubset(
            _REQUIRED_FIELDS | _OPTIONAL_FIELDS
        ):
            raise ControlRequestRejected("CONTROL_REQUEST_FIELDS_INVALID")
        try:
            request = ControlRequest(
                request_id=str(payload["request_id"]),
                command=ControlCommand(str(payload["command"])),
                created_at=datetime.fromisoformat(str(payload["created_at"])),
                expires_at=datetime.fromisoformat(str(payload["expires_at"])),
                host_hash=str(payload["host_hash"]),
                reason_sha256=(
                    None if "reason_sha256" not in payload else str(payload["reason_sha256"])
                ),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ControlRequestRejected("CONTROL_REQUEST_FIELDS_INVALID") from error
        if request.host_hash != self._host_hash:
            raise ControlRequestRejected("CONTROL_REQUEST_HOST_MISMATCH")
        if request.created_at > now:
            raise ControlRequestRejected("CONTROL_REQUEST_FROM_FUTURE")
        if request.expires_at <= now:
            raise ControlRequestRejected("CONTROL_REQUEST_EXPIRED")
        return request

    def _reject(self, path: Path, *, reason: str, raw_hash: str | None) -> ControlReceipt:
        stem = path.name[:-5] if path.name.endswith(".json") else ""
        request_id = stem if _REQUEST_ID.fullmatch(stem) is not None else None
        raw_identity = raw_hash or hashlib.sha256(f"{path.name}:{reason}".encode()).hexdigest()
        receipt_id = request_id or "invalid_" + hashlib.sha256(
            f"{path.name}:{raw_identity}".encode()
        ).hexdigest()
        receipt = ControlReceipt(
            request_id=receipt_id,
            command=None,
            status=ControlStatus.REJECTED,
            request_sha256=raw_identity,
            processed_at=_aware(self._clock(), label="control clock"),
            outcome={"reason": reason},
        )
        self._write_receipt(receipt)
        try:
            path.unlink(missing_ok=True)
            _fsync_directory(self._inbox)
        except OSError as error:
            raise ControlChannelError("CONTROL_REJECTED_REQUEST_CLEANUP_FAILED") from error
        return receipt

    def _write_receipt(self, receipt: ControlReceipt) -> None:
        path = self._receipt_path(receipt.request_id)
        if path.exists() and not path.is_symlink():
            existing = self._read_receipt(path)
            if existing.canonical_bytes() != receipt.canonical_bytes():
                raise ControlChannelError("CONTROL_RECEIPT_COLLISION")
            return
        if path.is_symlink():
            raise ControlChannelError("CONTROL_RECEIPT_SYMLINK")
        _atomic_write(self._receipts, path, receipt.canonical_bytes())

    def _read_receipt(self, path: Path) -> ControlReceipt:
        if path.is_symlink() or not path.is_file():
            raise ControlChannelError("CONTROL_RECEIPT_NOT_REGULAR")
        raw = path.read_bytes()
        if len(raw) > MAX_CONTROL_REQUEST_BYTES * 2:
            raise ControlChannelError("CONTROL_RECEIPT_TOO_LARGE")
        payload = _strict_json(raw)
        expected = {
            "command", "outcome", "processed_at", "request_id", "request_sha256", "schema", "status"
        }
        if set(payload) != expected or payload.get("schema") != "firmquant.control-receipt.v1":
            raise ControlChannelError("CONTROL_RECEIPT_INVALID")
        outcome = payload["outcome"]
        if not isinstance(outcome, dict):
            raise ControlChannelError("CONTROL_RECEIPT_OUTCOME_INVALID")
        raw_command = payload["command"]
        return ControlReceipt(
            request_id=str(payload["request_id"]),
            command=None if raw_command is None else ControlCommand(str(raw_command)),
            status=ControlStatus(str(payload["status"])),
            request_sha256=str(payload["request_sha256"]),
            processed_at=datetime.fromisoformat(str(payload["processed_at"])),
            outcome=cast(dict[str, object], outcome),
        )

    def _request_path(self, request_id: str) -> Path:
        if _REQUEST_ID.fullmatch(request_id) is None:
            raise ValueError("control request id is invalid")
        return self._inbox / f"{request_id}.json"

    def _receipt_path(self, request_id: str) -> Path:
        if _RECEIPT_ID.fullmatch(request_id) is None:
            raise ValueError("control receipt id is invalid")
        return self._receipts / f"{request_id}.json"


__all__ = (
    "MAX_CONTROL_REQUEST_BYTES",
    "ControlBatch",
    "ControlChannelError",
    "ControlCommand",
    "ControlExecution",
    "ControlInbox",
    "ControlReceipt",
    "ControlRequest",
    "ControlRequestRejected",
    "ControlStatus",
    "ControlStatusView",
    "local_host_hash",
)
