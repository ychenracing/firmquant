"""Session-scoped operational ledger view for today-only broker order/trade queries."""

from __future__ import annotations

from datetime import date

from firmquant.domain.broker_facts import AccountType, Side
from firmquant.domain.orders import OrderState
from firmquant.domain.values import Shares, Symbol
from firmquant.persistence.database import Database

from .authority_window import (
    fill_is_in_broker_authority_window,
    order_is_in_broker_authority_window,
)
from .models import OperationalLedgerView, OperationalOrderView


def build_operational_ledger_view(
    database: Database,
    *,
    broker_session: date,
    expected_account_id_hash: str,
    expected_account_type: AccountType,
) -> OperationalLedgerView:
    """Project only facts that the configured today-only broker query can authoritatively prove."""

    if not isinstance(database, Database):
        raise TypeError("operational ledger view requires Database")
    if type(broker_session) is not date:
        raise TypeError("broker authority window requires a calendar date")

    rows = database.query_all(
        """
        SELECT b.broker_order_id, b.session_date AS broker_session, i.uquant_order_id,
               i.symbol, i.side, i.requested_shares, i.filled_shares, i.state
        FROM broker_orders b
        JOIN execution_intents i ON i.execution_id = b.execution_id
        WHERE b.ownership = 'SYSTEM'
        ORDER BY b.broker_order_id
        """
    )
    orders: list[OperationalOrderView] = []
    for row in rows:
        local_state = OrderState(str(row["state"]))
        order_session = date.fromisoformat(str(row["broker_session"]))
        if not order_is_in_broker_authority_window(
            order_session=order_session,
            broker_session=broker_session,
            local_state=local_state,
        ):
            continue
        orders.append(
            OperationalOrderView(
                broker_order_id=str(row["broker_order_id"]),
                uquant_order_id=str(row["uquant_order_id"]),
                symbol=Symbol.parse(str(row["symbol"])),
                side=Side(str(row["side"])),
                requested_shares=Shares(int(row["requested_shares"])),
                filled_shares=Shares(int(row["filled_shares"])),
                local_state=local_state,
            )
        )

    fill_rows = database.query_all("SELECT broker_fill_id, session_date FROM fills ORDER BY broker_fill_id")
    known_fills = frozenset(
        str(row["broker_fill_id"])
        for row in fill_rows
        if fill_is_in_broker_authority_window(
            fill_session=date.fromisoformat(str(row["session_date"])),
            broker_session=broker_session,
        )
    )
    unresolved_rows = database.query_all(
        "SELECT execution_id FROM execution_intents "
        "WHERE state IN ('UNKNOWN','CANCEL_REQUESTED') ORDER BY execution_id"
    )
    submitting_rows = database.query_all(
        "SELECT execution_id FROM execution_intents WHERE state = 'SUBMITTING' ORDER BY execution_id"
    )
    return OperationalLedgerView(
        expected_account_id_hash=expected_account_id_hash,
        expected_account_type=expected_account_type,
        orders=tuple(orders),
        known_broker_fill_ids=known_fills,
        unresolved_execution_ids=tuple(str(row["execution_id"]) for row in unresolved_rows),
        submitting_unresolved_execution_ids=tuple(str(row["execution_id"]) for row in submitting_rows),
    )


__all__ = ("build_operational_ledger_view",)
