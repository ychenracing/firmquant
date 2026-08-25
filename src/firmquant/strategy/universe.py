"""Fail-closed deployment subset over uquant's point-in-time AI universe."""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol, cast

from firmquant.domain.errors import DomainTypeError, DomainValidationError
from firmquant.domain.values import Symbol

from .identity import StrategyIdentity, StrategyIdentityViolation


class UniverseViolation(RuntimeError):
    """Raised when a deployment universe could exceed the canonical AI contract."""


class _UniverseContract(Protocol):
    sha256: str
    symbols: tuple[str, ...]

    def symbols_as_of(self, as_of: str | date) -> tuple[str, ...]: ...


def _default_uquant_universe() -> _UniverseContract:
    module = importlib.import_module("uquant.contracts.universe")
    function = cast(Callable[[], _UniverseContract], module.default_ai_universe)
    return function()


def _point(value: date) -> date:
    if type(value) is not date:
        raise UniverseViolation("universe as_of must be a calendar date")
    return value


def _canonical_symbol(value: object) -> str:
    if not isinstance(value, (str, Symbol)):
        raise UniverseViolation("deployment universe contains an invalid A-share symbol")
    try:
        return value.canonical if isinstance(value, Symbol) else Symbol.parse(value).canonical
    except (DomainTypeError, DomainValidationError) as exc:
        raise UniverseViolation("deployment universe contains an invalid A-share symbol") from exc


@dataclass(frozen=True, slots=True)
class UniversePolicy:
    """A deployment allowlist that can only shrink point-in-time uquant membership."""

    manifest_sha256: str
    deployment_symbols: tuple[str, ...]
    validation_as_of: date
    _universe: _UniverseContract = field(repr=False, compare=False)

    @classmethod
    def from_uquant(
        cls,
        configured_symbols: Iterable[str | Symbol] | None,
        *,
        as_of: date,
    ) -> UniversePolicy:
        """Build a subset only after proving installed strategy and universe identity."""

        point = _point(as_of)
        try:
            identity = StrategyIdentity.locked()
            identity.verify()
        except StrategyIdentityViolation as exc:
            raise UniverseViolation("uquant strategy identity is not verified") from exc
        universe = _default_uquant_universe()
        if universe.sha256 != identity.canonical_universe_sha256:
            raise UniverseViolation("uquant canonical AI universe identity mismatch")

        canonical = frozenset(universe.symbols)
        active = frozenset(universe.symbols_as_of(point))
        if configured_symbols is None:
            deployment = canonical
        else:
            if isinstance(configured_symbols, (str, Symbol)):
                raise UniverseViolation("deployment universe must be an iterable of symbols")
            deployment = frozenset(_canonical_symbol(symbol) for symbol in configured_symbols)
            if not deployment <= canonical:
                raise UniverseViolation("deployment universe exceeds canonical AI universe")
            inactive = sorted(deployment - active)
            if inactive:
                raise UniverseViolation(
                    f"deployment universe member is not active on {point.isoformat()}: {inactive[0]}"
                )
        return cls(
            manifest_sha256=universe.sha256,
            deployment_symbols=tuple(sorted(deployment)),
            validation_as_of=point,
            _universe=universe,
        )

    def allowed(self, symbol: str | Symbol, as_of: date) -> bool:
        """Return false unless both deployment and canonical PIT membership authorize a symbol."""

        if type(as_of) is not date:
            return False
        try:
            canonical = _canonical_symbol(symbol)
        except UniverseViolation:
            return False
        if canonical not in self.deployment_symbols:
            return False
        return canonical in self._universe.symbols_as_of(as_of)


__all__ = ("UniversePolicy", "UniverseViolation")
