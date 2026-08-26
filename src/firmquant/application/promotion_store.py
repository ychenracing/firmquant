"""Append-only identity-bound SHADOW promotion evidence."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

from firmquant.application.promotion import ShadowPromotionEvidence
from firmquant.persistence.audit import AuditLedger
from firmquant.persistence.database import Database


class PromotionStore:
    """Persist immutable SHADOW observations and query only exact deployment identity."""

    def __init__(self, database: Database) -> None:
        if not isinstance(database, Database):
            raise TypeError("promotion store requires Database")
        self._database = database
        self._audit = AuditLedger(database)

    @staticmethod
    def _event_id(evidence: ShadowPromotionEvidence) -> str:
        return "shadow-promotion:" + evidence.sha256

    @staticmethod
    def _payload(evidence: ShadowPromotionEvidence) -> dict[str, object]:
        return {
            "schema": "firmquant.shadow-promotion-evidence.v1",
            "firmquant_commit": evidence.firmquant_commit,
            "uquant_commit": evidence.uquant_commit,
            "config_sha256": evidence.config_sha256,
            "account_hash": evidence.account_hash,
            "observed_sessions": evidence.observed_sessions,
            "hypothetical_orders": evidence.hypothetical_orders,
            "unresolved_orders": evidence.unresolved_orders,
            "external_orders": evidence.external_orders,
            "duplicate_economic_orders": evidence.duplicate_economic_orders,
            "duplicate_fills": evidence.duplicate_fills,
            "max_target_tracking_error": str(evidence.max_target_tracking_error),
            "created_at": evidence.created_at.isoformat(),
            "evidence_sha256": evidence.sha256,
        }

    @staticmethod
    def _from_payload(payload: object) -> ShadowPromotionEvidence:
        if not isinstance(payload, dict) or payload.get("schema") != "firmquant.shadow-promotion-evidence.v1":
            raise ValueError("stored SHADOW promotion payload is invalid")
        evidence = ShadowPromotionEvidence(
            firmquant_commit=str(payload["firmquant_commit"]),
            uquant_commit=str(payload["uquant_commit"]),
            config_sha256=str(payload["config_sha256"]),
            account_hash=str(payload["account_hash"]),
            observed_sessions=int(payload["observed_sessions"]),
            hypothetical_orders=int(payload["hypothetical_orders"]),
            unresolved_orders=int(payload["unresolved_orders"]),
            external_orders=int(payload["external_orders"]),
            duplicate_economic_orders=int(payload["duplicate_economic_orders"]),
            duplicate_fills=int(payload["duplicate_fills"]),
            max_target_tracking_error=Decimal(str(payload["max_target_tracking_error"])),
            created_at=datetime.fromisoformat(str(payload["created_at"])),
        )
        if payload.get("evidence_sha256") != evidence.sha256:
            raise ValueError("stored SHADOW promotion digest mismatch")
        return evidence

    def append(self, evidence: ShadowPromotionEvidence) -> bool:
        if not isinstance(evidence, ShadowPromotionEvidence):
            raise TypeError("promotion store requires ShadowPromotionEvidence")
        event_id = self._event_id(evidence)
        existing = self._database.query_one(
            "SELECT payload_json FROM audit_events WHERE audit_event_id = ?",
            (event_id,),
        )
        if existing is not None:
            stored = self._from_payload(json.loads(str(existing["payload_json"])))
            if stored != evidence:
                raise RuntimeError("SHADOW promotion identity collision")
            return False

        def append_event() -> None:
            self._audit.append(
                audit_event_id=event_id,
                category="SHADOW_PROMOTION",
                actor="production-promotion",
                payload=self._payload(evidence),
                created_at=evidence.created_at,
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
    ) -> ShadowPromotionEvidence | None:
        rows = self._database.query_all(
            "SELECT payload_json FROM audit_events WHERE category = 'SHADOW_PROMOTION' ORDER BY sequence DESC"
        )
        for row in rows:
            evidence = self._from_payload(json.loads(str(row["payload_json"])))
            if (
                evidence.firmquant_commit == firmquant_commit
                and evidence.uquant_commit == uquant_commit
                and evidence.config_sha256 == config_sha256
                and evidence.account_hash == account_hash
            ):
                return evidence
        return None

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
        evidence = self.latest(
            firmquant_commit=firmquant_commit,
            uquant_commit=uquant_commit,
            config_sha256=config_sha256,
            account_hash=account_hash,
        )
        return evidence is not None and evidence.qualifies(
            firmquant_commit=firmquant_commit,
            uquant_commit=uquant_commit,
            config_sha256=config_sha256,
            account_hash=account_hash,
            min_sessions=min_sessions,
            min_orders=min_orders,
            max_tracking_error=max_tracking_error,
        )


__all__ = ("PromotionStore",)
