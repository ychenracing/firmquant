from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from firmquant.config import Mode
from firmquant.risk.arm import (
    ArmBinding,
    ArmLeaseDenied,
    ArmService,
)
from firmquant.security.secrets import SecretBytes

NOW = datetime(2026, 8, 25, 1, 30, tzinfo=UTC)


def binding(
    *,
    mode: Mode = Mode.CANARY,
    hostname: str = "execution-host-a",
    account_id: str = "sensitive-account-001",
    config_sha256: str = "c" * 64,
) -> ArmBinding:
    return ArmBinding.create(
        mode=mode,
        hostname=hostname,
        account_id=account_id,
        firmquant_commit="f" * 40,
        uquant_commit="1" * 40,
        config_sha256=config_sha256,
    )


def service() -> ArmService:
    return ArmService(
        mac_key=SecretBytes(b"test-only-arm-mac-key-material-32"),
        lease_id_factory=lambda: "arm_" + "a" * 32,
    )


def issue(
    arm_service: ArmService,
    arm_binding: ArmBinding,
    *,
    ttl: timedelta = timedelta(minutes=5),
):
    return arm_service.issue(
        arm_binding,
        now=NOW,
        ttl=ttl,
        interactive_terminal=True,
        environment={},
        confirmation_reader=lambda: arm_service.confirmation_phrase(arm_binding.mode),
    )


def test_issue_and_verify_short_hmac_bound_lease_without_sensitive_identity() -> None:
    arm_service = service()
    arm_binding = binding()

    lease = issue(arm_service, arm_binding)
    arm_service.verify(lease, binding=arm_binding, now=NOW + timedelta(minutes=4))

    assert lease.expires_at == NOW + timedelta(minutes=5)
    assert lease.mode is Mode.CANARY
    assert "sensitive-account-001" not in repr(arm_binding)
    assert "execution-host-a" not in repr(arm_binding)
    assert "test-only-arm" not in repr(arm_service)


@pytest.mark.parametrize(
    "changed_binding",
    [
        binding(hostname="execution-host-b"),
        binding(account_id="different-account"),
        binding(config_sha256="d" * 64),
        ArmBinding.create(
            mode=Mode.CANARY,
            hostname="execution-host-a",
            account_id="sensitive-account-001",
            firmquant_commit="e" * 40,
            uquant_commit="1" * 40,
            config_sha256="c" * 64,
        ),
        ArmBinding.create(
            mode=Mode.CANARY,
            hostname="execution-host-a",
            account_id="sensitive-account-001",
            firmquant_commit="f" * 40,
            uquant_commit="2" * 40,
            config_sha256="c" * 64,
        ),
    ],
)
def test_lease_rejects_every_identity_binding_change(
    changed_binding: ArmBinding,
) -> None:
    arm_service = service()
    lease = issue(arm_service, binding())

    with pytest.raises(ArmLeaseDenied, match="binding"):
        arm_service.verify(lease, binding=changed_binding, now=NOW + timedelta(minutes=1))


def test_expired_or_tampered_lease_is_rejected() -> None:
    arm_service = service()
    arm_binding = binding()
    lease = issue(arm_service, arm_binding)

    with pytest.raises(ArmLeaseDenied, match="expired"):
        arm_service.verify(lease, binding=arm_binding, now=lease.expires_at)
    with pytest.raises(ArmLeaseDenied, match="authentication"):
        arm_service.verify(
            replace(lease, lease_mac="0" * 64),
            binding=arm_binding,
            now=NOW + timedelta(minutes=1),
        )


def test_non_tty_ci_and_wrong_phrase_cannot_issue_lease() -> None:
    arm_service = service()
    arm_binding = binding()
    reader_calls = 0

    def reader() -> str:
        nonlocal reader_calls
        reader_calls += 1
        return arm_service.confirmation_phrase(arm_binding.mode)

    with pytest.raises(ArmLeaseDenied, match="interactive terminal"):
        arm_service.issue(
            arm_binding,
            now=NOW,
            interactive_terminal=False,
            environment={},
            confirmation_reader=reader,
        )
    with pytest.raises(ArmLeaseDenied, match="CI"):
        arm_service.issue(
            arm_binding,
            now=NOW,
            interactive_terminal=True,
            environment={"GITHUB_ACTIONS": "true"},
            confirmation_reader=reader,
        )
    with pytest.raises(ArmLeaseDenied, match="confirmation"):
        arm_service.issue(
            arm_binding,
            now=NOW,
            interactive_terminal=True,
            environment={},
            confirmation_reader=lambda: "yes",
        )

    assert reader_calls == 0


def test_lease_ttl_is_positive_and_strictly_bounded() -> None:
    arm_service = service()

    for unsafe_ttl in (timedelta(0), timedelta(minutes=16)):
        with pytest.raises(ArmLeaseDenied, match="TTL"):
            issue(arm_service, binding(), ttl=unsafe_ttl)


@pytest.mark.parametrize("mode", [Mode.REPLAY, Mode.PAPER, Mode.SHADOW])
def test_non_live_mode_cannot_be_armed(mode: Mode) -> None:
    with pytest.raises(ArmLeaseDenied, match="CANARY or LIVE"):
        binding(mode=mode)
