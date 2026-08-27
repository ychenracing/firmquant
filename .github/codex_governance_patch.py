from __future__ import annotations

from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"expected one match in {path}, found {count}: {old[:100]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


# Fix the three strict-mypy issues exposed by the first full core CI pass.
replace_once(
    "src/firmquant/persistence/backup.py",
    '''    deployment = {\n        "schema": "firmquant.deployment-record.v1",\n''',
    '''    deployment: dict[str, object] = {\n        "schema": "firmquant.deployment-record.v1",\n''',
)
replace_once(
    "src/firmquant/persistence/backup.py",
    '''    if complete_inputs is not None:\n        complete_member_hashes, deployment = _complete_members(\n            temporary_bundle,\n            account_state_path=Path(account_state_path),\n''',
    '''    if complete_inputs is not None:\n        if account_state_path is None:\n            raise BackupError("complete backup requires uquant AccountState")\n        complete_member_hashes, deployment = _complete_members(\n            temporary_bundle,\n            account_state_path=Path(account_state_path),\n''',
)
replace_once(
    "src/firmquant/application/production_services.py",
    '''            engine.data = replacement\n''',
    '''            setattr(engine, "data", replacement)\n''',
)

# Non-production modes do not require an operational trading-calendar file merely to run diagnostics.
replace_once(
    "src/firmquant/application/data_calendar_control.py",
    "from firmquant.config import Settings, load_settings\n",
    "from firmquant.config import Mode, Settings, load_settings\n",
)
replace_once(
    "src/firmquant/application/data_calendar_control.py",
    '''        try:\n            calendar = load_trading_calendar_manifest(self._calendar_path(settings))\n        except Exception as error:\n            raise DataCalendarControlError("CALENDAR_MANIFEST_INVALID") from error\n''',
    '''        try:\n            calendar = load_trading_calendar_manifest(self._calendar_path(settings))\n        except Exception as error:\n            if settings.mode in {Mode.REPLAY, Mode.PAPER}:\n                return {\n                    "state": "NOT_REQUIRED",\n                    "as_of": self._now().astimezone(ZoneInfo(settings.timezone)).date().isoformat(),\n                    "covered_from": None,\n                    "covered_through": None,\n                    "remaining_days": None,\n                    "calendar_sha256": None,\n                    "source": None,\n                    "source_sha256": None,\n                    "warning_threshold_days": _CALENDAR_WARNING_DAYS,\n                    "blocker": None,\n                }\n            raise DataCalendarControlError("CALENDAR_MANIFEST_INVALID") from error\n''',
)

