from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import ClassVar

import pytest

import firmquant.observability.notifiers as notifier_module
from firmquant.observability.notifiers import (
    Alert,
    AlertSeverity,
    AlertStore,
    ConsoleNotifier,
    FileNotifier,
    NotifierFanout,
    WebhookNotifier,
)
from firmquant.persistence.database import Database
from firmquant.security.secrets import SecretBytes
from tests.unit.observability.test_notifiers import NOW

TEST_ACCOUNT_IDENTIFIER = "".join(("1234", "5678", "9012"))


class StaticSecrets:
    def __init__(self, token: bytes) -> None:
        self._token = token

    def get_secret(self, name: str) -> SecretBytes:
        assert name == "WEBHOOK_AUTH_TOKEN"
        return SecretBytes(self._token)


def _alert() -> Alert:
    return Alert.create(
        severity=AlertSeverity.CRITICAL,
        code="KILL_SWITCH_TRIGGERED",
        payload={"reason": "operator"},
        created_at=NOW,
    )


@pytest.fixture
def database(tmp_path: Path):
    opened = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        yield opened
    finally:
        opened.close()


@pytest.mark.parametrize(
    ("changes", "error", "message"),
    [
        ({"alert_id": None}, ValueError, "alert id"),
        ({"alert_id": "event_123"}, ValueError, "alert id"),
        ({"severity": "CRITICAL"}, TypeError, "severity"),
        ({"code": None}, ValueError, "code"),
        ({"code": "lowercase"}, ValueError, "code"),
        ({"code": "_INVALID"}, ValueError, "code"),
        ({"code": "A" * 129}, ValueError, "code"),
        ({"created_at": "2026-08-26"}, ValueError, "timezone-aware"),
        ({"created_at": datetime(2026, 8, 26, 8)}, ValueError, "timezone-aware"),
    ],
)
def test_alert_rejects_untyped_or_noncanonical_envelope(
    changes: dict[str, object], error: type[Exception], message: str
) -> None:
    values: dict[str, object] = {
        "alert_id": "alert_" + "a" * 64,
        "severity": AlertSeverity.CRITICAL,
        "code": "KILL_SWITCH_TRIGGERED",
        "payload": {},
        "created_at": NOW,
    }
    values.update(changes)
    with pytest.raises(error, match=message):
        Alert(**values)  # type: ignore[arg-type]


def test_alert_payload_is_redacted_finite_immutable_and_deterministic() -> None:
    payload = {"account_number": TEST_ACCOUNT_IDENTIFIER, "value": 1}
    first = Alert.create(
        severity=AlertSeverity.WARNING,
        code="ACCOUNT_MISMATCH",
        payload=payload,
        created_at=NOW,
    )
    payload["value"] = 2
    repeated = Alert.create(
        severity=AlertSeverity.WARNING,
        code="ACCOUNT_MISMATCH",
        payload={"value": 1, "account_number": TEST_ACCOUNT_IDENTIFIER},
        created_at=NOW,
    )

    assert first.alert_id == repeated.alert_id
    assert isinstance(first.payload, MappingProxyType)
    assert first.payload == {"account_number": "<redacted>", "value": 1}
    with pytest.raises(TypeError):
        first.payload["value"] = 3  # type: ignore[index]
    with pytest.raises(ValueError, match="JSON compliant"):
        Alert.create(
            severity=AlertSeverity.WARNING,
            code="NONFINITE_PAYLOAD",
            payload={"value": float("nan")},
            created_at=NOW,
        )


def test_alert_rejects_redactor_output_that_is_not_an_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(notifier_module, "redact", lambda payload: [payload])
    with pytest.raises(TypeError, match="must be an object"):
        _alert()


def test_alert_store_requires_typed_inputs(database: Database) -> None:
    with pytest.raises(TypeError, match="requires Database"):
        AlertStore(object())  # type: ignore[arg-type]
    store = AlertStore(database)
    with pytest.raises(TypeError, match="requires Alert"):
        store.persist(object())  # type: ignore[arg-type]


def test_alert_store_is_idempotent_and_detects_identity_collision(database: Database) -> None:
    store = AlertStore(database)
    alert = _alert()
    store.persist(alert)
    store.persist(alert)
    assert database.scalar("SELECT count(*) FROM alerts") == 1
    assert database.scalar("SELECT count(*) FROM audit_events") == 1

    with pytest.raises(RuntimeError, match="identity collision"):
        store.persist(replace(alert, code="ACCOUNT_MISMATCH"))


def test_alert_store_joins_existing_transaction(database: Database) -> None:
    store = AlertStore(database)
    with database.transaction():
        store.persist(_alert())
        assert database.scalar("SELECT count(*) FROM alerts") == 1
    assert database.scalar("SELECT count(*) FROM audit_events") == 1


@pytest.mark.parametrize("notifiers", [[], (object(),)])
def test_notifier_fanout_requires_typed_tuple(database: Database, notifiers: object) -> None:
    with pytest.raises(TypeError, match="typed notifier tuple"):
        NotifierFanout(notifiers=notifiers, alert_store=AlertStore(database))  # type: ignore[arg-type]


def test_notifier_fanout_requires_alert_store() -> None:
    with pytest.raises(TypeError, match="AlertStore"):
        NotifierFanout(notifiers=(), alert_store=object())  # type: ignore[arg-type]


