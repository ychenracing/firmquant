from pathlib import Path

ops = Path("src/firmquant/application/operations.py")
text = ops.read_text(encoding="utf-8")
text = text.replace(
    'type ReportPort = Callable[[date | None, Database], Mapping[str, object]]\n',
    'type ReportPort = Callable[[date | None, Database], Mapping[str, object]]\n'
    'type AccountBootstrapPort = Callable[[Path | None], Mapping[str, object]]\n',
    1,
)
text = text.replace(
    '    RECONCILE = "reconcile"\n',
    '    RECONCILE = "reconcile"\n    BOOTSTRAP_ACCOUNT = "bootstrap-account"\n',
    1,
)
text = text.replace(
    '        reporter: ReportPort | None = None,\n        doctor_broker_provider: DoctorBrokerProvider | None = None,\n',
    '        reporter: ReportPort | None = None,\n        account_bootstrapper: AccountBootstrapPort | None = None,\n'
    '        doctor_broker_provider: DoctorBrokerProvider | None = None,\n',
    1,
)
text = text.replace(
    '        for dependency in (runner, reconciler, reporter, doctor_broker_provider):\n',
    '        for dependency in (\n            runner,\n            reconciler,\n            reporter,\n            account_bootstrapper,\n            doctor_broker_provider,\n        ):\n',
    1,
)
text = text.replace(
    '        self._reporter = reporter\n        self._doctor_broker_provider = doctor_broker_provider\n',
    '        self._reporter = reporter\n        self._account_bootstrapper = account_bootstrapper\n'
    '        self._doctor_broker_provider = doctor_broker_provider\n',
    1,
)
text = text.replace(
    '            OperatorCommand.RECONCILE: lambda: self._reconcile(),\n',
    '            OperatorCommand.RECONCILE: lambda: self._reconcile(),\n'
    '            OperatorCommand.BOOTSTRAP_ACCOUNT: lambda: self._bootstrap_account(request),\n',
    1,
)
marker = '    def _reconcile(self) -> OperatorResult:\n'
method = '''    def _bootstrap_account(self, request: OperatorRequest) -> OperatorResult:\n        if self._account_bootstrapper is None:\n            raise OperatorCommandDenied("ACCOUNT_BOOTSTRAP_PORT_UNAVAILABLE")\n        try:\n            payload = self._account_bootstrapper(request.account_state_path)\n        except OperatorCommandDenied:\n            raise\n        except Exception as error:\n            raise OperatorCommandDenied("ACCOUNT_BOOTSTRAP_FAILED") from error\n        if not isinstance(payload, Mapping):\n            raise OperatorCommandDenied("ACCOUNT_BOOTSTRAP_RESULT_INVALID")\n        return OperatorResult(\n            message="账户权威基线已建立; 未发送任何券商写请求。",\n            payload=payload,\n        )\n\n'''
if marker not in text:
    raise SystemExit("operations reconcile marker drifted")
text = text.replace(marker, method + marker, 1)
text = text.replace(
    '        reporter=ports.report,\n        doctor_broker_provider=ports.doctor_broker,\n',
    '        reporter=ports.report,\n        account_bootstrapper=ports.bootstrap_account,\n'
    '        doctor_broker_provider=ports.doctor_broker,\n',
    1,
)
text = text.replace(
    '    "CapabilityBoundSystemOrderCanceller",\n',
    '    "AccountBootstrapPort",\n    "CapabilityBoundSystemOrderCanceller",\n',
    1,
)
ops.write_text(text, encoding="utf-8")

cli = Path("src/firmquant/cli.py")
text = cli.read_text(encoding="utf-8")
text = text.replace(
    '    ("reconcile", "执行完整券商与本地状态对账"),\n',
    '    ("reconcile", "执行完整券商与本地状态对账"),\n'
    '    ("bootstrap-account", "一次性建立真实券商账户与 uquant AccountState 权威绑定"),\n',
    1,
)
text = text.replace(
    '        elif name == "replay":\n',
    '        elif name == "bootstrap-account":\n'
    '            subparser.add_argument(\n'
    '                "--account-state",\n'
    '                type=Path,\n'
    '                help="非空真实账户必须提供已复核的 uquant AccountState; 路径不会写入结果",\n'
    '            )\n'
    '        elif name == "replay":\n',
    1,
)
cli.write_text(text, encoding="utf-8")

composition = Path("src/firmquant/application/composition.py")
text = composition.read_text(encoding="utf-8")
text = text.replace(
    'from firmquant.strategy.identity import StrategyIdentity\n',
    'from firmquant.strategy.account_bootstrap import (\n'
    '    AccountBootstrapDenied,\n'
    '    AccountBootstrapService,\n'
    '    BootstrapDataIdentity,\n'
    ')\n'
    'from firmquant.strategy.identity import StrategyIdentity\n',
    1,
)
protocol_marker = '''class _DataStoreFactory(Protocol):\n    def __call__(self, root: Path) -> _UquantDataStore: ...\n\n\n'''
protocols = '''class _UquantUniverse(Protocol):\n    def symbols_as_of(self, as_of: date) -> tuple[str, ...]: ...\n\n\nclass _UniverseFactory(Protocol):\n    def __call__(self) -> _UquantUniverse: ...\n\n\n'''
if protocol_marker not in text:
    raise SystemExit("composition protocol marker drifted")
