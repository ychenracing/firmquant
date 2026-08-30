from __future__ import annotations

import copy
import importlib
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest
from uquant.types import AccountState

from firmquant.execution import replay_runner as runner
from firmquant.execution.execution_replay import ReplayAccount
from tests.fixtures.session_cases import decision_snapshot


def _install_account_persistence(
    monkeypatch: pytest.MonkeyPatch,
    *,
    restored_account: object | None = None,
    mutate_runtime: Callable[[Path], None] | None = None,
) -> None:
    account_module = importlib.import_module("uquant.account")
    stored: list[object] = []

    def save_account(account: object, path: Path) -> None:
        if path.name != "account.json":
            raise AssertionError("restart must persist the account at its dedicated path")
        stored.append(copy.deepcopy(account))
        path.write_text('{"saved":true}', encoding="utf-8")

    def load_account(path: Path) -> object:
        if path.name != "account.json" or path.read_text(encoding="utf-8") != '{"saved":true}':
            raise AssertionError("restart must load the account artifact it just persisted")
        if mutate_runtime is not None:
            mutate_runtime(path.with_name("runtime.json"))
        value = stored[0] if restored_account is None else restored_account
        return copy.deepcopy(value)

    monkeypatch.setattr(account_module, "save_account", save_account)
    monkeypatch.setattr(account_module, "load_account", load_account)


def _restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    replay_account: object | None = None,
    average_costs: dict[str, Decimal] | None = None,
    pending: object = None,
) -> tuple[object, ReplayAccount, dict[str, Decimal], object, object]:
    engine = (tmp_path / "source", tmp_path / "data")
    monkeypatch.setattr(runner, "_engine", lambda _source, _data: engine)
    return runner._restart_roundtrip(
        strategy_account=AccountState.empty(1_000.0),
        replay_account=(
            ReplayAccount(
                cash=Decimal("725.50"),
                positions={"600000.SH": 20},
                sellable={"600000.SH": 10},
            )
            if replay_account is None
            else replay_account
        ),
        average_costs=({"600000.SH": Decimal("13.725")} if average_costs is None else average_costs),
        pending=pending,  # type: ignore[arg-type]
        source_checkout=tmp_path / "source",
        data_root=tmp_path / "data",
    )


def test_decision_restart_payload_preserves_complete_snapshot_or_none() -> None:
    snapshot = decision_snapshot()

    assert runner._decision_restart_payload(None) is None
    assert runner._restore_decision_restart_payload(None) is None

    payload = runner._decision_restart_payload(snapshot)
    assert payload == {
        "strategy_session": snapshot.strategy_session.isoformat(),
        "decision_id": snapshot.decision_id,
        "request_fingerprint": snapshot.request_fingerprint,
        "input_fingerprint": snapshot.input_fingerprint,
        "firmquant_commit": snapshot.firmquant_commit,
        "uquant_commit": snapshot.uquant_commit,
        "uquant_code_fingerprint": snapshot.uquant_code_fingerprint,
        "uquant_config_fingerprint": snapshot.uquant_config_fingerprint,
        "data_manifest_sha256": snapshot.data_manifest_sha256,
        "universe_manifest_sha256": snapshot.universe_manifest_sha256,
        "broker_snapshot_sha256": snapshot.broker_snapshot_sha256,
        "account_before_sha256": snapshot.account_before_sha256,
        "account_after_sha256": snapshot.account_after_sha256,
        "payload_json": snapshot.payload_json,
        "payload_sha256": snapshot.payload_sha256,
        "created_at": snapshot.created_at.isoformat(),
        "supersedes_decision_id": None,
    }
    assert runner._restore_decision_restart_payload(payload) == snapshot


@pytest.mark.parametrize(
    ("mutate", "message"),
    (
        (lambda _payload: [], "payload is malformed"),
        (
            lambda payload: {**payload, "decision_id": ""},
            "field is malformed: decision_id",
        ),
        (
            lambda payload: {**payload, "supersedes_decision_id": 7},
            "supersedes decision id is malformed",
        ),
        (
            lambda payload: {**payload, "strategy_session": "2026-99-99"},
            "payload cannot be decoded",
        ),
        (
            lambda payload: {**payload, "created_at": "not-a-timestamp"},
            "payload cannot be decoded",
        ),
    ),
)
def test_restore_decision_restart_payload_rejects_malformed_evidence(
    mutate: Callable[[dict[str, object]], object],
    message: str,
) -> None:
    payload = runner._decision_restart_payload(decision_snapshot())
    assert payload is not None

    with pytest.raises(runner.ExecutionReplayError, match=message):
        runner._restore_decision_restart_payload(mutate(payload))


def test_restart_share_and_cost_decoders_accept_only_canonical_maps() -> None:
    assert runner._decode_restart_share_map({"600000.SH": 100, "000001.SZ": 0}, label="positions") == {
        "600000.SH": 100,
        "000001.SZ": 0,
    }
    assert runner._decode_restart_cost_map({"600000.SH": "10.2500", "000001.SZ": "0.01"}) == {
        "600000.SH": Decimal("10.2500"),
        "000001.SZ": Decimal("0.01"),
    }


@pytest.mark.parametrize(
    ("value", "message"),
    (
        ([], "payload is malformed"),
        ({1: 100}, "symbol is malformed"),
        ({"": 100}, "symbol is malformed"),
        ({"600000.SH": True}, "shares are malformed"),
        ({"600000.SH": "100"}, "shares are malformed"),
        ({"600000.SH": -1}, "shares are malformed"),
    ),
)
def test_restart_share_decoder_rejects_ambiguous_or_invalid_entries(
    value: object,
    message: str,
) -> None:
    with pytest.raises(runner.ExecutionReplayError, match=message):
        runner._decode_restart_share_map(value, label="positions")


