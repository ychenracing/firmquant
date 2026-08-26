from __future__ import annotations

import io
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest

from firmquant.observability.notifiers import (
    Alert,
    AlertSeverity,
    ConsoleNotifier,
    FileNotifier,
    WebhookNotifier,
)
from firmquant.security.secrets import SecretBytes

NOW = datetime(2026, 8, 26, 8, tzinfo=UTC)
ACCOUNT_NUMBER = "".join(("1234", "5678", "9012"))


class StaticWebhookSecrets:
    def __init__(self, token: bytes) -> None:
        self._token = token

    def get_secret(self, name: str) -> SecretBytes:
        assert name == "WEBHOOK_AUTH_TOKEN"
        return SecretBytes(self._token)


def alert() -> Alert:
    return Alert.create(
        severity=AlertSeverity.CRITICAL,
        code="ACCOUNT_RECONCILIATION_MISMATCH",
        payload={
            "account_number": ACCOUNT_NUMBER,
            "reason_code": "POSITION_MISMATCH",
        },
        created_at=NOW,
    )


def test_console_and_file_notifiers_emit_the_same_redacted_canonical_alert(
    tmp_path: Path,
) -> None:
    stream = io.StringIO()
    path = tmp_path / "alerts.jsonl"
    observed = alert()

    ConsoleNotifier(stream).notify(observed)
    FileNotifier(path).notify(observed)

    console_payload = json.loads(stream.getvalue())
    file_payload = json.loads(path.read_text(encoding="utf-8"))
    assert console_payload == file_payload
    assert console_payload["payload"] == {
        "account_number": "<redacted>",
        "reason_code": "POSITION_MISMATCH",
    }
    assert ACCOUNT_NUMBER not in stream.getvalue()
    assert ACCOUNT_NUMBER not in path.read_text(encoding="utf-8")


def test_webhook_requires_https_and_keeps_configuration_out_of_repr() -> None:
    token = b"webhook-" + b"x" * 32
    secrets = StaticWebhookSecrets(token)
    sent: list[tuple[str, bytes, Mapping[str, str], float]] = []

    def sender(
        endpoint: str,
        body: bytes,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> None:
        sent.append((endpoint, body, headers, timeout_seconds))

    endpoint = "https://alerts.example.invalid/firmquant"
    notifier = WebhookNotifier(
        endpoint=endpoint,
        secret_provider=secrets,
        sender=sender,
        timeout_seconds=3,
    )
    notifier.notify(alert())

    assert len(sent) == 1
    assert sent[0][0] == endpoint
    assert json.loads(sent[0][1])["code"] == "ACCOUNT_RECONCILIATION_MISMATCH"
    assert sent[0][2]["Authorization"] == "Bearer " + token.decode("ascii")
    assert sent[0][3] == 3.0
    assert endpoint not in repr(notifier)
    assert token.decode("ascii") not in repr(notifier)

    with pytest.raises(ValueError, match="credential-free HTTPS"):
        WebhookNotifier(
            endpoint="http://alerts.example.invalid/firmquant",
            secret_provider=secrets,
            sender=sender,
        )