# CLI: add operator commands and surface calendar coverage in status/doctor/report.
replace_once(
    "src/firmquant/cli.py",
    "from .application.control_channel import ControlCommand, ControlInbox, ControlStatus\n",
    '''from .application.control_channel import ControlCommand, ControlInbox, ControlStatus\nfrom .application.data_calendar_control import DataCalendarControlError, DataCalendarController\n''',
)
replace_once(
    "src/firmquant/cli.py",
    '''    ("verify-backup", "执行备份恢复验证"),\n    ("cancel-system-orders", "请求安全取消 durable ledger 中 firmquant 拥有的活动订单"),\n''',
    '''    ("verify-backup", "执行备份恢复验证"),\n    ("data-candidates", "查看隔离的历史数据重写候选"),\n    ("verify-data-candidate", "重新验证历史数据重写候选完整性"),\n    ("approve-data-candidate", "交互式批准并原子切换历史数据 generation"),\n    ("calendar-update", "交互式验证并更新权威交易日历"),\n    ("cancel-system-orders", "请求安全取消 durable ledger 中 firmquant 拥有的活动订单"),\n''',
)
replace_once(
    "src/firmquant/cli.py",
    '''        elif name == "backup":\n            subparser.add_argument(\n                "--account-state",\n                type=Path,\n                help="可选 uquant AccountState 文件; 路径不会写入审计",\n            )\n''',
    '''        elif name == "backup":\n            subparser.add_argument(\n                "--account-state",\n                type=Path,\n                help="可选 uquant AccountState 文件; 路径不会写入审计",\n            )\n        elif name in {"verify-data-candidate", "approve-data-candidate"}:\n            subparser.add_argument(\n                "--candidate-id",\n                required=True,\n                help="candidate-<sha> 历史数据重写候选标识",\n            )\n        elif name == "calendar-update":\n            subparser.add_argument(\n                "--manifest",\n                type=Path,\n                required=True,\n                help="待验证的完整 trading-calendar.json",\n            )\n''',
)
insert_helpers = '''\n\ndef _governance_result(\n    *,\n    arguments: argparse.Namespace,\n    config_path: Path,\n    terminal: bool,\n    reader: ConfirmationReader,\n    environment: Mapping[str, str],\n) -> OperatorResult | None:\n    command = cast(str, arguments.command)\n    if command not in {\n        "data-candidates",\n        "verify-data-candidate",\n        "approve-data-candidate",\n        "calendar-update",\n    }:\n        return None\n    controller = DataCalendarController(config_path)\n    try:\n        if command == "data-candidates":\n            payload = controller.list_candidates()\n            message = "历史数据重写候选已读取。"\n        elif command == "verify-data-candidate":\n            payload = controller.verify_candidate(cast(str, arguments.candidate_id))\n            message = "历史数据重写候选已重新验证。"\n        elif command == "approve-data-candidate":\n            payload = controller.approve_candidate(\n                cast(str, arguments.candidate_id),\n                interactive_terminal=terminal,\n                confirmation_reader=reader,\n                environment=environment,\n            )\n            message = "历史数据 generation 已经显式批准并原子切换。"\n        else:\n            payload = controller.update_calendar(\n                cast(Path, arguments.manifest),\n                interactive_terminal=terminal,\n                confirmation_reader=reader,\n                environment=environment,\n            )\n            message = "权威交易日历已验证并更新。"\n    except DataCalendarControlError as error:\n        raise OperatorCommandDenied(error.reason_code) from error\n    return OperatorResult(message=message, payload=payload)\n\n\ndef _attach_calendar_coverage(\n    result: OperatorResult,\n    *,\n    raw_command: str,\n    config_path: Path,\n) -> OperatorResult:\n    if raw_command not in {"status", "doctor", "report"}:\n        return result\n    try:\n        coverage = DataCalendarController(config_path).calendar_status()\n    except DataCalendarControlError as error:\n        coverage = {\n            "state": "INVALID",\n            "blocker": error.reason_code,\n        }\n    payload = dict(result.payload)\n    payload["calendar_coverage"] = dict(coverage)\n    blocker = coverage.get("blocker")\n    exit_code = result.exit_code\n    if raw_command == "status" and isinstance(blocker, str):\n        raw_blockers = payload.get("blockers", [])\n        blockers = [item for item in raw_blockers if isinstance(item, str)] if isinstance(raw_blockers, list) else []\n        payload["blockers"] = sorted(set(blockers) | {blocker})\n    if raw_command == "doctor" and coverage.get("state") in {"EXPIRED", "INVALID"}:\n        payload["passed"] = False\n        exit_code = 2\n    return OperatorResult(message=result.message, payload=payload, exit_code=exit_code)\n'''
replace_once(
    "src/firmquant/cli.py",
    '\n\ndef main(\n',
    insert_helpers + '\n\ndef main(\n',
)
replace_once(
    "src/firmquant/cli.py",
    '''        if local_control_plane and raw_command == "cancel-system-orders":\n            result = _direct_cancel_or_queue(\n                config_path=config_path,\n                reason=cast(str | None, getattr(arguments, "reason", None)),\n                broker_factory=control_broker_factory,\n            )\n            _render(result, output_json=output_json)\n            return result.exit_code\n\n        request = _request(arguments)\n        terminal = sys.stdin.isatty() if interactive_terminal is None else interactive_terminal\n        reader = getpass.getpass if confirmation_reader is None else confirmation_reader\n        observed_environment = os.environ if environment is None else environment\n        interaction = OperatorInteraction(\n''',
    '''        if local_control_plane and raw_command == "cancel-system-orders":\n            result = _direct_cancel_or_queue(\n                config_path=config_path,\n                reason=cast(str | None, getattr(arguments, "reason", None)),\n                broker_factory=control_broker_factory,\n            )\n            _render(result, output_json=output_json)\n            return result.exit_code\n\n        terminal = sys.stdin.isatty() if interactive_terminal is None else interactive_terminal\n        reader = getpass.getpass if confirmation_reader is None else confirmation_reader\n        observed_environment = os.environ if environment is None else environment\n        if local_control_plane:\n            governance = _governance_result(\n                arguments=arguments,\n                config_path=config_path,\n                terminal=terminal,\n                reader=reader,\n                environment=observed_environment,\n            )\n            if governance is not None:\n                _render(governance, output_json=output_json)\n                return governance.exit_code\n\n        request = _request(arguments)\n        interaction = OperatorInteraction(\n''',
)
replace_once(
    "src/firmquant/cli.py",
    '''        if not isinstance(result, OperatorResult):\n            raise TypeError("operator service returned an invalid result")\n''',
    '''        if not isinstance(result, OperatorResult):\n            raise TypeError("operator service returned an invalid result")\n        if local_control_plane:\n            result = _attach_calendar_coverage(\n                result,\n                raw_command=raw_command,\n                config_path=config_path,\n            )\n''',
)

