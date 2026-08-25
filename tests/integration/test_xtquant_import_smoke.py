from __future__ import annotations

import importlib
import importlib.util
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from firmquant.broker.xtquant import (
    BrokerDependencyMissing,
    XtQuantBroker,
    diagnose_xtquant_sdk,
)
from firmquant.domain.values import Symbol
from tests.fixtures.xtquant_sdk_fake import OfficialSdkModules


def missing_importer(name: str) -> object:
    raise ModuleNotFoundError(name)


def test_importing_adapter_does_not_import_proprietary_sdk() -> None:
    command = (
        "import sys; import firmquant.broker.xtquant; raise SystemExit(1 if 'xtquant' in sys.modules else 0)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", command],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_missing_sdk_has_actionable_fail_closed_diagnostic(tmp_path: Path) -> None:
    diagnosis = diagnose_xtquant_sdk(importer=missing_importer)
    assert diagnosis.available is False
    assert diagnosis.readonly_smoke_completed is False
    assert "official MiniQMT/XtQuant SDK" in diagnosis.message

    with pytest.raises(BrokerDependencyMissing, match="official MiniQMT/XtQuant SDK"):
        XtQuantBroker.load_sdk(
            userdata_path=tmp_path,
            session_id=123456,
            account_id="placeholder-account",
            clock=lambda: datetime(2026, 8, 25, tzinfo=UTC),
            importer=missing_importer,
        )


def test_local_sdk_import_schema_smoke_is_read_only() -> None:
    """This never creates a trader, connects an account, submits, or cancels."""

    installed = importlib.util.find_spec("xtquant") is not None
    diagnosis = diagnose_xtquant_sdk()
    assert diagnosis.available is installed
    assert diagnosis.readonly_smoke_completed is False
    assert diagnosis.real_order_calls == 0


def test_documented_official_signatures_load_lazily_and_remain_read_only(
    tmp_path: Path,
) -> None:
    modules = OfficialSdkModules()
    assert modules.imported == []

    gateway = XtQuantBroker.load_sdk(
        userdata_path=tmp_path,
        session_id=123456,
        account_id="account-001",
        clock=lambda: datetime(2026, 8, 25, 1, 31, tzinfo=UTC),
        importer=modules.importer,
        safety_facts=modules.contract,
    )
    assert modules.imported == [
        "xtquant.xttrader",
        "xtquant.xttype",
        "xtquant.xtdata",
        "xtquant.xtconstant",
    ]

    gateway.connect()
    assert gateway.query_account().available_cash.canonical == "100000"
    assert gateway.query_quote(Symbol.parse("600519.SH")).last_price is not None
    gateway.disconnect()

    assert modules.contract.order_calls == []
    assert modules.contract.cancel_calls == []
