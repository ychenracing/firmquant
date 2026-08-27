from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / "src/firmquant/application/operations.py"
CLI = ROOT / "src/firmquant/cli.py"
WORKFLOW = ROOT / ".github/workflows/readiness-replay-cli-wiring.yml"


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")
    return text.replace(old, new, 1)


def replace_between(text: str, start: str, end: str, new: str, *, label: str) -> str:
    start_index = text.find(start)
    if start_index < 0:
        raise RuntimeError(f"{label}: start marker missing")
    end_index = text.find(end, start_index)
    if end_index < 0:
        raise RuntimeError(f"{label}: end marker missing")
    return text[:start_index] + new + text[end_index:]


ops = OPS.read_text(encoding="utf-8")
ops = replace_once(
    ops,
    "from firmquant.broker.production_smoke import run_readonly_production_smoke\n",
    "from firmquant.application.execution_evidence import EvidenceStage\n"
    "from firmquant.application.live_readiness_runtime import collect_live_readiness\n"
    "from firmquant.application.production_identity import promotion_config_sha256\n"
    "from firmquant.application.promotion_store import PromotionStore\n"
    "from firmquant.broker.production_smoke import run_readonly_production_smoke\n",
    label="operations readiness imports",
)
ops = replace_once(
    ops,
    "from firmquant.domain.states import RuntimeState, RuntimeStatus\n",
    "from firmquant.domain.states import RuntimeState, RuntimeStatus\n"
    "from firmquant.execution.replay_runner import run_execution_replay\n",
    label="operations replay import",
)
ops = replace_once(
    ops,
    "    REPLAY = \"replay\"\n",
    "    REPLAY = \"replay\"\n    EXECUTION_REPLAY = \"execution-replay\"\n    LIVE_READINESS = \"live-readiness\"\n",
    label="operator commands",
)
ops = replace_once(
    ops,
    "    session: date | None = None\n",
    "    session: date | None = None\n    start_session: date | None = None\n    end_session: date | None = None\n",
    label="operator replay range fields",
)
ops = replace_once(
    ops,
    "        if self.session is not None and type(self.session) is not date:\n            raise TypeError(\"operator session must be a date\")\n",
    "        if self.session is not None and type(self.session) is not date:\n            raise TypeError(\"operator session must be a date\")\n"
    "        for value in (self.start_session, self.end_session):\n"
    "            if value is not None and type(value) is not date:\n"
    "                raise TypeError(\"operator replay range must contain dates\")\n"
    "        if self.command is OperatorCommand.EXECUTION_REPLAY:\n"
    "            if self.start_session is None or self.end_session is None:\n"
    "                raise ValueError(\"execution replay requires start and end sessions\")\n"
    "            if self.start_session >= self.end_session:\n"
    "                raise ValueError(\"execution replay start must precede end\")\n",
    label="operator replay range validation",
)
ops = replace_once(
    ops,
    "            OperatorCommand.REPLAY: lambda: self._replay(request),\n",
    "            OperatorCommand.REPLAY: lambda: self._replay(request),\n"
    "            OperatorCommand.EXECUTION_REPLAY: lambda: self._execution_replay(request),\n"
    "            OperatorCommand.LIVE_READINESS: lambda: self._live_readiness(),\n",
    label="operator dispatch",
)

replay_anchor = "    def _backup(self, request: OperatorRequest) -> OperatorResult:\n"
new_methods = '''    def _execution_replay(self, request: OperatorRequest) -> OperatorResult:
        settings = self._settings()
        checkout = settings.paths.uquant_source_checkout
        if checkout is None:
            raise OperatorCommandDenied("UQUANT_SOURCE_CHECKOUT_MISSING")
        source_checkout = self._resolved(checkout).resolve()
        data_root = source_checkout / "data" / "frozen"
        if request.start_session is None or request.end_session is None:
            raise OperatorCommandDenied("EXECUTION_REPLAY_RANGE_REQUIRED")
        try:
            summary = run_execution_replay(
                source_checkout=source_checkout,
                data_root=data_root,
                start=request.start_session,
                end=request.end_session,
                max_price_deviation_bps=settings.execution.max_price_deviation_bps,
            )
        except OperatorCommandDenied:
            raise
        except Exception as error:
            raise OperatorCommandDenied("EXECUTION_REPLAY_FAILED") from error
        return OperatorResult(
            message="锁定 uquant 数据已完成因果 execution-aware Replay; 未触达真实券商写接口。",
            payload=summary.payload(),
        )

    def _live_readiness(self) -> OperatorResult:
        settings, database = self._open_read_database()
        try:
            snapshot = collect_live_readiness(
                settings=settings,
                config_path=self._config_path,
                database=database,
                now=self._now(),
            )
        except Exception as error:
            raise OperatorCommandDenied("LIVE_READINESS_EVIDENCE_INVALID") from error
        finally:
            database.close()
        return OperatorResult(
            message="机器可验证生产准入门槛已只读汇总; 未创建 arm 或券商写调用。",
            payload=snapshot.payload(),
            exit_code=0 if snapshot.software_ready else 2,
        )

'''
ops = replace_once(ops, replay_anchor, new_methods + replay_anchor, label="operator new methods")

