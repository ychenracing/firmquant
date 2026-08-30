from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn

import pytest

from firmquant.execution import replay_runner as runner


def _summary() -> runner.ReplaySummary:
    return runner.ReplaySummary(
        theoretical_uquant_cumulative_return=Decimal("1.2300"),
        firmquant_execution_aware_cumulative_return=Decimal("-0.125"),
        return_gap=Decimal("1.3550"),
        maximum_drawdown=Decimal("0.070"),
        turnover_notional=Decimal("123456.78"),
        turnover_ratio=Decimal("2.50"),
        commissions=Decimal("12.34"),
        stamp_duty=Decimal("5.67"),
        transfer_fee=Decimal("0.89"),
        slippage_cost=Decimal("3.21"),
        unfilled_loss=Decimal("4.56"),
        max_target_tracking_error=Decimal("0.10"),
        mean_target_tracking_error=Decimal("0.020"),
        notional_weighted_target_tracking_error=Decimal("0.0300"),
        planned_orders=11,
        filled_orders=7,
        unfilled_orders=2,
        partial_fill_count=1,
        price_limit_blocks=3,
        suspension_blocks=4,
        incomplete_sell_blocked_buys=5,
        firmquant_commit="a" * 40,
        uquant_commit="b" * 40,
        uquant_config_sha256="c" * 64,
        universe_sha256="d" * 64,
        frozen_data_manifest_sha256="e" * 64,
        input_start=date(2026, 1, 2),
        input_end=date(2026, 8, 7),
    )


def test_replay_summary_payload_preserves_exact_metrics_and_identity_axes() -> None:
    assert _summary().payload() == {
        "schema": "firmquant.execution-aware-replay.v1",
        "theoretical_uquant_cumulative_return": "1.2300",
        "firmquant_execution_aware_cumulative_return": "-0.125",
        "return_gap": "1.3550",
        "maximum_drawdown": "0.070",
        "turnover_notional": "123456.78",
        "turnover_ratio": "2.50",
        "commissions": "12.34",
        "stamp_duty": "5.67",
        "transfer_fee": "0.89",
        "slippage_cost": "3.21",
        "unfilled_loss": "4.56",
        "max_target_tracking_error": "0.10",
        "mean_target_tracking_error": "0.020",
        "notional_weighted_target_tracking_error": "0.0300",
        "planned_orders": 11,
        "filled_orders": 7,
        "unfilled_orders": 2,
        "partial_fill_count": 1,
        "price_limit_blocks": 3,
        "suspension_blocks": 4,
        "incomplete_sell_blocked_buys": 5,
        "identity": {
            "firmquant_commit": "a" * 40,
            "uquant_commit": "b" * 40,
            "uquant_config_sha256": "c" * 64,
            "universe_sha256": "d" * 64,
            "frozen_data_manifest_sha256": "e" * 64,
        },
        "input_date_range": {"start": "2026-01-02", "end": "2026-08-07"},
    }


def test_replay_summary_canonical_json_is_stable_and_compact() -> None:
    expected = (
        '{"commissions":"12.34","filled_orders":7,'
        '"firmquant_execution_aware_cumulative_return":"-0.125",'
        '"identity":{"firmquant_commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        '"frozen_data_manifest_sha256":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",'
        '"universe_sha256":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",'
        '"uquant_commit":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
        '"uquant_config_sha256":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"},'
        '"incomplete_sell_blocked_buys":5,'
        '"input_date_range":{"end":"2026-08-07","start":"2026-01-02"},'
        '"max_target_tracking_error":"0.10","maximum_drawdown":"0.070",'
        '"mean_target_tracking_error":"0.020",'
        '"notional_weighted_target_tracking_error":"0.0300",'
        '"partial_fill_count":1,"planned_orders":11,"price_limit_blocks":3,'
        '"return_gap":"1.3550","schema":"firmquant.execution-aware-replay.v1",'
        '"slippage_cost":"3.21","stamp_duty":"5.67","suspension_blocks":4,'
        '"theoretical_uquant_cumulative_return":"1.2300","transfer_fee":"0.89",'
        '"turnover_notional":"123456.78","turnover_ratio":"2.50",'
        '"unfilled_loss":"4.56","unfilled_orders":2}'
    )

    assert _summary().canonical_json() == expected


