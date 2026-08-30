from __future__ import annotations

import argparse
import dataclasses
import hashlib
import importlib
import json
import math
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast

import pandas as pd

_PUBLIC_TRACE_SURFACE = {
    "uquant.account": ("economic_state_sha256", "load_account", "save_account"),
    "uquant.config": ("DEFAULT_CONFIG", "config_fingerprint"),
    "uquant.data": ("DataStore", "DataStore.load"),
    "uquant.engine": ("ProductionEngine", "ProductionEngine.decide", "code_fingerprint"),
    "uquant.execution": ("ExecutionPlanner", "ExecutionPlanner.execute_open"),
    "uquant.types": (
        "AccountState",
        "AccountState.empty",
        "AccountState.pending_orders",
        "AccountState.to_dict",
        "Decision.canonical_payload",
        "Decision.pending_orders",
        "Fill",
    ),
}


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"public API contract contains non-standard JSON constant: {value}")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"public API contract contains duplicate field: {key}")
        result[key] = value
    return result


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_public_contract(source_checkout: Path) -> dict[str, object]:
    path = source_checkout / "benchmarks/public_api_contract.json"
    parsed = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_unique_json_object,
        parse_constant=_reject_json_constant,
    )
    if not isinstance(parsed, dict):
        raise ValueError("public API contract root must be an object")
    if set(parsed) != {
        "contract",
        "contract_id",
        "contract_sha256",
        "recorded_on",
        "schema_version",
    }:
        raise ValueError("public API contract envelope is invalid")
    contract = parsed["contract"]
    if not isinstance(contract, dict):
        raise ValueError("public API contract payload must be an object")
    if parsed["schema_version"] != 1 or parsed["contract_id"] != "uquant-public-api-v1":
        raise ValueError("public API contract identity is unsupported")
    if parsed["contract_sha256"] != _canonical_sha256(contract):
        raise ValueError("public API contract payload digest does not match")
    return parsed


def _contract_declares_member(module_contract: dict[str, object], member: str) -> bool:
    owner, separator, child = member.partition(".")
    public_names = module_contract.get("public_names")
    if not isinstance(public_names, list) or owner not in public_names:
        return False
    if not separator:
        return True
    classes = module_contract.get("classes")
    dataclasses_contract = module_contract.get("dataclasses")
    if not isinstance(classes, dict) or not isinstance(dataclasses_contract, dict):
        return False
    class_contract = classes.get(owner)
    if isinstance(class_contract, dict):
        methods = class_contract.get("methods")
        if isinstance(methods, dict) and child in methods:
            return True
    dataclass_contract = dataclasses_contract.get(owner)
    if isinstance(dataclass_contract, dict):
        fields = dataclass_contract.get("fields")
        if isinstance(fields, list) and any(
            isinstance(field, dict) and field.get("name") == child for field in fields
        ):
            return True
    return False


def _validated_public_api(contract: dict[str, object]) -> tuple[dict[str, object], dict[str, list[str]]]:
    payload = contract["contract"]
    if not isinstance(payload, dict) or not isinstance(payload.get("modules"), dict):
        raise ValueError("public API contract modules are invalid")
    modules = cast(dict[str, object], payload["modules"])
    resolved: dict[str, object] = {}
    surface: dict[str, list[str]] = {}
    for module_name, members in _PUBLIC_TRACE_SURFACE.items():
        module_contract = modules.get(module_name)
        if not isinstance(module_contract, dict):
            raise ValueError(f"public API contract omits module: {module_name}")
        runtime_module = importlib.import_module(module_name)
        surface[module_name] = []
        for member in members:
            if not _contract_declares_member(module_contract, member):
                raise ValueError(f"public API contract omits member: {module_name}.{member}")
            owner, separator, child = member.partition(".")
            runtime_owner = getattr(runtime_module, owner)
            if separator and child not in getattr(runtime_owner, "__dataclass_fields__", {}):
                getattr(runtime_owner, child)
            resolved[f"{module_name}.{member}"] = (
                runtime_owner
                if not separator
                else getattr(
                    runtime_owner,
                    child,
                    None,
                )
            )
            surface[module_name].append(member)
    return resolved, surface


