"""Short-lived, HMAC-authenticated authority lease bound to one deployment identity."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Final

from firmquant.config import Mode
from firmquant.domain.errors import DomainTypeError, DomainValidationError
from firmquant.security.secrets import SecretBytes

_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_LEASE_ID = re.compile(r"^arm_[0-9a-f]{32}$")
_CI_KEYS: Final = frozenset(
    {
        "CI",
        "GITHUB_ACTIONS",
        "GITLAB_CI",
        "TF_BUILD",
        "JENKINS_URL",
        "BUILDKITE",
        "CIRCLECI",
    }
)
DEFAULT_ARM_TTL: Final = timedelta(minutes=5)
MAX_ARM_TTL: Final = timedelta(minutes=15)


class ArmLeaseDenied(RuntimeError):
    """Raised when an arm lease cannot be issued or authenticated."""


def _aware(value: datetime, *, label: str) -> None:
    if not isinstance(value, datetime):
        raise DomainTypeError(f"{label} must be datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise DomainValidationError(f"{label} must be timezone-aware")


def _canonical_identity(value: str, *, label: str, fold_case: bool = False) -> str:
    if not isinstance(value, str):
        raise DomainTypeError(f"{label} must be text")
    if not value or value != value.strip() or len(value) > 512:
        raise DomainValidationError(f"{label} must be canonical non-empty text")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise DomainValidationError(f"{label} contains control characters")
    return value.casefold() if fold_case else value


def _identity_hash(value: str, *, label: str, fold_case: bool = False) -> str:
    canonical = _canonical_identity(value, label=label, fold_case=fold_case)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _require_digest(value: str, pattern: re.Pattern[str], *, label: str) -> None:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise DomainValidationError(f"{label} is not a canonical digest")


def _canonical_json(payload: Mapping[str, object]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class ArmBinding:
    """Hashed host/account identity plus exact code and configuration identity."""

    mode: Mode
    host_hash: str
    account_hash: str
    firmquant_commit: str
    uquant_commit: str
    config_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.mode, Mode) or self.mode not in {Mode.CANARY, Mode.LIVE}:
            raise ArmLeaseDenied("arm binding mode must be CANARY or LIVE")
        _require_digest(self.host_hash, _SHA256, label="arm host hash")
        _require_digest(self.account_hash, _SHA256, label="arm account hash")
        _require_digest(self.firmquant_commit, _SHA1, label="firmquant commit")
        _require_digest(self.uquant_commit, _SHA1, label="uquant commit")
        _require_digest(self.config_sha256, _SHA256, label="configuration digest")

    @classmethod
    def create(
        cls,
        *,
        mode: Mode,
        hostname: str,
        account_id: str,
        firmquant_commit: str,
        uquant_commit: str,
        config_sha256: str,
    ) -> ArmBinding:
        return cls(
            mode=mode,
            host_hash=_identity_hash(hostname, label="hostname", fold_case=True),
            account_hash=_identity_hash(account_id, label="account identity"),
            firmquant_commit=firmquant_commit,
            uquant_commit=uquant_commit,
            config_sha256=config_sha256,
        )

    @property
    def identity_payload_sha256(self) -> str:
        return hashlib.sha256(_canonical_json(self.as_payload())).hexdigest()

    def as_payload(self) -> dict[str, str]:
        return {
            "account_hash": self.account_hash,
            "config_sha256": self.config_sha256,
            "firmquant_commit": self.firmquant_commit,
            "host_hash": self.host_hash,
            "mode": self.mode.value,
            "uquant_commit": self.uquant_commit,
        }


@dataclass(frozen=True, slots=True)
class ArmLease:
    """Persistable signed lease; the operator confirmation phrase is never retained."""

    lease_id: str
    mode: Mode
    host_hash: str
    account_hash: str
    firmquant_commit: str
    uquant_commit: str
    config_sha256: str
    identity_payload_sha256: str
    issued_at: datetime
    expires_at: datetime
    lease_mac: str

    def __post_init__(self) -> None:
        if not isinstance(self.lease_id, str) or _LEASE_ID.fullmatch(self.lease_id) is None:
            raise DomainValidationError("arm lease id is not canonical")
        binding = self.binding
        if self.identity_payload_sha256 != binding.identity_payload_sha256:
            raise DomainValidationError("arm lease identity payload digest is inconsistent")
        _aware(self.issued_at, label="arm issued_at")
        _aware(self.expires_at, label="arm expires_at")
        if self.expires_at <= self.issued_at:
            raise DomainValidationError("arm lease expiry must follow issuance")
        _require_digest(self.lease_mac, _SHA256, label="arm lease authentication code")

    @property
    def binding(self) -> ArmBinding:
        return ArmBinding(
            mode=self.mode,
            host_hash=self.host_hash,
            account_hash=self.account_hash,
            firmquant_commit=self.firmquant_commit,
            uquant_commit=self.uquant_commit,
            config_sha256=self.config_sha256,
        )

    def authenticated_payload(self) -> dict[str, str]:
        return {
            **self.binding.as_payload(),
            "expires_at": self.expires_at.astimezone(UTC).isoformat(),
            "identity_payload_sha256": self.identity_payload_sha256,
            "issued_at": self.issued_at.astimezone(UTC).isoformat(),
            "lease_id": self.lease_id,
        }


def _ci_detected(environment: Mapping[str, str]) -> bool:
    for key in _CI_KEYS:
        value = environment.get(key)
        if value is not None and value.strip().casefold() not in {"", "0", "false", "no"}:
            return True
    return False


class ArmService:
    """Issue and validate expiring leases; environment values can never represent armed."""

    __slots__ = ("_lease_id_factory", "_mac_key")

    def __init__(
        self,
        *,
        mac_key: SecretBytes,
        lease_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(mac_key, SecretBytes):
            raise DomainTypeError("arm MAC key must be SecretBytes")
        if len(mac_key.copy_bytes()) < 32:
            raise DomainValidationError("arm MAC key must contain at least 32 bytes")
        if lease_id_factory is not None and not callable(lease_id_factory):
            raise DomainTypeError("arm lease id factory must be callable")
        self._mac_key = mac_key
        self._lease_id_factory = lease_id_factory or (lambda: "arm_" + os.urandom(16).hex())

    @staticmethod
    def confirmation_phrase(mode: Mode) -> str:
        if not isinstance(mode, Mode) or mode not in {Mode.CANARY, Mode.LIVE}:
            raise ArmLeaseDenied("confirmation mode must be CANARY or LIVE")
        return f"ARM FIRMQUANT REAL TRADING: {mode.value}"

    def _mac(self, lease: ArmLease) -> str:
        return hmac.new(
            self._mac_key.copy_bytes(),
            _canonical_json(lease.authenticated_payload()),
            hashlib.sha256,
        ).hexdigest()

    def issue(
        self,
        binding: ArmBinding,
        *,
        now: datetime,
        confirmation_reader: Callable[[], str],
        interactive_terminal: bool,
        environment: Mapping[str, str] | None = None,
        ttl: timedelta = DEFAULT_ARM_TTL,
    ) -> ArmLease:
        if not isinstance(binding, ArmBinding):
            raise DomainTypeError("arm issue binding must be ArmBinding")
        _aware(now, label="arm issue time")
        if not isinstance(interactive_terminal, bool):
            raise DomainTypeError("interactive terminal flag must be bool")
        if not interactive_terminal:
            raise ArmLeaseDenied("arm lease requires an interactive terminal")
        observed_environment = os.environ if environment is None else environment
        if _ci_detected(observed_environment):
            raise ArmLeaseDenied("arm lease issuance is forbidden in CI")
        if not isinstance(ttl, timedelta) or not timedelta(0) < ttl <= MAX_ARM_TTL:
            raise ArmLeaseDenied("arm lease TTL must be positive and no more than 15 minutes")
        if not callable(confirmation_reader):
            raise DomainTypeError("arm confirmation reader must be callable")
        confirmation = confirmation_reader()
        if not isinstance(confirmation, str) or not hmac.compare_digest(
            confirmation,
            self.confirmation_phrase(binding.mode),
        ):
            raise ArmLeaseDenied("arm confirmation phrase did not match")
        lease_id = self._lease_id_factory()
        unsigned = ArmLease(
            lease_id=lease_id,
            mode=binding.mode,
            host_hash=binding.host_hash,
            account_hash=binding.account_hash,
            firmquant_commit=binding.firmquant_commit,
            uquant_commit=binding.uquant_commit,
            config_sha256=binding.config_sha256,
            identity_payload_sha256=binding.identity_payload_sha256,
            issued_at=now,
            expires_at=now + ttl,
            lease_mac="0" * 64,
        )
        return replace(unsigned, lease_mac=self._mac(unsigned))

    def verify(
        self,
        lease: ArmLease,
        *,
        binding: ArmBinding,
        now: datetime,
    ) -> None:
        if not isinstance(lease, ArmLease):
            raise ArmLeaseDenied("arm lease is missing or malformed")
        if not isinstance(binding, ArmBinding):
            raise DomainTypeError("arm verification binding must be ArmBinding")
        _aware(now, label="arm verification time")
        if not hmac.compare_digest(lease.lease_mac, self._mac(lease)):
            raise ArmLeaseDenied("arm lease authentication failed")
        if now >= lease.expires_at:
            raise ArmLeaseDenied("arm lease expired")
        if now < lease.issued_at:
            raise ArmLeaseDenied("arm lease is not yet valid")
        if not hmac.compare_digest(
            lease.identity_payload_sha256,
            binding.identity_payload_sha256,
        ):
            raise ArmLeaseDenied("arm lease binding does not match current identity")

    def __repr__(self) -> str:
        return "<ArmService redacted>"


__all__ = (
    "DEFAULT_ARM_TTL",
    "MAX_ARM_TTL",
    "ArmBinding",
    "ArmLease",
    "ArmLeaseDenied",
    "ArmService",
)
