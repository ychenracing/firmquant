"""Fail-closed identity checks for the locked uquant production dependency."""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import re
import shutil
import subprocess  # nosec B404
import zipfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, fields
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import Final, Never, Protocol, cast

_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA: Final = re.compile(r"^[0-9a-f]{40}$")
_RESOURCE_PACKAGE: Final = "firmquant.resources"
_RESOURCE_NAME: Final = "source_identity.json"


class SourceIdentityError(RuntimeError):
    """Raised when reviewed source identity cannot be proven exactly."""


class _UniverseContract(Protocol):
    sha256: str


def _uquant_config_fingerprint() -> str:
    module = importlib.import_module("uquant.config.model")
    function = cast(Callable[[], str], module.config_fingerprint)
    return function()


def _uquant_default_universe() -> _UniverseContract:
    module = importlib.import_module("uquant.contracts.universe")
    function = cast(Callable[[], _UniverseContract], module.default_ai_universe)
    return function()


def _uquant_git_source_surface_fingerprint(
    repository_root: Path,
    revision: str,
    surface: str,
) -> str:
    module = importlib.import_module("uquant.provenance.fingerprints")
    function = cast(
        Callable[[str | Path, str, str], str],
        module.git_source_surface_fingerprint,
    )
    return function(repository_root, revision, surface)


def _reject_constant(value: str) -> Never:
    raise SourceIdentityError(f"source identity contains a non-standard JSON constant: {value}")


def _object_from_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SourceIdentityError(f"source identity contains duplicate field: {key}")
        result[key] = value
    return result


