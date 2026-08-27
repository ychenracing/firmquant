"""Thin local CLI over audited application operations and local runtime controls."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

from . import __version__
from .application.control_channel import ControlCommand, ControlInbox, ControlStatus
from .application.operations import (
    OperatorCommand,
    OperatorCommandDenied,
    OperatorInteraction,
    OperatorRequest,
    OperatorResult,
    OperatorService,
    create_local_operator_service,
)
from .application.runtime_control import RuntimeControlExecutor
from .broker.gateway import BrokerGateway
from .config import Mode, Settings, load_settings
from .persistence.database import Database
from .persistence.writer_lease import WriterLease, WriterLeaseBusy

_COMMAND_HELP: tuple[tuple[str, str], ...] = (
    ("init", "初始化本地 PAPER 状态目录"),
    ("doctor", "运行环境、身份与只读连接诊断"),
    ("run", "持续运行一个明确模式的 session"),
    ("status", "显示运行状态、阻断原因或本机控制请求状态"),
    ("arm-live", "创建短时效、绑定部署身份的实盘 lease"),
    ("disarm", "撤销实盘 lease"),
    ("halt", "触发 kill switch 并停止新增订单"),
    ("stop", "请求生产 daemon 停止新增工作并干净退出"),
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
    ("cancel-system-orders", "请求安全取消 durable ledger 中 firmquant 拥有的活动订单"),
)
_CONTROL_COMMANDS = {
    "halt": ControlCommand.HALT,
    "disarm": ControlCommand.DISARM,
    "cancel-system-orders": ControlCommand.CANCEL_SYSTEM_ORDERS,
    "stop": ControlCommand.STOP,
}

type ServiceFactory = Callable[[Path], OperatorService]
type ConfirmationReader = Callable[[str], str]
type ControlBrokerFactory = Callable[[Settings, Database, Callable[[], datetime]], BrokerGateway]


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
        elif name in _CONTROL_COMMANDS:
            subparser.add_argument(
                "--reason",
                help="可选操作说明; 控制请求仅保存摘要, 不保存原文",
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
        if name == "status":
            subparser.add_argument(
                "--request-id",
                help="查询 ctrl_<sha256> 本机控制请求是否仍排队或已持久化处理",
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


def _load_control_settings(config_path: Path) -> tuple[Settings, Path]:
    if config_path.is_symlink() or not config_path.is_file():
        raise OperatorCommandDenied("CONFIGURATION_UNAVAILABLE")
    try:
        settings = load_settings(config_path)
    except Exception as error:
        raise OperatorCommandDenied("CONFIGURATION_INVALID") from error
    raw_state = settings.paths.state_directory
    state = raw_state if raw_state.is_absolute() else config_path.parent / raw_state
    if state.is_symlink() or not state.is_dir():
        raise OperatorCommandDenied("STATE_PATH_INVALID")
    return settings, state


def _queue_control(
    *,
    config_path: Path,
    command: ControlCommand,
    reason: str | None,
) -> OperatorResult:
    _, state = _load_control_settings(config_path)
    request = ControlInbox(state).enqueue(command, reason=reason)
    return OperatorResult(
        message="本机控制请求已排队; daemon 尚未确认执行结果。",
        payload={
            "command": command.value,
            "control_status": ControlStatus.QUEUED.value,
            "request_id": request.request_id,
        },
    )


def _control_status(*, config_path: Path, request_id: str) -> OperatorResult:
    _, state = _load_control_settings(config_path)
    observed = ControlInbox(state).status(request_id)
    payload: dict[str, object] = {
        "control_status": observed.status.value,
        "request_id": observed.request_id,
    }
    if observed.command is not None:
        payload["command"] = observed.command.value
    if observed.processed_at is not None:
        payload["processed_at"] = observed.processed_at.isoformat()
    if observed.outcome is not None:
        payload["outcome"] = dict(observed.outcome)
    message = (
        "本机控制请求仍在排队; 尚未确认执行。"
        if observed.status is ControlStatus.QUEUED
        else "本机控制请求状态已读取。"
    )
    return OperatorResult(message=message, payload=payload)


def _default_control_broker(
    settings: Settings,
    database: Database,
    clock: Callable[[], datetime],
) -> BrokerGateway:
    from .broker.production_factory import build_production_xtquant_gateway

    return build_production_xtquant_gateway(
        settings=settings,
        database=database,
        clock=clock,
    )


def _direct_cancel_or_queue(
    *,
    config_path: Path,
    reason: str | None,
    broker_factory: ControlBrokerFactory,
) -> OperatorResult:
    settings, state = _load_control_settings(config_path)
    database_path = state / "firmquant.sqlite3"
    inbox = ControlInbox(state)
    clock = lambda: datetime.now(UTC)
    try:
        with WriterLease.acquire(database_path, owner="operator-cancel-system-orders") as writer:
            broker: BrokerGateway | None = None
            connected = False
            executor = RuntimeControlExecutor(
                mode=settings.mode,
                writer=writer,
                broker=None,
                clock=clock,
            )
            try:
                if settings.mode in {Mode.CANARY, Mode.LIVE}:
                    broker = broker_factory(settings, writer.database, clock)
                    if not isinstance(broker, BrokerGateway):
                        raise TypeError("control broker factory returned invalid gateway")
                    broker.connect()
                    connected = True
                    executor = RuntimeControlExecutor(
                        mode=settings.mode,
                        writer=writer,
                        broker=broker,
                        clock=clock,
                    )
                request = inbox.enqueue(ControlCommand.CANCEL_SYSTEM_ORDERS, reason=reason)
                inbox.process_pending(executor.execute)
                observed = inbox.status(request.request_id)
            finally:
                if connected and broker is not None:
                    broker.disconnect()
            if executor.stop_pending:
                executor.finalize_stop()
    except WriterLeaseBusy:
        return _queue_control(
            config_path=config_path,
            command=ControlCommand.CANCEL_SYSTEM_ORDERS,
            reason=reason,
        )
    payload: dict[str, object] = {
        "command": ControlCommand.CANCEL_SYSTEM_ORDERS.value,
        "control_status": observed.status.value,
        "request_id": observed.request_id,
    }
    if observed.outcome is not None:
        payload["outcome"] = dict(observed.outcome)
    return OperatorResult(
        message="本机安全撤单请求已处理; 具体结果以 durable receipt 为准。",
        payload=payload,
    )


def _direct_stop_or_queue(*, config_path: Path, reason: str | None) -> OperatorResult:
    settings, state = _load_control_settings(config_path)
    inbox = ControlInbox(state)
    database_path = state / "firmquant.sqlite3"
    try:
        with WriterLease.acquire(database_path, owner="operator-stop") as writer:
            request = inbox.enqueue(ControlCommand.STOP, reason=reason)
            executor = RuntimeControlExecutor(
                mode=settings.mode,
                writer=writer,
                broker=None,
                clock=lambda: datetime.now(UTC),
            )
            inbox.process_pending(executor.execute)
            executor.finalize_stop()
            observed = inbox.status(request.request_id)
    except WriterLeaseBusy:
        return _queue_control(
            config_path=config_path,
            command=ControlCommand.STOP,
            reason=reason,
        )
    return OperatorResult(
        message="本机 STOP 已完成持久化; 当前没有活动 daemon writer。",
        payload={
            "command": ControlCommand.STOP.value,
            "control_status": observed.status.value,
            "request_id": request.request_id,
            "runtime_state": "DISARMED",
        },
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    service_factory: ServiceFactory = create_local_operator_service,
    control_broker_factory: ControlBrokerFactory = _default_control_broker,
    interactive_terminal: bool | None = None,
    confirmation_reader: ConfirmationReader | None = None,
    environment: Mapping[str, str] | None = None,
) -> int:
    """Parse, delegate once to the application layer, and render a safe result."""

    _configure_utf8_output()
    arguments = build_parser().parse_args(argv)
    output_json = bool(getattr(arguments, "output_json", False))
    config_path = cast(Path, arguments.config)
    raw_command = cast(str, arguments.command)

    try:
        request_id = cast(str | None, getattr(arguments, "request_id", None))
        if raw_command == "status" and request_id is not None:
            result = _control_status(config_path=config_path, request_id=request_id)
            _render(result, output_json=output_json)
            return result.exit_code
        if raw_command == "stop":
            result = _direct_stop_or_queue(
                config_path=config_path,
                reason=cast(str | None, getattr(arguments, "reason", None)),
            )
            _render(result, output_json=output_json)
            return result.exit_code
        if raw_command == "cancel-system-orders":
            result = _direct_cancel_or_queue(
                config_path=config_path,
                reason=cast(str | None, getattr(arguments, "reason", None)),
                broker_factory=control_broker_factory,
            )
            _render(result, output_json=output_json)
            return result.exit_code

        request = _request(arguments)
        terminal = sys.stdin.isatty() if interactive_terminal is None else interactive_terminal
        reader = getpass.getpass if confirmation_reader is None else confirmation_reader
        observed_environment = os.environ if environment is None else environment
        interaction = OperatorInteraction(
            interactive_terminal=terminal,
            confirmation_reader=reader,
            environment=observed_environment,
        )
        service = service_factory(config_path)
        if not isinstance(service, OperatorService):
            raise TypeError("service factory returned an invalid operator service")
        try:
            result = service.execute(request, interaction)
        except WriterLeaseBusy:
            control_command = _CONTROL_COMMANDS.get(raw_command)
            if control_command is None:
                raise
            result = _queue_control(
                config_path=config_path,
                command=control_command,
                reason=request.reason,
            )
        if not isinstance(result, OperatorResult):
            raise TypeError("operator service returned an invalid result")
    except OperatorCommandDenied as error:
        _render_denial(error, output_json=output_json)
        return 2
    except Exception as error:
        if output_json:
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
    _render(result, output_json=output_json)
    return result.exit_code


__all__ = ("build_parser", "main")
