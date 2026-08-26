from __future__ import annotations

import re
from pathlib import Path

from firmquant.application.operations import OperatorCommand
from firmquant.config import BrokerAdapter, Mode, Settings, load_settings

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"
REQUIRED_DOCS = (
    "ARCHITECTURE.md",
    "STRATEGY_INTEGRATION.md",
    "BROKER_ADAPTER.md",
    "EXECUTION.md",
    "RISK_AND_SAFETY.md",
    "OPERATIONS.md",
    "RECOVERY.md",
    "COMPLIANCE.md",
    "DEPLOYMENT_WINDOWS.md",
    "CONFIGURATION.md",
    "DEVELOPMENT.md",
    "QUALITY.md",
    "SOURCE_BASELINE.md",
    "UPSTREAM_GAPS.md",
)
REQUIRED_ADRS = (
    "0001-modular-monolith.md",
    "0002-sqlite-single-writer.md",
    "0003-xtquant-first-adapter.md",
)


def test_canonical_document_set_exists() -> None:
    assert all((ROOT / "docs" / name).is_file() for name in REQUIRED_DOCS)
    assert all((ROOT / "docs" / "decisions" / name).is_file() for name in REQUIRED_ADRS)


def test_readme_states_the_current_safety_contract() -> None:
    text = README.read_text(encoding="utf-8")

    for required in (
        "实盘执行系统" + chr(0xFF0C) + "不是新的策略研究项目",
        "uquant 是唯一策略决策内核",
        "默认 PAPER",
        "实盘功能默认关闭",
        "券商授权 API",
        "不能保证成交",
        "程序化交易合规确认",
        "A 股 AI 产业链",
        "现金多头",
        "无杠杆",
        "禁止做空",
        "FREEZE_ONLY",
        "不自动清仓",
    ):
        assert required in text


def test_readme_never_contains_copyable_real_mode_run_command() -> None:
    text = README.read_text(encoding="utf-8").lower()

    assert "firmquant run --mode live" not in text
    assert "firmquant run --mode canary" not in text
    assert "firmquant run --mode paper" in text


def test_readme_cli_command_inventory_matches_code() -> None:
    text = README.read_text(encoding="utf-8")
    documented = set(re.findall(r"^\| `firmquant ([a-z-]+)` \|", text, flags=re.MULTILINE))

    assert documented == {command.value for command in OperatorCommand}


def test_documented_defaults_match_strict_configuration() -> None:
    defaults = Settings()
    example = load_settings(ROOT / "config" / "firmquant.example.toml")
    readme = README.read_text(encoding="utf-8")

    assert defaults.mode is Mode.PAPER
    assert defaults.live_trading_enabled is False
    assert defaults.broker.adapter is BrokerAdapter.PAPER
    assert defaults.compliance.program_trading_report_confirmed is False
    assert defaults.compliance.broker_api_authorized is False
    assert example == defaults
    assert "`live_trading_enabled = false`" in readme


def test_canonical_docs_do_not_duplicate_uquant_strategy_numeric_defaults() -> None:
    owned_by_uquant = ("60%", "75%", "95%", "0.5%")
    paths = [README]
    paths.extend(ROOT / "docs" / name for name in REQUIRED_DOCS if name != "SOURCE_BASELINE.md")

    rendered = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    assert all(value not in rendered for value in owned_by_uquant)
