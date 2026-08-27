from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

import firmquant.market_data.generations as generations
import firmquant.market_data.xtquant_history as history
import firmquant.persistence.backup as backup
from firmquant.config import Mode, Settings, load_settings
from firmquant.domain.broker_facts import InstrumentFact, SecurityStatus, SecurityType
from firmquant.domain.values import Price, Shares, Symbol
from firmquant.market_data.xtquant_daily import DailyDataUpdateError, InstrumentSessionState

NOW = datetime(2026, 8, 25, 8, tzinfo=UTC)
SESSION = date(2026, 8, 25)


def _write_csv(root: Path, name: str = "sz300308.csv", close: str = "10") -> bytes:
    root.mkdir(parents=True, exist_ok=True)
    content = (
        "date,open,high,low,close,volume,amount\n"
        f"2026-08-24,{close},{close},{close},{close},1000,10000\n"
    ).encode()
    (root / name).write_bytes(content)
    return content


def _seed_store(tmp_path: Path) -> tuple[generations.DataGenerationStore, generations.DataGeneration]:
    seed = tmp_path / "seed"
    _write_csv(seed)
    store = generations.DataGenerationStore(tmp_path / "state")
    active = store.ensure_active(seed, source="xtquant", created_at=NOW)
    return store, active


def _candidate(
    store: generations.DataGenerationStore,
    active: generations.DataGeneration,
) -> generations.RewriteCandidate:
    return store.create_candidate(
        active_generation_id=active.generation_id,
        replacement_rows={
            "sz300308": (
                b"date,open,high,low,close,volume,amount\n"
                b"2026-08-24,9,9,9,9,1000,9000\n"
            )
        },
        source="xtquant",
        generated_at=NOW,
    )


def _write_candidate_payload(path: Path, payload: dict[str, object]) -> None:
    unsigned = dict(payload)
    unsigned.pop("candidate_sha256", None)
    payload["candidate_sha256"] = generations._sha256(generations._canonical_bytes(unsigned))
    path.write_bytes(generations._canonical_bytes(payload))


def test_generation_parsers_and_state_root_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(generations.DataGenerationError, match="timezone-aware"):
        generations._iso(datetime(2026, 8, 25, 8))
    for payload, pattern in (
        (b"", "valid UTF-8 CSV"),
        (b"symbol,close\nsz300308,10\n", "date column"),
        (b"date,close\nnot-a-date,10\n", "invalid session"),
        (
            b"date,close\n2026-08-25,10\n2026-08-24,9\n",
            "sorted and unique",
        ),
    ):
        with pytest.raises(generations.DataGenerationError, match=pattern):
            generations._csv_rows(payload)
    with pytest.raises(generations.DataGenerationError, match="at least one CSV"):
        generations._dataset_members(tmp_path)
    with pytest.raises(generations.DataGenerationError, match="symbol is not canonical"):
        generations._symbol_path(tmp_path, "not-a-symbol")

    bare = tmp_path / "bare"
    _write_csv(bare, "300308.csv")
    assert generations._symbol_path(bare, "sz300308").name == "300308.csv"

    state_file = tmp_path / "state-file"
    state_file.write_text("not a directory", encoding="utf-8")
    with pytest.raises(generations.DataGenerationError, match="state root"):
        generations.DataGenerationStore(state_file)


def test_generation_pointer_and_seed_validation_fail_closed(tmp_path: Path) -> None:
    store = generations.DataGenerationStore(tmp_path / "state")
    with pytest.raises(generations.DataGenerationError, match="pointer is missing"):
        store.active()

    store.active_pointer.write_text("{", encoding="utf-8")
    with pytest.raises(generations.DataGenerationError, match="pointer is invalid"):
        store.active()
    store.active_pointer.write_text("{}", encoding="utf-8")
    with pytest.raises(generations.DataGenerationError, match="lacks identity"):
        store.active()
    store.active_pointer.unlink()

    with pytest.raises(generations.DataGenerationError, match="seed data root"):
        store.ensure_active(tmp_path / "missing", source="xtquant", created_at=NOW)
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(generations.DataGenerationError, match="at least one CSV"):
        store.ensure_active(empty, source="xtquant", created_at=NOW)


