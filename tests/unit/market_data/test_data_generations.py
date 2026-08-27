# ruff: noqa: I001
from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from firmquant.market_data.generations import DataGenerationError, DataGenerationStore


NOW = datetime(2026, 8, 25, 8, tzinfo=UTC)


def write_csv(root: Path, symbol: str, rows: tuple[tuple[str, str], ...]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    text = "date,open,high,low,close,volume,amount\n"
    for session, close in rows:
        text += f"{session},{close},{close},{close},{close},1000,10000\n"
    (root / f"{symbol}.csv").write_text(text, encoding="utf-8")


def candidate_store(tmp_path: Path) -> tuple[DataGenerationStore, str, str]:
    seed = tmp_path / "seed"
    write_csv(seed, "sz300308", (("2026-08-24", "10"),))
    store = DataGenerationStore(tmp_path / "state")
    active = store.ensure_active(seed, source="xtquant", created_at=NOW)
    candidate = store.create_candidate(
        active_generation_id=active.generation_id,
        replacement_rows={
            "sz300308": b"date,open,high,low,close,volume,amount\n2026-08-24,9,9,9,9,1000,9000\n"
        },
        source="xtquant",
        generated_at=NOW,
    )
    return store, active.generation_id, candidate.candidate_id


def test_history_rewrite_candidate_never_overwrites_active_generation(tmp_path: Path) -> None:
    seed = tmp_path / "seed"
    state = tmp_path / "state"
    write_csv(seed, "sz300308", (("2026-08-24", "10"),))
    store = DataGenerationStore(state)
    active = store.ensure_active(seed, source="xtquant", created_at=NOW)
    before = (active.path / "sz300308.csv").read_bytes()

    candidate = store.create_candidate(
        active_generation_id=active.generation_id,
        replacement_rows={
            "sz300308": (
                b"date,open,high,low,close,volume,amount\n"
                b"2026-08-24,9.5,9.5,9.5,9.5,1000,9500\n"
                b"2026-08-25,11,11,11,11,1000,11000\n"
            )
        },
        source="xtquant",
        generated_at=NOW,
    )

    assert candidate.changed_symbols == ("sz300308",)
    assert candidate.changed_sessions == (date(2026, 8, 24),)
    assert candidate.first_difference_session == date(2026, 8, 24)
    assert candidate.old_digest != candidate.new_digest
    assert (active.path / "sz300308.csv").read_bytes() == before


def test_tampered_candidate_cannot_be_verified_or_promoted(tmp_path: Path) -> None:
    seed = tmp_path / "seed"
    write_csv(seed, "sz300308", (("2026-08-24", "10"),))
    store = DataGenerationStore(tmp_path / "state")
    active = store.ensure_active(seed, source="xtquant", created_at=NOW)
    candidate = store.create_candidate(
        active_generation_id=active.generation_id,
        replacement_rows={
            "sz300308": b"date,open,high,low,close,volume,amount\n2026-08-24,9,9,9,9,1000,9000\n"
        },
        source="xtquant",
        generated_at=NOW,
    )
    (candidate.path / "sz300308.csv").write_text("tampered\n", encoding="utf-8")

    with pytest.raises(DataGenerationError, match="changed"):
        store.verify_candidate(candidate.candidate_id)
    with pytest.raises(DataGenerationError):
        store.promote_candidate(
            candidate.candidate_id,
            expected_candidate_sha256=candidate.candidate_sha256,
            promoted_at=NOW,
        )


def test_promotion_is_atomic_and_keeps_previous_generation_for_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, original, candidate_id = candidate_store(tmp_path)
    candidate = store.verify_candidate(candidate_id)
    real_replace = store._replace_active_pointer

    def crash(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated promotion crash")

    monkeypatch.setattr(store, "_replace_active_pointer", crash)
    with pytest.raises(OSError, match="promotion crash"):
        store.promote_candidate(
            candidate.candidate_id,
            expected_candidate_sha256=candidate.candidate_sha256,
            promoted_at=NOW,
        )
    assert store.active().generation_id == original

    monkeypatch.setattr(store, "_replace_active_pointer", real_replace)
    promoted = store.promote_candidate(
        candidate.candidate_id,
        expected_candidate_sha256=candidate.candidate_sha256,
        promoted_at=NOW,
    )
    assert promoted.generation_id != original
    assert (store.generations_root / original).is_dir()


def test_promotion_recovers_after_pointer_switch_before_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, original, candidate_id = candidate_store(tmp_path)
    candidate = store.verify_candidate(candidate_id)
    real_replace = store._replace_active_pointer
    switched: list[str] = []

    def switch_then_crash(generation) -> None:
        real_replace(generation)
        switched.append(generation.generation_id)
        raise OSError("simulated post-pointer crash")

    monkeypatch.setattr(store, "_replace_active_pointer", switch_then_crash)
    with pytest.raises(OSError, match="post-pointer crash"):
        store.promote_candidate(
            candidate.candidate_id,
            expected_candidate_sha256=candidate.candidate_sha256,
            promoted_at=NOW,
        )
    assert switched
    assert store.active().generation_id != original
    assert store.pending_promotion.is_file()

    monkeypatch.setattr(store, "_replace_active_pointer", real_replace)
    recovered = store.recover_pending_promotion()
    assert recovered is not None
    assert recovered.generation_id == store.active().generation_id
    assert not store.pending_promotion.exists()
    assert (store.promotions_root / f"{candidate.candidate_id}.json").is_file()
