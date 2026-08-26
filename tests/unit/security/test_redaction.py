from __future__ import annotations

from pathlib import Path

from firmquant.application.operations import OperatorResult
from firmquant.security.redaction import REDACTED, redact


def test_recursive_redaction_hides_accounts_secrets_and_local_paths() -> None:
    payload = {
        "account_id": "sensitive-account-123",
        "nested": {
            "password": "broker-password",
            "session_token": "token-value",
            "userdata_path": Path("C:/MiniQMT/userdata_mini"),
            "endpoint": "https://user:password@example.invalid/hook",
        },
        "orders": [
            {
                "broker_order_id": "broker-order-1",
                "symbol": "600519.SH",
            }
        ],
    }

    observed = redact(payload)

    assert observed["account_id"] == REDACTED
    assert observed["nested"] == {
        "password": REDACTED,
        "session_token": REDACTED,
        "userdata_path": REDACTED,
        "endpoint": REDACTED,
    }
    assert observed["orders"] == [{"broker_order_id": "broker-order-1", "symbol": "600519.SH"}]


def test_operator_result_applies_redaction_before_any_cli_renderer() -> None:
    result = OperatorResult(
        message="完成",
        payload={
            "account_alias": "production-cash-account",
            "report_path": "/srv/firmquant/private/report.json",
            "decision_id": "decision-safe",
        },
    )

    assert result.payload == {
        "account_alias": REDACTED,
        "report_path": REDACTED,
        "decision_id": "decision-safe",
    }
