"""Verify the checked-in firmquant lock and installed uquant runtime identity."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess  # nosec B404
from collections.abc import Sequence
from pathlib import Path

from firmquant.build_identity import (
    SourceIdentityError,
    load_locked_source_identity,
    verify_uquant_identity,
    verify_uquant_source_checkout,
)


def _repository_root() -> Path:
    git = shutil.which("git")
    if git is None:
        raise SourceIdentityError("git executable is unavailable")
    try:
        completed = subprocess.run(  # nosec B603
            [git, "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SourceIdentityError("cannot resolve firmquant repository root") from exc
    return Path(completed.stdout.strip())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify the immutable uquant source baseline.")
    parser.add_argument("--firmquant-root", type=Path)
    parser.add_argument("--uquant-source-root", type=Path)
    parser.add_argument("--wheel", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        identity = load_locked_source_identity()
        firmquant_root = (
            arguments.firmquant_root.resolve()
            if arguments.firmquant_root is not None
            else _repository_root()
        )
        identity.verify_firmquant_files(firmquant_root)
        verify_uquant_identity(identity, wheel_path=arguments.wheel)
        if arguments.uquant_source_root is not None:
            verify_uquant_source_checkout(identity, arguments.uquant_source_root)
    except SourceIdentityError as exc:
        parser.exit(1, f"source baseline verification failed: {exc}\n")
    print(
        json.dumps(
            {
                "status": "verified",
                "uquant_commit": identity.uquant_commit,
                "uquant_tree": identity.uquant_tree,
                "wheel_sha256": identity.wheel_sha256,
                "economic_code_fingerprint": identity.economic_code_fingerprint,
                "config_fingerprint": identity.config_fingerprint,
                "universe_sha256": identity.universe_sha256,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
