"""Structured and human-readable logging with one redaction boundary."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from types import MappingProxyType
from typing import TextIO

from firmquant.security.redaction import redact

_EVENT = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_CONTEXT_FIELDS = (
    "session",
    "correlation_id",
    "decision_id",
    "execution_id",
    "uquant_order_id",
    "broker_order_id",
    "symbol",
)


def _canonical_text(value: str | None, *, label: str) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 256
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{label} must be canonical text")
    return value


@dataclass(frozen=True, slots=True)
class EventContext:
    """Stable correlation fields attached to every structured event."""

    session: date | None = None
    correlation_id: str | None = None
    decision_id: str | None = None
    execution_id: str | None = None
    uquant_order_id: str | None = None
    broker_order_id: str | None = None
    symbol: str | None = None

    def __post_init__(self) -> None:
        if self.session is not None and type(self.session) is not date:
            raise TypeError("event session must be a date")
        for field in _CONTEXT_FIELDS[1:]:
            object.__setattr__(
                self,
                field,
                _canonical_text(getattr(self, field), label=f"event {field}"),
            )

    def as_mapping(self) -> MappingProxyType[str, str | None]:
        return MappingProxyType(
            {
                "session": None if self.session is None else self.session.isoformat(),
                "correlation_id": self.correlation_id,
                "decision_id": self.decision_id,
                "execution_id": self.execution_id,
                "uquant_order_id": self.uquant_order_id,
                "broker_order_id": self.broker_order_id,
                "symbol": self.symbol,
            }
        )


class _EventFormatter(logging.Formatter):
    @staticmethod
    def _payload(record: logging.LogRecord) -> dict[str, object]:
        raw_context = getattr(record, "firmquant_context", EventContext())
        context = raw_context if isinstance(raw_context, EventContext) else EventContext()
        raw_payload = getattr(record, "firmquant_payload", {})
        protected = redact(raw_payload)
        if not isinstance(protected, dict):
            protected = {"value": protected}
        raw_event = getattr(record, "firmquant_event", "LOG_EVENT")
        event = raw_event if isinstance(raw_event, str) and _EVENT.fullmatch(raw_event) else "LOG_EVENT"
        raw_message = redact(record.getMessage())
        message = raw_message if isinstance(raw_message, str) else "<redacted>"
        return {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            **dict(context.as_mapping()),
            "severity": record.levelname,
            "event": event,
            "message": message,
            "payload": protected,
        }


class JsonEventFormatter(_EventFormatter):
    """Canonical one-line JSON event formatter."""

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            self._payload(record),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )


class ConsoleEventFormatter(_EventFormatter):
    """Concise console formatter that omits verbose payloads."""

    def format(self, record: logging.LogRecord) -> str:
        payload = self._payload(record)
        correlation = payload["correlation_id"]
        suffix = "" if correlation is None else f" correlation={correlation}"
        return f"{payload['timestamp']} {payload['severity']} {payload['event']} {payload['message']}{suffix}"


def configure_logging(
    *,
    logger_name: str = "firmquant",
    json_stream: TextIO | None,
    console_stream: TextIO | None,
    level: int = logging.INFO,
) -> logging.Logger:
    """Configure an isolated logger without opening a network management surface."""

    _canonical_text(logger_name, label="logger name")
    if json_stream is None and console_stream is None:
        raise ValueError("at least one logging stream is required")
    if isinstance(level, bool) or not isinstance(level, int):
        raise TypeError("logging level must be an integer")
    logger = logging.getLogger(logger_name)
    for handler in tuple(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logger.setLevel(level)
    logger.propagate = False
    if json_stream is not None:
        json_handler = logging.StreamHandler(json_stream)
        json_handler.setFormatter(JsonEventFormatter())
        logger.addHandler(json_handler)
    if console_stream is not None:
        console_handler = logging.StreamHandler(console_stream)
        console_handler.setFormatter(ConsoleEventFormatter())
        logger.addHandler(console_handler)
    return logger


def log_event(
    logger: logging.Logger,
    *,
    level: int,
    event: str,
    message: str,
    context: EventContext | None = None,
    payload: dict[str, object] | None = None,
) -> None:
    """Emit one typed event; arbitrary exception text is deliberately not accepted."""

    if not isinstance(logger, logging.Logger):
        raise TypeError("event logger must be logging.Logger")
    if isinstance(level, bool) or not isinstance(level, int):
        raise TypeError("event logging level must be integer")
    if not isinstance(event, str) or _EVENT.fullmatch(event) is None:
        raise ValueError("event code must be canonical")
    canonical_message = _canonical_text(message, label="event message")
    if canonical_message is None:
        raise ValueError("event message is required")
    observed_context = EventContext() if context is None else context
    if not isinstance(observed_context, EventContext):
        raise TypeError("event context must be typed")
    observed_payload = {} if payload is None else payload
    if not isinstance(observed_payload, dict):
        raise TypeError("event payload must be a dictionary")
    logger.log(
        level,
        canonical_message,
        extra={
            "firmquant_event": event,
            "firmquant_context": observed_context,
            "firmquant_payload": observed_payload,
        },
    )


__all__ = (
    "ConsoleEventFormatter",
    "EventContext",
    "JsonEventFormatter",
    "configure_logging",
    "log_event",
)