def test_generation_manifest_and_pointer_tampering_is_detected(tmp_path: Path) -> None:
    store, active = _seed_store(tmp_path)
    manifest_path = active.path / "generation.json"
    original_manifest = manifest_path.read_bytes()
    original_pointer = store.active_pointer.read_bytes()
    payload = json.loads(original_manifest)

    manifest_path.write_text("{", encoding="utf-8")
    with pytest.raises(generations.DataGenerationError, match="manifest is invalid"):
        store._load_generation(active.generation_id)
    manifest_path.write_bytes(original_manifest)

    changed = dict(payload)
    changed["generation_id"] = "gen-" + "f" * 24
    manifest_path.write_bytes(generations._canonical_bytes(changed))
    with pytest.raises(generations.DataGenerationError, match="identity mismatch"):
        store._load_generation(active.generation_id)
    manifest_path.write_bytes(original_manifest)

    changed = dict(payload)
    changed["members"] = {"sz300308.csv": "0" * 64}
    manifest_path.write_bytes(generations._canonical_bytes(changed))
    with pytest.raises(generations.DataGenerationError, match="member digest changed"):
        store._load_generation(active.generation_id)
    manifest_path.write_bytes(original_manifest)

    changed = dict(payload)
    changed["data_sha256"] = "0" * 64
    manifest_path.write_bytes(generations._canonical_bytes(changed))
    with pytest.raises(generations.DataGenerationError, match="generation digest changed"):
        store._load_generation(active.generation_id)
    manifest_path.write_bytes(original_manifest)

    changed = dict(payload)
    changed["created_at"] = "invalid"
    manifest_path.write_bytes(generations._canonical_bytes(changed))
    with pytest.raises(generations.DataGenerationError, match="timestamp is invalid"):
        store._load_generation(active.generation_id)
    manifest_path.write_bytes(original_manifest)

    changed = dict(payload)
    changed["source"] = ""
    manifest_path.write_bytes(generations._canonical_bytes(changed))
    with pytest.raises(generations.DataGenerationError, match="source is invalid"):
        store._load_generation(active.generation_id)
    manifest_path.write_bytes(original_manifest)

    store.active_pointer.write_text("{", encoding="utf-8")
    with pytest.raises(generations.DataGenerationError, match="pointer is invalid"):
        store.active()
    pointer = json.loads(original_pointer)
    pointer["manifest_sha256"] = "0" * 64
    store.active_pointer.write_bytes(generations._canonical_bytes(pointer))
    with pytest.raises(generations.DataGenerationError, match="manifest identity changed"):
        store.active()
    pointer = json.loads(original_pointer)
    pointer["data_sha256"] = "0" * 64
    store.active_pointer.write_bytes(generations._canonical_bytes(pointer))
    with pytest.raises(generations.DataGenerationError, match="digest identity changed"):
        store.active()
    store.active_pointer.write_bytes(original_pointer)
    assert store.active().generation_id == active.generation_id


