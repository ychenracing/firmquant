from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import firmquant.application.production_identity as identity
import tests.unit.application.test_production_services_acceptance as base
from firmquant.config import Mode


def test_configuration_sha256_requires_regular_readable_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "firmquant.toml"
    config.write_bytes(b"candidate = true\n")
    assert identity.configuration_sha256(config) == hashlib.sha256(config.read_bytes()).hexdigest()

    with pytest.raises(RuntimeError, match="unavailable"):
        identity.configuration_sha256(tmp_path)

    original = Path.read_bytes

    def fail_read(path: Path) -> bytes:
        if path == config:
            raise OSError("read failure")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", fail_read)
    with pytest.raises(RuntimeError, match="cannot be read"):
        identity.configuration_sha256(config)


def test_promotion_config_identity_is_typed_stable_and_sensitive_to_execution_contract(
    tmp_path: Path,
) -> None:
    with pytest.raises(TypeError, match="requires Settings"):
        identity.promotion_config_sha256(object())  # type: ignore[arg-type]

    settings, _config = base.settings_for(tmp_path, Mode.SHADOW)
    first = identity.promotion_config_sha256(settings)
    second = identity.promotion_config_sha256(settings)
    assert first == second
    assert len(first) == 64

    changed_execution = settings.execution.model_copy(
        update={"max_quote_age_seconds": settings.execution.max_quote_age_seconds + 1}
    )
    changed = settings.model_copy(update={"execution": changed_execution})
    assert identity.promotion_config_sha256(changed) != first

    changed_account = settings.model_copy(
        update={"broker": settings.broker.model_copy(update={"account_alias": "another-account"})}
    )
    assert identity.promotion_config_sha256(changed_account) != first


def _result(stdout: str) -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout)


def test_current_clean_commit_requires_git_and_valid_clean_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(identity.shutil, "which", lambda _name: None)
    with pytest.raises(RuntimeError, match="git is required"):
        identity.current_clean_firmquant_commit(tmp_path)

    monkeypatch.setattr(identity.shutil, "which", lambda _name: "/usr/bin/git")
    monkeypatch.setattr(
        identity.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(subprocess.CalledProcessError(1, "git")),
    )
    with pytest.raises(RuntimeError, match="cannot be inspected"):
        identity.current_clean_firmquant_commit(tmp_path)

    responses = iter((_result("not-a-sha\n"), _result("")))
    monkeypatch.setattr(identity.subprocess, "run", lambda *_args, **_kwargs: next(responses))
    with pytest.raises(RuntimeError, match="commit identity is invalid"):
        identity.current_clean_firmquant_commit(tmp_path)

    responses = iter((_result("a" * 40 + "\n"), _result(" M dirty.py\n")))
    monkeypatch.setattr(identity.subprocess, "run", lambda *_args, **_kwargs: next(responses))
    with pytest.raises(RuntimeError, match="must be clean"):
        identity.current_clean_firmquant_commit(tmp_path)

    class BrokenLockedIdentity:
        def verify_firmquant_files(self, _root: Path) -> None:
            raise ValueError("identity mismatch")

    responses = iter((_result("b" * 40 + "\n"), _result("")))
    monkeypatch.setattr(identity.subprocess, "run", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr(identity, "load_locked_source_identity", lambda: BrokenLockedIdentity())
    with pytest.raises(RuntimeError, match="does not match reviewed files"):
        identity.current_clean_firmquant_commit(tmp_path)

    verified_roots: list[Path] = []

    class LockedIdentity:
        def verify_firmquant_files(self, root: Path) -> None:
            verified_roots.append(root)

    responses = iter((_result("c" * 40 + "\n"), _result("")))
    monkeypatch.setattr(identity.subprocess, "run", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr(identity, "load_locked_source_identity", lambda: LockedIdentity())
    assert identity.current_clean_firmquant_commit(tmp_path) == "c" * 40
    assert verified_roots == [tmp_path.resolve()]
