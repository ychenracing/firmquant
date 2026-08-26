"""Pure prepare contract for broker-to-uquant account synchronization."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass

from firmquant.domain.broker_facts import BrokerSnapshot

from .account_sync import AccountStateContract, AccountSyncReceipt, sync_account


@dataclass(frozen=True, slots=True)
class PreparedAccountSync:
    """Validated in-memory uquant state that is not durable until explicitly committed."""

    preparation_id: str
    prepared_account: AccountStateContract
    receipt: AccountSyncReceipt
    broker_snapshot_sha256: str

    @property
    def snapshot_id(self) -> str:
        return self.receipt.snapshot_id

    @property
    def as_of(self) -> str:
        return self.receipt.as_of

    @property
    def payload_sha256(self) -> str:
        return self.receipt.payload_sha256

    @property
    def account_before_sha256(self) -> str:
        return self.receipt.account_before_sha256

    @property
    def account_after_sha256(self) -> str:
        return self.receipt.account_after_sha256


def _preparation_id(receipt: AccountSyncReceipt, broker_snapshot_sha256: str) -> str:
    payload = {
        "account_after_sha256": receipt.account_after_sha256,
        "account_before_sha256": receipt.account_before_sha256,
        "as_of": receipt.as_of,
        "broker_snapshot_sha256": broker_snapshot_sha256,
        "payload_sha256": receipt.payload_sha256,
        "snapshot_id": receipt.snapshot_id,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "acctprep_" + hashlib.sha256(encoded).hexdigest()


def prepare_account_sync(
    account: AccountStateContract,
    snapshot: BrokerSnapshot,
) -> PreparedAccountSync:
    """Apply the locked uquant sync contract only to a deep copy of the caller state."""

    if not isinstance(snapshot, BrokerSnapshot):
        raise TypeError("account sync preparation requires BrokerSnapshot")
    working = copy.deepcopy(account)
    receipt = sync_account(working, snapshot)
    return PreparedAccountSync(
        preparation_id=_preparation_id(receipt, snapshot.raw_payload_sha256),
        prepared_account=working,
        receipt=receipt,
        broker_snapshot_sha256=snapshot.raw_payload_sha256,
    )


__all__ = ("PreparedAccountSync", "prepare_account_sync")
