from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    (ROOT / path).write_text(text, encoding="utf-8")


def once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count == 0 and new in text:
        return text
    if count != 1:
        raise RuntimeError(f"{label}: expected one fragment, found {count}")
    return text.replace(old, new, 1)


def patch_health() -> None:
    path = "src/firmquant/observability/health.py"
    text = read(path)
    text = once(
        text,
        "from firmquant.broker.xtquant import XtQuantSdkDiagnosis, diagnose_xtquant_sdk\n",
        "from firmquant.broker.xtquant import XtQuantSdkDiagnosis, diagnose_xtquant_sdk\n"
        "from firmquant.broker.xtquant_safety import XtQuantSafetyManifest\n",
        "health safety import",
    )
    text = once(
        text,
        "from firmquant.domain.broker_facts import AccountType, BrokerAccountFact\n",
        "from firmquant.domain.broker_facts import (\n"
        "    AccountType,\n    BrokerAccountFact,\n    BrokerFillFact,\n    BrokerOrderFact,\n"
        "    BrokerPositionFact,\n    InstrumentFact,\n    MarketSessionStatus,\n    QuoteFact,\n)\n"
        "from firmquant.domain.values import Symbol\n",
        "health broker facts imports",
    )
    text = once(
        text,
        "from firmquant.security.secrets import SecretBytes, SecretProvider\n",
        "from firmquant.scheduling.clock import ClockGuard, ClockObservation, ClockValidationError\n"
        "from firmquant.security.secrets import SecretBytes, SecretProvider\n",
        "health clock imports",
    )
    old = '''class ReadOnlyDoctorBroker(Protocol):
    """Narrow broker surface: a doctor can inspect facts but cannot reach writes."""

    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def health(self) -> BrokerHealth: ...

    def query_account(self) -> BrokerAccountFact: ...
'''
    new = '''class ReadOnlyDoctorBroker(Protocol):
    """Complete read authority surface; submit/cancel are deliberately absent."""

    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def health(self) -> BrokerHealth: ...

    def query_account(self) -> BrokerAccountFact: ...

    def query_positions(self) -> tuple[BrokerPositionFact, ...]: ...

    def query_orders(self) -> tuple[BrokerOrderFact, ...]: ...

    def query_fills(self) -> tuple[BrokerFillFact, ...]: ...

    def query_instrument(self, symbol: Symbol) -> InstrumentFact: ...

    def query_quote(self, symbol: Symbol) -> QuoteFact: ...

    def query_market_status(self) -> MarketSessionStatus: ...
'''
    text = once(text, old, new, "health readonly broker surface")
    text = once(
        text,
        "        data_manifest_validator: ManifestValidator | None = None,\n        repository_root: Path | None = None,\n",
        "        data_manifest_validator: ManifestValidator | None = None,\n"
        "        safety_manifest_path: Path | None = None,\n"
        "        repository_root: Path | None = None,\n",
        "health safety path parameter",
    )
    text = once(
        text,
        "        real_mode = settings.mode in {Mode.CANARY, Mode.LIVE}\n        xtquant_required = settings.broker.adapter is BrokerAdapter.XTQUANT\n",
        "        real_mode = settings.mode in {Mode.CANARY, Mode.LIVE}\n"
        "        production_mode = settings.mode in {Mode.SHADOW, Mode.CANARY, Mode.LIVE}\n"
        "        xtquant_required = settings.broker.adapter is BrokerAdapter.XTQUANT\n"
        "        safety_manifest: XtQuantSafetyManifest | None = None\n"
        "        if xtquant_required:\n"
        "            manifest_source = safety_manifest_path or settings.broker.safety_manifest_path\n"
        "            if manifest_source is not None:\n"
        "                safety_manifest = XtQuantSafetyManifest.load(Path(manifest_source))\n",
        "health production safety manifest",
    )
    old = '''        def timezone_clock() -> CheckEvidence:
            try:
                timezone = ZoneInfo(settings.timezone)
            except ZoneInfoNotFoundError as error:
                raise ValueError("configured timezone is unavailable") from error
            current = clock()
            if current.tzinfo is None or current.utcoffset() is None:
                raise ValueError("doctor clock is not timezone-aware")
            if (
                clock_drift_seconds is None
                or not isinstance(clock_drift_seconds, Decimal)
                or not clock_drift_seconds.is_finite()
            ):
                return _evidence(
                    False,
                    "CLOCK_DRIFT_UNVERIFIED",
                    timezone=timezone.key,
                    drift_verified=False,
                )
            passed = abs(clock_drift_seconds) <= maximum_clock_drift_seconds
            return _evidence(
                passed,
                "TIMEZONE_CLOCK_VERIFIED" if passed else "CLOCK_DRIFT_EXCEEDED",
                timezone=timezone.key,
                drift_verified=True,
                drift_milliseconds=int(clock_drift_seconds * 1000),
            )
'''
    new = '''        def timezone_clock() -> CheckEvidence:
            try:
                timezone = ZoneInfo(settings.timezone)
            except ZoneInfoNotFoundError as error:
                raise ValueError("configured timezone is unavailable") from error
            current = clock()
            if current.tzinfo is None or current.utcoffset() is None:
                raise ValueError("doctor clock is not timezone-aware")
            if production_mode:
                if broker is None or safety_manifest is None:
                    return _evidence(
                        False,
                        "CLOCK_DRIFT_UNVERIFIED",
                        timezone=timezone.key,
                        drift_verified=False,
                    )
                try:
                    with _broker_read_session(broker):
                        quote = broker.query_quote(safety_manifest.probe_symbol)
                    receipt = ClockGuard(
                        max_drift=timedelta(seconds=float(maximum_clock_drift_seconds))
                    ).verify(
                        ClockObservation(
                            system_time=current,
                            reference_time=quote.event_time,
                            local_timezone=settings.timezone,
                        )
                    )
                except ClockValidationError:
                    return _evidence(
                        False,
                        "CLOCK_DRIFT_EXCEEDED",
                        timezone=timezone.key,
                        drift_verified=True,
                    )
                return _evidence(
                    True,
                    "TIMEZONE_CLOCK_VERIFIED",
                    timezone=timezone.key,
                    drift_verified=True,
                    drift_milliseconds=receipt.drift_milliseconds,
                    clock_receipt_sha256=receipt.sha256,
                )
            if (
                clock_drift_seconds is None
                or not isinstance(clock_drift_seconds, Decimal)
                or not clock_drift_seconds.is_finite()
            ):
                return _evidence(
                    False,
                    "CLOCK_DRIFT_UNVERIFIED",
                    timezone=timezone.key,
                    drift_verified=False,
                )
            passed = abs(clock_drift_seconds) <= maximum_clock_drift_seconds
            return _evidence(
                passed,
                "TIMEZONE_CLOCK_VERIFIED" if passed else "CLOCK_DRIFT_EXCEEDED",
                timezone=timezone.key,
                drift_verified=True,
                drift_milliseconds=int(clock_drift_seconds * 1000),
            )
'''
    text = once(text, old, new, "health clock receipt")
    text = once(
        text,
        '''                "BROKER_SDK_READY" if passed else "BROKER_SDK_UNAVAILABLE",
''',
        '''                "BROKER_SDK_READY"
                if passed
                else "XTQUANT_SDK_UNAVAILABLE"
                if xtquant_required
                else "BROKER_SDK_UNAVAILABLE",
''',
        "health sdk unavailable code",
    )
    old = '''        def readonly_account() -> CheckEvidence:
            if broker is None:
                return _evidence(False, "READONLY_ACCOUNT_UNAVAILABLE", configured=False)
            with _broker_read_session(broker):
                account = broker.query_account()
                passed = (
                    isinstance(account, BrokerAccountFact)
                    and account.account_type is AccountType.CASH
                    and account.available_cash.value >= 0
                )
                return _evidence(
                    passed,
                    "READONLY_ACCOUNT_VERIFIED" if passed else "READONLY_ACCOUNT_INVALID",
                    configured=True,
                    account_type=account.account_type.value,
                    cash_nonnegative=account.available_cash.value >= 0,
                )
'''
    new = '''        def readonly_account() -> CheckEvidence:
            if broker is None:
                return _evidence(False, "READONLY_ACCOUNT_UNAVAILABLE", configured=False)
            if production_mode and safety_manifest is None:
                return _evidence(False, "XTQUANT_SAFETY_MANIFEST_UNAVAILABLE", configured=True)
            probe = (
                safety_manifest.probe_symbol
                if safety_manifest is not None
                else Symbol.parse("000001.SZ")
            )
            with _broker_read_session(broker):
                account = broker.query_account()
                positions = broker.query_positions()
                orders = broker.query_orders()
                fills = broker.query_fills()
                market_status = broker.query_market_status()
                instrument = broker.query_instrument(probe)
                quote = broker.query_quote(probe)
                health = broker.health()
            alias_configured = settings.broker.account_alias is not None or not production_mode
            manifest_verified = safety_manifest is not None or not production_mode
            passed = (
                isinstance(account, BrokerAccountFact)
                and account.account_type is AccountType.CASH
                and account.available_cash.value >= 0
                and isinstance(positions, tuple)
                and all(isinstance(item, BrokerPositionFact) for item in positions)
                and isinstance(orders, tuple)
                and all(isinstance(item, BrokerOrderFact) for item in orders)
                and isinstance(fills, tuple)
                and all(isinstance(item, BrokerFillFact) for item in fills)
                and isinstance(market_status, MarketSessionStatus)
                and isinstance(instrument, InstrumentFact)
                and isinstance(quote, QuoteFact)
                and alias_configured
                and manifest_verified
                and health.connected
                and health.read_healthy
            )
            return _evidence(
                passed,
                "READONLY_ACCOUNT_VERIFIED" if passed else "READONLY_ACCOUNT_INVALID",
                configured=True,
                account_type=account.account_type.value,
                cash_nonnegative=account.available_cash.value >= 0,
                position_count=len(positions),
                order_count=len(orders),
                fill_count=len(fills),
                market_status=market_status.value,
                instrument_symbol=instrument.symbol.canonical,
                quote_symbol=quote.symbol.canonical,
                account_alias_configured=alias_configured,
                safety_manifest_verified=manifest_verified,
                real_order_calls=0,
            )
'''
    text = once(text, old, new, "health comprehensive readonly authority")
    write(path, text)


