from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from firmquant.domain.broker_facts import MarketSessionStatus
from firmquant.execution.planner import ExecutionBrokerSnapshot
from firmquant.market_data.validation import (
    Adjustment,
    DataKind,
    DataManifest,
    DataValidationError,
    SeriesSeal,
    StrategyDataValidator,
    validate_execution_facts,
)
from tests.fixtures.session_cases import EXECUTION_SESSION, NOW, execution_snapshot

PREVIOUS_SESSION = date(2026, 8, 24)
FIRST_SESSION = date(2026, 1, 5)


def _seal(**overrides: object) -> SeriesSeal:
    payload: dict[str, object] = {
        "series_id": "sz300308",
        "kind": DataKind.EQUITY,
        "adjustment": Adjustment.FORWARD_ADJUSTED,
        "first_session": FIRST_SESSION,
        "last_session": EXECUTION_SESSION,
        "row_count": 11,
        "full_sha256": "b" * 64,
        "verified_prefix_row_count": 10,
        "verified_prefix_sha256": "a" * 64,
    }
    payload.update(overrides)
    return SeriesSeal(**payload)  # type: ignore[arg-type]


def _manifest(**overrides: object) -> DataManifest:
    payload: dict[str, object] = {
        "latest_common_session": EXECUTION_SESSION,
        "captured_at": NOW,
        "provider": "uquant-data-contract",
        "series": (_seal(),),
    }
    payload.update(overrides)
    return DataManifest(**payload)  # type: ignore[arg-type]


