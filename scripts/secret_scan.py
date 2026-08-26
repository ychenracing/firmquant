"""Fail CI when high-confidence secrets or sensitive artifacts are present."""

from __future__ import annotations

import argparse
from pathlib import Path

from firmquant.security.scanning import scan_repository


def main() -> int:
    parser = argparse.ArgumentParser(description="扫描仓库中的 secret 和敏感事故数据。")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    arguments = parser.parse_args()
    root = arguments.root.resolve(strict=True)
    violations = scan_repository(root)
    for violation in violations:
        location = violation.path.as_posix()
        if violation.line is not None:
            location += f":{violation.line}"
        print(f"{violation.code} {location}")
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
