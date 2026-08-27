"""Thin local CLI over audited application operations."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import cast

from . import __version__
from .application.operations import (
    OperatorCommand,
    OperatorCommandDenied,
    OperatorInteraction,
    OperatorRequest,
    OperatorResult,
    OperatorService,
    create_local_operator_service,
)
from .config import Mode

_COMMAND_HELP: tuple[tuple[str, str], ...] = (
    ("init", "初始化本地 PAPER 状态目录"),
    ("doctor", "运行环境、身份与只读连接诊断"),
    ("run", "持续运行一个明确模式的 session"),
    ("status", "显示运行状态和所有阻断原因"),
    ("arm-live", "创建短时效、绑定部署身份的实盘 lease"),
    ("disarm", "撤销实盘 lease"),
    ("halt", "触发 kill switch 并停止新增订单"),
    ("resume", "在显式复核后请求恢复"),
    ("reconcile", "执行完整券商与本地状态对账"),
    ("bootstrap-account", "一次性建立真实券商账户与 uquant AccountState 权威绑定"),
    ("decisions", "查询不可变策略决策快照"),
    ("orders", "查询经济意图与券商订单生命周期"),
    ("fills", "查询规范化成交事实"),
    ("report", "生成或读取 session 报告"),
    ("replay", "确定性重放冻结事件"),
    ("backup", "创建一致性状态备份"),
    ("verify-backup", "执行备份恢复验证"),
    ("cancel-system-orders", "请求取消 firmquant 拥有的未完成订单"),
)

type ServiceFactory = Callable[[Path], OperatorService]
type ConfirmationReader = Callable[[str], str]


def _bounded_integer(*, minimum: int, maximum: int) -> Callable[[str], int]:
    def parse(value: str) -> int:
        try:
            observed = int(value)
        except ValueError as error:
            raise argparse.ArgumentTypeError("必须是整数") from error
        if not minimum <= observed <= maximum:
            raise argparse.ArgumentTypeError(f"必须位于 {minimum} 到 {maximum} 之间")
        return observed

    return parse


def _session_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("session 必须是有效的 YYYY-MM-DD 日期") from error


def build_parser() -> argparse.ArgumentParser:
    """Build the stable operator command surface without opening broker authority."""

    parser = argparse.ArgumentParser(
        prog="firmquant",
        description="A 股 AI 产业链日频执行系统; 默认 PAPER, 实盘默认关闭。",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/firmquant.local.toml"),
        help="本地 TOML 配置路径 (不会从环境变量开启实盘)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    for name, help_text in _COMMAND_HELP:
        subparser = subparsers.add_parser(name, help=help_text, description=help_text)
        subparser.add_argument(
            "--json",
            action="store_true",
            dest="output_json",
            help="输出稳定 JSON, 适合本机运维脚本读取",
        )
        if name == "run":
            subparser.add_argument(
                "--mode",
                choices=("replay", "paper", "shadow", "canary", "live"),
                default="paper",
                help="显式运行模式; 配置与运行门禁仍具有最终否决权",
            )
        elif name == "arm-live":
            subparser.add_argument(
                "--ttl-seconds",
                type=_bounded_integer(minimum=1, maximum=900),
                default=300,
                help="短时 lease 秒数, 最多 900 秒",
            )
        elif name in {"disarm", "halt"}:
            subparser.add_argument(
                "--reason",
                help="可选操作说明; 仅保存摘要, 不保存原文",
            )
        elif name == "bootstrap-account":
            subparser.add_argument(
                "--account-state",
                type=Path,
                help="非空真实账户必须提供已复核的 uquant AccountState; 路径不会写入结果",
            )
        elif name == "replay":
            subparser.add_argument("--events", type=Path, required=True, help="冻结事件文件")
        elif name == "verify-backup":
            subparser.add_argument("--bundle", type=Path, required=True, help="备份 bundle 目录")
        elif name == "backup":
            subparser.add_argument(
                "--account-state",
                type=Path,
                help="可选 uquant AccountState 文件; 路径不会写入审计",
            )
        if name in {"decisions", "report"}:
            subparser.add_argument(
                "--session",
                type=_session_date,
                help="Asia/Shanghai 策略 session, YYYY-MM-DD",
            )
        if name in {"decisions", "orders", "fills"}:
            subparser.add_argument(
                "--limit",
                type=_bounded_integer(minimum=1, maximum=1000),
                default=100,
                help="最多返回 1-1000 条记录",
            )
    return parser


def _configure_utf8_output() -> None:
    """Keep Chinese operator output deterministic when Windows redirects stdio."""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="strict")


def _request(arguments: argparse.Namespace) -> OperatorRequest:
    command = OperatorCommand(cast(str, arguments.command))
    mode_value = getattr(arguments, "mode", None)
    return OperatorRequest(
        command=command,
        output_json=bool(getattr(arguments, "output_json", False)),
        mode=None if mode_value is None else Mode(str(mode_value).upper()),
        session=cast(date | None, getattr(arguments, "session", None)),
        events_path=cast(Path | None, getattr(arguments, "events", None)),
        bundle_path=cast(Path | None, getattr(arguments, "bundle", None)),
        account_state_path=cast(Path | None, getattr(arguments, "account_state", None)),
        ttl_seconds=int(getattr(arguments, "ttl_seconds", 300)),
        limit=int(getattr(arguments, "limit", 100)),
        reason=cast(str | None, getattr(arguments, "reason", None)),
    )


def _render(result: OperatorResult, *, output_json: bool) -> None:
    if output_json:
        print(
            json.dumps(
                dict(result.payload),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return
    print(result.message)
    if result.payload:
        print(
            json.dumps(
                dict(result.payload),
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
                sort_keys=True,
            )
        )


def _render_denial(error: OperatorCommandDenied, *, output_json: bool) -> None:
    if output_json:
        print(
            json.dumps(
                {"error": error.reason_code, "success": False},
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            file=sys.stderr,
        )
    else:
        print(
            f"firmquant: {error.reason_code}; 没有执行未授权券商写操作。",
            file=sys.stderr,
        )


def main(
    argv: Sequence[str] | None = None,
    *,
    service_factory: ServiceFactory = create_local_operator_service,
    interactive_terminal: bool | None = None,
    confirmation_reader: ConfirmationReader | None = None,
    environment: Mapping[str, str] | None = None,
) -> int:
    """Parse, delegate once to the application layer, and render a safe result."""

    _configure_utf8_output()
    arguments = build_parser().parse_args(argv)
    request = _request(arguments)
    terminal = sys.stdin.isatty() if interactive_terminal is None else interactive_terminal
    reader = getpass.getpass if confirmation_reader is None else confirmation_reader
    observed_environment = os.environ if environment is None else environment
    interaction = OperatorInteraction(
        interactive_terminal=terminal,
        confirmation_reader=reader,
        environment=observed_environment,
    )
    try:
        service = service_factory(cast(Path, arguments.config))
        if not isinstance(service, OperatorService):
            raise TypeError("service factory returned an invalid operator service")
        result = service.execute(request, interaction)
        if not isinstance(result, OperatorResult):
            raise TypeError("operator service returned an invalid result")
    except OperatorCommandDenied as error:
        _render_denial(error, output_json=request.output_json)
        return 2
    except Exception as error:
        if request.output_json:
            print(
                json.dumps(
                    {
                        "error": "OPERATION_FAILED",
                        "error_type": type(error).__name__,
                        "success": False,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
        else:
            print(
                f"firmquant: OPERATION_FAILED ({type(error).__name__}); 没有执行未授权券商写操作。",
                file=sys.stderr,
            )
        return 2
    _render(result, output_json=request.output_json)
    return result.exit_code


__all__ = ("build_parser", "main")
