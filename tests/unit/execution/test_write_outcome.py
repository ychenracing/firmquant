from __future__ import annotations

from firmquant.broker.gateway import BrokerWriteForbidden
from firmquant.execution.write_outcome import (
    BrokerWriteNotAccepted,
    BrokerWriteOutcomeUnknown,
    WriteFailureClass,
    classify_write_failure,
)
from firmquant.risk.capability import WriteCapabilityDenied


def test_pre_write_authorization_denial_is_not_an_unknown_broker_outcome() -> None:
    assert classify_write_failure(WriteCapabilityDenied(("QUOTE_STALE",))) is WriteFailureClass.NOT_ACCEPTED
    assert (
        classify_write_failure(BrokerWriteForbidden("capability required")) is WriteFailureClass.NOT_ACCEPTED
    )


def test_explicit_broker_negative_result_is_not_accepted_but_transport_failure_is_unknown() -> None:
    assert (
        classify_write_failure(BrokerWriteNotAccepted("broker returned -1")) is WriteFailureClass.NOT_ACCEPTED
    )
    assert (
        classify_write_failure(BrokerWriteOutcomeUnknown("transport interrupted"))
        is WriteFailureClass.OUTCOME_UNKNOWN
    )