def test_engine_verifies_locked_checkout_before_import(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    locked_identity = object()

    monkeypatch.setattr(runner, "load_locked_source_identity", lambda: locked_identity)

    def reject_checkout(identity: object, checkout: Path) -> NoReturn:
        assert identity is locked_identity
        assert checkout == tmp_path / "source"
        raise ValueError("checkout does not match the locked uquant commit")

    monkeypatch.setattr(runner, "verify_uquant_source_checkout", reject_checkout)
    monkeypatch.setattr(
        runner.importlib,
        "import_module",
        lambda _name: pytest.fail("uquant import occurred before checkout verification"),
    )

    with pytest.raises(ValueError, match="locked uquant commit"):
        runner._engine(tmp_path / "source", tmp_path / "data")


@pytest.mark.parametrize("module_file", [None, "/untrusted/uquant/engine.py"])
def test_engine_rejects_module_not_imported_from_verified_checkout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    module_file: str | None,
) -> None:
    monkeypatch.setattr(runner, "load_locked_source_identity", object)
    monkeypatch.setattr(runner, "verify_uquant_source_checkout", lambda *_args: None)
    module = SimpleNamespace(__file__=module_file, ProductionEngine=type("Engine", (), {}))
    monkeypatch.setattr(runner.importlib, "import_module", lambda name: {"uquant.engine": module}[name])

    with pytest.raises(runner.ExecutionReplayError, match="not imported from the verified source checkout"):
        runner._engine(tmp_path / "source", tmp_path / "data")


def test_engine_rejects_missing_production_engine_after_source_binding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    monkeypatch.setattr(runner, "load_locked_source_identity", object)
    monkeypatch.setattr(runner, "verify_uquant_source_checkout", lambda *_args: None)
    module = SimpleNamespace(__file__=str(source / "uquant/engine.py"))
    monkeypatch.setattr(runner.importlib, "import_module", lambda name: {"uquant.engine": module}[name])

    with pytest.raises(runner.ExecutionReplayError, match="ProductionEngine is unavailable"):
        runner._engine(source, tmp_path / "data")


def test_engine_constructs_only_the_source_bound_production_engine(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    data_root = tmp_path / "frozen-data"

    class ProductionEngine:
        def __init__(self, root: Path) -> None:
            self.data_root = root

    module = SimpleNamespace(
        __file__=str(source / "uquant/engine.py"),
        ProductionEngine=ProductionEngine,
    )
    monkeypatch.setattr(runner, "load_locked_source_identity", object)
    monkeypatch.setattr(runner, "verify_uquant_source_checkout", lambda *_args: None)
    monkeypatch.setattr(runner.importlib, "import_module", lambda name: {"uquant.engine": module}[name])

    engine = runner._engine(source, data_root)

    assert isinstance(engine, ProductionEngine)
    assert engine.data_root == data_root


@pytest.mark.parametrize("account_type", [None, SimpleNamespace(empty=lambda _cash: object())])
def test_account_state_requires_public_account_state_type(
    monkeypatch: pytest.MonkeyPatch,
    account_type: object,
) -> None:
    module = SimpleNamespace(AccountState=account_type)
    monkeypatch.setattr(runner.importlib, "import_module", lambda name: {"uquant.types": module}[name])

    with pytest.raises(runner.ExecutionReplayError, match="AccountState is unavailable"):
        runner._account_state(100_000.0)


def test_account_state_requires_public_empty_constructor(monkeypatch: pytest.MonkeyPatch) -> None:
    class AccountState:
        empty = None

    module = SimpleNamespace(AccountState=AccountState)
    monkeypatch.setattr(runner.importlib, "import_module", lambda name: {"uquant.types": module}[name])

    with pytest.raises(runner.ExecutionReplayError, match=r"AccountState\.empty is unavailable"):
        runner._account_state(100_000.0)


def test_account_state_uses_public_empty_constructor(monkeypatch: pytest.MonkeyPatch) -> None:
    account = object()

    class AccountState:
        @staticmethod
        def empty(initial_cash: float) -> object:
            assert initial_cash == 123_456.75
            return account

    module = SimpleNamespace(AccountState=AccountState)
    monkeypatch.setattr(runner.importlib, "import_module", lambda name: {"uquant.types": module}[name])

    assert runner._account_state(123_456.75) is account


def test_account_sha256_requires_public_identity_function(monkeypatch: pytest.MonkeyPatch) -> None:
    module = SimpleNamespace()
    monkeypatch.setattr(runner.importlib, "import_module", lambda name: {"uquant.account": module}[name])

    with pytest.raises(runner.ExecutionReplayError, match="account identity is unavailable"):
        runner._account_sha256(object())


@pytest.mark.parametrize("identity", [None, 64, "f" * 63, "f" * 65])
def test_account_sha256_rejects_malformed_public_identity(
    monkeypatch: pytest.MonkeyPatch,
    identity: object,
) -> None:
    module = SimpleNamespace(economic_state_sha256=lambda _account: identity)
    monkeypatch.setattr(runner.importlib, "import_module", lambda name: {"uquant.account": module}[name])

    with pytest.raises(runner.ExecutionReplayError, match="account identity is malformed"):
        runner._account_sha256(object())


def test_account_sha256_returns_public_economic_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    account = object()
    identity = "1" * 64

    def economic_state_sha256(candidate: object) -> str:
        assert candidate is account
        return identity

    module = SimpleNamespace(economic_state_sha256=economic_state_sha256)
    monkeypatch.setattr(runner.importlib, "import_module", lambda name: {"uquant.account": module}[name])

    assert runner._account_sha256(account) == identity