def test_generation_candidate_validation_and_pending_guards(tmp_path: Path) -> None:
    store, active = _seed_store(tmp_path)
    existing = (active.path / "sz300308.csv").read_bytes()
    with pytest.raises(generations.DataGenerationError, match="requires replacement"):
        store.create_candidate(
            active_generation_id=active.generation_id,
            replacement_rows={},
            source="xtquant",
            generated_at=NOW,
        )
    with pytest.raises(generations.DataGenerationError, match="does not change"):
        store.create_candidate(
            active_generation_id=active.generation_id,
            replacement_rows={"sz300308": existing},
            source="xtquant",
            generated_at=NOW,
        )
    with pytest.raises(generations.DataGenerationError, match="symbol is not canonical"):
        store.create_candidate(
            active_generation_id=active.generation_id,
            replacement_rows={"bad": b"date,close\n2026-08-24,9\n"},
            source="xtquant",
            generated_at=NOW,
        )
    with pytest.raises(generations.DataGenerationError, match="replacements are invalid"):
        store.create_candidate(
            active_generation_id=active.generation_id,
            replacement_rows={"sz300308": "not-bytes"},  # type: ignore[dict-item]
            source="xtquant",
            generated_at=NOW,
        )
    with pytest.raises(generations.DataGenerationError, match="date column"):
        store.create_candidate(
            active_generation_id=active.generation_id,
            replacement_rows={"sz300308": b"symbol,close\nsz300308,9\n"},
            source="xtquant",
            generated_at=NOW,
        )

    candidate = _candidate(store, active)
    duplicate = _candidate(store, active)
    assert duplicate.candidate_id == candidate.candidate_id
    with pytest.raises(generations.DataGenerationError, match="candidate id"):
        store.verify_candidate("bad")
    with pytest.raises(generations.DataGenerationError, match="candidate is missing"):
        store.verify_candidate("candidate-" + "f" * 24)

    manifest = candidate.path / "candidate.json"
    original = manifest.read_bytes()
    payload = json.loads(original)
    manifest.write_text("{", encoding="utf-8")
    with pytest.raises(generations.DataGenerationError, match="manifest is invalid"):
        store.verify_candidate(candidate.candidate_id)
    manifest.write_bytes(original)

    changed = dict(payload)
    changed["source"] = "changed"
    manifest.write_bytes(generations._canonical_bytes(changed))
    with pytest.raises(generations.DataGenerationError, match="manifest changed"):
        store.verify_candidate(candidate.candidate_id)
    manifest.write_bytes(original)

    changed = dict(payload)
    changed["changed_sessions"] = ["invalid"]
    _write_candidate_payload(manifest, changed)
    with pytest.raises(generations.DataGenerationError, match="metadata changed"):
        store.verify_candidate(candidate.candidate_id)
    manifest.write_bytes(original)

    changed = dict(payload)
    changed["changed_symbols"] = "sz300308"
    _write_candidate_payload(manifest, changed)
    with pytest.raises(generations.DataGenerationError, match="symbols changed"):
        store.verify_candidate(candidate.candidate_id)
    manifest.write_bytes(original)

    assert store.recover_pending_promotion() is None
    with pytest.raises(generations.DataGenerationError, match="pending.*unavailable"):
        store._pending_payload()
    store.pending_promotion.write_text("{", encoding="utf-8")
    with pytest.raises(generations.DataGenerationError, match="pending.*invalid"):
        store._pending_payload()
    with pytest.raises(generations.DataGenerationError, match="cannot refresh"):
        store.refresh_active_manifest()
    store.pending_promotion.unlink()

    with pytest.raises(generations.DataGenerationError, match="approval identity changed"):
        store.promote_candidate(
            candidate.candidate_id,
            expected_candidate_sha256="0" * 64,
            promoted_at=NOW,
        )
    store.pending_promotion.write_bytes(
        generations._canonical_bytes(
            {
                "candidate_id": "candidate-" + "f" * 24,
                "candidate_sha256": candidate.candidate_sha256,
            }
        )
    )
    with pytest.raises(generations.DataGenerationError, match="another source promotion"):
        store.promote_candidate(
            candidate.candidate_id,
            expected_candidate_sha256=candidate.candidate_sha256,
            promoted_at=NOW,
        )
    store.pending_promotion.unlink()

    store.pending_promotion.write_bytes(
        generations._canonical_bytes(
            {
                "candidate_id": candidate.candidate_id,
                "candidate_sha256": "0" * 64,
            }
        )
    )
    with pytest.raises(generations.DataGenerationError, match="candidate identity changed"):
        store.recover_pending_promotion()


class _Frame:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def reset_index(self) -> _Frame:
        return self

    def to_dict(self, orient: str) -> object:
        assert orient == "records"
        return self.rows


