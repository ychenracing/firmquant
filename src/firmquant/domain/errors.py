"""Domain error taxonomy used across ports and application services."""


class DomainValidationError(ValueError):
    """Raised when a value cannot enter the trusted domain."""


class DomainTypeError(TypeError):
    """Raised when a boundary receives an unsafe runtime type."""


class DomainTransitionError(RuntimeError):
    """Raised when an aggregate transition would violate an invariant."""


__all__ = ("DomainTransitionError", "DomainTypeError", "DomainValidationError")
