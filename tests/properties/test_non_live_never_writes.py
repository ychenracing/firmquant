from __future__ import annotations

from dataclasses import replace

import pytest
from hypothesis import given
from hypothesis import strategies as st

from firmquant.config import BrokerAdapter, BrokerSettings, Mode, Settings
from firmquant.risk.capability import WriteCapabilityDenied, WriteCapabilityFactory
from tests.unit.risk.test_capability import arm_service, context, fake_broker


@given(mode=st.sampled_from([Mode.REPLAY, Mode.PAPER, Mode.SHADOW]))
def test_non_live_mode_cannot_construct_write_capability(mode: Mode) -> None:
    service = arm_service()
    current = context(service=service)
    adapter = {
        Mode.REPLAY: BrokerAdapter.RECORDED_REPLAY,
        Mode.PAPER: BrokerAdapter.PAPER,
        Mode.SHADOW: BrokerAdapter.XTQUANT,
    }[mode]
    current = replace(
        current,
        settings=Settings(mode=mode, broker=BrokerSettings(adapter=adapter)),
    )
    broker = fake_broker()

    with pytest.raises(WriteCapabilityDenied, match="MODE_NOT_LIVE_WRITABLE"):
        WriteCapabilityFactory(arm_service=service).create(
            gateway=broker,
            context_provider=lambda operation, subject: current,
        )

    assert broker.submitted_commands == ()
    assert broker.cancelled_order_ids == ()
