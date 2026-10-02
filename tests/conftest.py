from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from cua.agent.discovery import DiscoveryAgent
from cua.agent.goal import GoalSpec
from cua.agent.planner import ScriptedPlanner
from cua.artifact.recorder import FlowRecorder
from cua.artifact.schema import Capability
from cua.control.handoff import ControlChannel
from cua.evidence import RunRecorder
from cua.replay.engine import ReplayEngine
from cua.replay.profile import AppProfile, load_profile
from cua.replay.result import ReplayResult
from cua.safety.policy import Policy
from cua.surface.web import WebSurface
from target_app.server import STATE, AppState, FaultState, create_app

ROOT = Path(__file__).resolve().parents[1]
BASE_PORT, VARIANT_PORT = 8691, 8692
BASE_URL, VARIANT_URL = f"http://127.0.0.1:{BASE_PORT}", f"http://127.0.0.1:{VARIANT_PORT}"


@pytest.fixture(autouse=True)
def _secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CUA_SECRET_TELLER_USERNAME", "teller01")
    monkeypatch.setenv("CUA_SECRET_TELLER_PASSWORD", "Demo-Only-Pass-1")


@pytest.fixture(scope="session")
async def apps() -> AsyncIterator[dict[str, AppState]]:
    runners, states = [], {}
    for name, variant, port in (("base", "base", BASE_PORT), ("lakeshore", "lakeshore", VARIANT_PORT)):
        app = create_app(variant)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()
        runners.append(runner)
        states[name] = app[STATE]
    yield states
    for r in runners:
        await r.cleanup()


@pytest.fixture
def target(apps: dict[str, AppState]) -> AppState:
    apps["base"].faults = FaultState()
    return apps["base"]


@pytest.fixture
def policy() -> Policy:
    p = Policy.load(ROOT / "config" / "policy.yaml")
    p.allowed_origins = [BASE_URL, VARIANT_URL]
    return p


@pytest.fixture
def profile() -> AppProfile:
    return load_profile("coreone-teller@1", ROOT / "config" / "profiles")


def goal(name: str) -> GoalSpec:
    return GoalSpec.load(ROOT / "config" / "goals" / f"{name}.yaml")


def script(name: str) -> list[dict[str, Any]]:
    return list(json.loads((ROOT / "config" / "scripts" / f"{name}.json").read_text()))


async def discover(tmp_path: Path, policy: Policy, profile: AppProfile, name: str, params: dict[str, str],
                   channel_factory: Any = None, operator: Any = None) -> tuple[Capability | None, RunRecorder]:
    g = goal(name)
    rec = RunRecorder(tmp_path / "runs", "discovery", policy.redactor())
    async with WebSurface(policy, rec.redactor) as surface:
        channel = ControlChannel(rec, timeout_s=20) if channel_factory else None
        flow = FlowRecorder(g, profile)
        agent = DiscoveryAgent(g, profile, policy, surface, ScriptedPlanner(script(name)), rec, flow,
                               base_url=BASE_URL, params=params, channel=channel)
        task = asyncio.create_task(operator(channel)) if operator and channel else None
        outcome = await agent.run()
        if task is not None:
            await task
    rec.close()
    if outcome.status != "success" or agent.success is None:
        return None, rec
    cap = flow.build(success=agent.success, run_id=rec.run_id, planner="scripted", recorded_against="test", version="1.0.0")
    return cap, rec


async def replay(tmp_path: Path, policy: Policy, profile: AppProfile, cap: Capability, params: dict[str, str], *,
                 base_url: str = BASE_URL, operator: Any = None, **kw: Any) -> tuple[ReplayResult, RunRecorder]:
    rec = RunRecorder(tmp_path / "runs", "replay", policy.redactor())
    async with WebSurface(policy, rec.redactor) as surface:
        channel = ControlChannel(rec, timeout_s=20) if operator else None
        engine = ReplayEngine(cap, profile, policy, surface, rec, base_url=base_url, channel=channel,
                              escalate=operator is not None, **kw)
        task = asyncio.create_task(operator(channel, surface)) if operator else None
        result = await engine.run(params)
        if task is not None:
            await task
    rec.close()
    return result, rec
