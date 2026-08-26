"""Identity-bound SHADOW evidence required before CANARY or LIVE promotion."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _digest(value: str, pattern: re.Pattern[str], *, label: str) -> None:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"{label} must be a canonical lowercase digest")


def _count(value: int, *, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")


def _fraction(value: Decimal, *, label: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or not Decimal(0) <= value <= Decimal(1):
        raise ValueError(f"{label} must be a Decimal between zero and one")


@dataclass(frozen=True, slots=True)
class ShadowPromotionEvidence:
    firmquant_commit: str
    uquant_commit: str
    config_sha256: str
    account_hash: str
    observed_sessions: int
    hypothetical_orders: int
    unresolved_orders: int
    external_orders: int
    duplicate_economic_orders: int
    duplicate_fills: int
    max_target_tracking_error: Decimal
    created_at: datetime

    def __post_init__(self) -> None:
        _digest(self.firmquant_commit, _GIT_SHA, label="firmquant commit")
        _digest(self.uquant_commit, _GIT_SHA, label="uquant commit")
        _digest(self.config_sha256, _SHA256, label="configuration digest")
        _digest(self.account_hash, _SHA256, label="account hash")
        for label, value in (
            ("observed sessions", self.observed_sessions),
            ("hypothetical orders", self.hypothetical_orders),
            ("unresolved orders", self.unresolved_orders),
            ("external orders", self.external_orders),
            ("duplicate economic orders", self.duplicate_economic_orders),
            ("duplicate fills", self.duplicate_fills),
        ):
            _count(value, label=label)
        _fraction(self.max_target_tracking_error, label="maximum target tracking error")
        if not isinstance(self.created_at, datetime) or self.created_at.tzinfo is None:
            raise ValueError("promotion evidence time must be timezone-aware")

    @property
    def sha256(self) -> str:
        payload = {
            "schema": "firmquant.shadow-promotion.v1",
            "firmquant_commit": self.firmquant_commit,
            "uquant_commit": self.uquant_commit,
            "config_sha256": self.config_sha256,
            "account_hash": self.account_hash,
            "observed_sessions": self.observed_sessions,
            "hypothetical_orders": self.hypothetical_orders,
            "unresolved_orders": self.unresolved_orders,
            "external_orders": self.external_orders,
            "duplicate_economic_orders": self.duplicate_economic_orders,
            "duplicate_fills": self.duplicate_fills,
            "max_target_tracking_error": str(self.max_target_tracking_error),
            "created_at": self.created_at.isoformat(),
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    def qualifies(
        self,
        *,
        firmquant_commit: str,
        uquant_commit: str,
        config_sha256: str,
        account_hash: str,
        min_sessions: int,
        min_orders: int,
        max_tracking_error: Decimal,
    ) -> bool:
        _count(min_sessions, label="minimum sessions")
        _count(min_orders, label="minimum orders")
        _fraction(max_tracking_error, label="promotion tracking error limit")
        return (
            self.firmquant_commit == firmquant_commit
            and self.uquant_commit == uquant_commit
            and self.config_sha256 == config_sha256
            and self.account_hash == account_hash
            and self.observed_sessions >= min_sessions
            and self.hypothetical_orders >= min_orders
            and self.unresolved_orders == 0
            and self.external_orders == 0
            and self.duplicate_economic_orders == 0
            and self.duplicate_fills == 0
            and self.max_target_tracking_error <= max_tracking_error
        )


__all__ = ("ShadowPromotionEvidence",)