def _parse_json_object(raw: bytes) -> dict[str, object]:
    try:
        decoded = raw.decode("utf-8")
        payload: object = json.loads(
            decoded,
            object_pairs_hook=_object_from_pairs,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceIdentityError("source identity is not canonical UTF-8 JSON") from exc
    if not isinstance(payload, dict) or not all(isinstance(key, str) for key in payload):
        raise SourceIdentityError("source identity root must be a JSON object")
    return payload


def _string(mapping: Mapping[str, object], name: str) -> str:
    value = mapping[name]
    if not isinstance(value, str):
        raise SourceIdentityError(f"source identity field must be a string: {name}")
    return value


def _integer(mapping: Mapping[str, object], name: str) -> int:
    value = mapping[name]
    if not isinstance(value, int) or isinstance(value, bool):
        raise SourceIdentityError(f"source identity field must be an integer: {name}")
    return value


def _require_digest(value: str, *, label: str, pattern: re.Pattern[str] = _SHA256) -> None:
    if pattern.fullmatch(value) is None:
        raise SourceIdentityError(f"{label} is not a canonical lowercase digest")


@dataclass(frozen=True, slots=True)
class SourceIdentity:
    """Immutable build and runtime identity for one reviewed uquant commit."""

    schema_version: int
    known_baseline_commit: str
    relation_to_known_baseline: str
    uquant_repository: str
    uquant_version: str
    uquant_commit: str
    uquant_tree: str
    uquant_pyproject_sha256: str
    uquant_uv_lock_sha256: str
    wheel_filename: str
    wheel_sha256: str
    wheel_payload_manifest_sha256: str
    wheel_bytes: int
    wheel_member_count: int
    uquant_package_manifest_sha256: str
    uquant_package_member_count: int
    economic_code_fingerprint: str
    account_code_fingerprint: str
    config_fingerprint: str
    universe_manifest_sha256: str
    universe_sha256: str
    firmquant_pyproject_sha256: str
    firmquant_uv_lock_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise SourceIdentityError("unsupported source identity schema version")
        for label, digest in (
            ("known baseline commit", self.known_baseline_commit),
            ("uquant commit", self.uquant_commit),
            ("uquant tree", self.uquant_tree),
        ):
            _require_digest(digest, label=label, pattern=_GIT_SHA)
        for label, digest in (
            ("uquant pyproject SHA-256", self.uquant_pyproject_sha256),
            ("uquant uv.lock SHA-256", self.uquant_uv_lock_sha256),
            ("wheel SHA-256", self.wheel_sha256),
            ("wheel payload manifest SHA-256", self.wheel_payload_manifest_sha256),
            ("uquant package manifest SHA-256", self.uquant_package_manifest_sha256),
            ("economic code fingerprint", self.economic_code_fingerprint),
            ("account code fingerprint", self.account_code_fingerprint),
            ("config fingerprint", self.config_fingerprint),
            ("universe manifest SHA-256", self.universe_manifest_sha256),
            ("universe SHA-256", self.universe_sha256),
            ("firmquant pyproject SHA-256", self.firmquant_pyproject_sha256),
            ("firmquant uv.lock SHA-256", self.firmquant_uv_lock_sha256),
        ):
            _require_digest(digest, label=label)
        if self.relation_to_known_baseline not in {"identical", "descendant"}:
            raise SourceIdentityError("invalid relation to known uquant baseline")
        if self.uquant_repository != "https://github.com/ychenracing/uquant.git":
            raise SourceIdentityError("unexpected uquant repository")
        if not self.uquant_version or self.uquant_version.isspace():
            raise SourceIdentityError("uquant version is empty")
        if PurePosixPath(self.wheel_filename).name != self.wheel_filename:
            raise SourceIdentityError("wheel filename is not a safe basename")
        if self.wheel_bytes <= 0 or self.wheel_member_count <= 0:
            raise SourceIdentityError("wheel sizes and member counts must be positive")
        if self.uquant_package_member_count <= 0:
            raise SourceIdentityError("uquant package member count must be positive")

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, object]) -> SourceIdentity:
        """Parse the strict, versioned JSON resource without accepting extra fields."""

        expected_fields = {field.name for field in fields(cls)}
        if set(mapping) != expected_fields:
            raise SourceIdentityError("source identity schema fields do not match the contract")
        return cls(
            schema_version=_integer(mapping, "schema_version"),
            known_baseline_commit=_string(mapping, "known_baseline_commit"),
            relation_to_known_baseline=_string(mapping, "relation_to_known_baseline"),
            uquant_repository=_string(mapping, "uquant_repository"),
            uquant_version=_string(mapping, "uquant_version"),
            uquant_commit=_string(mapping, "uquant_commit"),
            uquant_tree=_string(mapping, "uquant_tree"),
            uquant_pyproject_sha256=_string(mapping, "uquant_pyproject_sha256"),
            uquant_uv_lock_sha256=_string(mapping, "uquant_uv_lock_sha256"),
            wheel_filename=_string(mapping, "wheel_filename"),
            wheel_sha256=_string(mapping, "wheel_sha256"),
            wheel_payload_manifest_sha256=_string(mapping, "wheel_payload_manifest_sha256"),
            wheel_bytes=_integer(mapping, "wheel_bytes"),
            wheel_member_count=_integer(mapping, "wheel_member_count"),
            uquant_package_manifest_sha256=_string(
                mapping, "uquant_package_manifest_sha256"
            ),
            uquant_package_member_count=_integer(mapping, "uquant_package_member_count"),
            economic_code_fingerprint=_string(mapping, "economic_code_fingerprint"),
            account_code_fingerprint=_string(mapping, "account_code_fingerprint"),
            config_fingerprint=_string(mapping, "config_fingerprint"),
            universe_manifest_sha256=_string(mapping, "universe_manifest_sha256"),
            universe_sha256=_string(mapping, "universe_sha256"),
            firmquant_pyproject_sha256=_string(mapping, "firmquant_pyproject_sha256"),
            firmquant_uv_lock_sha256=_string(mapping, "firmquant_uv_lock_sha256"),
        )

    def verify_firmquant_files(self, repository_root: Path) -> None:
        """Verify the dependency declaration and lock used to record this baseline."""

        _require_file_sha256(
            repository_root / "pyproject.toml",
            self.firmquant_pyproject_sha256,
            label="firmquant pyproject SHA-256",
        )
        _require_file_sha256(
            repository_root / "uv.lock",
            self.firmquant_uv_lock_sha256,
            label="firmquant uv.lock SHA-256",
        )


def load_locked_source_identity(path: Path | None = None) -> SourceIdentity:
    """Load the reviewed identity embedded in the installed firmquant package."""

    raw = (
        path.read_bytes()
        if path is not None
        else files(_RESOURCE_PACKAGE).joinpath(_RESOURCE_NAME).read_bytes()
    )
    return SourceIdentity.from_mapping(_parse_json_object(raw))


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_file(path: Path) -> str:
    try:
        with path.open("rb") as stream:
            digest = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise SourceIdentityError(f"cannot read identity-bearing file: {path.name}") from exc
    return digest.hexdigest()


def _require_file_sha256(path: Path, expected: str, *, label: str) -> None:
    observed = _sha256_file(path)
    if observed != expected:
        raise SourceIdentityError(f"{label} mismatch: expected {expected}, observed {observed}")


