"""Classify broker-write failures by whether broker acceptance is still possible."""

from __future__ import annotations

from enum import StrEnum

from firmquant.broker.gateway import BrokerWriteForbidden
from firmquant.risk.capability import WriteCapabilityDenied


class WriteFailureClass(StrEnum):
    NOT_ACCEPTED = "NOT_ACCEPTED"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"


class BrokerWriteNotAccepted(RuntimeError):
    """The broker explicitly proved that a write request was not accepted."""


class BrokerWriteOutcomeUnknown(RuntimeError):
    """A write crossed the broker boundary but its final acceptance/result is unproven."""


def classify_write_failure(error: BaseException) -> WriteFailureClass:
    """Return the only safe retry classification for a failed broker write."""

    if isinstance(
        error,
        (
            WriteCapabilityDenied,
            BrokerWriteForbidden,
            BrokerWriteNotAccepted,
        ),
    ):
        return WriteFailureClass.NOT_ACCEPTED
    return WriteFailureClass.OUTCOME_UNKNOWN


__all__ = (
    "BrokerWriteNotAccepted",
    "BrokerWriteOutcomeUnknown",
    "WriteFailureClass",
    "classify_write_failure",
)