class _HistoryData:
    def __init__(self, rows: list[dict[str, object]] | None = None) -> None:
        self.rows = rows or [_history_row()]
        self.reader_kwargs: dict[str, object] = {}
        self.return_mapping = True

    def download_history_data(self, **_kwargs: object) -> None:
        return None

    def get_market_data_ex(self, **kwargs: object) -> object:
        self.reader_kwargs = kwargs
        if not self.return_mapping:
            return []
        stock_list = kwargs["stock_list"]
        assert isinstance(stock_list, list)
        return {stock_list[0]: _Frame(self.rows)}


def _history_row(
    *,
    session: object = "20260824",
    volume: object = 100,
) -> dict[str, object]:
    return {
        "time": session,
        "open": 10,
        "high": 10,
        "low": 10,
        "close": 10,
        "volume": volume,
        "amount": 1000,
    }


def _instrument(symbol: Symbol, *, status: SecurityStatus = SecurityStatus.TRADING) -> InstrumentFact:
    return InstrumentFact(
        symbol=symbol,
        security_type=SecurityType.EQUITY,
        status=status,
        trading_unit=Shares(100),
        price_tick=Price(Decimal("0.01")),
        price_precision=2,
        lower_limit=None,
        upper_limit=None,
        session_date=SESSION,
        observed_at=NOW,
    )


def _provider(
    data: _HistoryData | None = None,
    *,
    lookup=None,
) -> history.OfficialXtQuantDailyHistoryProvider:
    return history.OfficialXtQuantDailyHistoryProvider(
        xtdata=data or _HistoryData(),
        volume_multipliers={"SH": 1, "SZ": 1, "BJ": 1},
        instrument_lookup=lookup,
    )


def test_xtquant_history_scalar_parsers_cover_supported_and_invalid_shapes() -> None:
    for value in (True, "bad", "NaN"):
        with pytest.raises(DailyDataUpdateError):
            history._decimal(value, label="value")
    assert history._decimal("1.25", label="value") == Decimal("1.25")

    epoch_ms = int(datetime(2026, 8, 25, tzinfo=UTC).timestamp() * 1000)
    observed = (
        history._session(epoch_ms),
        history._session(datetime(2026, 8, 25, 8)),
        history._session(datetime(2026, 8, 25, 0, tzinfo=UTC)),
        history._session(SESSION),
        history._session("20260825"),
        history._session("2026-08-25"),
        history._session("20260825 15:00:00"),
        history._session("2026-08-25 15:00:00"),
    )
    assert all(item == SESSION for item in observed)
    for value in (True, object(), "not-a-time"):
        with pytest.raises(DailyDataUpdateError):
            history._session(value)

    class NoRecords:
        pass

    class BadRecords:
        def to_dict(self, _orient: str) -> object:
            return ("bad",)

    with pytest.raises(DailyDataUpdateError, match="record conversion"):
        history._records(NoRecords())
    with pytest.raises(DailyDataUpdateError, match="records are malformed"):
        history._records(BadRecords())
    for key in ("time", "date", "index", "timetag"):
        assert history._time_value({key: "20260825"}) == "20260825"
    with pytest.raises(DailyDataUpdateError, match="missing time"):
        history._time_value({"close": 10})


