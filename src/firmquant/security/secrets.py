"""Log-safe secret values and providers with no authority-escalation semantics."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Never, Protocol, SupportsIndex, runtime_checkable

from firmquant.domain.errors import DomainTypeError, DomainValidationError

_SECRET_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")


class SecretNotFound(RuntimeError):
    """Raised without exposing a secret value or local provider details."""


class SecretBytes:
    """Opaque byte material whose string, repr, and pickle surfaces are redacted."""

    __slots__ = ("__value",)

    def __init__(self, value: bytes) -> None:
        if not isinstance(value, bytes):
            raise DomainTypeError("secret value must be bytes")
        if not value:
            raise DomainValidationError("secret value must not be empty")
        self.__value = value

    def copy_bytes(self) -> bytes:
        """Return a short-lived copy for a cryptographic operation."""

        return bytes(self.__value)

    def __repr__(self) -> str:
        return "<SecretBytes redacted>"

    __str__ = __repr__

    def __reduce_ex__(self, protocol: SupportsIndex, /) -> Never:
        del protocol
        raise TypeError("SecretBytes is not serializable")

    def __getstate__(self) -> Never:
        raise TypeError("SecretBytes is not serializable")


@runtime_checkable
class SecretProvider(Protocol):
    """Load secret material by canonical logical name."""

    def get_secret(self, name: str) -> SecretBytes: ...


class EnvironmentSecretProvider:
    """Optional environment-backed secret loader; values never imply an arm state."""

    __slots__ = ("_environment", "_prefix")

    def __init__(
        self,
        *,
        prefix: str = "FIRMQUANT_SECRET_",
        environment: Mapping[str, str] | None = None,
    ) -> None:
        if not isinstance(prefix, str) or _SECRET_NAME.fullmatch(prefix.rstrip("_")) is None:
            raise DomainValidationError("secret environment prefix is not canonical")
        self._prefix = prefix
        self._environment = os.environ if environment is None else environment

    def get_secret(self, name: str) -> SecretBytes:
        if not isinstance(name, str) or _SECRET_NAME.fullmatch(name) is None:
            raise DomainValidationError("secret name is not canonical")
        environment_name = self._prefix + name
        value = self._environment.get(environment_name)
        if value is None or not value:
            raise SecretNotFound(f"required secret is unavailable: {name}")
        if "\x00" in value:
            raise SecretNotFound(f"required secret is malformed: {name}")
        return SecretBytes(value.encode("utf-8"))

    def __repr__(self) -> str:
        return "<EnvironmentSecretProvider redacted>"


__all__ = (
    "EnvironmentSecretProvider",
    "SecretBytes",
    "SecretNotFound",
    "SecretProvider",
)