# Generic session orchestrator must distinguish calendar coverage and missing decisions.
replace_once(
    "src/firmquant/application/sessions.py",
    "from firmquant.market_data.calendar import AuthoritativeTradingCalendar\n",
    "from firmquant.market_data.calendar import AuthoritativeTradingCalendar, CalendarCoverageError\n",
)
replace_once(
    "src/firmquant/application/sessions.py",
    '''                blocker="DECISION_RECEIPT_INVALID",\n''',
    '''                blocker="MISSING_DECISION",\n''',
)
replace_once(
    "src/firmquant/application/sessions.py",
    '''            except DataValidationError as exc:\n                raise SessionWorkflowError(\n                    "strategy data validation failed",\n                    blocker="STRATEGY_DATA_INVALID",\n                ) from exc\n''',
    '''            except CalendarCoverageError as exc:\n                raise SessionWorkflowError(\n                    "previous trading session is outside calendar coverage",\n                    blocker="CALENDAR_COVERAGE",\n                ) from exc\n            except DataValidationError as exc:\n                raise SessionWorkflowError(\n                    "strategy data validation failed",\n                    blocker="STRATEGY_DATA_INVALID",\n                ) from exc\n''',
)
replace_once(
    "src/firmquant/application/sessions.py",
    '''            strategy_session = self._calendar.previous_trading_session(execution_session)\n            decision = self._linked_decision(strategy_session)\n''',
    '''            try:\n                strategy_session = self._calendar.previous_trading_session(execution_session)\n            except CalendarCoverageError as exc:\n                raise SessionWorkflowError(\n                    "previous trading session is outside calendar coverage",\n                    blocker="CALENDAR_COVERAGE",\n                ) from exc\n            decision = self._linked_decision(strategy_session)\n''',
)