def test_xtquant_provider_constructor_and_fetch_guards(tmp_path: Path) -> None:
    del tmp_path
    with pytest.raises(TypeError, match="history APIs"):
        history.OfficialXtQuantDailyHistoryProvider(
            xtdata=object(),
            volume_multipliers={"SH": 1, "SZ": 1, "BJ": 1},
        )
    with pytest.raises(TypeError, match="must be a mapping"):
        history.OfficialXtQuantDailyHistoryProvider(
            xtdata=_HistoryData(),
            volume_multipliers=[],  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="cover SH/SZ/BJ"):
        history.OfficialXtQuantDailyHistoryProvider(
            xtdata=_HistoryData(),
            volume_multipliers={"SH": 1, "SZ": 1},
        )
    with pytest.raises(TypeError, match="lookup must be callable"):
        history.OfficialXtQuantDailyHistoryProvider(
            xtdata=_HistoryData(),
            volume_multipliers={"SH": 1, "SZ": 1, "BJ": 1},
            instrument_lookup=1,  # type: ignore[arg-type]
        )

    provider = _provider()
    with pytest.raises(TypeError, match="target must be calendar date"):
        provider.fetch(("sz300308",), through=datetime(2026, 8, 25, tzinfo=UTC))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="requires symbols"):
        provider.fetch((), through=SESSION)
    with pytest.raises(ValueError, match="requires symbols"):
        provider.fetch(["sz300308"], through=SESSION)  # type: ignore[arg-type]

    equity_data = _HistoryData()
    equity = _provider(equity_data).fetch(("sz300308",), through=SESSION)
    assert equity["sz300308"][0].volume == 100
    assert equity_data.reader_kwargs["dividend_type"] == "front"
    index_data = _HistoryData()
    _provider(index_data).fetch(("sh000300",), through=SESSION)
    assert index_data.reader_kwargs["dividend_type"] == "none"


def test_xtquant_provider_rejects_missing_empty_fractional_and_duplicate_history() -> None:
    missing = _HistoryData()
    missing.return_mapping = False
    with pytest.raises(DailyDataUpdateError, match="history is unavailable"):
        _provider(missing).fetch(("sz300308",), through=SESSION)

    future = _HistoryData([_history_row(session="20260826")])
    with pytest.raises(DailyDataUpdateError, match="history is empty"):
        _provider(future).fetch(("sz300308",), through=SESSION)

    fractional = _HistoryData([_history_row(volume="0.5")])
    with pytest.raises(DailyDataUpdateError, match="whole shares"):
        _provider(fractional).fetch(("sz300308",), through=SESSION)

    duplicate = _HistoryData([_history_row(), _history_row()])
    with pytest.raises(DailyDataUpdateError, match="duplicate sessions"):
        _provider(duplicate).fetch(("sz300308",), through=SESSION)


def test_xtquant_status_lookup_fail_closed_and_trading_path() -> None:
    provider = _provider()
    with pytest.raises(TypeError, match="status session"):
        provider.fetch_status(("sz300308",), session=datetime(2026, 8, 25, tzinfo=UTC))  # type: ignore[arg-type]
    with pytest.raises(DailyDataUpdateError, match="status lookup is unavailable"):
        provider.fetch_status(("sz300308",), session=SESSION)

    def broken(_symbol: Symbol) -> InstrumentFact:
        raise RuntimeError("lookup failed")

    with pytest.raises(DailyDataUpdateError, match="status is unavailable"):
        _provider(lookup=broken).fetch_status(("sz300308",), session=SESSION)

    with pytest.raises(DailyDataUpdateError, match="identity mismatch"):
        _provider(lookup=lambda _symbol: object()).fetch_status(("sz300308",), session=SESSION)
    with pytest.raises(DailyDataUpdateError, match="identity mismatch"):
        _provider(lookup=lambda _symbol: _instrument(Symbol.parse("sz300502"))).fetch_status(
            ("sz300308",), session=SESSION
        )

    wrong_session = _instrument(Symbol.parse("sz300308"))
    object.__setattr__(wrong_session, "session_date", date(2026, 8, 24))
    with pytest.raises(DailyDataUpdateError, match="session mismatch"):
        _provider(lookup=lambda _symbol: wrong_session).fetch_status(("sz300308",), session=SESSION)

    status = _provider(lookup=lambda symbol: _instrument(symbol)).fetch_status(
        ("sz300308",), session=SESSION
    )["sz300308"]
    assert status.state is InstrumentSessionState.TRADING
    assert status.source == "xtquant-instrument"


