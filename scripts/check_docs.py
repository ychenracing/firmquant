"""Validate canonical Markdown links and the documented local CLI surface."""

from __future__ import annotations

import io
import re
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from firmquant.application.operations import OperatorCommand
from firmquant.cli import build_parser

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
CANONICAL_DOCS = (
    README,
    ROOT / "AGENTS.md",
    ROOT / "docs" / "ARCHITECTURE.md",
    ROOT / "docs" / "STRATEGY_INTEGRATION.md",
    ROOT / "docs" / "BROKER_ADAPTER.md",
    ROOT / "docs" / "EXECUTION.md",
    ROOT / "docs" / "RISK_AND_SAFETY.md",
    ROOT / "docs" / "OPERATIONS.md",
    ROOT / "docs" / "RECOVERY.md",
    ROOT / "docs" / "COMPLIANCE.md",
    ROOT / "docs" / "DEPLOYMENT_WINDOWS.md",
    ROOT / "docs" / "CONFIGURATION.md",
    ROOT / "docs" / "DEVELOPMENT.md",
    ROOT / "docs" / "QUALITY.md",
    ROOT / "docs" / "SOURCE_BASELINE.md",
    ROOT / "docs" / "UPSTREAM_GAPS.md",
    ROOT / "docs" / "decisions" / "0001-modular-monolith.md",
    ROOT / "docs" / "decisions" / "0002-sqlite-single-writer.md",
    ROOT / "docs" / "decisions" / "0003-xtquant-first-adapter.md",
)
_MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
_CLI_ROW = re.compile(r"^\| `firmquant ([a-z-]+)` \|", flags=re.MULTILINE)


def _local_link_target(source: Path, raw_target: str) -> Path | None:
    target = raw_target.strip().split("#", maxsplit=1)[0]
    if not target or "://" in target or target.startswith(("mailto:", "#")):
        return None
    candidate = (source.parent / target).resolve()
    if not candidate.is_relative_to(ROOT):
        raise ValueError(f"文档链接越出仓库: {source.relative_to(ROOT)} -> {raw_target}")
    return candidate


def _check_files_and_links() -> list[str]:
    failures: list[str] = []
    for source in CANONICAL_DOCS:
        if not source.is_file():
            failures.append(f"缺少 canonical 文档: {source.relative_to(ROOT)}")
            continue
        text = source.read_text(encoding="utf-8")
        for raw_target in _MARKDOWN_LINK.findall(text):
            try:
                target = _local_link_target(source, raw_target)
            except ValueError as error:
                failures.append(str(error))
                continue
            if target is not None and not target.exists():
                failures.append(f"断开的本地链接: {source.relative_to(ROOT)} -> {raw_target}")
    return failures


def _check_readme_contract() -> list[str]:
    if not README.is_file():
        return ["README.md 不存在"]
    text = README.read_text(encoding="utf-8")
    lower = text.lower()
    failures = [
        f"README 包含可复制的真实模式命令: {forbidden}"
        for forbidden in ("firmquant run --mode live", "firmquant run --mode canary")
        if forbidden in lower
    ]
    documented = set(_CLI_ROW.findall(text))
    expected = {command.value for command in OperatorCommand}
    if documented != expected:
        failures.append(
            f"README CLI 清单不一致: missing={sorted(expected - documented)!r}, "
            f"unexpected={sorted(documented - expected)!r}"
        )
    return failures


def _check_cli_help() -> list[str]:
    failures: list[str] = []
    for command in OperatorCommand:
        output = io.StringIO()
        try:
            with redirect_stdout(output), redirect_stderr(output):
                build_parser().parse_args([command.value, "--help"])
        except SystemExit as error:
            if error.code != 0:
                failures.append(f"CLI help 失败: {command.value} (exit={error.code})")
        else:
            failures.append(f"CLI help 未按 argparse 合同退出: {command.value}")
        if "usage:" not in output.getvalue():
            failures.append(f"CLI help 缺少 usage: {command.value}")
    return failures


def main() -> int:
    failures = [
        *_check_files_and_links(),
        *_check_readme_contract(),
        *_check_cli_help(),
    ]
    for failure in failures:
        print(failure)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
