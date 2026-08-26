"""Stable MiniQMT client-order tags derived from uquant economic order identity."""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Iterable

_TAG = re.compile(r"^fq[0-9a-f]{22}$")


def _canonical(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be canonical non-empty text")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{label} contains control characters")
    return value


def client_order_tag(uquant_order_id: str) -> str:
    """Return a deterministic 24-byte ASCII tag that fits MiniQMT ``order_remark``."""

    canonical = _canonical(uquant_order_id, label="uquant order id")
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return "fq" + digest[:22]


def is_client_order_tag(value: object) -> bool:
    return isinstance(value, str) and _TAG.fullmatch(value) is not None


def matches_uquant_order(client_order_id: str | None, uquant_order_id: str) -> bool:
    """Accept the native uquant id or its deterministic MiniQMT tag."""

    if client_order_id is None:
        return False
    canonical = _canonical(uquant_order_id, label="uquant order id")
    return hmac.compare_digest(client_order_id, canonical) or hmac.compare_digest(
        client_order_id,
        client_order_tag(canonical),
    )


def resolve_uquant_order_id(
    client_order_id: str,
    known_uquant_order_ids: Iterable[str],
) -> str:
    """Resolve a broker-returned client id/tag without guessing or accepting collisions."""

    observed = _canonical(client_order_id, label="client order id")
    known = tuple(
        sorted(
            {
                _canonical(item, label="known uquant order id")
                for item in known_uquant_order_ids
            }
        )
    )
    exact = tuple(item for item in known if hmac.compare_digest(observed, item))
    if len(exact) == 1:
        return exact[0]
    tagged = tuple(
        item
        for item in known
        if hmac.compare_digest(observed, client_order_tag(item))
    )
    if len(tagged) == 1:
        return tagged[0]
    if len(tagged) > 1:
        raise ValueError("client order id maps to multiple uquant orders")
    raise ValueError("client order id does not map to a known uquant order")


__all__ = (
    "client_order_tag",
    "is_client_order_tag",
    "matches_uquant_order",
    "resolve_uquant_order_id",
)
