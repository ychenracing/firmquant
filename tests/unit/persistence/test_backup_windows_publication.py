from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

import firmquant.persistence.backup as backup_module
from firmquant.persistence.backup import BackupError


class _FakeMoveFileExW:
    def __init__(self, result: int) -> None:
        self._result = result
        self.argtypes: list[object] | None = None
        self.restype: object | None = None
        self.calls: list[tuple[str, str, int]] = []

    def __call__(self, source: str, destination: str, flags: int) -> int:
        self.calls.append((source, destination, flags))
        return self._result


def _install_fake_windows_ctypes(
    monkeypatch: pytest.MonkeyPatch,
    move_file_ex: _FakeMoveFileExW,
    *,
    get_last_error: Callable[[], int],
) -> list[tuple[str, bool]]:
    loader_calls: list[tuple[str, bool]] = []

    def loader(name: str, *, use_last_error: bool) -> object:
        loader_calls.append((name, use_last_error))
        return SimpleNamespace(MoveFileExW=move_file_ex)

    monkeypatch.setattr(backup_module.ctypes, "WinDLL", loader, raising=False)
    monkeypatch.setattr(
        backup_module.ctypes,
        "get_last_error",
        get_last_error,
        raising=False,
    )
    return loader_calls


@pytest.mark.parametrize("unavailable", ["WinDLL", "get_last_error"])
def test_move_file_ex_rejects_unavailable_windows_ctypes_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unavailable: str,
) -> None:
    move_file_ex = _FakeMoveFileExW(1)
    _install_fake_windows_ctypes(
        monkeypatch,
        move_file_ex,
        get_last_error=lambda: 0,
    )
    monkeypatch.delattr(backup_module.ctypes, unavailable)

    with pytest.raises(OSError, match="MoveFileExW is unavailable"):
        backup_module._move_file_ex(tmp_path / "source", tmp_path / "destination", 0x8)

    assert move_file_ex.calls == []


def test_move_file_ex_configures_and_invokes_windows_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    move_file_ex = _FakeMoveFileExW(1)
    loader_calls = _install_fake_windows_ctypes(
        monkeypatch,
        move_file_ex,
        get_last_error=lambda: pytest.fail("last error queried after successful move"),
    )

    assert backup_module._move_file_ex(source, destination, 0x8) is True

    assert loader_calls == [("kernel32", True)]
    assert move_file_ex.argtypes == [
        backup_module.ctypes.c_wchar_p,
        backup_module.ctypes.c_wchar_p,
        backup_module.ctypes.c_uint,
    ]
    assert move_file_ex.restype is backup_module.ctypes.c_int
    assert move_file_ex.calls == [(str(source), str(destination), 0x8)]


def test_move_file_ex_propagates_windows_last_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    move_file_ex = _FakeMoveFileExW(0)
    loader_calls = _install_fake_windows_ctypes(
        monkeypatch,
        move_file_ex,
        get_last_error=lambda: 1234,
    )

    with pytest.raises(OSError, match="MoveFileExW failed") as exc_info:
        backup_module._move_file_ex(source, destination, 0x8)

    assert exc_info.value.errno == 1234
    assert loader_calls == [("kernel32", True)]
    assert move_file_ex.calls == [(str(source), str(destination), 0x8)]


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
