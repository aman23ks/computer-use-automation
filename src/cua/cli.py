"""cua: discover, review, approve, replay and catalogue computer-use capabilities."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import aiohttp
import yaml
from pydantic import BaseModel, ConfigDict, Field

from cua.agent.discovery import DiscoveryAgent
from cua.agent.goal import GoalSpec
from cua.agent.planner import ClaudePlanner, Planner, ScriptedPlanner
from cua.artifact.overlay import Overlay, apply_overlay
from cua.artifact.recorder import FlowRecorder, RecordingError
from cua.artifact.schema import Capability, Status, export_json_schema
from cua.artifact.store import ArtifactError, ArtifactStore
from cua.control.console import ConsoleServer
from cua.control.handoff import ControlChannel, StderrSink
from cua.evidence import RunRecorder
from cua.replay.engine import ReplayEngine
from cua.replay.params import SecretUnavailable
from cua.replay.profile import load_profile
from cua.replay.result import ErrorCode, Failure, ReplayResult, RunStatus
from cua.safety.policy import Policy
from cua.surface.web import WebSurface

EXIT = {RunStatus.SUCCEEDED: 0, RunStatus.BUSINESS_OUTCOME: 2, RunStatus.NEEDS_APPROVAL: 3, RunStatus.FAILED: 1}


class Tenant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant: str
    base_url: str
    overlays: list[str] = Field(default_factory=list)


class Home:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.config = root / "config"
        self.runs = root / "runs"
        self.store = ArtifactStore(root / "artifacts")

    def policy(self) -> Policy:
        return Policy.load(self.config / "policy.yaml")

    def tenant(self, name: str) -> Tenant:
        return Tenant.model_validate(yaml.safe_load((self.config / "tenants" / f"{name}.yaml").read_text()))


def _load_dotenv(root: Path) -> None:
    env = root / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            if value.strip():
                os.environ.setdefault(key.strip(), value.strip())


def _params(pairs: list[str]) -> dict[str, str]:
    out = {}
    for p in pairs:
        key, sep, value = p.partition("=")
        if not sep:
            raise SystemExit(f"--param expects name=value, got {p!r}")
        out[key] = value
    return out


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


async def cmd_discover(home: Home, args: argparse.Namespace) -> int:
    goal = GoalSpec.load(Path(args.goal))
    tenant = home.tenant(args.tenant)
    policy = home.policy()
    profile = load_profile(goal.profile, home.config / "profiles")
    redactor = policy.redactor()
    recorder = RunRecorder(home.runs, "discovery", redactor)
    params = _params(args.param)
    missing = [p.name for p in goal.inputs if p.required and p.name not in params]
    if missing:
        raise SystemExit(f"discovery needs example values for inputs: {missing} (use --param name=value)")
    max_steps = goal.max_steps or policy.max_discovery_steps
    planner: Planner
    if args.planner == "claude":
        planner = ClaudePlanner(goal, tenant.base_url, max_steps, recorder, model=args.model)
    else:
        planner = ScriptedPlanner(json.loads(Path(args.planner.removeprefix("script:")).read_text()))

    async with AsyncExitStack() as stack:
        surface = await stack.enter_async_context(WebSurface(policy, redactor, headless=not args.headed))
        channel = None
        if not args.no_console:
            channel = ControlChannel(recorder, [StderrSink(f"http://127.0.0.1:{args.console_port}/")], timeout_s=policy.human_timeout_s)
            console = await stack.enter_async_context(ConsoleServer(channel, surface, port=args.console_port))
            print(f"operator console: {console.url}", file=sys.stderr)
        flow = FlowRecorder(goal, profile)
        agent = DiscoveryAgent(goal, profile, policy, surface, planner, recorder, flow, base_url=tenant.base_url,
                               params=params, channel=channel)
        try:
            outcome = await agent.run()
        except SecretUnavailable as e:
            raise SystemExit(f"secret {e} not configured: set CUA_SECRET_{str(e).strip(chr(39)).upper()}") from e

    summary: dict[str, Any] = {"run_id": recorder.run_id, "status": outcome.status, "message": outcome.message,
                               "steps_taken": outcome.steps_taken, "evidence_dir": str(recorder.dir)}
    if outcome.status == "success" and agent.success is not None:
        try:
            draft = flow.build(success=agent.success, run_id=recorder.run_id, planner=planner.name,
                               recorded_against=f"{tenant.tenant} ({tenant.base_url})", version="0.0.0")
            version, changed = home.store.next_version(draft.id, draft.compute_hash())
            draft.version = version
            draft.status = Status.DRAFT
            path = home.store.save(draft)
            recorder.write_json("artifact.json", draft.model_dump(mode="json", exclude_none=True))
            summary |= {"artifact": draft.ref, "artifact_path": str(path), "new_version": changed,
                        "steps": len(draft.steps), "content_hash": draft.content_hash}
            recorder.event("artifact.saved", ref=draft.ref, path=str(path), content_hash=draft.content_hash)
        except RecordingError as e:
            summary |= {"status": "recording_failed", "message": str(e)}
    recorder.write_json("summary.json", summary)
    recorder.close()
    _print(summary)
    return 0 if summary["status"] == "success" else 1


async def _replay_once(home: Home, cap: Capability, tenant: Tenant, args: argparse.Namespace) -> ReplayResult:
    policy = home.policy()
    profile = load_profile(cap.app.profile, home.config / "profiles")
    redactor = policy.redactor()
    recorder = RunRecorder(home.runs, "replay", redactor)
    recorder.write_json("artifact.json", cap.model_dump(mode="json", exclude_none=True))
    async with AsyncExitStack() as stack:
        surface = await stack.enter_async_context(WebSurface(policy, redactor, headless=not args.headed))
        channel = None
        if args.escalate:
            channel = ControlChannel(recorder, [StderrSink(f"http://127.0.0.1:{args.console_port}/")], timeout_s=policy.human_timeout_s)
            console = await stack.enter_async_context(ConsoleServer(channel, surface, port=args.console_port))
            print(f"operator console: {console.url}", file=sys.stderr)
        engine = ReplayEngine(cap, profile, policy, surface, recorder, base_url=tenant.base_url, channel=channel,
                              authorize_irreversible=args.authorize_irreversible, escalate=args.escalate, tenant=tenant.tenant)
        result = await engine.run(_params(args.param))
    recorder.close()
    return result


def _effective(home: Home, ref: str, tenant: Tenant) -> Capability:
    cap = home.store.load(ref)
    for rel in tenant.overlays:
        cap = apply_overlay(cap, Overlay.load(home.root / rel))
    return cap


async def cmd_replay(home: Home, args: argparse.Namespace) -> int:
    tenant = home.tenant(args.tenant)
    try:
        cap = _effective(home, args.capability, tenant)
    except ArtifactError as e:
        raise SystemExit(str(e)) from e
    if cap.status is not Status.APPROVED and not args.allow_draft:
        result = ReplayResult(run_id="-", capability=cap.ref, tenant=tenant.tenant, status=RunStatus.FAILED,
                              error=Failure(code=ErrorCode.ARTIFACT_NOT_APPROVED, message=f"{cap.ref} is {cap.status}; "
                                            "approve it (cua approve) or pass --allow-draft for supervised runs"))
        _print(result.model_dump(mode="json", exclude_none=True))
        return 1
    results = []
    for _ in range(args.repeat):
        results.append(await _replay_once(home, cap, tenant, args))
    last = results[-1]
    if args.repeat > 1:
        ok = sum(r.status is RunStatus.SUCCEEDED for r in results)
        stats = {"capability": cap.ref, "tenant": tenant.tenant, "runs": len(results), "succeeded": ok,
                 "stability": round(ok / len(results), 3), "statuses": [r.status.value for r in results],
                 "degraded_runs": sum(any(s.degraded for s in r.steps) for r in results),
                 "mean_duration_ms": int(sum(r.duration_ms for r in results) / len(results))}
        stats_path = home.store.root / cap.id / f"{cap.version}.{tenant.tenant}.stats.json"
        stats_path.write_text(json.dumps(stats, indent=2) + "\n")
        _print(stats)
    else:
        _print(last.model_dump(mode="json", exclude_none=True))
    return EXIT[last.status]


def cmd_approve(home: Home, args: argparse.Namespace) -> int:
    cap = home.store.approve(args.capability, args.by, args.note)
    _print({"approved": cap.ref, "by": args.by, "content_hash": cap.content_hash})
    return 0


def cmd_show(home: Home, args: argparse.Namespace) -> int:
    cap = home.store.load(args.capability)
    print(f"{cap.ref}  [{cap.status}]  {cap.title}\n  {cap.description}")
    print(f"  app: {cap.app.product} (profile {cap.app.profile})  max effect: {cap.risk.max_effect}"
          f"  approval steps: {cap.risk.approval_required_steps or 'none'}")
    print(f"  inputs:  {[(p.name, p.type.value, p.sensitivity.value) for p in cap.inputs]}")
    print(f"  secrets: {[s.name for s in cap.secrets]}")
    print(f"  outputs: {[(o.name, o.type.value, o.sensitivity.value) for o in cap.outputs]}")
    print(f"  outcomes: {[o.code for o in cap.outcomes]}")
    print(f"  provenance: run {cap.provenance.discovery_run_id} by {cap.provenance.planner} against {cap.provenance.recorded_against}")
    for s in cap.steps:
        target = getattr(s, "target", None)
        loc = " > ".join(f"{st.kind}({st.score})" for st in target.strategies) if target else ""
        flags = ("  [APPROVAL]" if s.requires_approval else "") + (f"  [{s.recorded_by}]" if s.recorded_by != "agent" else "")
        print(f"  {s.id} {s.phase:<4} {s.effect.value:<12} {s.intent}{flags}\n        locators: {loc}")
        if s.expect:
            print(f"        expect: {[c.model_dump(exclude_none=True) for c in s.expect.all]}")
    print(f"  success: {[c.model_dump(exclude_none=True) for c in cap.success.all]}")
    return 0


def cmd_catalog(home: Home, args: argparse.Namespace) -> int:
    caps = [c for c in home.store.all() if args.include_drafts or c.status is Status.APPROVED]
    _print([{"capability": c.ref, "status": c.status.value, "tool": c.tool_definition()} for c in caps])
    return 0


def cmd_schema(home: Home, args: argparse.Namespace) -> int:
    _print(export_json_schema())
    return 0


async def cmd_faults(home: Home, args: argparse.Namespace) -> int:
    url = home.tenant(args.tenant).base_url + "/__faults"
    async with aiohttp.ClientSession() as http:
        if args.clear:
            resp = await http.delete(url)
        else:
            payload: dict[str, Any] = {"faults": [{"type": f, "count": args.count} for f in args.add]}
            if args.slow_ms is not None:
                payload["slow_ms"] = args.slow_ms
            resp = await http.post(url, json=payload)
        _print(await resp.json())
    return 0


async def cmd_operator_bot(home: Home, args: argparse.Namespace) -> int:
    """Scripted stand-in for a human at the operator console; uses only the console's public API."""
    base = args.console.rstrip("/")
    async with aiohttp.ClientSession() as http:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + args.wait_s
        iv = None
        while loop.time() < deadline:
            try:
                state = await (await http.get(f"{base}/api/state")).json()
            except aiohttp.ClientError:
                await asyncio.sleep(0.5)
                continue
            if state.get("intervention") and state["intervention"]["status"] == "pending":
                iv = state["intervention"]
                break
            await asyncio.sleep(0.5)
        if iv is None:
            print("operator-bot: no intervention appeared", file=sys.stderr)
            return 1
        print(f"operator-bot: intervention {iv['id']} ({iv['reason']}): {iv['message']}", file=sys.stderr)
        lease = (await (await http.post(f"{base}/api/interventions/{iv['id']}/claim", json={"operator": args.operator})).json())["lease"]
        headers = {"X-Lease": lease}
        for text in args.click:
            await asyncio.sleep(args.think_s)
            els = (await (await http.get(f"{base}/api/session/elements", headers=headers)).json())["elements"]
            match = next((e for e in els if (e.get("name") or "").strip() == text), None)
            if match is None:
                print(f"operator-bot: nothing named {text!r} on screen", file=sys.stderr)
                continue
            await http.post(f"{base}/api/session/click", headers=headers, json=match["center"])
            print(f"operator-bot: clicked {text!r}", file=sys.stderr)
            await asyncio.sleep(1.0)
        await asyncio.sleep(args.think_s)
        resp = await http.post(f"{base}/api/interventions/{iv['id']}/resolve", headers=headers,
                               json={"decision": args.decision, "note": args.note})
        print(f"operator-bot: resolved with {args.decision}: {await resp.json()}", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cua", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="LLM-driven discovery run that records a capability artifact")
    d.add_argument("goal")
    d.add_argument("--tenant", default="meridian")
    d.add_argument("--param", action="append", default=[], metavar="NAME=VALUE", help="example input value for this run")
    d.add_argument("--planner", default="claude", help="'claude' or 'script:<path.json>' (offline, deterministic)")
    d.add_argument("--model", default=None)
    d.add_argument("--headed", action="store_true")
    d.add_argument("--no-console", action="store_true", help="no operator channel (escalations end the run)")
    d.add_argument("--console-port", type=int, default=8765)

    r = sub.add_parser("replay", help="deterministic replay of a saved capability (no LLM)")
    r.add_argument("capability", help="capability id, id@version")
    r.add_argument("--tenant", default="meridian")
    r.add_argument("--param", action="append", default=[], metavar="NAME=VALUE")
    r.add_argument("--authorize-irreversible", action="store_true", help="caller authorizes irreversible steps for this invocation")
    r.add_argument("--escalate", action="store_true", help="route approvals/unrecoverable failures to the operator console")
    r.add_argument("--allow-draft", action="store_true")
    r.add_argument("--repeat", type=int, default=1, help="replay N times and report stability")
    r.add_argument("--headed", action="store_true")
    r.add_argument("--console-port", type=int, default=8765)

    a = sub.add_parser("approve", help="mark an artifact version approved for unattended replay")
    a.add_argument("capability")
    a.add_argument("--by", required=True)
    a.add_argument("--note", default="")

    s = sub.add_parser("show", help="human-readable review of an artifact")
    s.add_argument("capability")

    c = sub.add_parser("catalog", help="agent-facing tool definitions for approved capabilities")
    c.add_argument("--include-drafts", action="store_true")

    sub.add_parser("schema", help="print the artifact JSON Schema")

    f = sub.add_parser("faults", help="inject runtime faults into the mock target (test hook)")
    f.add_argument("--tenant", default="meridian")
    f.add_argument("--add", action="append", default=[],
                   choices=["interstitial", "session_expire", "app_error", "alert_dialog", "pwd_notice"])
    f.add_argument("--count", type=int, default=1)
    f.add_argument("--slow-ms", type=int, default=None)
    f.add_argument("--clear", action="store_true")

    o = sub.add_parser("operator-bot", help="scripted operator that claims an intervention via the console API")
    o.add_argument("--console", default="http://127.0.0.1:8765")
    o.add_argument("--decision", choices=["resume", "approve", "deny", "abort"], default="resume")
    o.add_argument("--click", action="append", default=[], help="accessible name of an element to click before resolving")
    o.add_argument("--note", default="")
    o.add_argument("--operator", default="operator-bot")
    o.add_argument("--wait-s", type=float, default=120)
    o.add_argument("--think-s", type=float, default=1.0)
    return p


def main(argv: list[str] | None = None) -> None:
    root = Path(os.environ.get("CUA_HOME", Path.cwd()))
    _load_dotenv(root)
    home = Home(root)
    args = build_parser().parse_args(argv)
    handlers: dict[str, Any] = {
        "discover": cmd_discover, "replay": cmd_replay, "approve": cmd_approve, "show": cmd_show,
        "catalog": cmd_catalog, "schema": cmd_schema, "faults": cmd_faults, "operator-bot": cmd_operator_bot,
    }
    handler = handlers[args.cmd]
    code = asyncio.run(handler(home, args)) if asyncio.iscoroutinefunction(handler) else handler(home, args)
    sys.exit(code)


if __name__ == "__main__":
    main()