def _backup_inputs(tmp_path: Path, *, settings: Settings | None = None) -> backup.BackupBundleInputs:
    tmp_path.mkdir(parents=True, exist_ok=True)
    config = tmp_path / "production.toml"
    config.write_text("", encoding="utf-8")
    observed_settings = load_settings(config) if settings is None else settings
    members = []
    for name in ("safety.json", "calendar.json", "active.json", "strategy.json"):
        path = tmp_path / name
        path.write_text("{}", encoding="utf-8")
        members.append(path)
    return backup.BackupBundleInputs(
        settings=observed_settings,
        config_path=config,
        config_sha256=hashlib.sha256(config.read_bytes()).hexdigest(),
        safety_manifest_path=members[0],
        calendar_manifest_path=members[1],
        active_data_manifest_path=members[2],
        strategy_data_manifest_path=members[3],
        firmquant_commit="a" * 40,
        uquant_commit="b" * 40,
        account_sha256="c" * 64,
        decision_id="decision-test",
        strategy_session=SESSION,
    )


def test_backup_input_and_config_identity_guards(tmp_path: Path) -> None:
    inputs = _backup_inputs(tmp_path)
    assert backup._validated_config_bytes(inputs) == b""

    missing = _backup_inputs(tmp_path / "missing")
    missing.config_path.unlink()
    with pytest.raises(backup.BackupError, match="regular non-symlink"):
        backup._validated_config_bytes(missing)

    invalid = _backup_inputs(tmp_path / "invalid")
    invalid.config_path.write_text("[", encoding="utf-8")
    object.__setattr__(invalid, "config_sha256", hashlib.sha256(invalid.config_path.read_bytes()).hexdigest())
    with pytest.raises(backup.BackupError, match="cannot be validated"):
        backup._validated_config_bytes(invalid)

    mismatch = _backup_inputs(tmp_path / "mismatch", settings=Settings(mode=Mode.SHADOW))
    with pytest.raises(backup.BackupError, match="validated settings changed"):
        backup._validated_config_bytes(mismatch)

    forbidden = _backup_inputs(tmp_path / "forbidden")
    forbidden.config_path.write_text("# password must stay external\n", encoding="utf-8")
    object.__setattr__(
        forbidden,
        "config_sha256",
        hashlib.sha256(forbidden.config_path.read_bytes()).hexdigest(),
    )
    with pytest.raises(backup.BackupError, match="secret material"):
        backup._validated_config_bytes(forbidden)


def test_backup_json_and_manifest_validation_helpers_fail_closed(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    with pytest.raises(backup.BackupVerificationError, match="regular file"):
        backup._sha256_file(missing)

    malformed = tmp_path / "malformed.json"
    malformed.write_text("{", encoding="utf-8")
    with pytest.raises(backup.BackupVerificationError, match="valid UTF-8 JSON"):
        backup._json_object(malformed, label="test")
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"a":1,"a":2}', encoding="utf-8")
    with pytest.raises(backup.BackupVerificationError, match="duplicate key"):
        backup._json_object(duplicate, label="test")
    scalar = tmp_path / "scalar.json"
    scalar.write_text("[]", encoding="utf-8")
    with pytest.raises(backup.BackupVerificationError, match="root must be an object"):
        backup._json_object(scalar, label="test")
    nonstandard = tmp_path / "nan.json"
    nonstandard.write_text('{"value":NaN}', encoding="utf-8")
    with pytest.raises(backup.BackupVerificationError, match="non-standard constant"):
        backup._json_object(nonstandard, label="test")

    with pytest.raises(backup.BackupVerificationError, match="must be an object"):
        backup._mapping([], label="x")
    with pytest.raises(backup.BackupVerificationError, match="must be text"):
        backup._text({}, "x", label="root")
    with pytest.raises(backup.BackupVerificationError, match="must be integer"):
        backup._integer({"x": True}, "x", label="root")

    file_bundle = tmp_path / "bundle"
    file_bundle.write_text("not-dir", encoding="utf-8")
    with pytest.raises(backup.BackupVerificationError, match="regular directory"):
        backup.verify_backup(file_bundle)