text = text.replace(protocol_marker, protocol_marker + protocols, 1)
method_marker = '    def cancel_system_orders(self, broker_order_ids: tuple[str, ...]) -> tuple[str, ...]:\n'
methods = '''    def _bootstrap_data_identity(\n        self,\n        settings: Settings,\n        snapshot: BrokerSnapshot,\n    ) -> BootstrapDataIdentity:\n        identity = StrategyIdentity.locked()\n        try:\n            identity.verify()\n        except Exception as error:\n            raise OperatorCommandDenied("UQUANT_IDENTITY_UNAVAILABLE") from error\n        factory = _uquant_symbol("uquant.contracts.universe", "default_ai_universe")\n        if not callable(factory):\n            raise OperatorCommandDenied("UQUANT_CONTRACT_INVALID")\n        universe = cast(_UniverseFactory, factory)()\n        try:\n            symbols = universe.symbols_as_of(snapshot.session_date)\n        except Exception as error:\n            raise OperatorCommandDenied("UQUANT_UNIVERSE_UNAVAILABLE") from error\n        if (\n            not isinstance(symbols, tuple)\n            or not symbols\n            or tuple(sorted(set(symbols))) != symbols\n            or any(not isinstance(symbol, str) or not symbol for symbol in symbols)\n        ):\n            raise OperatorCommandDenied("UQUANT_UNIVERSE_INVALID")\n        manifest = _uquant_data_manifest(\n            settings.paths.data_directory,\n            symbols,\n            as_of=snapshot.session_date.isoformat(),\n        )\n        try:\n            return BootstrapDataIdentity(\n                data_hash=manifest.digest,\n                as_of=manifest.end,\n                symbols=manifest.symbols,\n            )\n        except (TypeError, ValueError) as error:\n            raise OperatorCommandDenied("UQUANT_DATA_IDENTITY_INVALID") from error\n\n    def bootstrap_account(self, seed_path: Path | None) -> Mapping[str, object]:\n        """Establish the sole real-account binding from read-only broker facts."""\n\n        if seed_path is not None and not isinstance(seed_path, Path):\n            raise OperatorCommandDenied("ACCOUNT_STATE_SEED_INVALID")\n        settings = self._settings()\n        if settings.mode not in {Mode.SHADOW, Mode.CANARY, Mode.LIVE}:\n            raise OperatorCommandDenied("ACCOUNT_BOOTSTRAP_REQUIRES_PRODUCTION_BROKER")\n        state_directory = settings.paths.state_directory\n        if state_directory.is_symlink():\n            raise OperatorCommandDenied("STATE_PATH_INVALID")\n        try:\n            state_directory.mkdir(parents=True, exist_ok=True)\n        except OSError as error:\n            raise OperatorCommandDenied("STATE_PATH_UNAVAILABLE") from error\n        with WriterLease.acquire(\n            state_directory / "firmquant.sqlite3",\n            owner="operator-bootstrap-account",\n            clock=self._clock,\n        ) as writer:\n            gateway = self._production_gateway(settings, writer.database)\n            gateway.connect()\n            try:\n                snapshot = ReadOnlyBrokerSession(\n                    gateway=gateway,\n                    clock=self._clock,\n                ).capture_snapshot()\n            finally:\n                gateway.disconnect()\n            _persist_snapshot(writer.database, snapshot)\n            service = AccountBootstrapService(\n                database=writer.database,\n                account_path=self._account_path(settings),\n                data_identity_provider=lambda observed: self._bootstrap_data_identity(\n                    settings,\n                    observed,\n                ),\n                clock=self._clock,\n            )\n            try:\n                receipt = service.bootstrap(snapshot, seed_path=seed_path)\n            except AccountBootstrapDenied as error:\n                raise OperatorCommandDenied(error.reason_code) from error\n        return {\n            "binding_id": receipt.binding_id,\n            "account_state_sha256": receipt.account_state_sha256,\n            "broker_snapshot_sha256": receipt.broker_snapshot_sha256,\n        }\n\n'''
if method_marker not in text:
    raise SystemExit("composition cancel marker drifted")
text = text.replace(method_marker, methods + method_marker, 1)
composition.write_text(text, encoding="utf-8")

readme = Path("README.md")
text = readme.read_text(encoding="utf-8")
text = text.replace(
    '| `firmquant reconcile` | 对账券商、uquant AccountState 与 operational ledger |\n',
    '| `firmquant reconcile` | 对账券商、uquant AccountState 与 operational ledger |\n'
    '| `firmquant bootstrap-account` | 一次性建立真实券商账户、uquant AccountState 与持久 binding；非空账户必须提供已复核 seed |\n',
    1,
)
readme.write_text(text, encoding="utf-8")