def write_synthetic_market_data(source_checkout: Path, data_directory: Path) -> None:
    manifest = json.loads(
        (source_checkout / "uquant/contracts/resources/ai_universe_manifest.json").read_text(encoding="utf-8")
    )
    symbols = tuple(member["symbol"] for member in manifest["members"])
    data_directory.mkdir()
    dates = pd.bdate_range("2023-01-02", "2026-06-30")
    for symbol_index, symbol in enumerate((*symbols, "sh000300", "sh000682")):
        lines = ["date,open,high,low,close,volume,amount"]
        for session_index, session in enumerate(dates):
            base = 20.0 + symbol_index * 0.7
            close = (
                base
                * (1.0 + 0.00045 * session_index)
                * (1.0 + 0.012 * math.sin(session_index / (13 + symbol_index % 5) + symbol_index))
            )
            open_price = close * (1.0 + 0.001 * math.sin(session_index / 7 + symbol_index))
            high = max(open_price, close) * 1.01
            low = min(open_price, close) * 0.99
            volume = 1_000_000 + symbol_index * 1_000 + session_index * 10
            amount = close * volume
            lines.append(
                f"{session.date()},{open_price:.8f},{high:.8f},{low:.8f},{close:.8f},{volume},{amount:.4f}"
            )
        (data_directory / f"{symbol}.csv").write_text(
            "\n".join(lines) + "\n",
            encoding="utf-8",
        )


