"""Production deployment identities used by smoke, promotion, arm, and runtime gates."""

from __future__ import annotations

import hashlib
import shutil
import subprocess  # nosec B404
from pathlib import Path

from firmquant.build_identity import load_locked_source_identity
from firmquant.config import Settings
from firmquant.persistence.repositories import canonical_sha256


def configuration_sha256(path: Path) -> str:
    candidate = Path(path)
    if candidate.is_symlink() or not candidate.is_file():
        raise RuntimeError("production configuration is unavailable")
    try:
        return hashlib.sha256(candidate.read_bytes()).hexdigest()
    except OSError as error:
        raise RuntimeError("production configuration cannot be read") from error


def promotion_config_sha256(settings: Settings) -> str:
    """Hash the SHADOW-observed execution contract while excluding risk-shrinking live caps."""

    if not isinstance(settings, Settings):
        raise TypeError("promotion identity requires Settings")
    broker = settings.broker
    paths = settings.paths
    account_hash = hashlib.sha256((broker.account_alias or "").encode("utf-8")).hexdigest()
    return canonical_sha256(
        {
            "schema": "firmquant.promotion-config.v1",
            "timezone": settings.timezone,
            "broker": {
                "adapter": broker.adapter,
                "account_alias_sha256": account_hash,
                "userdata_path": (
                    None if broker.xtquant_userdata_path is None else str(broker.xtquant_userdata_path)
                ),
                "session_id": broker.session_id,
                "safety_manifest_path": (
                    None if broker.safety_manifest_path is None else str(broker.safety_manifest_path)
                ),
            },
            "data_directory": str(paths.data_directory),
            "uquant_source_checkout": (
                None if paths.uquant_source_checkout is None else str(paths.uquant_source_checkout)
            ),
            "execution": settings.execution.model_dump(mode="python"),
        }
    )


def current_clean_firmquant_commit(repository_root: Path | None = None) -> str:
    """Require a clean Git checkout whose identity-bearing files match the locked build record."""

    root = (
        Path(__file__).resolve().parents[3]
        if repository_root is None
        else Path(repository_root).resolve()
    )
    executable = shutil.which("git")
    if executable is None:
        raise RuntimeError("git is required to prove firmquant production identity")
    try:
        commit = subprocess.run(  # nosec B603
            [executable, "rev-parse", "HEAD^{commit}"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        status = subprocess.run(  # nosec B603
            [executable, "status", "--porcelain", "--untracked-files=all"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise RuntimeError("firmquant Git identity cannot be inspected") from error
    if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
        raise RuntimeError("firmquant commit identity is invalid")
    if status:
        raise RuntimeError("firmquant production checkout must be clean")
    try:
        load_locked_source_identity().verify_firmquant_files(root)
    except Exception as error:
        raise RuntimeError("firmquant build identity does not match reviewed files") from error
    return commit


__all__ = (
    "configuration_sha256",
    "current_clean_firmquant_commit",
    "promotion_config_sha256",
)
