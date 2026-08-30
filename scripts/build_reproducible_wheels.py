"""Build and verify the exact deterministic uquant production wheel."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess  # nosec B404
import sys
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from firmquant.build_identity import (
    SourceIdentity,
    SourceIdentityError,
    load_locked_source_identity,
    verify_uquant_identity,
    verify_uquant_source_checkout,
    wheel_sha256,
)


def _run(arguments: Sequence[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # nosec B603
            list(arguments),
            cwd=cwd,
            env={**os.environ, "PYTHONHASHSEED": "0", "SOURCE_DATE_EPOCH": "315532800"},
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        raise SourceIdentityError(f"uquant wheel build command failed: {detail}") from exc


def _resolved_executable(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise SourceIdentityError(f"required executable is unavailable: {name}")
    return executable


def _clone_reviewed_source(identity: SourceIdentity, destination: Path) -> None:
    git = _resolved_executable("git")
    _run(
        [git, "clone", "--filter=blob:none", "--no-checkout", identity.uquant_repository, str(destination)],
        cwd=destination.parent,
    )
    _run([git, "checkout", "--detach", identity.uquant_commit], cwd=destination)


@contextmanager
def _source_checkout(identity: SourceIdentity, supplied: Path | None) -> Iterator[Path]:
    if supplied is not None:
        root = supplied.resolve()
        verify_uquant_source_checkout(identity, root)
        yield root
        return
    with tempfile.TemporaryDirectory(prefix="firmquant-uquant-source-") as temporary:
        root = Path(temporary) / "uquant"
        _clone_reviewed_source(identity, root)
        verify_uquant_source_checkout(identity, root)
        yield root


def _build_once(identity: SourceIdentity, source_root: Path, output_directory: Path) -> Path:
    builder = source_root / "scripts/build_reproducible_wheel.py"
    if not builder.is_file():
        raise SourceIdentityError("reviewed uquant wheel builder is missing")
    _run(
        [
            sys.executable,
            str(builder),
            "--source-ref",
            identity.uquant_commit,
            "--output-dir",
            str(output_directory),
        ],
        cwd=source_root,
    )
    wheels = sorted(output_directory.glob("*.whl"))
    if len(wheels) != 1:
        raise SourceIdentityError(f"expected one uquant wheel, observed {len(wheels)}")
    wheel = wheels[0]
    verify_uquant_identity(identity, wheel_path=wheel)
    return wheel


def build_reviewed_wheel(
    *,
    source_root: Path | None,
    output_directory: Path,
    verify_twice: bool,
    force: bool,
) -> dict[str, object]:
    """Build the reviewed commit once or twice and publish only an exact artifact."""

    identity = load_locked_source_identity()
    destination = output_directory.resolve() / identity.wheel_filename
    if destination.exists() and not force:
        raise SourceIdentityError(f"refusing to overwrite existing wheel: {destination}")
    with _source_checkout(identity, source_root) as checkout:
        build_count = 2 if verify_twice else 1
        with tempfile.TemporaryDirectory(prefix="firmquant-uquant-builds-") as temporary:
            build_root = Path(temporary)
            wheels = [
                _build_once(identity, checkout, build_root / f"build-{index}")
                for index in range(1, build_count + 1)
            ]
            hashes = [wheel_sha256(wheel) for wheel in wheels]
            if len(set(hashes)) != 1:
                raise SourceIdentityError(f"uquant wheel builds are not byte-identical: {hashes}")
            output_directory.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(wheels[0], destination)
    return {
        "source_commit": identity.uquant_commit,
        "source_tree": identity.uquant_tree,
        "public_api_contract_sha256": identity.public_api_contract_sha256,
        "wheel": str(destination),
        "wheel_sha256": identity.wheel_sha256,
        "build_hashes": hashes,
        "byte_reproducible": len(set(hashes)) == 1,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build the exact reviewed uquant wheel from a clean Git commit."
    )
    parser.add_argument(
        "--source-root",
        type=Path,
        help="clean uquant checkout at the locked commit; otherwise clone the reviewed repository",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("dist/uquant"))
    parser.add_argument("--verify-twice", action="store_true")
    parser.add_argument("--force", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        result = build_reviewed_wheel(
            source_root=arguments.source_root,
            output_directory=arguments.output_dir,
            verify_twice=arguments.verify_twice,
            force=arguments.force,
        )
    except SourceIdentityError as exc:
        parser.exit(1, f"source identity verification failed: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
