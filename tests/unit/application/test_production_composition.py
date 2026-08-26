from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from firmquant.application.composition import ConfiguredOperatorPorts
from firmquant.application.production_runtime import ProductionRuntimeReceipt
from firmquant.config import Mode
from firmquant.persistence.database import Database
from tests.integration.test_cli_operations import paper_config

NOW = datetime(2026, 8, 25, 1, 31, tzinfo=UTC)


def _write_shadow_config(path: Path, runtime_root: Path) -> None:
    userdata = runtime_root / "userdata"
    data = runtime_root / "data"
    source = runtime_root / "uquant-source"
    for directory in (userdata, data, source):
        directory.mkdir(parents=True, exist_ok=True)
    safety = runtime_root / "xtquant-safety.json"
    safety.write_text("{}", encoding="utf-8")
    path.write_text(
        f'''schema_version = 1
mode = "SHADOW"
live_trading_enabled = false
timezone = "Asia/Shanghai"
[broker]
adapter = "XTQUANT"
account_alias = "account-001"
xtquant_userdata_path = "{userdata.as_posix()}"
session_id = 123456
safety_manifest_path = "{safety.as_posix()}"
[paths]
state_directory = "{(runtime_root / "state").as_posix()}"
data_directory = "{data.as_posix()}"
report_directory = "{(runtime_root / "reports").as_posix()}"
backup_directory = "{(runtime_root / "backups").as_posix()}"
uquant_source_checkout = "{source.as_posix()}"
[compliance]
program_trading_report_confirmed = false
broker_api_authorized = false
''',
        encoding="utf-8",
    )


def test_real_mode_run_delegates_to_long_running_production_runtime(tmp_path: Path) -> None:
    config = tmp_path / "firmquant.toml"
    _write_shadow_config(config, tmp_path)
    calls: list[Mode] = []

    class Runtime:
        def run(self) -> ProductionRuntimeReceipt:
            calls.append(Mode.SHADOW)
            return ProductionRuntimeReceipt(
                mode=Mode.SHADOW,
                started_at=NOW,
                stopped_at=NOW,
                startup_reconciliation_id="recon_" + "a" * 64,
                event_count=3,
                decision_count=1,
                execution_count=0,
                eod_count=1,
                writer_renewals=2,
                real_order_calls=0,
                stopped_cleanly=True,
            )

    ports = ConfiguredOperatorPorts(
        config_path=config,
        clock=lambda: NOW,
        production_runtime_factory=lambda _settings, _writer: Runtime(),
    )

    payload = ports.run(Mode.SHADOW)

    assert calls == [Mode.SHADOW]
    assert payload["mode"] == "SHADOW"
    assert payload["runtime_state"] == "DISARMED"
    assert payload["real_order_calls"] == 0
    assert payload["writer_renewals"] == 2


def test_paper_mode_does_not_construct_production_runtime(tmp_path: Path) -> None:
    config = tmp_path / "firmquant.toml"
    paper_config(config)
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "reports").mkdir(exist_ok=True)
    (tmp_path / "backups").mkdir(exist_ok=True)

    def forbidden(_settings: object, _writer: object) -> object:
        raise AssertionError("PAPER must not construct production runtime")

    ports = ConfiguredOperatorPorts(
        config_path=config,
        clock=lambda: NOW,
        production_runtime_factory=forbidden,
    )
    assert callable(ports.run)


def test_production_gateway_receives_active_database_for_economic_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "firmquant.toml"
    _write_shadow_config(config, tmp_path)
    ports = ConfiguredOperatorPorts(config_path=config, clock=lambda: NOW)
    settings = ports._settings()
    database = Database.open(tmp_path / "runtime.sqlite3")
    observed: list[Database] = []
    gateway = SimpleNamespace()

    def factory(*, settings: object, database: Database, clock: object) -> object:
        del settings, clock
        observed.append(database)
        return gateway

    monkeypatch.setattr(
        "firmquant.application.composition.build_production_xtquant_gateway",
        factory,
    )
    try:
        assert ports._production_gateway(settings, database) is gateway
        assert observed == [database]
    finally:
        database.close()
