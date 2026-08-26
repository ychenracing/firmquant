from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from firmquant.application import composition
from firmquant.application.composition import (
    ConfiguredOperatorPorts,
    _configuration_identity_matches,
    _data_identity_matches,
    _money,
    _operational_ledger,
    _paper_gateway,
    _persist_snapshot,
    _previous_account_identity,
    _safe_account,
    _strategy_account,
    _uquant_account_contract,
    _uquant_data_manifest,
    _uquant_execution_config,
    _uquant_symbol,
    compose_operator_ports,
)
from firmquant.application.operations import OperatorCommandDenied
from firmquant.config import Mode, load_settings
from firmquant.domain.broker_facts import AccountType
from firmquant.domain.values import Money
from firmquant.persistence.database import Database
from tests.fixtures.broker_snapshots import completed_buy_snapshot
from tests.integration.test_cli_operations import NOW, paper_config


def _account(**changes: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "cash": 100_000.0,
        "positions": {},
        "pending_orders": [],
        "order_ledger": [],
        "fills": [],
        "data_hash": "",
        "data_hash_as_of": "",
        "data_hash_symbols": [],
        "code_hash": "",
    }
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.mark.parametrize("value", [True, "1", None, float("nan"), float("inf"), -1, Decimal("NaN")])
def test_uquant_money_boundary_rejects_nonfinite_or_negative_values(value: object) -> None:
    with pytest.raises(OperatorCommandDenied, match="UQUANT_ACCOUNT_STATE_INVALID"):
        _money(value, label="TEST")


def test_uquant_money_boundary_uses_exact_decimal_conversion() -> None:
    assert _money(1, label="TEST") == Money(Decimal(1))
    assert _money(Decimal("1.2500"), label="TEST") == Money(Decimal("1.2500"))


def test_locked_uquant_contract_symbols_are_present() -> None:
    assert _uquant_symbol("uquant.types", "AccountState") is not None
    account_type, loader, economic_hash = _uquant_account_contract()
    assert isinstance(account_type, type)
    assert callable(loader)
    assert callable(economic_hash)
    assert _uquant_execution_config().max_volume_participation == 0.005


def test_uquant_symbol_and_manifest_fail_closed_on_missing_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(OperatorCommandDenied, match="UQUANT_CONTRACT_UNAVAILABLE"):
        _uquant_symbol("uquant.missing", "Missing")

    monkeypatch.setattr(composition, "_uquant_symbol", lambda _module, _symbol: object())
    with pytest.raises(OperatorCommandDenied, match="UQUANT_CONTRACT_INVALID"):
        _uquant_account_contract()
    with pytest.raises(OperatorCommandDenied, match="UQUANT_CONTRACT_INVALID"):
        _uquant_execution_config()
    with pytest.raises(OperatorCommandDenied, match="UQUANT_CONTRACT_INVALID"):
        _uquant_data_manifest(tmp_path, (), as_of="2026-08-25")