def _run_public_trace(args: argparse.Namespace) -> dict[str, object]:
    contract = _load_public_contract(Path(args.source_checkout))
    api, public_surface = _validated_public_api(contract)
    payload = cast(dict[str, object], contract["contract"])
    expected_trace = cast(dict[str, object], payload["decision_fill_account_trace"])
    inputs = cast(dict[str, object], expected_trace["inputs"])
    symbols = tuple(cast(list[str], inputs["symbols"]))
    initial_cash = cast(float, inputs["initial_cash"])
    account_state = cast(Any, api["uquant.types.AccountState"])
    production_engine = cast(Any, api["uquant.engine.ProductionEngine"])
    default_config = api["uquant.config.DEFAULT_CONFIG"]
    config_fingerprint = cast(Any, api["uquant.config.config_fingerprint"])
    data_store = cast(Any, api["uquant.data.DataStore"])
    execution_planner = cast(Any, api["uquant.execution.ExecutionPlanner"])
    save_account = cast(Any, api["uquant.account.save_account"])
    load_account = cast(Any, api["uquant.account.load_account"])
    economic_state_sha256 = cast(Any, api["uquant.account.economic_state_sha256"])

    account = account_state.empty(initial_cash)
    initial_payload = account.to_dict()
    engine = production_engine(Path(args.data_directory), default_config)
    engine.decide(
        symbols=symbols,
        as_of=cast(str, inputs["warmup_decision_date"]),
        account=account,
    )
    decision = engine.decide(
        symbols=symbols,
        as_of=cast(str, inputs["signal_date"]),
        account=account,
    )
    account.pending_orders = list(decision.pending_orders)
    store = data_store(Path(args.data_directory))
    panel = {symbol: store.load(symbol) for symbol in symbols}
    fills = execution_planner(default_config).execute_open(
        date=pd.Timestamp(cast(str, inputs["fill_date"])),
        account=account,
        panel=panel,
    )
    observed_trace = {
        "inputs": inputs,
        "initial_account_sha256": _canonical_sha256(initial_payload),
        "decision": decision.canonical_payload(effective_config_sha256=config_fingerprint(default_config)),
        "fills": [dataclasses.asdict(fill) for fill in fills],
        "account_after": account.to_dict(),
        "account_after_sha256": _canonical_sha256(account.to_dict()),
    }
    if observed_trace != expected_trace:
        raise RuntimeError("public decision/fill/account trace differs from reviewed contract")

    account_path = Path(args.database)
    save_account(account, account_path)
    reloaded = load_account(
        account_path,
        require_hashes=True,
        allow_legacy_schema=False,
    )
    account_payload_equal = reloaded.to_dict() == account.to_dict()
    if not account_payload_equal:
        raise RuntimeError("strict AccountState save/load changed the canonical payload")
    economic_sha256 = economic_state_sha256(account)
    reloaded_economic_sha256 = economic_state_sha256(reloaded)
    if economic_sha256 != reloaded_economic_sha256:
        raise RuntimeError("strict AccountState save/load changed the economic identity")

    return {
        "contract": {
            "contract_id": contract["contract_id"],
            "contract_sha256": contract["contract_sha256"],
            "schema_version": contract["schema_version"],
        },
        "persistence": {
            "account_payload_equal": account_payload_equal,
            "economic_sha256": economic_sha256,
            "reloaded_economic_sha256": reloaded_economic_sha256,
            "reloaded_schema_version": reloaded.schema_version,
        },
        "public_surface": public_surface,
        "trace": observed_trace,
    }


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
    direct_payload = direct.canonical_payload(effective_config_sha256=config_fingerprint(direct_engine.cfg))
    direct_account_sha256 = economic_state_sha256(direct_account)

    class PublicEngineFacade:
        __module__ = "uquant.engine"

        def __init__(self, engine: ProductionEngine) -> None:
            self.cfg = engine.cfg
            self.data = engine.data
            self._engine = engine

        def __getattribute__(self, name: str) -> object:
            if name == "_code_hash":
                raise AssertionError("private engine state was accessed")
            return object.__getattribute__(self, name)

        def decide(self, *, symbols: tuple[str, ...], as_of: str, account: object) -> object:
            engine = object.__getattribute__(self, "_engine")
            return engine.decide(symbols=symbols, as_of=as_of, account=account)

    database = Database.open(Path(args.database))
    try:
        adapted_account = AccountState.empty(2_000_000.0)
        policy = UniversePolicy.from_uquant(symbols, as_of=session)
        adapter = StrategyAdapter(
            engine=PublicEngineFacade(ProductionEngine(data_directory)),
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
        repeated_account_unchanged = economic_state_sha256(adapted_account) == repeated_before

        recovery_required = False
        unapplied = AccountState.empty(2_000_000.0)
        recovery_request = replace(request, account=unapplied)
        try:
            adapter.decide_once(recovery_request)
        except DecisionRecoveryRequired:
            recovery_required = True
        recovered = adapter.recover_existing_decision(recovery_request, snapshot)
        recovered_account_matches = (
            economic_state_sha256(unapplied) == snapshot.account_after_sha256 == direct_account_sha256
            and recovered.decision_id == snapshot.decision_id
        )

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
            conflict_recorded = (
                database.scalar("SELECT count(*) FROM audit_events WHERE category = 'DECISION_CONFLICT'") == 1
            )

        stored = database.query_one(
            "SELECT payload_sha256 FROM decision_snapshots WHERE decision_id = ?",
            (snapshot.decision_id,),
        )
        return {
            "uquant_payload_equal": snapshot.uquant_payload == direct_payload,
            "adapter_public_engine_surface": True,
            "account_equal": adapted_account_sha256 == direct_account_sha256,
            "decision_digest": direct.decision_digest,
            "opportunity": direct.opportunity.value,
            "risk": direct.risk.value,
            "targets": len(direct.targets),
            "orders": len(direct.pending_orders),
            "repeated_decision_id_equal": repeated.decision_id == snapshot.decision_id,
            "repeated_account_unchanged": repeated_account_unchanged,
            "recovery_required_for_unapplied_account": recovery_required,
            "recovered_unapplied_account": recovered_account_matches,
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
    parser.add_argument("--public-trace-only", action="store_true")
    args = parser.parse_args()
    result = _run_public_trace(args) if args.public_trace_only else _run(args)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
