from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import subprocess
import zipfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest

from firmquant import build_identity
from firmquant.build_identity import (
    SourceIdentity,
    SourceIdentityError,
    _member_manifest,
    _parse_json_object,
    _verify_direct_url,
    _verify_installed_payload,
    _verify_runtime_contracts,
    _verify_wheel,
    load_locked_source_identity,
    verify_uquant_source_checkout,
    wheel_sha256,
)


class FakeDistribution:
    def __init__(
        self,
        root: Path,
        *,
        package_files: list[PurePosixPath] | None = None,
        direct_url: str | None = None,
        version: str = "1.1.0",
    ) -> None:
        self._root = root
        self.files = package_files
        self._direct_url = direct_url
        self.version = version

    def locate_file(self, package_path: PurePosixPath) -> Path:
        return self._root / str(package_path)

    def read_text(self, name: str) -> str | None:
        assert name == "direct_url.json"
        return self._direct_url


def _mapping() -> dict[str, object]:
    return dataclasses.asdict(load_locked_source_identity())


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"schema_version": 2}, "schema version"),
        ({"known_baseline_commit": "bad"}, "known baseline commit"),
        ({"uquant_commit": "bad"}, "uquant commit"),
        ({"uquant_tree": "bad"}, "uquant tree"),
        ({"wheel_sha256": "BAD"}, "wheel SHA-256"),
        ({"relation_to_known_baseline": "unrelated"}, "relation"),
        ({"uquant_repository": "https://example.invalid/uquant"}, "repository"),
        ({"uquant_version": ""}, "version"),
        ({"uquant_version": "   "}, "version"),
        ({"wheel_filename": "../uquant.whl"}, "safe basename"),
        ({"wheel_bytes": 0}, "positive"),
        ({"wheel_member_count": 0}, "positive"),
        ({"uquant_package_member_count": 0}, "positive"),
    ],
)
def test_source_identity_model_rejects_any_unreviewed_value(change: dict[str, object], message: str) -> None:
    with pytest.raises(SourceIdentityError, match=message):
        replace(load_locked_source_identity(), **change)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"schema_version": True}, "integer"),
        ({"wheel_bytes": "1"}, "integer"),
        ({"uquant_version": 1}, "string"),
    ],
)
def test_source_identity_mapping_rejects_wrong_json_types(change: dict[str, object], message: str) -> None:
    mapping = _mapping()
    mapping.update(change)
    with pytest.raises(SourceIdentityError, match=message):
        SourceIdentity.from_mapping(mapping)


@pytest.mark.parametrize(
    "raw",
    [
        b"\xff",
        b"{",
        b"[]",
        b'{"field":1,"field":2}',
        b'{"field":NaN}',
    ],
)
def test_source_identity_json_parser_rejects_noncanonical_input(raw: bytes) -> None:
    with pytest.raises(SourceIdentityError):
        _parse_json_object(raw)


def test_file_identity_errors_do_not_expose_contents(tmp_path: Path) -> None:
    missing = tmp_path / "sensitive-name.json"
    with pytest.raises(SourceIdentityError, match="cannot read"):
        wheel_sha256(missing)

    file = tmp_path / "value.bin"
    file.write_bytes(b"content")
    assert wheel_sha256(file) == hashlib.sha256(b"content").hexdigest()
    with pytest.raises(SourceIdentityError, match="mismatch"):
        build_identity._require_file_sha256(file, "0" * 64, label="test")


@pytest.mark.parametrize(
    "members",
    [
        [("", b"")],
        [("/absolute", b"")],
        [("../escape", b"")],
        [("bad\\path", b"")],
        [("same", b"1"), ("same", b"2")],
    ],
)
def test_package_member_manifest_rejects_path_confusion(members: list[tuple[str, bytes]]) -> None:
    with pytest.raises(SourceIdentityError, match="unsafe or duplicate"):
        _member_manifest(members)


def test_package_member_manifest_is_order_independent() -> None:
    first = _member_manifest([("uquant/b.py", b"b"), ("uquant/a.py", b"a")])
    second = _member_manifest([("uquant/a.py", b"a"), ("uquant/b.py", b"b")])
    assert first == second
    assert first[1] == 2


@pytest.mark.parametrize(
    ("direct_url", "message"),
    [
        ("{", "malformed"),
        ("[]", "malformed"),
        ('{"vcs_info":[]}', "VCS identity"),
        ('{"url":"x","vcs_info":{"commit_id":"bad"}}', "commit mismatch"),
        (
            json.dumps(
                {
                    "url": "https://example.invalid/uquant",
                    "vcs_info": {"commit_id": load_locked_source_identity().uquant_commit},
                }
            ),
            "repository mismatch",
        ),
    ],
)
def test_direct_url_metadata_rejects_unreviewed_vcs_identity(
    tmp_path: Path, direct_url: str, message: str
) -> None:
    distribution = FakeDistribution(tmp_path, direct_url=direct_url)
    with pytest.raises(SourceIdentityError, match=message):
        _verify_direct_url(load_locked_source_identity(), distribution)  # type: ignore[arg-type]


def test_direct_url_without_vcs_metadata_is_not_treated_as_a_commit_claim(tmp_path: Path) -> None:
    identity = load_locked_source_identity()
    _verify_direct_url(identity, FakeDistribution(tmp_path, direct_url=None))  # type: ignore[arg-type]
    _verify_direct_url(identity, FakeDistribution(tmp_path, direct_url='{"url":"local"}'))  # type: ignore[arg-type]