def test_safe_account_requires_regular_strict_uquant_state(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    with pytest.raises(OperatorCommandDenied, match="UNAVAILABLE"):
        _safe_account(missing)

    invalid = tmp_path / "account.json"
    invalid.write_text("{}", encoding="utf-8")
    with pytest.raises(OperatorCommandDenied, match="INVALID"):
        _safe_account(invalid)

    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    linked = tmp_path / "linked.json"
    linked.symlink_to(target)
    with pytest.raises(OperatorCommandDenied, match="UNAVAILABLE"):
        _safe_account(linked)


def test_paper_gateway_rejects_preseeded_strategy_economic_state(tmp_path: Path) -> None:
    config = tmp_path / "firmquant.toml"
    paper_config(config)
    settings = load_settings(config)
    for change in (
        {"positions": {"sz300308": object()}},
        {"pending_orders": [object()]},
        {"order_ledger": [object()]},
        {"fills": [object()]},
    ):
        with pytest.raises(OperatorCommandDenied, match="PAPER_BROKER_SEED_REQUIRED"):
            _paper_gateway(settings=settings, account=_account(**change), clock=lambda: NOW)

    gateway = _paper_gateway(settings=settings, account=_account(cash=Decimal("1000")), clock=lambda: NOW)
    assert gateway.health().connected is False
    assert gateway.query_positions(connected_required=False) == ()


def test_snapshot_persistence_is_idempotent_and_detects_identity_collision(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    snapshot = completed_buy_snapshot()
    try:
        _persist_snapshot(database, snapshot)
        _persist_snapshot(database, snapshot)
        assert database.scalar("SELECT count(*) FROM broker_snapshots") == 1
        assert database.scalar("SELECT count(*) FROM position_snapshots") == 1

        changed = replace(
            snapshot,
            account=replace(
                snapshot.account,
                available_cash=Money(snapshot.account.available_cash.value + Decimal("1")),
                total_assets=Money(snapshot.account.total_assets.value + Decimal("1")),
            ),
        )
        with pytest.raises(OperatorCommandDenied, match="IDENTITY_COLLISION"):
            _persist_snapshot(database, changed)
    finally:
        database.close()


def test_previous_account_identity_binds_first_snapshot_and_retains_existing(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    snapshot = completed_buy_snapshot()
    try:
        assert _previous_account_identity(database, snapshot) == (
            snapshot.account.account_id_hash,
            AccountType.CASH,
        )
        _persist_snapshot(database, snapshot)
        changed = replace(
            snapshot,
            snapshot_id="other-snapshot",
            account=replace(snapshot.account, account_id_hash="b" * 64),
        )
        assert _previous_account_identity(database, changed) == (
            snapshot.account.account_id_hash,
            AccountType.CASH,
        )
    finally:
        database.close()


def test_strategy_account_projection_preserves_uquant_positions_without_guessing_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = completed_buy_snapshot()
    position = snapshot.positions[0]
    account = _account(
        cash=float(snapshot.account.available_cash.value),
        positions={position.symbol.canonical: SimpleNamespace(shares=position.total_shares.value)},
        order_ledger=[SimpleNamespace(order_id="O-1")],
    )
    monkeypatch.setattr(
        composition,
        "_uquant_account_contract",
        lambda: (object, lambda *_args, **_kwargs: account, lambda _account: "e" * 64),
    )
    projected = _strategy_account(account, snapshot)
    assert projected.positions[0].sellable_shares == position.sellable_shares
    assert projected.known_uquant_order_ids == frozenset({"O-1"})
    assert projected.economic_state_sha256 == "e" * 64

    account.positions[position.symbol.canonical].shares = 0
    with pytest.raises(OperatorCommandDenied, match="UQUANT_ACCOUNT_STATE_INVALID"):
        _strategy_account(account, snapshot)


def test_operational_ledger_empty_state_has_no_economic_authority(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        ledger = _operational_ledger(
            database,
            expected_account_id_hash="a" * 64,
            expected_account_type=AccountType.CASH,
        )
        assert ledger.orders == ()
        assert ledger.known_broker_fill_ids == frozenset()
        assert ledger.unresolved_execution_ids == ()
    finally:
        database.close()


def test_data_and_configuration_identity_checks_are_exact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _data_identity_matches(_account(), tmp_path) is False
    account = _account(
        data_hash="d" * 64,
        data_hash_as_of="2026-08-25",
        data_hash_symbols=["sz300308"],
    )
    monkeypatch.setattr(
        composition,
        "_uquant_data_manifest",
        lambda *_args, **_kwargs: SimpleNamespace(
            digest="d" * 64,
            end="2026-08-25",
            symbols=("sz300308",),
        ),
    )
    assert _data_identity_matches(account, tmp_path) is True
    account.data_hash = "e" * 64
    assert _data_identity_matches(account, tmp_path) is False

    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        assert _configuration_identity_matches(database, "c" * 64) is True
        with database.transaction():
            database.write(
                """
                INSERT INTO arm_leases(
                    lease_id, mode, host_hash, account_hash, firmquant_commit, uquant_commit,
                    config_sha256, identity_payload_sha256, issued_at, expires_at,
                    revoked_at, revoke_reason, lease_mac
                ) VALUES ('lease', 'CANARY', ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
                """,
                (
                    "a" * 64,
                    "b" * 64,
                    "f" * 40,
                    "1" * 40,
                    "c" * 64,
                    "d" * 64,
                    NOW.isoformat(),
                    NOW.replace(year=2027).isoformat(),
                    "e" * 64,
                ),
            )
        assert _configuration_identity_matches(database, "c" * 64) is True
        assert _configuration_identity_matches(database, "0" * 64) is False
    finally:
        database.close()


def _mode_config(path: Path, *, mode: Mode, adapter: str) -> None:
    path.write_text(
        f'''schema_version = 1
mode = "{mode.value}"
live_trading_enabled = false
timezone = "Asia/Shanghai"
[broker]
adapter = "{adapter}"
[paths]
state_directory = "state"
data_directory = "data"
report_directory = "reports"
backup_directory = "backups"
[compliance]
program_trading_report_confirmed = false
broker_api_authorized = false
''',
        encoding="utf-8",
    )


def test_configured_gateway_modes_and_cancel_surface_fail_closed(tmp_path: Path) -> None:
    config = tmp_path / "firmquant.toml"
    paper_config(config)
    ports = ConfiguredOperatorPorts(config_path=config, clock=lambda: NOW)
    assert isinstance(compose_operator_ports(config), ConfiguredOperatorPorts)
    for invalid in (["order"], ("order", "order"), ("",), (1,)):
        with pytest.raises(OperatorCommandDenied, match="CANCEL_REQUEST_INVALID"):
            ports.cancel_system_orders(invalid)  # type: ignore[arg-type]
    assert ports.cancel_system_orders(()) == ()
    with pytest.raises(OperatorCommandDenied, match="PAPER_BROKER_REATTACH_REQUIRED"):
        ports.cancel_system_orders(("order",))

    _mode_config(config, mode=Mode.REPLAY, adapter="RECORDED_REPLAY")
    replay_ports = ConfiguredOperatorPorts(config_path=config, clock=lambda: NOW)
    with pytest.raises(OperatorCommandDenied, match="REPLAY_RECORDING_UNAVAILABLE"):
        replay_ports._gateway(replay_ports._settings(), _account())  # type: ignore[attr-defined]
    with pytest.raises(OperatorCommandDenied, match="MODE_NOT_WRITE_CAPABLE"):
        replay_ports.cancel_system_orders(("order",))


def test_report_without_session_or_snapshot_fails_closed(tmp_path: Path) -> None:
    config = tmp_path / "firmquant.toml"
    paper_config(config)
    (tmp_path / "reports").mkdir()
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        ports = ConfiguredOperatorPorts(config_path=config, clock=lambda: NOW)
        with pytest.raises(OperatorCommandDenied, match="REPORT_SESSION_UNAVAILABLE"):
            ports.report(None, database)
        with pytest.raises(OperatorCommandDenied, match="REPORT_BUILD_FAILED"):
            ports.report(date(2026, 8, 25), database)
    finally:
        database.close()