@pytest.mark.parametrize(
    ("value", "message"),
    (
        ([], "payload is malformed"),
        ({1: "10"}, "entry is malformed"),
        ({"": "10"}, "entry is malformed"),
        ({"600000.SH": 10}, "entry is malformed"),
        ({"600000.SH": "not-a-decimal"}, "value is malformed"),
        ({"600000.SH": "NaN"}, "outside its permitted bound"),
        ({"600000.SH": "Infinity"}, "outside its permitted bound"),
        ({"600000.SH": "0"}, "outside its permitted bound"),
        ({"600000.SH": "-0.01"}, "outside its permitted bound"),
    ),
)
def test_restart_cost_decoder_rejects_ambiguous_or_invalid_entries(
    value: object,
    message: str,
) -> None:
    with pytest.raises(runner.ExecutionReplayError, match=message):
        runner._decode_restart_cost_map(value)


@pytest.mark.parametrize("missing_name", ("save_account", "load_account"))
def test_restart_requires_both_locked_account_persistence_functions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    missing_name: str,
) -> None:
    account_module = importlib.import_module("uquant.account")
    monkeypatch.setattr(account_module, missing_name, None)

    with pytest.raises(runner.ExecutionReplayError, match="persistence API is unavailable"):
        _restart(monkeypatch, tmp_path)


def test_restart_roundtrip_restores_economics_runtime_and_pending_decision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_account_persistence(monkeypatch)
    pending = decision_snapshot()

    restored_account, replay, costs, restored_pending, engine = _restart(
        monkeypatch,
        tmp_path,
        pending=pending,
    )

    assert runner._account_sha256(restored_account) == runner._account_sha256(AccountState.empty(1_000.0))
    assert replay == ReplayAccount(
        cash=Decimal("725.50"),
        positions={"600000.SH": 20},
        sellable={"600000.SH": 10},
    )
    assert costs == {"600000.SH": Decimal("13.725")}
    assert restored_pending == pending
    assert engine == (tmp_path / "source", tmp_path / "data")


def test_restart_rejects_account_economic_identity_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_account_persistence(
        monkeypatch,
        restored_account=AccountState.empty(999.0),
    )

    with pytest.raises(runner.ExecutionReplayError, match="economic state changed"):
        _restart(monkeypatch, tmp_path)


def _replace_runtime(raw: str) -> Callable[[Path], None]:
    def replace(path: Path) -> None:
        path.write_text(raw, encoding="utf-8")

    return replace


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (lambda path: path.unlink(), "missing or corrupt"),
        (_replace_runtime("{"), "missing or corrupt"),
        (_replace_runtime("[]"), "must be an object"),
        (
            _replace_runtime('{"schema":"firmquant.execution-replay-restart.v0"}'),
            "schema mismatch",
        ),
        (
            _replace_runtime(
                '{"average_costs":{"600000.SH":"13.725"},"cash":"725.50",'
                '"extra":true,"pending":null,"positions":{"600000.SH":20},'
                '"schema":"firmquant.execution-replay-restart.v1",'
                '"sellable":{"600000.SH":10}}'
            ),
            "is not canonical",
        ),
    ),
)
def test_restart_rejects_corrupt_replaced_or_noncanonical_runtime_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutation: Callable[[Path], None],
    message: str,
) -> None:
    _install_account_persistence(monkeypatch, mutate_runtime=mutation)

    with pytest.raises(runner.ExecutionReplayError, match=message):
        _restart(monkeypatch, tmp_path)


@pytest.mark.parametrize(
    ("encoded_cash", "message"),
    (
        (7, "cash payload is malformed"),
        ("not-a-decimal", "cash payload cannot be decoded"),
    ),
)
def test_restart_rejects_corrupt_cash_from_runtime_encoder_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    encoded_cash: object,
    message: str,
) -> None:
    _install_account_persistence(monkeypatch)
    real_decimal_text = runner._decimal_text

    def corrupt_cash(value: Decimal) -> str:
        if value == Decimal("725.50"):
            return encoded_cash  # type: ignore[return-value]
        return real_decimal_text(value)

    monkeypatch.setattr(runner, "_decimal_text", corrupt_cash)

    with pytest.raises(runner.ExecutionReplayError, match=message):
        _restart(monkeypatch, tmp_path)


@pytest.mark.parametrize("cost", (Decimal("NaN"), Decimal("0"), Decimal("-0.01")))
def test_restart_rejects_invalid_average_costs_after_persistence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    cost: Decimal,
) -> None:
    _install_account_persistence(monkeypatch)

    with pytest.raises(runner.ExecutionReplayError, match="outside its permitted bound"):
        _restart(monkeypatch, tmp_path, average_costs={"600000.SH": cost})


@pytest.mark.parametrize(
    ("equity", "expected"),
    (
        ([], Decimal("0")),
        ([Decimal("100"), Decimal("110"), Decimal("120")], Decimal("0")),
        ([Decimal("100"), Decimal("80"), Decimal("120")], Decimal("0.2")),
        ([Decimal("0"), Decimal("100"), Decimal("50"), Decimal("75")], Decimal("0.5")),
    ),
)
def test_max_drawdown_tracks_the_high_water_mark(
    equity: list[Decimal],
    expected: Decimal,
) -> None:
    assert runner._max_drawdown(equity) == expected
