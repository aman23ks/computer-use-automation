"""End-to-end: scripted discovery -> artifact -> deterministic replay against the in-process mock app."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from cua.artifact.overlay import Overlay, apply_overlay
from cua.artifact.schema import Capability
from cua.control.handoff import ControlChannel, ControlError, Controller, Decision
from cua.replay.profile import AppProfile
from cua.replay.result import ErrorCode, RunStatus
from cua.safety.policy import Policy
from cua.surface.web import WebSurface
from target_app.server import AppState

from .conftest import ROOT, VARIANT_URL, discover, replay

LEAKS = ["100234", "Demo-Only-Pass-1", "teller01", "SMITH", "900-12-3456", "1,234.56", "1234.56", "jane.smith"]
BALANCE = {"member_number": "100234"}


def assert_no_leaks(*dirs: Path) -> None:
    for d in dirs:
        for f in d.rglob("*"):
            if f.is_file() and f.suffix in (".json", ".jsonl", ".txt"):
                text = f.read_text()
                if f.name == "result.json":
                    text = json.dumps({k: v for k, v in json.loads(text).items() if k != "outputs"})
                for leak in LEAKS:
                    assert leak not in text, f"{leak!r} leaked into {f}"


@pytest.fixture(scope="module")
def balance_cap_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("cap") / "balance.json"


async def balance_cap(tmp_path: Path, policy: Policy, profile: AppProfile, cache: Path) -> Capability:
    if cache.exists():
        return Capability.model_validate_json(cache.read_text())
    cap, _ = await discover(tmp_path, policy, profile, "read_primary_savings_balance", BALANCE)
    assert cap is not None
    cache.write_text(cap.model_dump_json())
    return cap


async def test_discovery_records_parameterised_robust_artifact(tmp_path: Path, target: AppState, policy: Policy,
                                                               profile: AppProfile, balance_cap_path: Path) -> None:
    cap = await balance_cap(tmp_path, policy, profile, balance_cap_path)
    assert [s.phase for s in cap.steps[:3]] == ["auth"] * 3
    fill = next(s for s in cap.steps if s.action == "fill" and s.phase == "main")
    assert fill.value.kind == "param"  # type: ignore[union-attr]
    extract = cap.steps[-1]
    assert extract.action == "extract" and extract.target.strategies[0].kind == "table_cell"  # type: ignore[union-attr]
    assert cap.content_hash == cap.compute_hash()
    blob = cap.model_dump_json()
    for leak in LEAKS:
        assert leak not in blob.replace('"1234.56"', "")
    assert_no_leaks(tmp_path / "runs")


async def test_replay_success_is_deterministic(tmp_path: Path, target: AppState, policy: Policy, profile: AppProfile,
                                               balance_cap_path: Path) -> None:
    cap = await balance_cap(tmp_path, policy, profile, balance_cap_path)
    for _ in range(2):
        result, _ = await replay(tmp_path, policy, profile, cap, BALANCE)
        assert result.status is RunStatus.SUCCEEDED, result.error
        assert result.outputs == {"savings_balance": "1234.56"}
        assert not result.warnings and not any(s.degraded for s in result.steps)
    assert_no_leaks(tmp_path / "runs")


@pytest.mark.parametrize(("member", "code"), [("555555", "MEMBER_NOT_FOUND"), ("100999", "ACCESS_RESTRICTED")])
async def test_business_outcomes(tmp_path: Path, target: AppState, policy: Policy, profile: AppProfile,
                                 balance_cap_path: Path, member: str, code: str) -> None:
    cap = await balance_cap(tmp_path, policy, profile, balance_cap_path)
    result, _ = await replay(tmp_path, policy, profile, cap, {"member_number": member})
    assert result.status is RunStatus.BUSINESS_OUTCOME and result.outcome is not None
    assert result.outcome.code == code and result.error is None


async def test_bad_input_rejected_before_ui(tmp_path: Path, target: AppState, policy: Policy, profile: AppProfile,
                                            balance_cap_path: Path) -> None:
    cap = await balance_cap(tmp_path, policy, profile, balance_cap_path)
    result, rec = await replay(tmp_path, policy, profile, cap, {"member_number": "12AB"})
    assert result.status is RunStatus.FAILED and result.error and result.error.code is ErrorCode.INPUT_INVALID
    assert "navigate" not in (rec.dir / "events.jsonl").read_text()


@pytest.mark.parametrize(("fault", "recovery"), [
    ("interstitial", "system_notice"),
    ("session_expire", "session_expired"),
    ("alert_dialog", "pending_alerts_notice"),
])
async def test_recoverable_states(tmp_path: Path, target: AppState, policy: Policy, profile: AppProfile,
                                  balance_cap_path: Path, fault: str, recovery: str) -> None:
    cap = await balance_cap(tmp_path, policy, profile, balance_cap_path)
    target.faults.pending.append({"type": fault, "remaining": 1})
    result, _ = await replay(tmp_path, policy, profile, cap, BALANCE)
    assert result.status is RunStatus.SUCCEEDED, result.error
    assert recovery in [r.state_id for r in result.recoveries]


async def test_hard_failure_carries_evidence(tmp_path: Path, target: AppState, policy: Policy, profile: AppProfile,
                                             balance_cap_path: Path) -> None:
    cap = await balance_cap(tmp_path, policy, profile, balance_cap_path)
    target.faults.pending.append({"type": "app_error", "remaining": 1})
    result, rec = await replay(tmp_path, policy, profile, cap, BALANCE)
    assert result.status is RunStatus.FAILED and result.error is not None
    assert result.error.code is ErrorCode.APP_ERROR and result.error.step_id
    assert (rec.dir / result.error.evidence["screenshot"]).exists()
    assert (rec.dir / result.error.evidence["dom"]).exists()


async def test_handoff_on_unknown_state_resumes_same_session(tmp_path: Path, target: AppState, policy: Policy,
                                                             profile: AppProfile, balance_cap_path: Path) -> None:
    cap = await balance_cap(tmp_path, policy, profile, balance_cap_path)
    target.faults.pending.append({"type": "pwd_notice", "remaining": 1})

    async def operator(channel: ControlChannel, surface: WebSurface) -> None:
        while channel.controller is not Controller.PENDING:
            await asyncio.sleep(0.1)
        assert channel.current is not None
        lease = channel.claim(channel.current.id, "tester")
        with pytest.raises(ControlError):
            channel.assert_automation()
        obs = await surface.observe(screenshot=False)
        ref = next(r for r, e in obs.elements.items() if e["name"] == "Remind Me Later")
        frame = surface._last_frames[obs.ref_frames[ref]]
        handle = await frame.evaluate_handle("(r) => window.__cua.byRef(r)", ref)
        await handle.as_element().click()  # type: ignore[union-attr]
        channel.resolve(channel.current.id, lease, Decision.RESUME, "dismissed")

    result, rec = await replay(tmp_path, policy, profile, cap, BALANCE, operator=operator)
    assert result.status is RunStatus.SUCCEEDED, result.error
    assert len(result.interventions) == 1
    log = (rec.dir / "events.jsonl").read_text()
    for event in ("control.escalated", "control.claimed", "control.resolved", "control.returned", "replay.resumed"):
        assert event in log


async def test_irreversible_step_requires_authorization(tmp_path: Path, target: AppState, policy: Policy,
                                                        profile: AppProfile) -> None:
    params = {"member_number": "100234", "share_type": "Money Market", "nickname": "Rainy Day", "opening_deposit": "25.00"}

    async def approve(channel: ControlChannel) -> None:
        while channel.controller is not Controller.PENDING:
            await asyncio.sleep(0.1)
        assert channel.current is not None
        iv = channel.current
        lease = channel.claim(iv.id, "tester")
        channel.resolve(iv.id, lease, Decision.APPROVE, "ok")

    cap, _ = await discover(tmp_path, policy, profile, "open_share_account", params, channel_factory=True, operator=approve)
    assert cap is not None and cap.risk.approval_required_steps
    submit = next(s for s in cap.steps if s.requires_approval)
    assert submit.dialog is not None and submit.dialog.action == "accept"

    before = len(target.confirmations)
    gated, _ = await replay(tmp_path, policy, profile, cap, params)
    assert gated.status is RunStatus.NEEDS_APPROVAL and gated.approval and gated.approval.step_id == submit.id
    assert len(target.confirmations) == before

    ok, _ = await replay(tmp_path, policy, profile, cap, params, authorize_irreversible=True)
    assert ok.status is RunStatus.SUCCEEDED, ok.error
    assert ok.outputs["confirmation_number"].startswith("CU-")
    assert len(target.confirmations) == before + 1

    low, _ = await replay(tmp_path, policy, profile, cap, {**params, "opening_deposit": "1.00"}, authorize_irreversible=True)
    assert low.status is RunStatus.BUSINESS_OUTCOME and low.outcome and low.outcome.code == "VALIDATION_ERROR"
    assert_no_leaks(tmp_path / "runs")


async def test_tenant_variant_degrades_then_overlay_restores(tmp_path: Path, target: AppState, policy: Policy,
                                                             profile: AppProfile, balance_cap_path: Path) -> None:
    cap = await balance_cap(tmp_path, policy, profile, balance_cap_path)
    cap.content_hash = cap.compute_hash()
    raw, _ = await replay(tmp_path, policy, profile, cap, BALANCE, base_url=VARIANT_URL)
    assert raw.status is RunStatus.SUCCEEDED and raw.warnings
    overlay = Overlay.load(ROOT / "config" / "overlays" / "lakeshore" / "read_primary_savings_balance.yaml")
    patched, _ = await replay(tmp_path, policy, profile, apply_overlay(cap, overlay), BALANCE, base_url=VARIANT_URL)
    assert patched.status is RunStatus.SUCCEEDED and not patched.warnings
