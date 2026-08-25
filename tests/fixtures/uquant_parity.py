from __future__ import annotations

import argparse
import json
import math
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd


def write_synthetic_market_data(source_checkout: Path, data_directory: Path) -> None:
    manifest = json.loads(
        (source_checkout / "uquant/contracts/resources/ai_universe_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    symbols = tuple(member["symbol"] for member in manifest["members"])
    data_directory.mkdir()
    dates = pd.bdate_range("2023-01-02", "2026-06-30")
    for symbol_index, symbol in enumerate((*symbols, "sh000300", "sh000682")):
        lines = ["date,open,high,low,close,volume,amount"]
        for session_index, session in enumerate(dates):
            base = 20.0 + symbol_index * 0.7
            close = base * (1.0 + 0.00045 * session_index) * (
                1.0
                + 0.012
                * math.sin(
                    session_index / (13 + symbol_index % 5) + symbol_index
                )
            )
            open_price = close * (
                1.0 + 0.001 * math.sin(session_index / 7 + symbol_index)
            )
            high = max(open_price, close) * 1.01
            low = min(open_price, close) * 0.99
            volume = 1_000_000 + symbol_index * 1_000 + session_index * 10
            amount = close * volume
            lines.append(
                f"{session.date()},{open_price:.8f},{high:.8f},{low:.8f},"
                f"{close:.8f},{volume},{amount:.4f}"
            )
        (data_directory / f"{symbol}.csv").write_text(
            "\n".join(lines) + "\n",
            encoding="utf-8",
        )


def _run(args: argparse.Namespace) -> dict[str, object]:
    from uquant.account import economic_state_sha256
    from uquant.config import config_fingerprint
    from uquant.engine import ProductionEngine
    from uquant.types import AccountState

    from firmquant.persistence.database import Database
    from firmquant.strategy.adapter import (
        DecisionConflict,
        DecisionRecoveryRequired,
        DecisionRequest,
        StrategyAdapter,
    )
    from firmquant.strategy.universe import UniversePolicy

    source_checkout = Path(args.source_checkout)
    data_directory = Path(args.data_directory)
    symbols = ("sz300308", "sz300502", "sh603986")
    session = date(2026, 6, 30)
    direct_account = AccountState.empty(2_000_000.0)
    direct_engine = ProductionEngine(data_directory)
    direct = direct_engine.decide(
        symbols=symbols,
        as_of=session.isoformat(),
        account=direct_account,
    )
    direct_payload = direct.canonical_payload(
        effective_config_sha256=config_fingerprint(direct_engine.cfg)
    )
    direct_account_sha256 = economic_state_sha256(direct_account)

    database = Database.open(Path(args.database))
    try:
        adapted_account = AccountState.empty(2_000_000.0)
        policy = UniversePolicy.from_uquant(symbols, as_of=session)
        adapter = StrategyAdapter(
            engine=ProductionEngine(data_directory),
            database=database,
            source_checkout=source_checkout,
            universe_policy=policy,
        )
        request = DecisionRequest(
            strategy_session=session,
            symbols=symbols,
            account=adapted_account,
            firmquant_commit=args.firmquant_commit,
            data_manifest_sha256=direct_account.data_hash,
            broker_snapshot_sha256="3" * 64,
            created_at=datetime(2026, 6, 30, 9, tzinfo=UTC),
        )
        snapshot = adapter.decide_once(request)
        adapted_account_sha256 = economic_state_sha256(adapted_account)

        repeated_before = economic_state_sha256(adapted_account)
        repeated = adapter.decide_once(request)
        repeated_account_unchanged = (
            economic_state_sha256(adapted_account) == repeated_before
        )

        recovery_required = False
        try:
            adapter.decide_once(
                replace(request, account=AccountState.empty(2_000_000.0))
            )
        except DecisionRecoveryRequired:
            recovery_required = True

        conflict_recorded = False
        try:
            adapter.decide_once(
                replace(
                    request,
                    account=AccountState.empty(2_000_000.0),
                    broker_snapshot_sha256="4" * 64,
                )
            )
        except DecisionConflict:
            conflict_recorded = database.scalar(
                "SELECT count(*) FROM audit_events WHERE category = 'DECISION_CONFLICT'"
            ) == 1

        stored = database.query_one(
            "SELECT payload_sha256 FROM decision_snapshots WHERE decision_id = ?",
            (snapshot.decision_id,),
        )
        return {
            "uquant_payload_equal": snapshot.uquant_payload == direct_payload,
            "account_equal": adapted_account_sha256 == direct_account_sha256,
            "decision_digest": direct.decision_digest,
            "opportunity": direct.opportunity.value,
            "risk": direct.risk.value,
            "targets": len(direct.targets),
            "orders": len(direct.pending_orders),
            "repeated_decision_id_equal": repeated.decision_id == snapshot.decision_id,
            "repeated_account_unchanged": repeated_account_unchanged,
            "recovery_required_for_unapplied_account": recovery_required,
            "conflict_recorded": conflict_recorded,
            "stored_payload_equal": (
                stored is not None and stored["payload_sha256"] == snapshot.payload_sha256
            ),
            "account_code_hash": adapted_account.code_hash,
        }
    finally:
        database.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-checkout", required=True)
    parser.add_argument("--data-directory", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--firmquant-commit", required=True)
    args = parser.parse_args()
    print(json.dumps(_run(args), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