# Persist calendar coverage into the actual close-session report as well as CLI output.
replace_once(
    "src/firmquant/observability/reports.py",
    "from firmquant.execution.planner import ExecutionBrokerSnapshot\n" if False else "from firmquant.domain.values import Symbol\n",
    "from firmquant.domain.values import Symbol\nfrom firmquant.market_data.calendar import AuthoritativeTradingCalendar, CalendarCoverageState\n",
)
replace_once(
    "src/firmquant/observability/reports.py",
    '''    health_blockers: tuple[str, ...]\n    intent_state: str = "INTENT"\n''',
    '''    health_blockers: tuple[str, ...]\n    intent_state: str = "INTENT"\n    calendar_coverage_state: str = "UNKNOWN"\n    calendar_covered_through: date | None = None\n    calendar_remaining_days: int | None = None\n''',
)
replace_once(
    "src/firmquant/observability/reports.py",
    '''        if self.intent_state not in {"INTENT", "NO_INTENT", "MISSING_DECISION"}:\n            raise ReportError("report intent state is invalid")\n''',
    '''        if self.intent_state not in {"INTENT", "NO_INTENT", "MISSING_DECISION"}:\n            raise ReportError("report intent state is invalid")\n        if self.calendar_coverage_state not in {"HEALTHY", "WARNING", "EXPIRED", "UNKNOWN"}:\n            raise ReportError("report calendar coverage state is invalid")\n        if self.calendar_covered_through is not None and type(self.calendar_covered_through) is not date:\n            raise ReportError("report calendar coverage end must be a date")\n        if self.calendar_remaining_days is not None and (\n            isinstance(self.calendar_remaining_days, bool)\n            or not isinstance(self.calendar_remaining_days, int)\n        ):\n            raise ReportError("report calendar remaining days must be integer")\n''',
)
replace_once(
    "src/firmquant/observability/reports.py",
    '''            "intent_state": self.intent_state,\n            "funds": {\n''',
    '''            "intent_state": self.intent_state,\n            "calendar_coverage": {\n                "state": self.calendar_coverage_state,\n                "covered_through": (\n                    None\n                    if self.calendar_covered_through is None\n                    else self.calendar_covered_through.isoformat()\n                ),\n                "remaining_days": self.calendar_remaining_days,\n            },\n            "funds": {\n''',
)
replace_once(
    "src/firmquant/observability/reports.py",
    '''            f"- 意图状态: `{report.intent_state}`",\n            f"- 运行状态: `{report.runtime_state}`",\n''',
    '''            f"- 意图状态: `{report.intent_state}`",\n            f"- 日历覆盖: `{report.calendar_coverage_state}` ",\n            f"(through={report.calendar_covered_through}, remaining_days={report.calendar_remaining_days})",\n            f"- 运行状态: `{report.runtime_state}`",\n''',
)
replace_once(
    "src/firmquant/observability/reports.py",
    '''    def __init__(self, database: Database, *, clock: Callable[[], datetime]) -> None:\n''',
    '''    def __init__(\n        self,\n        database: Database,\n        *,\n        clock: Callable[[], datetime],\n        calendar: AuthoritativeTradingCalendar | None = None,\n    ) -> None:\n''',
)
replace_once(
    "src/firmquant/observability/reports.py",
    '''        self._database = database\n        self._clock = clock\n''',
    '''        self._database = database\n        self._clock = clock\n        if calendar is not None and not isinstance(calendar, AuthoritativeTradingCalendar):\n            raise TypeError("daily report calendar must be authoritative")\n        self._calendar = calendar\n''',
)
replace_once(
    "src/firmquant/observability/reports.py",
    '''        generated_at = max(evidence_times)\n        observed_clock = self._clock()\n''',
    '''        calendar_state = "UNKNOWN"\n        calendar_covered_through: date | None = None\n        calendar_remaining_days: int | None = None\n        if self._calendar is not None:\n            coverage = self._calendar.coverage_status(session, warning_days=10)\n            calendar_state = coverage.state.value\n            calendar_covered_through = coverage.covered_through\n            calendar_remaining_days = coverage.remaining_days\n            if coverage.state is CalendarCoverageState.WARNING:\n                health_blockers = tuple(sorted(set(health_blockers) | {"CALENDAR_COVERAGE_WARNING"}))\n            elif coverage.state is CalendarCoverageState.EXPIRED:\n                health_blockers = tuple(sorted(set(health_blockers) | {"CALENDAR_COVERAGE_EXPIRED"}))\n        generated_at = max(evidence_times)\n        observed_clock = self._clock()\n''',
)
replace_once(
    "src/firmquant/observability/reports.py",
    '''            health_blockers=health_blockers,\n            intent_state=intent_state,\n        )\n''',
    '''            health_blockers=health_blockers,\n            intent_state=intent_state,\n            calendar_coverage_state=calendar_state,\n            calendar_covered_through=calendar_covered_through,\n            calendar_remaining_days=calendar_remaining_days,\n        )\n''',
)
replace_once(
    "src/firmquant/observability/reports.py",
    '"schema": "firmquant.daily-report.v1",\n',
    '"schema": "firmquant.daily-report.v2",\n',
)
replace_once(
    "src/firmquant/application/production_services.py",
    '''            report = DatabaseDailyReportBuilder(self._database, clock=self._clock).build(session)\n''',
    '''            report = DatabaseDailyReportBuilder(\n                self._database,\n                clock=self._clock,\n                calendar=self._calendar,\n            ).build(session)\n''',
)