def patch_smoke() -> None:
    path = "src/firmquant/broker/production_smoke.py"
    text = read(path)
    text = once(
        text,
        "from typing import dataclass",
        "from typing import dataclass",
        "noop",
    ) if False else text
    text = text.replace("from dataclasses import dataclass\n", "from dataclasses import dataclass\nfrom typing import Protocol, runtime_checkable\n")
    text = text.replace("from firmquant.broker.gateway import BrokerGateway\n", "from firmquant.broker.gateway import BrokerHealth\n")
    text = once(
        text,
        "from firmquant.domain.values import Symbol\n",
        "from firmquant.domain.broker_facts import (\n"
        "    BrokerAccountFact, BrokerFillFact, BrokerOrderFact, BrokerPositionFact,\n"
        "    InstrumentFact, MarketSessionStatus, QuoteFact,\n)\n"
        "from firmquant.domain.values import Symbol\n",
        "smoke read facts imports",
    )
    anchor = "_SHA256 = re.compile(r\"^[0-9a-f]{64}$\")\n\n\n"
    protocol = '''_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@runtime_checkable
class ReadOnlyProductionSmokeBroker(Protocol):
    """Full production read surface with no submit/cancel methods."""

    def health(self) -> BrokerHealth: ...
    def query_account(self) -> BrokerAccountFact: ...
    def query_positions(self) -> tuple[BrokerPositionFact, ...]: ...
    def query_orders(self) -> tuple[BrokerOrderFact, ...]: ...
    def query_fills(self) -> tuple[BrokerFillFact, ...]: ...
    def query_market_status(self) -> MarketSessionStatus: ...
    def query_instrument(self, symbol: Symbol) -> InstrumentFact: ...
    def query_quote(self, symbol: Symbol) -> QuoteFact: ...


'''
    text = once(text, anchor, protocol, "smoke readonly protocol")
    text = text.replace("    broker: BrokerGateway,\n", "    broker: ReadOnlyProductionSmokeBroker,\n")
    text = text.replace(
        '    if not isinstance(broker, BrokerGateway):\n        raise TypeError("production smoke broker must satisfy BrokerGateway")\n',
        '    if not isinstance(broker, ReadOnlyProductionSmokeBroker):\n        raise TypeError("production smoke broker must satisfy read-only production protocol")\n',
    )
    text = text.replace(
        '    "ProductionSmokeReceipt",\n',
        '    "ProductionSmokeReceipt",\n    "ReadOnlyProductionSmokeBroker",\n',
    )
    write(path, text)


