"""Local operator CLI; unavailable use cases always fail closed."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import cast

from . import __version__

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
    ("decisions", "查询不可变策略决策快照"),
    ("orders", "查询经济意图与券商订单生命周期"),
    ("fills", "查询规范化成交事实"),
    ("report", "生成或读取 session 报告"),
    ("replay", "确定性重放冻结事件"),
    ("backup", "创建一致性状态备份"),
    ("verify-backup", "执行备份恢复验证"),
    ("cancel-system-orders", "请求取消 firmquant 拥有的未完成订单"),
)


def _fail_closed(arguments: argparse.Namespace) -> int:
    command = cast(str, arguments.command)
    print(
        f"firmquant: 命令 '{command}' 尚未启用; 没有执行券商写操作。",
        file=sys.stderr,
    )
    return 2


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
        subparser.set_defaults(handler=_fail_closed)
        if name == "run":
            subparser.add_argument(
                "--mode",
                choices=("replay", "paper", "shadow", "canary", "live"),
                default="paper",
                help="显式运行模式; 配置与运行门禁仍具有最终否决权",
            )
        elif name == "replay":
            subparser.add_argument("--events", type=Path, help="冻结事件文件")
        elif name in {"decisions", "report"}:
            subparser.add_argument("--session", help="Asia/Shanghai 策略 session, YYYY-MM-DD")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse and dispatch one local operator command."""

    arguments = build_parser().parse_args(argv)
    handler = cast(Callable[[argparse.Namespace], int], arguments.handler)
    return handler(arguments)


__all__ = ("build_parser", "main")
