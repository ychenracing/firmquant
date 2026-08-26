"""High-confidence repository secret and sensitive-artifact scanning."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_MAX_FILE_BYTES = 16 * 1024 * 1024
_SENSITIVE_NAMES = frozenset(
    {
        ".env",
        "secrets.json",
        "secrets.toml",
        "secrets.yaml",
        "secrets.yml",
        "credentials.json",
    }
)
_SENSITIVE_SUFFIXES = frozenset({".pem", ".p12", ".pfx", ".key"})
_MINIQMT_USERDATA_DIRECTORIES = frozenset({"miniqmt_userdata", "userdata_mini", "xtquant_userdata"})
_PATTERNS = (
    ("GITHUB_TOKEN", re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}")),
    ("OPENAI_TOKEN", re.compile(r"sk-[A-Za-z0-9_-]{30,}")),
    ("AWS_ACCESS_KEY", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("PRIVATE_KEY", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
)
_ACCOUNT_IDENTIFIER = re.compile(
    r"(?i)[\"']?(?:account_id|account_number|account_no)[\"']?"
    r"\s*[:=]\s*[\"']?([0-9]{8,})[\"']?"
)
_EXCLUDED_PARTS = frozenset(
    {
        ".git",
        ".hypothesis",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".uv-cache",
        ".venv",
        "__pycache__",
        "build",
        "dist",
    }
)
_EXCLUDED_NAMES = frozenset({".coverage"})


@dataclass(frozen=True, slots=True)
class ScanViolation:
    path: Path
    code: str
    line: int | None


def _relative(root: Path, path: Path) -> Path:
    try:
        return path.resolve(strict=False).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise ValueError("secret scan path escapes repository root") from error


def scan_paths(root: Path, paths: tuple[Path, ...]) -> tuple[ScanViolation, ...]:
    """Scan explicit repository files without ever returning matched secret text."""

    repository = Path(root)
    if repository.is_symlink() or not repository.is_dir():
        raise ValueError("secret scan root must be a regular directory")
    violations: list[ScanViolation] = []
    for candidate in paths:
        path = Path(candidate)
        relative = _relative(repository, path)
        if path.is_symlink():
            violations.append(ScanViolation(relative, "SYMLINK_FILE", None))
            continue
        if not path.is_file():
            continue
        if _MINIQMT_USERDATA_DIRECTORIES.intersection(part.casefold() for part in relative.parts[:-1]):
            violations.append(ScanViolation(relative, "MINIQMT_USERDATA_FILE", None))
        if path.name.casefold() in _SENSITIVE_NAMES or path.suffix.casefold() in _SENSITIVE_SUFFIXES:
            violations.append(ScanViolation(relative, "SENSITIVE_FILENAME", None))
        try:
            if path.stat().st_size > _MAX_FILE_BYTES:
                violations.append(ScanViolation(relative, "FILE_TOO_LARGE", None))
                continue
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            violations.append(ScanViolation(relative, "NON_UTF8_FILE", None))
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            for code, pattern in _PATTERNS:
                if pattern.search(line):
                    violations.append(ScanViolation(relative, code, line_number))
            if _ACCOUNT_IDENTIFIER.search(line):
                code = (
                    "REAL_ACCOUNT_SNAPSHOT"
                    if path.suffix.casefold() == ".json" and "account" in path.name.casefold()
                    else "ACCOUNT_IDENTIFIER"
                )
                violations.append(ScanViolation(relative, code, line_number))
    return tuple(sorted(set(violations), key=lambda item: (item.path.as_posix(), item.code, item.line or 0)))


def repository_paths(root: Path) -> tuple[Path, ...]:
    repository = Path(root)
    return tuple(
        sorted(
            (
                path
                for path in repository.rglob("*")
                if path.is_file()
                and path.name not in _EXCLUDED_NAMES
                and not _EXCLUDED_PARTS.intersection(path.relative_to(repository).parts)
            ),
            key=lambda path: path.relative_to(repository).as_posix(),
        )
    )


def scan_repository(root: Path) -> tuple[ScanViolation, ...]:
    return scan_paths(root, repository_paths(root))


__all__ = ("ScanViolation", "repository_paths", "scan_paths", "scan_repository")
