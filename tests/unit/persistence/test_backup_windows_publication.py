from __future__ import annotations

from pathlib import Path

import pytest

import firmquant.persistence.backup as backup_module
from firmquant.persistence.backup import BackupError


def test_windows_publication_uses_only_movefile_write_through(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "staging"
    source.mkdir()
    destination = tmp_path / "published"
    observed: list[tuple[Path, Path, int]] = []

    def move_file_ex(source_path: Path, destination_path: Path, flags: int) -> bool:
        observed.append((source_path, destination_path, flags))
        source_path.rename(destination_path)
        return True

    monkeypatch.setattr(backup_module, "_move_file_ex", move_file_ex, raising=False)
    monkeypatch.setattr(
        backup_module.os,
        "replace",
        lambda *_args: pytest.fail("Windows publication must not fall back to os.replace"),
    )

    backup_module._publish_directory(source, destination, platform_name="nt")

    assert observed == [(source, destination, 0x8)]
    assert destination.is_dir()


@pytest.mark.parametrize("platform_name", ["posix", "nt"])
def test_publication_rejects_a_different_directory_object_after_move(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform_name: str,
) -> None:
    source = tmp_path / "staging"
    source.mkdir()
    (source / "member").write_bytes(b"expected")
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    (replacement / "member").write_bytes(b"unrelated")
    preserved = tmp_path / "preserved-staging"
    destination = tmp_path / "published"
    real_replace = backup_module.os.replace

    def substitute_directory(source_path: Path, destination_path: Path) -> None:
        real_replace(source_path, preserved)
        real_replace(replacement, destination_path)

    if platform_name == "nt":

        def substitute_move_file_ex(source_path: Path, destination_path: Path, _flags: int) -> bool:
            substitute_directory(source_path, destination_path)
            return True

        monkeypatch.setattr(backup_module, "_move_file_ex", substitute_move_file_ex)
    else:
        monkeypatch.setattr(backup_module.os, "replace", substitute_directory)

    with pytest.raises(BackupError, match=r"publication|directory|identity"):
        backup_module._publish_directory(source, destination, platform_name=platform_name)

    assert (preserved / "member").read_bytes() == b"expected"
    assert (destination / "member").read_bytes() == b"unrelated"


@pytest.mark.parametrize("failure", [False, OSError("MoveFileExW unavailable")])
def test_windows_publication_failure_preserves_staging_and_never_publishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: bool | OSError,
) -> None:
    source = tmp_path / "staging"
    source.mkdir()
    (source / "member").write_bytes(b"preserved")
    destination = tmp_path / "published"

    def move_file_ex(_source: Path, _destination: Path, _flags: int) -> bool:
        if isinstance(failure, OSError):
            raise failure
        return failure

    monkeypatch.setattr(backup_module, "_move_file_ex", move_file_ex, raising=False)
    monkeypatch.setattr(
        backup_module.os,
        "replace",
        lambda *_args: pytest.fail("Windows publication must not fall back to os.replace"),
    )

    with pytest.raises(BackupError, match=r"write-through|publish|MoveFileExW"):
        backup_module._publish_directory(source, destination, platform_name="nt")

    assert (source / "member").read_bytes() == b"preserved"
    assert not destination.exists()


def test_publication_rejects_cross_parent_move_before_platform_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_parent = tmp_path / "source"
    destination_parent = tmp_path / "destination"
    source_parent.mkdir()
    destination_parent.mkdir()
    source = source_parent / "staging"
    source.mkdir()
    destination = destination_parent / "published"
    monkeypatch.setattr(
        backup_module,
        "_move_file_ex",
        lambda *_args: pytest.fail("cross-parent publication reached platform API"),
        raising=False,
    )

    with pytest.raises(BackupError, match=r"same parent|volume"):
        backup_module._publish_directory(source, destination, platform_name="nt")

    assert source.is_dir()
    assert not destination.exists()