def _previous_manifest() -> DataManifest:
    return DataManifest(
        latest_common_session=PREVIOUS_SESSION,
        captured_at=NOW - timedelta(days=1),
        provider="uquant-data-contract",
        series=(
            _seal(
                last_session=PREVIOUS_SESSION,
                row_count=10,
                full_sha256="a" * 64,
                verified_prefix_row_count=9,
                verified_prefix_sha256="0" * 64,
            ),
        ),
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"series_id": None}, "series id"),
        ({"series_id": ""}, "series id"),
        ({"series_id": " sz300308"}, "series id"),
        ({"kind": "EQUITY"}, "series kind"),
        ({"adjustment": "FORWARD_ADJUSTED"}, "series adjustment"),
        ({"first_session": datetime(2026, 1, 5, tzinfo=UTC)}, "first session"),
        ({"last_session": datetime(2026, 8, 25, tzinfo=UTC)}, "last session"),
        ({"first_session": EXECUTION_SESSION, "last_session": PREVIOUS_SESSION}, "precedes"),
        ({"row_count": True}, "row count"),
        ({"row_count": -1}, "row count"),
        ({"row_count": 0, "verified_prefix_row_count": 0}, "row count must be positive"),
        ({"verified_prefix_row_count": True}, "prefix row count"),
        ({"verified_prefix_row_count": -1}, "prefix row count"),
        ({"verified_prefix_row_count": 11}, "prefix must be shorter"),
        ({"full_sha256": "A" * 64}, "full digest"),
        ({"verified_prefix_sha256": "short"}, "prefix digest"),
    ],
)
def test_series_seal_rejects_noncanonical_or_inconsistent_history(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(DataValidationError, match=message):
        _seal(**overrides)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"latest_common_session": datetime(2026, 8, 25, tzinfo=UTC)}, "common session"),
        ({"captured_at": "2026-08-25T01:31:00Z"}, "must be datetime"),
        ({"captured_at": datetime(2026, 8, 25, 1, 31)}, "timezone-aware"),
        ({"provider": None}, "provider"),
        ({"provider": ""}, "provider"),
        ({"provider": " uquant"}, "provider"),
        ({"series": [_seal()]}, "typed tuple"),
        ({"series": (object(),)}, "typed tuple"),
        ({"series": ()}, "contain series"),
        ({"series": (_seal(), _seal())}, "duplicate series"),
        (
            {"series": (_seal(last_session=PREVIOUS_SESSION),)},
            "latest common session",
        ),
    ],
)
def test_data_manifest_rejects_ambiguous_or_incomplete_evidence(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(DataValidationError, match=message):
        _manifest(**overrides)


def test_manifest_identity_is_independent_of_series_order() -> None:
    equity = _seal()
    index = _seal(
        series_id="000300.SH",
        kind=DataKind.INDEX,
        adjustment=Adjustment.UNADJUSTED,
        full_sha256="c" * 64,
        verified_prefix_sha256="d" * 64,
    )

    assert _manifest(series=(equity, index)).sha256 == _manifest(series=(index, equity)).sha256


@pytest.mark.parametrize("maximum", [1, timedelta(0), timedelta(seconds=-1)])
def test_strategy_data_validator_requires_positive_duration(maximum: object) -> None:
    error = TypeError if not isinstance(maximum, timedelta) else ValueError
    with pytest.raises(error, match="maximum manifest age"):
        StrategyDataValidator(max_manifest_age=maximum)  # type: ignore[arg-type]


def _validate(current: DataManifest, *, previous: DataManifest | None = None) -> None:
    StrategyDataValidator(max_manifest_age=timedelta(minutes=5)).validate(
        previous=_previous_manifest() if previous is None else previous,
        current=current,
        target_session=EXECUTION_SESSION,
        now=NOW + timedelta(minutes=1),
    )


@pytest.mark.parametrize("invalid", [None, object()])
def test_strategy_data_validator_requires_typed_manifests(invalid: object) -> None:
    with pytest.raises(DataValidationError, match="manifests must be typed"):
        StrategyDataValidator(max_manifest_age=timedelta(minutes=5)).validate(
            previous=invalid,  # type: ignore[arg-type]
            current=_manifest(),
            target_session=EXECUTION_SESSION,
            now=NOW,
        )


@pytest.mark.parametrize(
    ("target", "now", "message"),
    [
        (datetime(2026, 8, 25, tzinfo=UTC), NOW, "target session"),
        (EXECUTION_SESSION, "now", "validation time"),
        (EXECUTION_SESSION, datetime(2026, 8, 25, 1, 31), "timezone-aware"),
    ],
)
def test_strategy_data_validator_rejects_untyped_time_boundaries(
    target: object, now: object, message: str
) -> None:
    with pytest.raises(DataValidationError, match=message):
        StrategyDataValidator(max_manifest_age=timedelta(minutes=5)).validate(
            previous=_previous_manifest(),
            current=_manifest(),
            target_session=target,  # type: ignore[arg-type]
            now=now,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("current", "message"),
    [
        (
            _manifest(latest_common_session=PREVIOUS_SESSION, series=(_seal(last_session=PREVIOUS_SESSION),)),
            "latest common",
        ),
        (_manifest(captured_at=NOW + timedelta(minutes=2)), "future"),
        (_manifest(captured_at=NOW - timedelta(minutes=10)), "stale"),
        (_manifest(provider="other-provider"), "provider changed"),
        (_manifest(series=(_seal(series_id="sz300502"),)), "series set changed"),
        (_manifest(series=(_seal(adjustment=Adjustment.UNADJUSTED),)), "adjustment contract"),
        (
            _manifest(series=(_seal(kind=DataKind.INDEX, adjustment=Adjustment.UNADJUSTED),)),
            "series semantics",
        ),
        (_manifest(series=(_seal(first_session=date(2026, 1, 6)),)), "historical start"),
        (
            _manifest(series=(_seal(row_count=10, verified_prefix_row_count=9),)),
            "did not append",
        ),
        (_manifest(series=(_seal(verified_prefix_row_count=9),)), "prefix row count"),
        (_manifest(series=(_seal(verified_prefix_sha256="f" * 64),)), "prefix drift"),
    ],
)
def test_strategy_data_validator_rejects_non_append_only_change(current: DataManifest, message: str) -> None:
    with pytest.raises(DataValidationError, match=message):
        _validate(current)


def _execution_facts(
    *,
    session: date = EXECUTION_SESSION,
    captured_at: datetime = NOW,
    market_status: MarketSessionStatus = MarketSessionStatus.OPEN,
) -> ExecutionBrokerSnapshot:
    base = execution_snapshot()
    instruments = tuple(
        replace(item, session_date=session, observed_at=captured_at) for item in base.instruments
    )
    quotes = tuple(
        replace(
            item,
            session_date=session,
            market_status=market_status,
            event_time=captured_at,
            received_at=captured_at,
        )
        for item in base.quotes
    )
    return ExecutionBrokerSnapshot(
        broker_snapshot=replace(base.broker_snapshot, session_date=session, captured_at=captured_at),
        instruments=instruments,
        quotes=quotes,
        market_status=market_status,
    )


@pytest.mark.parametrize("invalid", [None, object()])
def test_execution_validation_requires_typed_snapshot(invalid: object) -> None:
    with pytest.raises(DataValidationError, match="typed broker snapshot"):
        validate_execution_facts(
            invalid,  # type: ignore[arg-type]
            execution_session=EXECUTION_SESSION,
            now=NOW,
            max_age=timedelta(minutes=1),
        )


@pytest.mark.parametrize(
    ("session", "now", "maximum", "message"),
    [
        (datetime(2026, 8, 25, tzinfo=UTC), NOW, timedelta(minutes=1), "execution session"),
        (EXECUTION_SESSION, "now", timedelta(minutes=1), "validation time"),
        (EXECUTION_SESSION, datetime(2026, 8, 25, 1, 31), timedelta(minutes=1), "timezone-aware"),
        (EXECUTION_SESSION, NOW, 1, "maximum execution fact age"),
        (EXECUTION_SESSION, NOW, timedelta(0), "maximum execution fact age"),
    ],
)
def test_execution_validation_rejects_invalid_time_contracts(
    session: object, now: object, maximum: object, message: str
) -> None:
    with pytest.raises(DataValidationError, match=message):
        validate_execution_facts(
            execution_snapshot(),
            execution_session=session,  # type: ignore[arg-type]
            now=now,  # type: ignore[arg-type]
            max_age=maximum,  # type: ignore[arg-type]
        )


def test_execution_validation_rejects_non_open_market_and_wrong_session() -> None:
    closed = _execution_facts(market_status=MarketSessionStatus.CLOSED)
    with pytest.raises(DataValidationError, match="continuous OPEN"):
        validate_execution_facts(
            closed,
            execution_session=EXECUTION_SESSION,
            now=NOW,
            max_age=timedelta(minutes=1),
        )

    prior = _execution_facts(session=PREVIOUS_SESSION)
    with pytest.raises(DataValidationError, match="snapshot session differs"):
        validate_execution_facts(
            prior,
            execution_session=EXECUTION_SESSION,
            now=NOW,
            max_age=timedelta(minutes=1),
        )


@pytest.mark.parametrize("captured_at", [NOW - timedelta(minutes=2), NOW + timedelta(seconds=1)])
def test_execution_validation_rejects_stale_or_future_snapshot(captured_at: datetime) -> None:
    with pytest.raises(DataValidationError, match="broker snapshot is stale or future-dated"):
        validate_execution_facts(
            _execution_facts(captured_at=captured_at),
            execution_session=EXECUTION_SESSION,
            now=NOW,
            max_age=timedelta(minutes=1),
        )


def test_execution_validation_rejects_missing_or_mismatched_symbol_facts() -> None:
    base = execution_snapshot()
    without_quotes = replace(base, quotes=())
    with pytest.raises(DataValidationError, match="no quotes"):
        validate_execution_facts(
            without_quotes,
            execution_session=EXECUTION_SESSION,
            now=NOW,
            max_age=timedelta(minutes=1),
        )

    mismatched = replace(base, instruments=base.instruments[:1], quotes=base.quotes[1:])
    with pytest.raises(DataValidationError, match="sets differ"):
        validate_execution_facts(
            mismatched,
            execution_session=EXECUTION_SESSION,
            now=NOW,
            max_age=timedelta(minutes=1),
        )


@pytest.mark.parametrize("observed_at", [NOW - timedelta(minutes=2), NOW + timedelta(seconds=1)])
def test_execution_validation_rejects_stale_or_future_instrument(observed_at: datetime) -> None:
    base = execution_snapshot()
    facts = replace(base, instruments=(replace(base.instruments[0], observed_at=observed_at),))
    facts = replace(facts, quotes=(base.quotes[0],))
    with pytest.raises(DataValidationError, match="instrument metadata"):
        validate_execution_facts(
            facts,
            execution_session=EXECUTION_SESSION,
            now=NOW,
            max_age=timedelta(minutes=1),
        )


@pytest.mark.parametrize("field", ["event_time", "received_at"])
@pytest.mark.parametrize("observed_at", [NOW - timedelta(minutes=2), NOW + timedelta(seconds=1)])
def test_execution_validation_rejects_stale_or_future_quote(field: str, observed_at: datetime) -> None:
    base = execution_snapshot()
    quote = replace(base.quotes[0], **{field: observed_at})
    facts = replace(base, instruments=(base.instruments[0],), quotes=(quote,))
    with pytest.raises(DataValidationError, match="quote is stale or future-dated"):
        validate_execution_facts(
            facts,
            execution_session=EXECUTION_SESSION,
            now=NOW,
            max_age=timedelta(minutes=1),
        )


def test_execution_validation_returns_snapshot_identity_and_quote_count() -> None:
    facts = execution_snapshot()

    receipt = validate_execution_facts(
        facts,
        execution_session=EXECUTION_SESSION,
        now=NOW + timedelta(seconds=1),
        max_age=timedelta(minutes=1),
    )

    assert receipt.execution_session == EXECUTION_SESSION
    assert receipt.broker_snapshot_sha256 == facts.sha256
    assert receipt.quote_count == 2