def patch_operations() -> None:
    path = "src/firmquant/application/operations.py"
    text = read(path)
    text = once(
        text,
        "from firmquant.broker.replay import RecordedReplayBroker\n",
        "from firmquant.broker.production_smoke import run_readonly_production_smoke\n"
        "from firmquant.broker.replay import RecordedReplayBroker\n"
        "from firmquant.broker.xtquant_safety import XtQuantSafetyManifest\n",
        "operations smoke imports",
    )
    text = once(
        text,
        '    CANCEL_SYSTEM_ORDERS = "cancel-system-orders"\n',
        '    CANCEL_SYSTEM_ORDERS = "cancel-system-orders"\n    SMOKE_READONLY = "smoke-readonly"\n',
        "operations smoke enum",
    )
    text = once(
        text,
        "            OperatorCommand.CANCEL_SYSTEM_ORDERS: lambda: self._cancel_system_orders(),\n",
        "            OperatorCommand.CANCEL_SYSTEM_ORDERS: lambda: self._cancel_system_orders(),\n"
        "            OperatorCommand.SMOKE_READONLY: lambda: self._smoke_readonly(),\n",
        "operations smoke handler",
    )
    text = once(
        text,
        "            clock_drift_seconds=None,\n        )\n",
        "            clock_drift_seconds=None,\n"
        "            safety_manifest_path=(\n"
        "                None\n"
        "                if settings.broker.safety_manifest_path is None\n"
        "                else self._resolved(settings.broker.safety_manifest_path)\n"
        "            ),\n"
        "        )\n",
        "operations doctor safety path",
    )
    anchor = '''    def _cancel_system_orders(self) -> OperatorResult:
'''
    method = '''    def _smoke_readonly(self) -> OperatorResult:
        settings = self._settings()
        if settings.mode not in {Mode.SHADOW, Mode.CANARY, Mode.LIVE}:
            raise OperatorCommandDenied("MODE_NOT_PRODUCTION_XTQUANT")
        if self._doctor_broker_provider is None:
            raise OperatorCommandDenied("BROKER_CLIENT_UNAVAILABLE")
        manifest_path = settings.broker.safety_manifest_path
        if manifest_path is None:
            raise OperatorCommandDenied("XTQUANT_SAFETY_MANIFEST_MISSING")
        try:
            manifest = XtQuantSafetyManifest.load(self._resolved(manifest_path))
            identity = StrategyIdentity.locked()
            identity.verify()
            broker = self._doctor_broker_provider()
            if broker is None:
                raise OperatorCommandDenied("BROKER_CLIENT_UNAVAILABLE")
        except OperatorCommandDenied:
            raise
        except Exception as error:
            raise OperatorCommandDenied("READONLY_SMOKE_PREREQUISITES_UNAVAILABLE") from error
        database_path = self._database_path(settings)
        with WriterLease.acquire(
            database_path,
            owner="smoke-readonly",
            clock=self._clock,
        ) as writer:
            connected_here = False
            try:
                health = broker.health()
                if not health.connected:
                    broker.connect()
                    connected_here = True
                receipt = run_readonly_production_smoke(
                    broker=broker,
                    database=writer.database,
                    probe_symbol=manifest.probe_symbol,
                    firmquant_commit=self._firmquant_commit(),
                    uquant_commit=identity.uquant_commit,
                    config_sha256=self._configuration_sha256(),
                    safety_manifest_sha256=manifest.sha256,
                    clock=self._clock,
                )
            finally:
                if connected_here:
                    broker.disconnect()
        if receipt.real_order_calls != 0:
            raise OperatorCommandDenied("READONLY_SMOKE_WRITE_CALL_DETECTED")
        return OperatorResult(
            message="生产只读 smoke 已完成并持久化证据，券商写调用为 0。",
            payload={**receipt.payload(), "receipt_sha256": receipt.sha256},
        )

'''
    text = once(text, anchor, method + anchor, "operations smoke method")
    write(path, text)


def main() -> None:
    patch_health()
    patch_smoke()
    patch_operations()


if __name__ == "__main__":
    main()
