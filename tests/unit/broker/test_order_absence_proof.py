from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from firmquant.broker.gateway import BrokerOrderAbsenceProof, BrokerOrderCommand
from firmquant.domain.broker_facts import PriceType, Side
from firmquant.domain.errors import DomainTypeError, DomainValidationError
from firmquant.domain.values import Price, Shares, Symbol

NOW = datetime(2026, 8, 25, 9, 31, tzinfo=UTC)
SESSION = date(2026, 8, 25)


def command() -> BrokerOrderCommand:
    return BrokerOrderCommand(
        execution_id="exec_" + "1" * 64,
        idempotency_key="2" * 64,
        client_order_id="O-ABSENCE-PROOF",
        symbol=Symbol.parse("600519.SH"),
        side=Side.BUY,
        price_type=PriceType.LIMIT,
        requested_shares=Shares(100),
        limit_price=Price(Decimal("10.10")),
        strategy_session=SESSION,
    )


def proof(**overrides: object) -> BrokerOrderAbsenceProof:
    values: dict[str, object] = {
        "command": command(),
        "snapshot_id": "absence-proof-snapshot",
        "session_date": SESSION,
        "captured_at": NOW,
        "broker_event_watermark": 10,
        "evidence_sha256": "a" * 64,
    }
    values.update(overrides)
    return BrokerOrderAbsenceProof(**values)  # type: ignore[arg-type]


def test_valid_absence_proof_binds_exact_command_session_and_evidence() -> None:
    observed = proof()

    assert observed.command == command()
    assert observed.session_date == SESSION
    assert observed.broker_event_watermark == 10


def test_absence_proof_requires_typed_command() -> None:
    with pytest.raises(DomainTypeError, match="command"):
        proof(command=object())


def test_absence_proof_requires_canonical_snapshot_id() -> None:
    with pytest.raises(DomainValidationError, match="snapshot id"):
        proof(snapshot_id=" bad ")


def test_absence_proof_requires_date_session() -> None:
    with pytest.raises(DomainTypeError, match="session date"):
        proof(session_date=NOW)


def test_absence_proof_session_must_match_durable_command() -> None:
    other_session = date(2026, 8, 26)
    with pytest.raises(DomainValidationError, match="differs from durable command"):
        proof(session_date=other_session)


def test_absence_proof_requires_aware_capture_time() -> None:
    with pytest.raises(DomainValidationError, match="timezone-aware"):
        proof(captured_at=NOW.replace(tzinfo=None))


@pytest.mark.parametrize("watermark", [True, "10"])
def test_absence_proof_requires_integer_watermark(watermark: object) -> None:
    with pytest.raises(DomainTypeError, match="watermark must be integer"):
        proof(broker_event_watermark=watermark)


def test_absence_proof_rejects_negative_watermark() -> None:
    with pytest.raises(DomainValidationError, match="watermark must be nonnegative"):
        proof(broker_event_watermark=-1)


@pytest.mark.parametrize("evidence", ["", "A" * 64, "a" * 63])
def test_absence_proof_requires_canonical_sha256(evidence: str) -> None:
    with pytest.raises(DomainValidationError, match="evidence must be SHA-256"):
        proof(evidence_sha256=evidence)


def test_proof_command_identity_changes_are_observable() -> None:
    original = command()
    changed = replace(original, client_order_id="O-ABSENCE-PROOF-DIFFERENT")

    assert proof(command=original).command != proof(command=changed).command
