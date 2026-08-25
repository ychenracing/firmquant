"""Strategy-facing projection of the reviewed uquant build identity."""

from __future__ import annotations

from dataclasses import dataclass, fields

from firmquant.build_identity import (
    SourceIdentity,
    SourceIdentityError,
    installed_uquant_identity,
    load_locked_source_identity,
)


class StrategyIdentityViolation(RuntimeError):
    """Raised when strategy code, config, universe, or package bytes are not reviewed."""


@dataclass(frozen=True, slots=True)
class StrategyIdentity:
    """Immutable identities embedded in every decision and execution authorization."""

    uquant_commit: str
    uquant_tree: str
    economic_code_fingerprint: str
    account_code_fingerprint: str
    config_fingerprint: str
    canonical_universe_sha256: str
    universe_resource_sha256: str
    wheel_sha256: str
    package_manifest_sha256: str

    @classmethod
    def _from_source(cls, source: SourceIdentity) -> StrategyIdentity:
        return cls(
            uquant_commit=source.uquant_commit,
            uquant_tree=source.uquant_tree,
            economic_code_fingerprint=source.economic_code_fingerprint,
            account_code_fingerprint=source.account_code_fingerprint,
            config_fingerprint=source.config_fingerprint,
            canonical_universe_sha256=source.universe_sha256,
            universe_resource_sha256=source.universe_manifest_sha256,
            wheel_sha256=source.wheel_sha256,
            package_manifest_sha256=source.uquant_package_manifest_sha256,
        )

    @classmethod
    def locked(cls) -> StrategyIdentity:
        """Load the reviewed identity embedded in firmquant without probing mutable state."""

        return cls._from_source(load_locked_source_identity())

    def verify(self) -> None:
        """Verify this projection and every installed uquant package/runtime contract."""

        reviewed = type(self).locked()
        labels = {
            "uquant_commit": "uquant commit",
            "uquant_tree": "uquant tree",
            "economic_code_fingerprint": "economic code fingerprint",
            "account_code_fingerprint": "account code fingerprint",
            "config_fingerprint": "config fingerprint",
            "canonical_universe_sha256": "canonical universe SHA-256",
            "universe_resource_sha256": "universe resource SHA-256",
            "wheel_sha256": "wheel SHA-256",
            "package_manifest_sha256": "package manifest SHA-256",
        }
        for field in fields(self):
            if getattr(self, field.name) != getattr(reviewed, field.name):
                raise StrategyIdentityViolation(f"{labels[field.name]} is not the reviewed identity")
        try:
            installed = type(self)._from_source(installed_uquant_identity())
        except SourceIdentityError as exc:
            raise StrategyIdentityViolation("installed uquant identity verification failed") from exc
        if installed != self:
            raise StrategyIdentityViolation("installed uquant identity differs from the reviewed identity")


__all__ = ("StrategyIdentity", "StrategyIdentityViolation")
