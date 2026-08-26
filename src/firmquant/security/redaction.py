"""Central recursive redaction for operator, log, report, and notifier boundaries."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

REDACTED: Final = "<redacted>"
_MAX_DEPTH: Final = 32
_SENSITIVE_KEY_FRAGMENTS: Final = (
    "account_id",
    "account_alias",
    "authorization",
    "cookie",
    "credential",
    "password",
    "passwd",
    "private_key",
    "secret",
    "session_token",
    "webhook_url",
)
_TOKEN_VALUE = re.compile(
    r"(?:gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,}|Bearer\s+\S+)",
    re.IGNORECASE,
)
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")


def _sensitive_key(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    return (
        any(fragment in normalized for fragment in _SENSITIVE_KEY_FRAGMENTS)
        or normalized.endswith("_path")
        or normalized.endswith("_directory")
        or normalized.endswith("_token")
        or normalized.endswith("_api_key")
    )


def _sensitive_text(value: str) -> bool:
    if value.startswith(("/", "\\\\")) or _WINDOWS_ABSOLUTE.match(value):
        return True
    if "-----BEGIN " in value or _TOKEN_VALUE.search(value):
        return True
    if "://" not in value:
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return True
    return parsed.username is not None or parsed.password is not None


def _redact(value: object, *, depth: int, seen: set[int]) -> object:
    if depth > _MAX_DEPTH:
        return REDACTED
    if isinstance(value, Path | bytes | bytearray | memoryview):
        return REDACTED
    if isinstance(value, str):
        return REDACTED if _sensitive_text(value) else value
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in seen:
            return REDACTED
        seen.add(identity)
        try:
            result: dict[str, object] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise TypeError("redacted mappings require text keys")
                result[key] = REDACTED if _sensitive_key(key) else _redact(item, depth=depth + 1, seen=seen)
            return result
        finally:
            seen.remove(identity)
    if isinstance(value, Sequence) and not isinstance(value, str):
        identity = id(value)
        if identity in seen:
            return REDACTED
        seen.add(identity)
        try:
            return [_redact(item, depth=depth + 1, seen=seen) for item in value]
        finally:
            seen.remove(identity)
    return value


def redact(value: object) -> object:
    """Return a detached structure with secret-bearing values fully replaced."""

    return _redact(value, depth=0, seen=set())


__all__ = ("REDACTED", "redact")