def test_empty_notifier_fanout_still_persists_alert(database: Database) -> None:
    alert = _alert()
    receipt = NotifierFanout(notifiers=(), alert_store=AlertStore(database)).publish(alert)
    assert receipt.alert_id == alert.alert_id
    assert receipt.delivered_count == 0
    assert receipt.failed_count == 0
    assert receipt.failure_alert_ids == ()
    assert database.scalar("SELECT count(*) FROM alerts") == 1


def test_console_and_file_notifiers_reject_unavailable_sinks(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="stream must be writable"):
        ConsoleNotifier(object())  # type: ignore[arg-type]

    missing_parent = tmp_path / "missing" / "alerts.jsonl"
    with pytest.raises(RuntimeError, match="path is unavailable"):
        FileNotifier(missing_parent).notify(_alert())

    target = tmp_path / "target.jsonl"
    target.write_text("", encoding="utf-8")
    link = tmp_path / "alerts.jsonl"
    link.symlink_to(target)
    with pytest.raises(RuntimeError, match="path is unavailable"):
        FileNotifier(link).notify(_alert())


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://alerts.example.invalid/hook",
        "https:///missing-host",
        "https://user@alerts.example.invalid/hook",
        "https://user:password@alerts.example.invalid/hook",
        "https://alerts.example.invalid/hook?token=x",
        "https://alerts.example.invalid/hook#fragment",
        "https://alerts.example.invalid/path here",
        "https://alerts.example.invalid:70000/hook",
        "https://[invalid/hook",
    ],
)
def test_webhook_endpoint_rejects_credential_or_ambiguous_authority(endpoint: str) -> None:
    with pytest.raises(ValueError, match="credential-free HTTPS"):
        WebhookNotifier(endpoint=endpoint, secret_provider=StaticSecrets(b"token"))


@pytest.mark.parametrize(
    ("changes", "error", "message"),
    [
        ({"endpoint": None}, TypeError, "endpoint must be text"),
        ({"endpoint": ""}, TypeError, "endpoint must be text"),
        ({"secret_provider": object()}, TypeError, "secret provider"),
        ({"sender": None}, TypeError, "sender must be callable"),
        ({"timeout_seconds": True}, TypeError, "timeout must be numeric"),
        ({"timeout_seconds": "5"}, TypeError, "timeout must be numeric"),
        ({"timeout_seconds": 0}, ValueError, "between zero and thirty"),
        ({"timeout_seconds": 31}, ValueError, "between zero and thirty"),
    ],
)
def test_webhook_notifier_rejects_invalid_dependencies_and_timeout(
    changes: dict[str, object], error: type[Exception], message: str
) -> None:
    values: dict[str, object] = {
        "endpoint": "https://alerts.example.invalid/hook",
        "secret_provider": StaticSecrets(b"token"),
        "sender": lambda endpoint, body, headers, timeout: None,
        "timeout_seconds": 5,
    }
    values.update(changes)
    with pytest.raises(error, match=message):
        WebhookNotifier(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("token", [b"\xff", b"token\rheader", b"token\nheader"])
def test_webhook_notifier_rejects_non_ascii_or_header_injection_token(token: bytes) -> None:
    notifier = WebhookNotifier(
        endpoint="https://alerts.example.invalid/hook",
        secret_provider=StaticSecrets(token),
        sender=lambda endpoint, body, headers, timeout: None,
    )
    with pytest.raises(RuntimeError, match="authentication secret is invalid"):
        notifier.notify(_alert())


class FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status
        self.read_limit: int | None = None

    def read(self, limit: int) -> bytes:
        self.read_limit = limit
        return b"response"


class FakeHttpsConnection:
    instances: ClassVar[list[FakeHttpsConnection]] = []
    response_status: ClassVar[int] = 204

    def __init__(self, host: str, port: int | None, *, timeout: float) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.requests: list[tuple[str, str, bytes, dict[str, str]]] = []
        self.response = FakeResponse(self.response_status)
        self.closed = False
        self.instances.append(self)

    def request(self, method: str, target: str, *, body: bytes, headers: dict[str, str]) -> None:
        self.requests.append((method, target, body, headers))

    def getresponse(self) -> FakeResponse:
        return self.response

    def close(self) -> None:
        self.closed = True


def test_https_sender_uses_only_validated_target_and_closes_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeHttpsConnection.instances.clear()
    FakeHttpsConnection.response_status = 204
    monkeypatch.setattr(notifier_module.http.client, "HTTPSConnection", FakeHttpsConnection)

    notifier_module._send_https(
        "https://alerts.example.invalid:8443",
        b"{}",
        {"Content-Type": "application/json"},
        3.0,
    )

    connection = FakeHttpsConnection.instances[-1]
    assert (connection.host, connection.port, connection.timeout) == (
        "alerts.example.invalid",
        8443,
        3.0,
    )
    assert connection.requests == [("POST", "/", b"{}", {"Content-Type": "application/json"})]
    assert connection.response.read_limit == 4096
    assert connection.closed is True


def test_https_sender_closes_connection_on_non_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeHttpsConnection.instances.clear()
    FakeHttpsConnection.response_status = 503
    monkeypatch.setattr(notifier_module.http.client, "HTTPSConnection", FakeHttpsConnection)

    with pytest.raises(RuntimeError, match="non-success status"):
        notifier_module._send_https(
            "https://alerts.example.invalid/hook",
            b"{}",
            {},
            1.0,
        )
    assert FakeHttpsConnection.instances[-1].closed is True
