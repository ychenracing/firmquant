"""Crash-safe market-data generations and operator-reviewed rewrite candidates."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

_GENERATION_ID = re.compile(r"^gen-[0-9a-f]{24}$")
_CANDIDATE_ID = re.compile(r"^candidate-[0-9a-f]{24}$")


class DataGenerationError(RuntimeError):
    """Market-data generation identity or candidate integrity is invalid."""


def _canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha256(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise DataGenerationError("data generation member must be a regular file")
    return _sha256(path.read_bytes())


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DataGenerationError("generation timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".new")
    temporary.write_bytes(_canonical_bytes(payload))
    os.replace(temporary, path)


def _csv_rows(data: bytes) -> tuple[tuple[date, tuple[str, ...]], ...]:
    try:
        text = data.decode("utf-8")
        reader = csv.reader(text.splitlines())
        header = next(reader)
    except (UnicodeDecodeError, StopIteration, csv.Error) as error:
        raise DataGenerationError("candidate CSV is not valid UTF-8 CSV") from error
    if not header or header[0] != "date":
        raise DataGenerationError("candidate CSV must begin with date column")
    rows: list[tuple[date, tuple[str, ...]]] = []
    try:
        for row in reader:
            if not row:
                continue
            rows.append((date.fromisoformat(row[0]), tuple(row)))
    except ValueError as error:
        raise DataGenerationError("candidate CSV contains an invalid session") from error
    sessions = tuple(item[0] for item in rows)
    if sessions != tuple(sorted(set(sessions))):
        raise DataGenerationError("candidate CSV sessions must be sorted and unique")
    return tuple(rows)


def _dataset_members(root: Path) -> dict[str, str]:
    members: dict[str, str] = {}
    for path in sorted(root.glob("*.csv")):
        if path.is_symlink() or not path.is_file():
            raise DataGenerationError("generation contains a non-regular CSV member")
        members[path.name] = _file_sha256(path)
    if not members:
        raise DataGenerationError("generation requires at least one CSV member")
    return members


def _dataset_digest(members: Mapping[str, str]) -> str:
    return _sha256(_canonical_bytes(dict(sorted(members.items()))))


def _symbol_path(root: Path, symbol: str) -> Path:
    canonical = symbol.strip().lower().replace(".", "")
    if len(canonical) != 8 or canonical[:2] not in {"sh", "sz", "bj"} or not canonical[2:].isdigit():
        raise DataGenerationError("rewrite candidate symbol is not canonical")
    prefixed = root / f"{canonical}.csv"
    bare = root / f"{canonical[2:]}.csv"
    if prefixed.is_file():
        return prefixed
    if bare.is_file():
        return bare
    return prefixed


@dataclass(frozen=True, slots=True)
class DataGeneration:
    generation_id: str
    path: Path
    source: str
    created_at: datetime
    manifest_sha256: str
    data_sha256: str


@dataclass(frozen=True, slots=True)
class RewriteCandidate:
    candidate_id: str
    path: Path
    active_generation_id: str
    changed_symbols: tuple[str, ...]
    changed_sessions: tuple[date, ...]
    first_difference_session: date | None
    old_digest: str
    new_digest: str
    old_row_count: int
    new_row_count: int
    source: str
    generated_at: datetime
    candidate_sha256: str


class DataGenerationStore:
    """Owns reviewed generations, isolated rewrite candidates, and one atomic active pointer."""

    def __init__(self, state_root: Path) -> None:
        state = Path(state_root)
        if state.exists() and (state.is_symlink() or not state.is_dir()):
            raise DataGenerationError("data generation state root must be a regular directory")
        self.root = state / "market-data"
        self.generations_root = self.root / "generations"
        self.candidates_root = self.root / "candidates"
        self.promotions_root = self.root / "promotions"
        self.active_pointer = self.root / "active.json"
        self.pending_promotion = self.root / "pending-promotion.json"
        for directory in (self.generations_root, self.candidates_root, self.promotions_root):
            directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _require_generation_id(generation_id: str) -> str:
        if not isinstance(generation_id, str) or _GENERATION_ID.fullmatch(generation_id) is None:
            raise DataGenerationError("data generation id is not canonical")
        return generation_id

    @staticmethod
    def _require_candidate_id(candidate_id: str) -> str:
        if not isinstance(candidate_id, str) or _CANDIDATE_ID.fullmatch(candidate_id) is None:
            raise DataGenerationError("rewrite candidate id is not canonical")
        return candidate_id

    def _generation_payload(
        self,
        *,
        generation_id: str,
        source: str,
        created_at: datetime,
        members: Mapping[str, str],
    ) -> dict[str, Any]:
        return {
            "schema": "firmquant.data-generation.v1",
            "generation_id": self._require_generation_id(generation_id),
            "source": source,
            "created_at": _iso(created_at),
            "members": dict(sorted(members.items())),
            "data_sha256": _dataset_digest(members),
        }

    def _load_generation(self, generation_id: str) -> DataGeneration:
        canonical_id = self._require_generation_id(generation_id)
        path = self.generations_root / canonical_id
        manifest_path = path / "generation.json"
        if path.is_symlink() or not path.is_dir() or not manifest_path.is_file():
            raise DataGenerationError("active data generation is missing")
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DataGenerationError("data generation manifest is invalid") from error
        if not isinstance(payload, dict) or payload.get("generation_id") != canonical_id:
            raise DataGenerationError("data generation identity mismatch")
        members = _dataset_members(path)
        if payload.get("members") != members:
            raise DataGenerationError("data generation member digest changed")
        if payload.get("data_sha256") != _dataset_digest(members):
            raise DataGenerationError("data generation digest changed")
        try:
            created_at = datetime.fromisoformat(str(payload["created_at"]))
        except (KeyError, ValueError) as error:
            raise DataGenerationError("data generation timestamp is invalid") from error
        source = payload.get("source")
        if not isinstance(source, str) or not source or source != source.strip():
            raise DataGenerationError("data generation source is invalid")
        return DataGeneration(
            generation_id=canonical_id,
            path=path,
            source=source,
            created_at=created_at,
            manifest_sha256=_file_sha256(manifest_path),
            data_sha256=str(payload["data_sha256"]),
        )

    def active(self) -> DataGeneration:
        if not self.active_pointer.is_file() or self.active_pointer.is_symlink():
            raise DataGenerationError("active data generation pointer is missing")
        try:
            payload = json.loads(self.active_pointer.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DataGenerationError("active data generation pointer is invalid") from error
        generation_id = payload.get("generation_id") if isinstance(payload, dict) else None
        if not isinstance(generation_id, str):
            raise DataGenerationError("active data generation pointer lacks identity")
        generation = self._load_generation(generation_id)
        if payload.get("manifest_sha256") != generation.manifest_sha256:
            raise DataGenerationError("active data generation manifest identity changed")
        if payload.get("data_sha256") != generation.data_sha256:
            raise DataGenerationError("active data generation digest identity changed")
        return generation

    def _replace_active_pointer(self, generation: DataGeneration) -> None:
        _atomic_json(
            self.active_pointer,
            {
                "schema": "firmquant.active-data-generation.v1",
                "generation_id": generation.generation_id,
                "manifest_sha256": generation.manifest_sha256,
                "data_sha256": generation.data_sha256,
                "source": generation.source,
            },
        )

    def ensure_active(self, seed_root: Path, *, source: str, created_at: datetime) -> DataGeneration:
        if self.pending_promotion.exists() and self.active_pointer.exists():
            self.recover_pending_promotion()
        if self.active_pointer.exists():
            return self.active()
        seed = Path(seed_root)
        if seed.is_symlink() or not seed.is_dir():
            raise DataGenerationError("seed data root must be an existing regular directory")
        with tempfile.TemporaryDirectory(prefix="generation-", dir=self.root) as temporary:
            staged = Path(temporary)
            for member in sorted(seed.glob("*.csv")):
                if member.is_symlink() or not member.is_file():
                    raise DataGenerationError("seed data contains a non-regular CSV member")
                shutil.copyfile(member, staged / member.name)
            members = _dataset_members(staged)
            digest = _dataset_digest(members)
            generation_id = f"gen-{digest[:24]}"
            payload = self._generation_payload(
                generation_id=generation_id,
                source=source,
                created_at=created_at,
                members=members,
            )
            (staged / "generation.json").write_bytes(_canonical_bytes(payload))
            destination = self.generations_root / generation_id
            if not destination.exists():
                shutil.copytree(staged, destination)
        generation = self._load_generation(generation_id)
        self._replace_active_pointer(generation)
        return generation

    def refresh_active_manifest(self) -> DataGeneration:
        """Reseal the active generation after a proven append-only publish."""

        if self.pending_promotion.exists():
            raise DataGenerationError("cannot refresh data while a source promotion is pending")
        active = self.active()
        members = _dataset_members(active.path)
        payload = self._generation_payload(
            generation_id=active.generation_id,
            source=active.source,
            created_at=active.created_at,
            members=members,
        )
        _atomic_json(active.path / "generation.json", payload)
        refreshed = self._load_generation(active.generation_id)
        self._replace_active_pointer(refreshed)
        return refreshed

    def _candidate_payload(
        self,
        *,
        candidate_id: str,
        active_generation_id: str,
        changed_symbols: tuple[str, ...],
        changed_sessions: tuple[date, ...],
        first_difference_session: date | None,
        old_digest: str,
        new_digest: str,
        old_row_count: int,
        new_row_count: int,
        source: str,
        generated_at: datetime,
        members: Mapping[str, str],
    ) -> dict[str, Any]:
        return {
            "schema": "firmquant.data-rewrite-candidate.v1",
            "candidate_id": candidate_id,
            "active_generation_id": active_generation_id,
            "changed_symbols": list(changed_symbols),
            "changed_sessions": [item.isoformat() for item in changed_sessions],
            "first_difference_session": (
                first_difference_session.isoformat() if first_difference_session is not None else None
            ),
            "old_digest": old_digest,
            "new_digest": new_digest,
            "old_row_count": old_row_count,
            "new_row_count": new_row_count,
            "source": source,
            "generated_at": _iso(generated_at),
            "members": dict(sorted(members.items())),
        }

    def create_candidate(
        self,
        *,
        active_generation_id: str,
        replacement_rows: Mapping[str, bytes],
        source: str,
        generated_at: datetime,
    ) -> RewriteCandidate:
        if self.pending_promotion.exists():
            raise DataGenerationError("cannot create rewrite candidate while promotion is pending")
        active = self._load_generation(active_generation_id)
        if self.active().generation_id != active_generation_id:
            raise DataGenerationError("rewrite candidate is not based on the active generation")
        if not replacement_rows:
            raise DataGenerationError("rewrite candidate requires replacement data")
        with tempfile.TemporaryDirectory(prefix="candidate-", dir=self.root) as temporary:
            staged = Path(temporary)
            for member in sorted(active.path.glob("*.csv")):
                shutil.copyfile(member, staged / member.name)
            changed_symbols: list[str] = []
            changed_sessions: set[date] = set()
            old_row_count = 0
            new_row_count = 0
            for symbol, data in sorted(replacement_rows.items()):
                if not isinstance(symbol, str) or not symbol or not isinstance(data, bytes):
                    raise DataGenerationError("rewrite candidate replacements are invalid")
                prior_path = _symbol_path(active.path, symbol)
                staged_path = staged / prior_path.name
                prior = prior_path.read_bytes() if prior_path.is_file() else b"date\n"
                if prior == data:
                    continue
                old_rows = dict(_csv_rows(prior))
                new_rows = dict(_csv_rows(data))
                old_row_count += len(old_rows)
                new_row_count += len(new_rows)
                for session in set(old_rows) | set(new_rows):
                    if old_rows.get(session) != new_rows.get(session):
                        changed_sessions.add(session)
                changed_symbols.append(symbol)
                staged_path.write_bytes(data)
            if not changed_symbols:
                raise DataGenerationError("rewrite candidate does not change active data")
            members = _dataset_members(staged)
            old_members = _dataset_members(active.path)
            old_digest = _dataset_digest(old_members)
            new_digest = _dataset_digest(members)
            sessions = tuple(sorted(changed_sessions))
            first_difference = sessions[0] if sessions else None
            seed = {
                "active_generation_id": active_generation_id,
                "new_digest": new_digest,
                "generated_at": _iso(generated_at),
                "source": source,
            }
            candidate_id = f"candidate-{_sha256(_canonical_bytes(seed))[:24]}"
            payload = self._candidate_payload(
                candidate_id=candidate_id,
                active_generation_id=active_generation_id,
                changed_symbols=tuple(changed_symbols),
                changed_sessions=sessions,
                first_difference_session=first_difference,
                old_digest=old_digest,
                new_digest=new_digest,
                old_row_count=old_row_count,
                new_row_count=new_row_count,
                source=source,
                generated_at=generated_at,
                members=members,
            )
            candidate_sha256 = _sha256(_canonical_bytes(payload))
            payload["candidate_sha256"] = candidate_sha256
            (staged / "candidate.json").write_bytes(_canonical_bytes(payload))
            destination = self.candidates_root / candidate_id
            if destination.exists():
                existing = self.verify_candidate(candidate_id)
                if existing.candidate_sha256 == candidate_sha256:
                    return existing
                raise DataGenerationError("rewrite candidate identity collision")
            shutil.copytree(staged, destination)
        return self.verify_candidate(candidate_id)

    def verify_candidate(self, candidate_id: str) -> RewriteCandidate:
        canonical_id = self._require_candidate_id(candidate_id)
        path = self.candidates_root / canonical_id
        manifest = path / "candidate.json"
        if path.is_symlink() or not path.is_dir() or manifest.is_symlink() or not manifest.is_file():
            raise DataGenerationError("rewrite candidate is missing")
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DataGenerationError("rewrite candidate manifest is invalid") from error
        if not isinstance(payload, dict) or payload.get("candidate_id") != canonical_id:
            raise DataGenerationError("rewrite candidate identity changed")
        expected = payload.get("candidate_sha256")
        unsigned = dict(payload)
        unsigned.pop("candidate_sha256", None)
        if expected != _sha256(_canonical_bytes(unsigned)):
            raise DataGenerationError("rewrite candidate manifest changed")
        members = _dataset_members(path)
        if payload.get("members") != members:
            raise DataGenerationError("rewrite candidate data changed")
        try:
            changed_sessions = tuple(date.fromisoformat(item) for item in payload["changed_sessions"])
            generated_at = datetime.fromisoformat(str(payload["generated_at"]))
            first_raw = payload.get("first_difference_session")
            first = date.fromisoformat(first_raw) if isinstance(first_raw, str) else None
        except (KeyError, TypeError, ValueError) as error:
            raise DataGenerationError("rewrite candidate metadata changed") from error
        changed_symbols = payload.get("changed_symbols")
        if not isinstance(changed_symbols, list) or not all(
            isinstance(item, str) for item in changed_symbols
        ):
            raise DataGenerationError("rewrite candidate symbols changed")
        return RewriteCandidate(
            candidate_id=canonical_id,
            path=path,
            active_generation_id=self._require_generation_id(str(payload["active_generation_id"])),
            changed_symbols=tuple(changed_symbols),
            changed_sessions=changed_sessions,
            first_difference_session=first,
            old_digest=str(payload["old_digest"]),
            new_digest=str(payload["new_digest"]),
            old_row_count=int(payload["old_row_count"]),
            new_row_count=int(payload["new_row_count"]),
            source=str(payload["source"]),
            generated_at=generated_at,
            candidate_sha256=str(expected),
        )

    def _pending_payload(self) -> dict[str, object]:
        if self.pending_promotion.is_symlink() or not self.pending_promotion.is_file():
            raise DataGenerationError("pending source promotion receipt is unavailable")
        try:
            payload = json.loads(self.pending_promotion.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DataGenerationError("pending source promotion receipt is invalid") from error
        if not isinstance(payload, dict):
            raise DataGenerationError("pending source promotion receipt is invalid")
        return payload

    def recover_pending_promotion(self) -> DataGeneration | None:
        """Finalize a promotion whether a crash happened before or after pointer replacement."""

        if not self.pending_promotion.exists():
            return None
        pending = self._pending_payload()
        candidate_id = self._require_candidate_id(str(pending.get("candidate_id")))
        candidate = self.verify_candidate(candidate_id)
        if pending.get("candidate_sha256") != candidate.candidate_sha256:
            raise DataGenerationError("pending promotion candidate identity changed")
        previous_id = self._require_generation_id(str(pending.get("previous_generation_id")))
        new_id = self._require_generation_id(str(pending.get("new_generation_id")))
        previous = self._load_generation(previous_id)
        promoted = self._load_generation(new_id)
        if previous.data_sha256 != candidate.old_digest or promoted.data_sha256 != candidate.new_digest:
            raise DataGenerationError("pending promotion generation identity changed")
        active = self.active()
        if active.generation_id == previous_id:
            self._replace_active_pointer(promoted)
        elif active.generation_id != new_id:
            raise DataGenerationError("active data conflicts with pending promotion")
        promoted_at_raw = pending.get("promoted_at")
        try:
            promoted_at = datetime.fromisoformat(str(promoted_at_raw))
        except ValueError as error:
            raise DataGenerationError("pending promotion time is invalid") from error
        receipt = {
            "schema": "firmquant.data-source-promotion.v1",
            "candidate_id": candidate_id,
            "candidate_sha256": candidate.candidate_sha256,
            "previous_generation_id": previous.generation_id,
            "new_generation_id": promoted.generation_id,
            "source": promoted.source,
            "promoted_at": _iso(promoted_at),
        }
        receipt_path = self.promotions_root / f"{candidate_id}.json"
        if receipt_path.exists():
            if receipt_path.read_bytes() != _canonical_bytes(receipt):
                raise DataGenerationError("source promotion receipt conflicts with pending promotion")
        else:
            _atomic_json(receipt_path, receipt)
        self.pending_promotion.unlink(missing_ok=True)
        return promoted

    def promote_candidate(
        self,
        candidate_id: str,
        *,
        expected_candidate_sha256: str,
        promoted_at: datetime,
    ) -> DataGeneration:
        canonical_id = self._require_candidate_id(candidate_id)
        if self.pending_promotion.exists():
            pending = self._pending_payload()
            if (
                pending.get("candidate_id") != canonical_id
                or pending.get("candidate_sha256") != expected_candidate_sha256
            ):
                raise DataGenerationError("another source promotion is already pending")
            recovered = self.recover_pending_promotion()
            if recovered is None:
                raise DataGenerationError("pending source promotion disappeared")
            return recovered
        candidate = self.verify_candidate(canonical_id)
        if candidate.candidate_sha256 != expected_candidate_sha256:
            raise DataGenerationError("rewrite candidate approval identity changed")
        active = self.active()
        if (
            active.generation_id != candidate.active_generation_id
            or active.data_sha256 != candidate.old_digest
        ):
            raise DataGenerationError("active data changed after rewrite candidate generation")
        generation_id = f"gen-{candidate.new_digest[:24]}"
        destination = self.generations_root / generation_id
        if not destination.exists():
            with tempfile.TemporaryDirectory(prefix="promotion-", dir=self.root) as temporary:
                staged = Path(temporary)
                for member in sorted(candidate.path.glob("*.csv")):
                    shutil.copyfile(member, staged / member.name)
                members = _dataset_members(staged)
                payload = self._generation_payload(
                    generation_id=generation_id,
                    source=candidate.source,
                    created_at=promoted_at,
                    members=members,
                )
                (staged / "generation.json").write_bytes(_canonical_bytes(payload))
                shutil.copytree(staged, destination)
        promoted = self._load_generation(generation_id)
        if promoted.data_sha256 != candidate.new_digest:
            raise DataGenerationError("promoted generation does not match approved candidate")
        _atomic_json(
            self.pending_promotion,
            {
                "schema": "firmquant.pending-data-source-promotion.v1",
                "candidate_id": canonical_id,
                "candidate_sha256": candidate.candidate_sha256,
                "previous_generation_id": active.generation_id,
                "new_generation_id": promoted.generation_id,
                "promoted_at": _iso(promoted_at),
            },
        )
        recovered = self.recover_pending_promotion()
        if recovered is None:
            raise DataGenerationError("source promotion finalization was not durable")
        return recovered


__all__ = (
    "DataGeneration",
    "DataGenerationError",
    "DataGenerationStore",
    "RewriteCandidate",
)