def test_installed_payload_requires_manifest_and_exact_members(tmp_path: Path) -> None:
    identity = load_locked_source_identity()
    with pytest.raises(SourceIdentityError, match="no file manifest"):
        _verify_installed_payload(identity, FakeDistribution(tmp_path))  # type: ignore[arg-type]

    package = tmp_path / "uquant"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    distribution = FakeDistribution(
        tmp_path,
        package_files=[PurePosixPath("uquant/__init__.py")],
    )
    with pytest.raises(SourceIdentityError, match="member count mismatch"):
        _verify_installed_payload(identity, distribution)  # type: ignore[arg-type]


def test_runtime_contract_fingerprints_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    identity = load_locked_source_identity()
    monkeypatch.setattr(build_identity, "_uquant_config_fingerprint", lambda: "0" * 64)
    with pytest.raises(SourceIdentityError, match="config fingerprint mismatch"):
        _verify_runtime_contracts(identity)

    monkeypatch.setattr(build_identity, "_uquant_config_fingerprint", lambda: identity.config_fingerprint)
    monkeypatch.setattr(
        build_identity,
        "_uquant_default_universe",
        lambda: type("Universe", (), {"sha256": "0" * 64})(),
    )
    with pytest.raises(SourceIdentityError, match="universe SHA-256 mismatch"):
        _verify_runtime_contracts(identity)


def test_public_api_contract_requires_matching_embedded_seal(tmp_path: Path) -> None:
    raw = json.dumps(
        {
            "contract": {"value": 1},
            "contract_id": "uquant-public-api-v1",
            "contract_sha256": "0" * 64,
            "recorded_on": "2026-08-28",
            "schema_version": 1,
        },
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
    contract = tmp_path / "public_api_contract.json"
    contract.write_bytes(raw)

    with pytest.raises(SourceIdentityError, match="public API contract SHA-256 mismatch"):
        build_identity._verify_public_api_contract(
            contract,
            expected_file_sha256=hashlib.sha256(raw).hexdigest(),
            expected_contract_sha256=hashlib.sha256(b'{"value":1}').hexdigest(),
        )


def test_source_checkout_must_match_exact_reviewed_commit(tmp_path: Path) -> None:
    repository = tmp_path / "uquant"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
    (repository / "file.txt").write_text("content", encoding="utf-8")
    subprocess.run(["git", "add", "file.txt"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "test"], cwd=repository, check=True)
    subprocess.run(["git", "checkout", "--detach"], cwd=repository, check=True, capture_output=True)

    with pytest.raises(SourceIdentityError, match="uquant commit mismatch"):
        verify_uquant_source_checkout(load_locked_source_identity(), repository)


def test_source_checkout_must_be_detached(tmp_path: Path) -> None:
    configured_source = os.environ.get("FIRMQUANT_UQUANT_SOURCE_CHECKOUT")
    if configured_source is None:
        pytest.skip("set FIRMQUANT_UQUANT_SOURCE_CHECKOUT to test checkout posture")
    identity = load_locked_source_identity()
    repository = tmp_path / "uquant"
    subprocess.run(
        ["git", "clone", "--no-checkout", configured_source, str(repository)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "checkout", "--detach", identity.uquant_commit],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "switch", "-c", "attached"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )

    with pytest.raises(SourceIdentityError, match="detached"):
        verify_uquant_source_checkout(identity, repository)


def _wheel(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)


def test_wheel_verification_checks_filename_size_and_member_manifests(tmp_path: Path) -> None:
    members = {"uquant/__init__.py": b"", "uquant/module.py": b"value = 1\n"}
    wheel = tmp_path / "test.whl"
    _wheel(wheel, members)
    payload_digest, payload_count = _member_manifest(members.items())
    package_digest, package_count = _member_manifest(members.items())
    identity = replace(
        load_locked_source_identity(),
        wheel_filename=wheel.name,
        wheel_sha256=wheel_sha256(wheel),
        wheel_payload_manifest_sha256=payload_digest,
        wheel_bytes=wheel.stat().st_size,
        wheel_member_count=payload_count,
        uquant_package_manifest_sha256=package_digest,
        uquant_package_member_count=package_count,
    )
    _verify_wheel(identity, wheel)

    with pytest.raises(SourceIdentityError, match="filename mismatch"):
        _verify_wheel(replace(identity, wheel_filename="other.whl"), wheel)
    with pytest.raises(SourceIdentityError, match="byte size mismatch"):
        _verify_wheel(replace(identity, wheel_bytes=identity.wheel_bytes + 1), wheel)
    with pytest.raises(SourceIdentityError, match="member count mismatch"):
        _verify_wheel(replace(identity, wheel_member_count=identity.wheel_member_count + 1), wheel)
    with pytest.raises(SourceIdentityError, match="payload manifest"):
        _verify_wheel(replace(identity, wheel_payload_manifest_sha256="0" * 64), wheel)
    with pytest.raises(SourceIdentityError, match="package member count"):
        _verify_wheel(
            replace(identity, uquant_package_member_count=identity.uquant_package_member_count + 1), wheel
        )


def test_git_inspection_requires_git_and_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(build_identity.shutil, "which", lambda _name: None)
    with pytest.raises(SourceIdentityError, match="git executable"):
        build_identity._git(tmp_path, "status")


@pytest.mark.parametrize(
    "function", [build_identity._uquant_config_fingerprint, build_identity._uquant_default_universe]
)
def test_runtime_identity_helpers_are_callable(function: Callable[[], object]) -> None:
    assert function() is not None