old_shadow_validated_start = "    @staticmethod\n    def _shadow_validated(database: Database) -> bool:\n"
old_shadow_validated_end = "\n    @staticmethod\n    def _active_arm_preconditions"
ops = replace_between(
    ops,
    old_shadow_validated_start,
    old_shadow_validated_end,
    "",
    label="remove shadow ready audit shortcut",
)

ops = replace_once(
    ops,
    "            if not self._shadow_validated(database):\n                raise OperatorCommandDenied(\"SHADOW_VALIDATION_REQUIRED\")\n"
    "            if self._unresolved_orders(database):\n",
    "            account_hash = self._active_arm_preconditions(database, now)\n"
    "            firmquant_commit = self._firmquant_commit()\n"
    "            try:\n"
    "                identity = StrategyIdentity.locked()\n"
    "                identity.verify()\n"
    "            except Exception as error:\n"
    "                raise OperatorCommandDenied(\"UQUANT_IDENTITY_UNAVAILABLE\") from error\n"
    "            if settings.mode is Mode.CANARY:\n"
    "                qualified = PromotionStore(database).qualifies(\n"
    "                    stage=EvidenceStage.SHADOW,\n"
    "                    firmquant_commit=firmquant_commit,\n"
    "                    uquant_commit=identity.uquant_commit,\n"
    "                    config_sha256=promotion_config_sha256(settings),\n"
    "                    account_hash=account_hash,\n"
    "                    min_sessions=settings.promotion.min_shadow_sessions,\n"
    "                    min_orders=settings.promotion.min_shadow_orders,\n"
    "                    max_tracking_error=settings.promotion.max_target_tracking_error,\n"
    "                )\n"
    "                if not qualified:\n"
    "                    raise OperatorCommandDenied(\"SHADOW_VALIDATION_REQUIRED\")\n"
    "            else:\n"
    "                try:\n"
    "                    readiness = collect_live_readiness(\n"
    "                        settings=settings,\n"
    "                        config_path=self._config_path,\n"
    "                        database=database,\n"
    "                        now=now,\n"
    "                    )\n"
    "                except Exception as error:\n"
    "                    raise OperatorCommandDenied(\"LIVE_READINESS_EVIDENCE_INVALID\") from error\n"
    "                if not readiness.software_ready:\n"
    "                    raise OperatorCommandDenied(\"LIVE_READINESS_REQUIRED\")\n"
    "            if self._unresolved_orders(database):\n",
    label="arm evidence gate",
)
ops = replace_once(
    ops,
    "            account_hash = self._active_arm_preconditions(database, now)\n"
    "            firmquant_commit = self._firmquant_commit()\n"
    "            try:\n"
    "                identity = StrategyIdentity.locked()\n"
    "                identity.verify()\n"
    "            except Exception as error:\n"
    "                raise OperatorCommandDenied(\"UQUANT_IDENTITY_UNAVAILABLE\") from error\n"
    "            binding = ArmBinding(\n",
    "            binding = ArmBinding(\n",
    label="remove duplicated arm identity setup",
)
OPS.write_text(ops, encoding="utf-8")

cli = CLI.read_text(encoding="utf-8")
cli = replace_once(
    cli,
    "    (\"replay\", \"确定性重放冻结事件\"),\n",
    "    (\"replay\", \"确定性重放冻结事件\"),\n"
    "    (\"execution-replay\", \"使用锁定 uquant 数据运行跨日 execution-aware Replay\"),\n"
    "    (\"live-readiness\", \"只读汇总全部机器可验证生产准入门槛\"),\n",
    label="CLI command help",
)
cli = replace_once(
    cli,
    "        elif name == \"replay\":\n            subparser.add_argument(\"--events\", type=Path, required=True, help=\"冻结事件文件\")\n",
    "        elif name == \"replay\":\n"
    "            subparser.add_argument(\"--events\", type=Path, required=True, help=\"冻结事件文件\")\n"
    "        elif name == \"execution-replay\":\n"
    "            subparser.add_argument(\"--start\", dest=\"start_session\", type=_session_date, required=True)\n"
    "            subparser.add_argument(\"--end\", dest=\"end_session\", type=_session_date, required=True)\n",
    label="CLI replay arguments",
)
cli = replace_once(
    cli,
    "        session=cast(date | None, getattr(arguments, \"session\", None)),\n",
    "        session=cast(date | None, getattr(arguments, \"session\", None)),\n"
    "        start_session=cast(date | None, getattr(arguments, \"start_session\", None)),\n"
    "        end_session=cast(date | None, getattr(arguments, \"end_session\", None)),\n",
    label="CLI request range",
)
CLI.write_text(cli, encoding="utf-8")
Path(__file__).unlink()
WORKFLOW.unlink()
