"""Identity-bound, zero-write MiniQMT/XtQuant production smoke evidence."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from firmquant.broker.gateway import BrokerGateway
from firmquant.domain.values import Symbol
from firmquant.persistence.audit import AuditLedger
from firmquant.persistence.database import Database
from firmquant.persistence.repositories import canonical_json

_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _digest(value: str, pattern: re.Pattern[str], *, label: str) -> None:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"{label} is not a canonical lowercase digest")


def _aware(value: datetime, *, label: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ProductionSmokeReceipt:
    firmquant_commit: str
    uquant_commit: str
    config_sha256: str
    account_hash: str
    safety_manifest_sha256: str
    observed_at: datetime
    read_healthy: bool
    position_count: int
    order_count: int
    fill_count: int
    real_order_calls: int = 0

    def __post_init__(self) -> None:
        _digest(self.firmquant_commit, _GIT_SHA, label="firmquant commit")
        _digest(self.uquant_commit, _GIT_SHA, label="uquant commit")
        for digest_label, digest_value in (
            ("configuration digest", self.config_sha256),
            ("account hash", self.account_hash),
            ("safety manifest digest", self.safety_manifest_sha256),
        ):
            _digest(digest_value, _SHA256, label=digest_label)
        _aware(self.observed_at, label="smoke observation")
        if type(self.read_healthy) is not bool:
            raise TypeError("smoke read health must be bool")
        for count_label, count_value in (
            ("position count", self.position_count),
            ("order count", self.order_count),
            ("fill count", self.fill_count),
            ("real order calls", self.real_order_calls),
        ):
            if isinstance(count_value, bool) or not isinstance(count_value, int) or count_value < 0:
                raise ValueError(f"smoke {count_label} must be nonnegative integer")
        if self.real_order_calls != 0:
            raise ValueError("production smoke must never perform broker writes")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(canonical_json(self.payload()).encode("utf-8")).hexdigest()

    def payload(self) -> dict[str, object]:
        return {
            "schema": "firmquant.production-smoke.v1",
            "firmquant_commit": self.firmquant_commit,
            "uquant_commit": self.uquant_commit,
            "config_sha256": self.config_sha256,
            "account_hash": self.account_hash,
            "safety_manifest_sha256": self.safety_manifest_sha256,
            "observed_at": self.observed_at.isoformat(),
            "read_healthy": self.read_healthy,
            "position_count": self.position_count,
            "order_count": self.order_count,
            "fill_count": self.fill_count,
            "real_order_calls": self.real_order_calls,
        }


class ProductionSmokeStore:
    def __init__(self, database: Database) -> None:
        if not isinstance(database, Database):
            raise TypeError("production smoke store requires Database")
        self._database = database
        self._audit = AuditLedger(database)

    @staticmethod
    def _from_payload(payload: object) -> ProductionSmokeReceipt:
        if not isinstance(payload, dict) or payload.get("schema") != "firmquant.production-smoke.v1":
            raise ValueError("stored production smoke payload is invalid")
        return ProductionSmokeReceipt(
            firmquant_commit=str(payload["firmquant_commit"]),
            uquant_commit=str(payload["uquant_commit"]),
            config_sha256=str(payload["config_sha256"]),
            account_hash=str(payload["account_hash"]),
            safety_manifest_sha256=str(payload["safety_manifest_sha256"]),
            observed_at=datetime.fromisoformat(str(payload["observed_at"])),
            read_healthy=bool(payload["read_healthy"]),
            position_count=int(payload["position_count"]),
            order_count=int(payload["order_count"]),
            fill_count=int(payload["fill_count"]),
            real_order_calls=int(payload["real_order_calls"]),
        )

    def append(self, receipt: ProductionSmokeReceipt) -> bool:
        if not isinstance(receipt, ProductionSmokeReceipt):
            raise TypeError("production smoke store requires ProductionSmokeReceipt")
        event_id = "production-smoke:" + receipt.sha256
        existing = self._database.query_one(
            "SELECT payload_json FROM audit_events WHERE audit_event_id = ?",
            (event_id,),
        )
        if existing is not None:
            stored = self._from_payload(json.loads(str(existing["payload_json"])))
            if stored != receipt:
                raise RuntimeError("production smoke identity collision")
            return False

        def append_event() -> None:
            self._audit.append(
                audit_event_id=event_id,
                category="PRODUCTION_SMOKE",
                actor="production-readonly-smoke",
                payload=receipt.payload(),
                created_at=receipt.observed_at,
            )

        if self._database.in_transaction:
            append_event()
        else:
            with self._database.transaction():
                append_event()
        return True

    def latest(
        self,
        *,
        firmquant_commit: str,
        uquant_commit: str,
        config_sha256: str,
        account_hash: str,
        safety_manifest_sha256: str,
    ) -> ProductionSmokeReceipt | None:
        rows = self._database.query_all(
            "SELECT payload_json FROM audit_events WHERE category = 'PRODUCTION_SMOKE' ORDER BY sequence DESC"
        )
        for row in rows:
            receipt = self._from_payload(json.loads(str(row["payload_json"])))
            if (
                receipt.firmquant_commit == firmquant_commit
                and receipt.uquant_commit == uquant_commit
                and receipt.config_sha256 == config_sha256
                and receipt.account_hash == account_hash
                and receipt.safety_manifest_sha256 == safety_manifest_sha256
            ):
                return receipt
        return None


def run_readonly_production_smoke(
    *,
    broker: BrokerGateway,
    database: Database,
    probe_symbol: Symbol,
    firmquant_commit: str,
    uquant_commit: str,
    config_sha256: str,
    safety_manifest_sha256: str,
    clock: Callable[[], datetime],
) -> ProductionSmokeReceipt:
    """Read all live authority surfaces once. This function has no write call sites."""

    if not isinstance(broker, BrokerGateway):
        raise TypeError("production smoke broker must satisfy BrokerGateway")
    if not isinstance(database, Database):
        raise TypeError("production smoke database must be Database")
    if not isinstance(probe_symbol, Symbol):
        raise TypeError("production smoke probe symbol must be Symbol")
    if not callable(clock):
        raise TypeError("production smoke clock must be callable")
    health = broker.health()
    account = broker.query_account()
    positions = broker.query_positions()
    orders = broker.query_orders()
    fills = broker.query_fills()
    broker.query_market_status()
    broker.query_instrument(probe_symbol)
    broker.query_quote(probe_symbol)
    observed_at = clock()
    _aware(observed_at, label="production smoke clock")
    receipt = ProductionSmokeReceipt(
        firmquant_commit=firmquant_commit,
        uquant_commit=uquant_commit,
        config_sha256=config_sha256,
        account_hash=account.account_id_hash,
        safety_manifest_sha256=safety_manifest_sha256,
        observed_at=observed_at,
        read_healthy=health.connected and health.read_healthy,
        position_count=len(positions),
        order_count=len(orders),
        fill_count=len(fills),
        real_order_calls=0,
    )
    if not receipt.read_healthy:
        raise RuntimeError("production read-only smoke is not healthy")
    ProductionSmokeStore(database).append(receipt)
    return receipt


__all__ = (
    "ProductionSmokeReceipt",
    "ProductionSmokeStore",
    "run_readonly_production_smoke",
)