def _git(repository_root: Path, *arguments: str) -> str:
    executable = shutil.which("git")
    if executable is None:
        raise SourceIdentityError("git executable is unavailable")
    try:
        completed = subprocess.run(  # nosec B603
            [executable, *arguments],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SourceIdentityError(f"cannot inspect uquant Git source: {' '.join(arguments)}") from exc
    return completed.stdout.strip()


def verify_uquant_source_checkout(identity: SourceIdentity, repository_root: Path) -> None:
    """Verify that a clean checkout is the exact reviewed uquant source tree."""

    root = repository_root.resolve()
    observed_commit = _git(root, "rev-parse", "HEAD^{commit}")
    if observed_commit != identity.uquant_commit:
        raise SourceIdentityError(
            f"uquant commit mismatch: expected {identity.uquant_commit}, observed {observed_commit}"
        )
    observed_tree = _git(root, "rev-parse", "HEAD^{tree}")
    if observed_tree != identity.uquant_tree:
        raise SourceIdentityError(
            f"uquant tree mismatch: expected {identity.uquant_tree}, observed {observed_tree}"
        )
    if _git(root, "status", "--porcelain", "--untracked-files=all"):
        raise SourceIdentityError("uquant source checkout is dirty")
    _require_file_sha256(
        root / "pyproject.toml",
        identity.uquant_pyproject_sha256,
        label="uquant pyproject SHA-256",
    )
    _require_file_sha256(
        root / "uv.lock",
        identity.uquant_uv_lock_sha256,
        label="uquant uv.lock SHA-256",
    )
    manifest = root / "uquant/contracts/resources/ai_universe_manifest.json"
    _require_file_sha256(
        manifest,
        identity.universe_manifest_sha256,
        label="uquant universe manifest SHA-256",
    )
    for surface, expected in (
        ("economic_decision_v1", identity.economic_code_fingerprint),
        ("execution_account_v1", identity.account_code_fingerprint),
    ):
        try:
            observed = _uquant_git_source_surface_fingerprint(
                root, identity.uquant_commit, surface
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise SourceIdentityError(f"cannot fingerprint uquant source surface: {surface}") from exc
        if observed != expected:
            raise SourceIdentityError(
                f"uquant {surface} fingerprint mismatch: expected {expected}, observed {observed}"
            )


def _member_manifest(
    members: Iterable[tuple[str, bytes]],
) -> tuple[str, int]:
    summaries: list[dict[str, object]] = []
    observed_names: set[str] = set()
    for name, content in members:
        normalized = PurePosixPath(name)
        if (
            not name
            or normalized.is_absolute()
            or ".." in normalized.parts
            or "\\" in name
            or name in observed_names
        ):
            raise SourceIdentityError(f"unsafe or duplicate package member: {name!r}")
        observed_names.add(name)
        summaries.append(
            {"path": name, "sha256": _sha256_bytes(content), "size": len(content)}
        )
    summaries.sort(key=lambda member: str(member["path"]))
    encoded = json.dumps(
        summaries,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return _sha256_bytes(encoded), len(summaries)


def _installed_distribution() -> importlib.metadata.Distribution:
    try:
        return importlib.metadata.distribution("uquant")
    except importlib.metadata.PackageNotFoundError as exc:
        raise SourceIdentityError("locked uquant distribution is not installed") from exc


def _verify_installed_payload(
    identity: SourceIdentity,
    distribution: importlib.metadata.Distribution,
) -> None:
    package_files = distribution.files
    if package_files is None:
        raise SourceIdentityError("installed uquant distribution has no file manifest")
    members: list[tuple[str, bytes]] = []
    for package_path in package_files:
        name = str(package_path).replace("\\", "/")
        if not name.startswith("uquant/"):
            continue
        physical = Path(str(distribution.locate_file(package_path)))
        if physical.is_file():
            try:
                members.append((name, physical.read_bytes()))
            except OSError as exc:
                raise SourceIdentityError(f"cannot read installed uquant member: {name}") from exc
    digest, count = _member_manifest(members)
    if count != identity.uquant_package_member_count:
        raise SourceIdentityError(
            "installed uquant package member count mismatch: "
            f"expected {identity.uquant_package_member_count}, observed {count}"
        )
    if digest != identity.uquant_package_manifest_sha256:
        raise SourceIdentityError(
            "installed uquant package manifest SHA-256 mismatch: "
            f"expected {identity.uquant_package_manifest_sha256}, observed {digest}"
        )


def _verify_direct_url(
    identity: SourceIdentity,
    distribution: importlib.metadata.Distribution,
) -> None:
    raw = distribution.read_text("direct_url.json")
    if raw is None:
        return
    try:
        payload: object = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SourceIdentityError("installed uquant direct_url.json is malformed") from exc
    if not isinstance(payload, dict):
        raise SourceIdentityError("installed uquant direct_url.json is malformed")
    vcs_info = payload.get("vcs_info")
    if vcs_info is None:
        return
    if not isinstance(vcs_info, dict):
        raise SourceIdentityError("installed uquant VCS identity is malformed")
    observed_commit = vcs_info.get("commit_id")
    if observed_commit != identity.uquant_commit:
        raise SourceIdentityError(
            f"uquant commit mismatch: expected {identity.uquant_commit}, observed {observed_commit}"
        )
    observed_url = payload.get("url")
    if observed_url != identity.uquant_repository:
        raise SourceIdentityError(
            f"uquant repository mismatch: expected {identity.uquant_repository}, observed {observed_url}"
        )


def _verify_runtime_contracts(identity: SourceIdentity) -> None:
    observed_config = _uquant_config_fingerprint()
    if observed_config != identity.config_fingerprint:
        raise SourceIdentityError(
            f"uquant config fingerprint mismatch: expected {identity.config_fingerprint}, "
            f"observed {observed_config}"
        )
    observed_universe = _uquant_default_universe().sha256
    if observed_universe != identity.universe_sha256:
        raise SourceIdentityError(
            f"uquant universe SHA-256 mismatch: expected {identity.universe_sha256}, "
            f"observed {observed_universe}"
        )


def _verify_wheel(identity: SourceIdentity, wheel_path: Path) -> None:
    _require_file_sha256(wheel_path, identity.wheel_sha256, label="uquant wheel SHA-256")
    if wheel_path.name != identity.wheel_filename:
        raise SourceIdentityError(
            f"uquant wheel filename mismatch: expected {identity.wheel_filename}, "
            f"observed {wheel_path.name}"
        )
    if wheel_path.stat().st_size != identity.wheel_bytes:
        raise SourceIdentityError("uquant wheel byte size mismatch")
    try:
        with zipfile.ZipFile(wheel_path) as archive:
            all_members = [
                (entry.filename, archive.read(entry.filename))
                for entry in archive.infolist()
                if not entry.is_dir()
            ]
    except (OSError, zipfile.BadZipFile) as exc:
        raise SourceIdentityError("uquant wheel is not a readable ZIP archive") from exc
    payload_digest, payload_count = _member_manifest(all_members)
    if payload_count != identity.wheel_member_count:
        raise SourceIdentityError("uquant wheel member count mismatch")
    if payload_digest != identity.wheel_payload_manifest_sha256:
        raise SourceIdentityError("uquant wheel payload manifest SHA-256 mismatch")
    package_digest, package_count = _member_manifest(
        (name, content) for name, content in all_members if name.startswith("uquant/")
    )
    if package_count != identity.uquant_package_member_count:
        raise SourceIdentityError("uquant wheel package member count mismatch")
    if package_digest != identity.uquant_package_manifest_sha256:
        raise SourceIdentityError("uquant wheel package manifest SHA-256 mismatch")


def _verify_embedded_expectation(identity: SourceIdentity) -> None:
    locked = load_locked_source_identity()
    labels: Mapping[str, str] = {
        "uquant_commit": "uquant commit",
        "uquant_tree": "uquant tree",
        "wheel_sha256": "uquant wheel SHA-256",
    }
    for field in fields(SourceIdentity):
        observed = getattr(identity, field.name)
        reviewed = getattr(locked, field.name)
        if observed != reviewed:
            label = labels.get(field.name, field.name.replace("_", " "))
            raise SourceIdentityError(
                f"{label} differs from embedded reviewed baseline: "
                f"expected {reviewed}, observed {observed}"
            )


def verify_uquant_identity(
    expected: SourceIdentity,
    *,
    wheel_path: Path | None = None,
) -> None:
    """Prove installed uquant and, when supplied, its wheel match the reviewed bytes."""

    _verify_embedded_expectation(expected)
    distribution = _installed_distribution()
    observed_version = distribution.version
    if observed_version != expected.uquant_version:
        raise SourceIdentityError(
            f"uquant version mismatch: expected {expected.uquant_version}, observed {observed_version}"
        )
    _verify_installed_payload(expected, distribution)
    _verify_direct_url(expected, distribution)
    _verify_runtime_contracts(expected)
    if wheel_path is not None:
        _verify_wheel(expected, wheel_path)


def installed_uquant_identity() -> SourceIdentity:
    """Return the reviewed identity only after verifying the installed distribution."""

    identity = load_locked_source_identity()
    verify_uquant_identity(identity)
    return identity


def wheel_sha256(path: Path) -> str:
    """Return a wheel digest using the same streaming implementation as verification."""

    return _sha256_file(path)


__all__ = (
    "SourceIdentity",
    "SourceIdentityError",
    "installed_uquant_identity",
    "load_locked_source_identity",
    "verify_uquant_identity",
    "verify_uquant_source_checkout",
    "wheel_sha256",
)
