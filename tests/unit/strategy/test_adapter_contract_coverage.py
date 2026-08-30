from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, tzinfo
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import firmquant.strategy.adapter as adapter_module
from firmquant.build_identity import SourceIdentityError
from firmquant.strategy.adapter import (
    DecisionConflict,
    DecisionRecoveryRequired,
    DecisionRequest,
    StrategyAdapter,
    StrategyAdapterError,
)
from firmquant.strategy.identity import StrategyIdentityViolation

SESSION = date(2026, 6, 30)
CREATED_AT = datetime(2026, 6, 30, 9, tzinfo=UTC)


def _request(**changes: object) -> DecisionRequest:
    values: dict[str, Any] = {
        "strategy_session": SESSION,
        "symbols": ("300308.SZ",),
        "account": object(),
        "firmquant_commit": "f" * 40,
        "data_manifest_sha256": "d" * 64,
        "broker_snapshot_sha256": "b" * 64,
        "created_at": CREATED_AT,
    }
    values.update(changes)
    return DecisionRequest(**values)


class _NoneOffset(tzinfo):
    def utcoffset(self, dt: datetime | None) -> None:
        return None

    def dst(self, dt: datetime | None) -> None:
        return None


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"strategy_session": datetime(2026, 6, 30, tzinfo=UTC)}, "calendar date"),
        ({"symbols": ["300308.SZ"]}, "non-empty tuple"),
        ({"symbols": ()}, "non-empty tuple"),
        ({"symbols": ("",)}, "non-empty text"),
        ({"symbols": (300308,)}, "non-empty text"),
        ({"firmquant_commit": "F" * 40}, "firmquant commit"),
        ({"firmquant_commit": "f" * 64}, "firmquant commit"),
        ({"data_manifest_sha256": "D" * 64}, "data manifest"),
        ({"broker_snapshot_sha256": "b" * 63}, "broker snapshot"),
        ({"created_at": "2026-06-30T09:00:00Z"}, "timezone-aware"),
        ({"created_at": datetime(2026, 6, 30, 9)}, "timezone-aware"),
        (
            {"created_at": datetime(2026, 6, 30, 9, tzinfo=_NoneOffset())},
            "timezone-aware",
        ),
    ],
)
def test_decision_request_rejects_every_noncanonical_input_axis(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(StrategyAdapterError, match=message):
        _request(**changes)


def test_decision_request_accepts_exact_strict_contract() -> None:
    request = _request()

    assert request.strategy_session == SESSION
    assert request.symbols == ("300308.SZ",)
    assert request.created_at == CREATED_AT


@pytest.mark.parametrize("failure", [TypeError("bad account"), ValueError("bad value")])
def test_account_sha256_translates_public_uquant_contract_failures(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    def failed(_account: object) -> str:
        raise failure

    monkeypatch.setattr(
        adapter_module.importlib,
        "import_module",
        lambda _name: SimpleNamespace(economic_state_sha256=failed),
    )

    with pytest.raises(StrategyAdapterError, match="economic account identity failed"):
        adapter_module._account_sha256(object())


@pytest.mark.parametrize("result", [None, "A" * 64, "a" * 63])
def test_account_sha256_requires_a_callable_returning_a_canonical_digest(
    monkeypatch: pytest.MonkeyPatch, result: object
) -> None:
    provider = result if result is None else lambda _account: result
    monkeypatch.setattr(
        adapter_module.importlib,
        "import_module",
        lambda _name: SimpleNamespace(economic_state_sha256=provider),
    )

    message = "unavailable" if result is None else "malformed"
    with pytest.raises(StrategyAdapterError, match=message):
        adapter_module._account_sha256(object())


def test_account_and_config_helpers_return_exact_public_uquant_identities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = object()
    config = object()

    def public_module(name: str) -> object:
        if name == "uquant.account":
            return SimpleNamespace(
                economic_state_sha256=lambda observed: "a" * 64 if observed is account else ""
            )
        assert name == "uquant.config"
        return SimpleNamespace(config_fingerprint=lambda observed: "c" * 64 if observed is config else "")

    monkeypatch.setattr(adapter_module.importlib, "import_module", public_module)

    assert adapter_module._account_sha256(account) == "a" * 64
    assert adapter_module._config_fingerprint(config) == "c" * 64


@pytest.mark.parametrize("provider", [None, lambda _config: "C" * 64, lambda _config: 7])
def test_config_fingerprint_requires_a_callable_returning_a_canonical_digest(
    monkeypatch: pytest.MonkeyPatch, provider: object
) -> None:
    monkeypatch.setattr(
        adapter_module.importlib,
        "import_module",
        lambda _name: SimpleNamespace(config_fingerprint=provider),
    )

    message = "unavailable" if provider is None else "malformed"
    with pytest.raises(StrategyAdapterError, match=message):
        adapter_module._config_fingerprint(object())


def test_canonical_sha256_is_order_independent_and_utf8_exact() -> None:
    payload = {"证券": "深市", "nested": {"b": 2, "a": 1}}
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")

    assert adapter_module._canonical_sha256(payload) == hashlib.sha256(encoded).hexdigest()
    assert (
        adapter_module._canonical_sha256(dict(reversed(payload.items())))
        == hashlib.sha256(encoded).hexdigest()
    )


@pytest.mark.parametrize("payload", [{"bad": object()}, {"bad": float("nan")}])
def test_canonical_sha256_rejects_non_json_and_nonfinite_values(payload: dict[str, object]) -> None:
    with pytest.raises(StrategyAdapterError, match="not canonical JSON"):
        adapter_module._canonical_sha256(payload)


class _Policy:
    manifest_sha256 = "u" * 64

    def __init__(self, *, denied: frozenset[str] = frozenset()) -> None:
        self.denied = denied

    def allowed(self, symbol: str, session: date) -> bool:
        assert session == SESSION
        return symbol not in self.denied


def test_symbol_normalization_canonicalizes_sorts_and_deduplicates() -> None:
    observed = adapter_module._normalized_symbols(
        ("300308.SZ", "SH600000", "sz300308"),
        session=SESSION,
        policy=_Policy(),  # type: ignore[arg-type]
    )

    assert observed == ("sh600000", "sz300308")


@pytest.mark.parametrize(
    ("symbols", "policy", "message"),
    [
        (("US.AAPL",), _Policy(), "invalid decision symbol"),
        (("300308.SZ",), _Policy(denied=frozenset({"sz300308"})), "outside deployment"),
        ((), _Policy(), "empty after normalization"),
    ],
)
def test_symbol_normalization_fails_closed_on_invalid_denied_or_empty_universe(
    symbols: tuple[str, ...], policy: _Policy, message: str
) -> None:
    with pytest.raises(StrategyAdapterError, match=message):
        adapter_module._normalized_symbols(
            symbols,
            session=SESSION,
            policy=policy,  # type: ignore[arg-type]
        )


def _identity(**changes: object) -> SimpleNamespace:
    values = {
        "uquant_commit": "1" * 40,
        "economic_code_fingerprint": "2" * 64,
        "config_fingerprint": "3" * 64,
        "canonical_universe_sha256": "4" * 64,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def test_decision_fingerprints_bind_request_context_and_account_prestate() -> None:
    identity = _identity()
    first = StrategyAdapter._fingerprints(
        _request(),
        symbols=("sz300308",),
        identity=identity,  # type: ignore[arg-type]
        account_before_sha256="a" * 64,
    )
    reordered_source = StrategyAdapter._fingerprints(
        _request(symbols=("sz300308",)),
        symbols=("sz300308",),
        identity=identity,  # type: ignore[arg-type]
        account_before_sha256="a" * 64,
    )
    changed_account = StrategyAdapter._fingerprints(
        _request(),
        symbols=("sz300308",),
        identity=identity,  # type: ignore[arg-type]
        account_before_sha256="b" * 64,
    )

    assert first == reordered_source
    assert first[0] == changed_account[0]
    assert first[1] != changed_account[1]
    assert all(len(item) == 64 for item in first)


class _Snapshots:
    def __init__(self, *, exact: object | None, existing: tuple[object, ...]) -> None:
        self.exact = exact
        self.existing = existing

    def find_by_input(self, **_kwargs: object) -> object | None:
        return self.exact

    def for_session(self, _session: date) -> tuple[object, ...]:
        return self.existing


def _adapter_with_snapshots(*, exact: object | None, existing: tuple[object, ...]) -> StrategyAdapter:
    instance = object.__new__(StrategyAdapter)
    instance._snapshots = _Snapshots(exact=exact, existing=existing)  # type: ignore[assignment]
    return instance


def _candidate(*, request_fingerprint: str = "r" * 64) -> SimpleNamespace:
    return SimpleNamespace(
        decision_id="decision_" + "d" * 64,
        request_fingerprint=request_fingerprint,
        account_before_sha256="a" * 64,
        account_after_sha256="b" * 64,
    )


def _existing(adapter: StrategyAdapter, account_sha256: str) -> object | None:
    return adapter._existing_or_conflict(
        _request(),
        request_fingerprint="r" * 64,
        input_fingerprint="i" * 64,
        account_sha256=account_sha256,
    )


def test_existing_decision_exact_poststate_returns_immutable_snapshot() -> None:
    exact = _candidate()
    adapter = _adapter_with_snapshots(exact=exact, existing=())

    assert _existing(adapter, "b" * 64) is exact


def test_existing_decision_request_match_falls_back_when_input_fingerprint_changed() -> None:
    candidate = _candidate()
    unrelated = _candidate(request_fingerprint="x" * 64)
    adapter = _adapter_with_snapshots(exact=None, existing=(unrelated, candidate))

    assert _existing(adapter, "b" * 64) is candidate


@pytest.mark.parametrize(
    ("account_sha256", "message"),
    [
        ("a" * 64, "not durably advanced"),
        ("c" * 64, "neither decision pre-state nor post-state"),
    ],
)
def test_existing_decision_requires_recovery_for_before_or_unknown_account_state(
    account_sha256: str, message: str
) -> None:
    adapter = _adapter_with_snapshots(exact=_candidate(), existing=())

    with pytest.raises(DecisionRecoveryRequired, match=message):
        _existing(adapter, account_sha256)


def test_existing_session_with_different_request_records_conflict_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = (_candidate(request_fingerprint="x" * 64),)
    adapter = _adapter_with_snapshots(exact=None, existing=existing)
    recorded: list[tuple[object, ...]] = []
    monkeypatch.setattr(adapter, "_record_conflict", lambda *args, **kwargs: recorded.append((*args, kwargs)))

    with pytest.raises(DecisionConflict, match="immutable decision"):
        _existing(adapter, "a" * 64)

    assert len(recorded) == 1
    assert recorded[0][-1]["existing"] == existing


def test_no_existing_session_returns_none_without_recording_conflict() -> None:
    adapter = _adapter_with_snapshots(exact=None, existing=())

    assert _existing(adapter, "a" * 64) is None


class _Engine:
    __module__ = "verified_uquant_engine"

    def __init__(self) -> None:
        self.cfg = object()


class _VerifiableIdentity(SimpleNamespace):
    def verify(self) -> None:
        failure = getattr(self, "verify_failure", None)
        if failure is not None:
            raise failure


def _verified_adapter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    identity_changes: dict[str, object] | None = None,
    engine_module_file: object | None = None,
    observed_config: str | None = None,
    observed_universe: str | None = None,
    checkout_failure: Exception | None = None,
) -> tuple[StrategyAdapter, _VerifiableIdentity]:
    identity = _VerifiableIdentity(**vars(_identity()))
    for key, value in (identity_changes or {}).items():
        setattr(identity, key, value)
    expected_engine = tmp_path / "uquant" / "engine.py"
    module_file = str(expected_engine) if engine_module_file is None else engine_module_file
    config_sha = identity.config_fingerprint if observed_config is None else observed_config

    def import_module(name: str) -> object:
        if name == "verified_uquant_engine":
            return SimpleNamespace(__file__=module_file)
        if name == "uquant.config":
            return SimpleNamespace(config_fingerprint=lambda _config: config_sha)
        raise AssertionError(name)

    def verify_checkout(_source: object, _checkout: Path) -> None:
        if checkout_failure is not None:
            raise checkout_failure

    monkeypatch.setattr(adapter_module, "StrategyIdentity", SimpleNamespace(locked=lambda: identity))
    monkeypatch.setattr(adapter_module, "load_locked_source_identity", lambda: object())
    monkeypatch.setattr(adapter_module, "verify_uquant_source_checkout", verify_checkout)
    monkeypatch.setattr(adapter_module.importlib, "import_module", import_module)

    instance = object.__new__(StrategyAdapter)
    instance._engine = _Engine()  # type: ignore[assignment]
    instance._source_checkout = tmp_path
    instance._universe_policy = SimpleNamespace(
        manifest_sha256=(
            identity.canonical_universe_sha256 if observed_universe is None else observed_universe
        )
    )  # type: ignore[assignment]
    return instance, identity


def test_verified_identity_accepts_only_exact_source_engine_config_and_universe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    adapter, identity = _verified_adapter(monkeypatch, tmp_path)

    assert adapter._verified_identity() is identity


@pytest.mark.parametrize(
    ("axis", "message"),
    [
        ("identity", "source identity is not verified"),
        ("checkout", "source identity is not verified"),
        ("module-missing", "not loaded from the verified checkout"),
        ("module-wrong", "not loaded from the verified checkout"),
        ("config", "config differs from locked"),
        ("universe", "universe identity differs from locked"),
    ],
)
def test_verified_identity_fails_closed_on_each_independent_identity_axis(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, axis: str, message: str
) -> None:
    identity_changes: dict[str, object] = {}
    engine_module_file: object | None = None
    observed_config: str | None = None
    observed_universe: str | None = None
    checkout_failure: Exception | None = None
    if axis == "identity":
        identity_changes["verify_failure"] = StrategyIdentityViolation("drift")
    elif axis == "checkout":
        checkout_failure = SourceIdentityError("checkout drift")
    elif axis == "module-missing":
        engine_module_file = 7
    elif axis == "module-wrong":
        engine_module_file = str(tmp_path / "installed" / "engine.py")
    elif axis == "config":
        observed_config = "9" * 64
    elif axis == "universe":
        observed_universe = "8" * 64

    adapter, _identity_value = _verified_adapter(
        monkeypatch,
        tmp_path,
        identity_changes=identity_changes,
        engine_module_file=engine_module_file,
        observed_config=observed_config,
        observed_universe=observed_universe,
        checkout_failure=checkout_failure,
    )

    with pytest.raises(StrategyAdapterError, match=message):
        adapter._verified_identity()
