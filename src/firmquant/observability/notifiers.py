"""Failure-isolated console, file, and optional HTTPS alert delivery."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, TextIO, runtime_checkable
from urllib.parse import urlsplit

from firmquant.persistence.audit import AuditLedger
from firmquant.persistence.database import Database
from firmquant.security.redaction import redact
from firmquant.security.secrets import SecretProvider

_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")


class AlertSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


def _aware(value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("alert time must be timezone-aware")


def _safe_mapping(payload: Mapping[str, object]) -> MappingProxyType[str, object]:
    protected = redact(dict(payload))
    if not isinstance(protected, dict):
        raise TypeError("redacted alert payload must be an object")
    encoded = json.dumps(
        protected,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    decoded: object = json.loads(encoded)
    if not isinstance(decoded, dict):
        raise TypeError("alert payload must remain an object")
    return MappingProxyType(decoded)


@dataclass(frozen=True, slots=True)
class Alert:
    alert_id: str
    severity: AlertSeverity
    code: str
    payload: Mapping[str, object]
    created_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.alert_id, str) or not self.alert_id.startswith("alert_"):
            raise ValueError("alert id must be canonical")
        if not isinstance(self.severity, AlertSeverity):
            raise TypeError("alert severity must be typed")
        if not isinstance(self.code, str) or _CODE.fullmatch(self.code) is None:
            raise ValueError("alert code must be canonical")
        _aware(self.created_at)
        object.__setattr__(self, "payload", _safe_mapping(self.payload))

    @classmethod
    def create(
        cls,
        *,
        severity: AlertSeverity,
        code: str,
        payload: Mapping[str, object],
        created_at: datetime,
    ) -> Alert:
        protected = _safe_mapping(payload)
        identity = json.dumps(
            {
                "severity": severity.value,
                "code": code,
                "payload": dict(protected),
                "created_at": created_at.isoformat(),
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return cls(
            alert_id="alert_" + hashlib.sha256(identity.encode("utf-8")).hexdigest(),
            severity=severity,
            code=code,
            payload=protected,
            created_at=created_at,
        )

    def canonical_json(self) -> str:
        return json.dumps(
            {
                "alert_id": self.alert_id,
                "severity": self.severity.value,
                "code": self.code,
                "payload": dict(self.payload),
                "created_at": self.created_at.isoformat(),
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )


@runtime_checkable
class Notifier(Protocol):
    def notify(self, alert: Alert) -> None: ...


class AlertStore:
    """Idempotently persist OPEN alerts and tamper-evident audit evidence."""

    def __init__(self, database: Database) -> None:
        if not isinstance(database, Database):
            raise TypeError("alert store requires Database")
        self._database = database

    def persist(self, alert: Alert) -> None:
        if not isinstance(alert, Alert):
            raise TypeError("alert store requires Alert")
        payload_json = json.dumps(
            dict(alert.payload),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()

        def write() -> None:
            existing = self._database.query_one(
                "SELECT severity, code, payload_json, payload_sha256, created_at "
                "FROM alerts WHERE alert_id = ?",
                (alert.alert_id,),
            )
            stable = (
                alert.severity.value,
                alert.code,
                payload_json,
                payload_sha256,
                alert.created_at.isoformat(),
            )
            if existing is not None:
                if tuple(existing) != stable:
                    raise RuntimeError("alert identity collision")
                return
            self._database.write(
                """
                INSERT INTO alerts(
                    alert_id, severity, code, status, payload_json, payload_sha256,
                    created_at, acknowledged_at, resolved_at
                ) VALUES (?, ?, ?, 'OPEN', ?, ?, ?, NULL, NULL)
                """,
                (alert.alert_id, *stable),
            )
            AuditLedger(self._database).append(
                audit_event_id="alert:" + alert.alert_id,
                category="ALERT",
                actor="notifier-fanout",
                payload={
                    "schema": "firmquant.alert.v1",
                    "alert_id": alert.alert_id,
                    "severity": alert.severity,
                    "code": alert.code,
                    "payload_sha256": payload_sha256,
                },
                created_at=alert.created_at,
            )

        if self._database.in_transaction:
            write()
        else:
            with self._database.transaction():
                write()


@dataclass(frozen=True, slots=True)
class NotificationReceipt:
    alert_id: str
    delivered_count: int
    failed_count: int
    failure_alert_ids: tuple[str, ...]


class NotifierFanout:
    """Deliver independently; a broken optional channel never escapes to trading code."""

    def __init__(self, *, notifiers: tuple[Notifier, ...], alert_store: AlertStore) -> None:
        if not isinstance(notifiers, tuple) or any(
            not isinstance(notifier, Notifier) for notifier in notifiers
        ):
            raise TypeError("notifier fanout requires typed notifier tuple")
        if not isinstance(alert_store, AlertStore):
            raise TypeError("notifier fanout requires AlertStore")
        self._notifiers = notifiers
        self._store = alert_store

    def publish(self, alert: Alert) -> NotificationReceipt:
        self._store.persist(alert)
        delivered = 0
        failures: list[str] = []
        for notifier in self._notifiers:
            try:
                notifier.notify(alert)
            except Exception as error:
                failure = Alert.create(
                    severity=AlertSeverity.WARNING,
                    code="NOTIFIER_DELIVERY_FAILED",
                    payload={
                        "source_alert_id": alert.alert_id,
                        "notifier_type": type(notifier).__name__,
                        "error_type": type(error).__name__,
                    },
                    created_at=alert.created_at,
                )
                with suppress(Exception):
                    self._store.persist(failure)
                failures.append(failure.alert_id)
            else:
                delivered += 1
        return NotificationReceipt(
            alert_id=alert.alert_id,
            delivered_count=delivered,
            failed_count=len(failures),
            failure_alert_ids=tuple(failures),
        )


class ConsoleNotifier:
    def __init__(self, stream: TextIO) -> None:
        if not hasattr(stream, "write"):
            raise TypeError("console notifier stream must be writable")
        self._stream = stream

    def notify(self, alert: Alert) -> None:
        self._stream.write(alert.canonical_json() + "\n")
        self._stream.flush()


class FileNotifier:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    def notify(self, alert: Alert) -> None:
        if self._path.is_symlink() or not self._path.parent.is_dir():
            raise RuntimeError("file notifier path is unavailable")
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self._path, flags, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8", newline="\n") as stream:
            stream.write(alert.canonical_json() + "\n")
            stream.flush()
            os.fsync(stream.fileno())


type WebhookSender = Callable[[str, bytes, Mapping[str, str], float], None]


def _validate_webhook_endpoint(endpoint: str) -> None:
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError as error:
        raise ValueError("webhook endpoint must be credential-free HTTPS") from error
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(ord(character) < 33 or ord(character) == 127 for character in endpoint)
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ValueError("webhook endpoint must be credential-free HTTPS")


def _send_https(
    endpoint: str,
    body: bytes,
    headers: Mapping[str, str],
    timeout_seconds: float,
) -> None:
    _validate_webhook_endpoint(endpoint)
    parsed = urlsplit(endpoint)
    hostname = parsed.hostname
    if hostname is None:
        raise RuntimeError("validated webhook endpoint lost its hostname")
    target = parsed.path or "/"
    connection = http.client.HTTPSConnection(
        hostname,
        parsed.port,
        timeout=timeout_seconds,
    )
    try:
        connection.request("POST", target, body=body, headers=dict(headers))
        response = connection.getresponse()
        response.read(4096)
        if not 200 <= response.status < 300:
            raise RuntimeError("webhook returned a non-success status")
    finally:
        connection.close()


class WebhookNotifier:
    __slots__ = ("_endpoint", "_secret_provider", "_sender", "_timeout_seconds")

    def __init__(
        self,
        *,
        endpoint: str,
        secret_provider: SecretProvider,
        sender: WebhookSender = _send_https,
        timeout_seconds: float = 5.0,
    ) -> None:
        if not isinstance(endpoint, str) or not endpoint:
            raise TypeError("webhook endpoint must be text")
        _validate_webhook_endpoint(endpoint)
        if not isinstance(secret_provider, SecretProvider):
            raise TypeError("webhook secret provider is invalid")
        if not callable(sender):
            raise TypeError("webhook sender must be callable")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise TypeError("webhook timeout must be numeric")
        if timeout_seconds <= 0 or timeout_seconds > 30:
            raise ValueError("webhook timeout must be between zero and thirty seconds")
        self._endpoint = endpoint
        self._secret_provider = secret_provider
        self._sender = sender
        self._timeout_seconds = float(timeout_seconds)

    def notify(self, alert: Alert) -> None:
        token = self._secret_provider.get_secret("WEBHOOK_AUTH_TOKEN").copy_bytes()
        try:
            encoded_token = token.decode("ascii")
        except UnicodeDecodeError as error:
            raise RuntimeError("webhook authentication secret is invalid") from error
        if any(character in encoded_token for character in "\r\n"):
            raise RuntimeError("webhook authentication secret is invalid")
        self._sender(
            self._endpoint,
            alert.canonical_json().encode("utf-8"),
            {
                "Authorization": "Bearer " + encoded_token,
                "Content-Type": "application/json; charset=utf-8",
            },
            self._timeout_seconds,
        )

    def __repr__(self) -> str:
        return "<WebhookNotifier redacted>"


__all__ = (
    "Alert",
    "AlertSeverity",
    "AlertStore",
    "ConsoleNotifier",
    "FileNotifier",
    "NotificationReceipt",
    "Notifier",
    "NotifierFanout",
    "WebhookNotifier",
)
