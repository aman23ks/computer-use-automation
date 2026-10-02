"""Discovery: LLM-driven observe -> decide -> act loop that records a replayable capability.

Every model action passes the policy gate, is executed through the same locator machinery
replay will use (the persisted locator must resolve to the exact element the model chose,
or the action is refused), and is recorded with a post-state checkpoint.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any

from playwright.async_api import Error as PlaywrightError

from cua.agent.goal import GoalSpec
from cua.agent.planner import Planner, PlannerError, Turn
from cua.artifact.locators import VOLATILE, build_target
from cua.artifact.recorder import DraftStep, FlowRecorder, RecordingError, infer_parse, parse_value
from cua.artifact.schema import (
    Capability,
    Checkpoint,
    Condition,
    DialogExpectation,
    Effect,
    FrameRef,
    ParamValue,
    SecretValue,
    Sensitivity,
    Target,
    TextCondition,
    UrlCondition,
    ValueExpr,
)
from cua.control.handoff import ControlChannel, Decision, HumanAction, Reason
from cua.evidence import RunRecorder
from cua.replay.engine import parse_output
from cua.replay.params import EnvSecretProvider
from cua.replay.profile import AppProfile
from cua.safety.policy import Policy, Verdict
from cua.surface.base import ElementDescriptor, Observation
from cua.surface.web import LocatorConflict, SurfaceError, WebSurface
from cua.urls import canonical_path_regex

STUCK_REPEATS = 3
MAX_POLICY_BLOCKS = 3


@dataclass
class DiscoveryOutcome:
    status: str
    message: str
    steps_taken: int
    capability: Capability | None = None


class DiscoveryAgent:
    def __init__(
        self,
        goal: GoalSpec,
        profile: AppProfile,
        policy: Policy,
        surface: WebSurface,
        planner: Planner,
        recorder: RunRecorder,
        flow: FlowRecorder,
        *,
        base_url: str,
        params: dict[str, str],
        channel: ControlChannel | None,
        secrets: EnvSecretProvider | None = None,
    ) -> None:
        self.goal = goal
        self.profile = profile
        self.policy = policy
        self.surface = surface
        self.planner = planner
        self.rec = recorder
        self.flow = flow
        self.base_url = base_url.rstrip("/")
        self.params = params
        self.channel = channel
        self.secrets = secrets or EnvSecretProvider()
        self.secret_values: dict[str, str] = {}
        self.max_steps = goal.max_steps or policy.max_discovery_steps
        self.finished: tuple[str, str] | None = None
        self.success: Checkpoint | None = None
        self.last_frame: FrameRef | None = None
        self._policy_blocks = 0
        self._inputs = {p.name for p in goal.inputs}
        self._secrets = {s.name for s in goal.secrets}

    async def run(self) -> DiscoveryOutcome:
        for spec in self.goal.inputs:
            if spec.name in self.params:
                self.rec.redactor.register(self.params[spec.name], spec.sensitivity, spec.name)
        for ref in self.goal.secrets:
            self.secret_values[ref.name] = self.secrets.get(ref)
            self.rec.redactor.register(self.secret_values[ref.name], Sensitivity.SECRET, ref.name)

        entry = self.base_url + self.goal.entry_path
        self.rec.event("discovery.start", capability=self.goal.capability_id, goal=self.goal.goal, planner=self.planner.name,
                       entry=entry, max_steps=self.max_steps)
        await self.surface.navigate(entry)
        feedback = "Session started at the application entry point."
        last_sig, repeats, step = "", 0, 0
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.policy.discovery_timeout_s
        while step < self.max_steps and self.finished is None:
            if loop.time() > deadline:
                return self._stop("timeout", f"discovery exceeded {self.policy.discovery_timeout_s}s", step)
            step += 1
            await self.surface.settle()
            obs = await self.surface.observe(screenshot=True)
            if obs.screenshot:
                self.rec.write_bytes(f"screenshots/step-{step:02d}.jpg", obs.screenshot)
            self.rec.write_text(f"dom/step-{step:02d}.txt", obs.render())
            sig = obs.signature()
            repeats = repeats + 1 if sig == last_sig else 0
            last_sig = sig
            if repeats >= STUCK_REPEATS:
                repeats = 0
                feedback = await self._human(Reason.AGENT_STUCK, f"no visible progress in the last {STUCK_REPEATS} actions", obs, feedback)
                if self.finished:
                    break
                continue
            try:
                action = await self.planner.decide(Turn(obs, feedback, step, self.max_steps - step))
            except PlannerError as e:
                self.rec.event("planner.error", error=str(e))
                if self.channel is None:
                    return self._stop("failed", f"planner error: {e}", step)
                feedback = await self._human(Reason.PLANNER_ERROR, str(e), obs, feedback)
                if self.finished:
                    break
                return self._stop("failed", f"planner error: {e}", step)
            self.rec.event("agent.decision", step=step, tool=action.tool, args=action.args, rationale=action.rationale)
            feedback = await self._handle(action.tool, action.args, obs)
            self.rec.event("agent.feedback", step=step, feedback=feedback)

        transcript = getattr(self.planner, "transcript", None)
        if callable(transcript):
            self.rec.write_json("llm_transcript.json", transcript())
        totals = getattr(self.planner, "totals", None)
        if totals:
            self.rec.event("llm.usage_total", **totals)
        if self.finished is None:
            return self._stop("exhausted", f"action budget of {self.max_steps} exhausted", step)
        status, summary = self.finished
        if status != "success":
            return self._stop(status, summary, step)
        return DiscoveryOutcome("success", summary, step)

    def _stop(self, status: str, message: str, steps: int) -> DiscoveryOutcome:
        self.rec.event("discovery.stopped", status=status, message=message, steps=steps)
        return DiscoveryOutcome(status, message, steps)

    async def _phase(self, frame: FrameRef | None) -> str:
        f = self.surface.find_frame(frame)
        if f is None:
            return "main"
        text = (await self.surface.text_state()).get(f, "")
        return "auth" if re.search(self.profile.auth.login_text_regex, text, re.I) else "main"

    def _resolve_text(self, expr: ValueExpr) -> str:
        if isinstance(expr, ParamValue):
            return self.params[expr.name]
        if isinstance(expr, SecretValue):
            return self.secret_values[expr.name]
        return expr.value

    async def _handle(self, tool: str, args: dict[str, Any], obs: Observation) -> str:
        try:
            if tool == "finish":
                return await self._finish(args)
            if tool == "request_human":
                return await self._human(Reason.AGENT_REQUESTED, str(args.get("reason", "")), obs, "")
            if tool == "navigate":
                return await self._navigate(str(args.get("path", "/")))
            if tool in ("click", "type_text", "select_option", "press_key", "extract"):
                return await self._element_action(tool, args, obs)
            return f"ERROR: unknown tool {tool}"
        except (SurfaceError, RecordingError) as e:
            return f"ERROR: {e}"
        except PlaywrightError as e:
            return f"ERROR: the action failed in the browser: {str(e).splitlines()[0]}"

    async def _navigate(self, path: str) -> str:
        url = self.base_url + (path if path.startswith("/") else "/" + path)
        decision = self.policy.decide("navigate", None, url, mode="discovery")
        if decision.verdict is not Verdict.ALLOW:
            return await self._blocked(decision.reason)
        before = await self.surface.frame_urls()
        await self.surface.navigate(url)
        await self.surface.settle()
        top = self.surface.page.main_frame
        self.flow.add(DraftStep(action="navigate", intent=f"Open {path}", effect=Effect.NAVIGATE, phase="main",
                                url="{base_url}" + path,
                                expect=Checkpoint(all=[UrlCondition(frame=FrameRef(), path_regex=canonical_path_regex(top.url))])))
        return f"OK: navigated. Frame URLs before={list(before.values())} after={list((await self.surface.frame_urls()).values())}"

    async def _blocked(self, reason: str) -> str:
        self._policy_blocks += 1
        self.rec.event("policy.blocked", reason=reason, count=self._policy_blocks)
        if self._policy_blocks >= MAX_POLICY_BLOCKS and self.channel is not None:
            self._policy_blocks = 0
            obs = await self.surface.observe(screenshot=False)
            return await self._human(Reason.POLICY_BLOCKED, f"repeated policy blocks, last: {reason}", obs, "")
        return f"BLOCKED by policy: {reason}. Choose a different, permitted action."

    async def _element_action(self, tool: str, args: dict[str, Any], obs: Observation) -> str:
        ref = str(args.get("ref") or "")
        if tool == "press_key" and not ref:
            key = str(args["key"])
            await self.surface.press(key, None)
            await self.surface.settle()
            self.flow.add(DraftStep(action="press", intent=f"Press {key}", effect=Effect.INPUT, phase="main", key=key))
            return f"OK: pressed {key}."
        desc = await self.surface.describe_ref(ref, obs)
        action = {"click": "click", "type_text": "fill", "select_option": "select", "press_key": "press", "extract": "extract"}[tool]
        decision = self.policy.decide(action, desc.info, desc.frame_url, mode="discovery")
        self.rec.event("policy.decision", ref=ref, action=action, verdict=decision.verdict.value, effect=decision.effect.value, reason=decision.reason)
        if decision.verdict is Verdict.DENY:
            return await self._blocked(decision.reason)
        label = desc.info.get("name") or desc.info.get("label") or desc.info.get("role")
        description = f'{desc.info["role"]} "{label}"'
        approved = False
        if decision.verdict is Verdict.REQUIRE_APPROVAL:
            if self.channel is None:
                return "DENIED: this irreversible action needs operator approval and no operator channel is configured."
            res = await self.channel.escalate(
                reason=Reason.APPROVAL_REQUIRED, capability=self.goal.capability_id, step=f"{action} {description}",
                message=f"Agent wants to {action} {description}: {decision.reason}. Rationale: {args.get('rationale', '')}",
                state_summary=obs.render(), screenshot=await self._shot("approval"),
            )
            if res.decision is not Decision.APPROVE:
                return f"DENIED by operator ({res.decision}): {res.note or 'no note'}. Do not retry this action."
            approved = True

        target = build_target(desc, purpose="extract" if action == "extract" else "act", params=self.params,
                              redactor=self.rec.redactor, description=description)
        if target is None:
            return f"ERROR: {ref} has no stable, non-sensitive locator. Pick a different element or request_human."
        description = target.description
        res_el = await self._verify_target(target, ref, desc)
        if isinstance(res_el, str):
            return res_el

        value: ValueExpr | None = None
        if action in ("fill", "select"):
            value = parse_value(str(args.get("text", args.get("option", ""))), inputs=self._inputs, secrets=self._secrets, raw_params=self.params)
        if action == "extract":
            output = str(args["output"])
            spec = next((o for o in self.goal.outputs if o.name == output), None)
            if spec is None:
                return f"ERROR: {output} is not a declared output"
            raw = await self.surface.read(res_el, "text")
            parse = infer_parse(raw, spec.type.value)
            try:
                value_out = parse_output(raw, parse, spec.type)
            except Exception:
                return f"ERROR: the element's content could not be parsed as {spec.type}; choose the element that holds only the value."
            self.rec.redactor.register(raw, spec.sensitivity, output)
            self.rec.redactor.register(value_out, spec.sensitivity, output)
            self.rec.event("extract", output=output, value=value_out, parse=parse.value)
            self.flow.add(DraftStep(action="extract", intent=f"Read {output} from {description}", effect=Effect.READ,
                                    phase=await self._phase(desc.frame), target=target, output=output, parse=parse))
            self.last_frame = desc.frame
            return f"OK: extracted {output} (value withheld from you; parsed as {parse.value})."

        phase = await self._phase(desc.frame)
        before_urls = await self.surface.frame_urls()
        before_marks = await self.surface.landmarks()
        accept = approved or bool(args.get("accept_dialog"))
        self.surface.expect_dialog(DialogExpectation(action="accept", message_regex=".*") if accept else None)
        self.surface.drain_dialogs()
        if action == "click":
            await self.surface.click(res_el)
        elif action == "fill":
            await self.surface.fill(res_el, self._resolve_text(value))  # type: ignore[arg-type]
        elif action == "select":
            await self.surface.select(res_el, self._resolve_text(value))  # type: ignore[arg-type]
        else:
            await self.surface.press(str(args["key"]), res_el)
        await self.surface.settle()
        self.surface.expect_dialog(None)
        dialogs = self.surface.drain_dialogs()
        dialog_exp = None
        notes = []
        for d in dialogs:
            self.rec.event("dialog", kind=d.kind, message=d.message, handled=d.handled, expected=d.expected)
            notes.append(f"{d.kind} dialog {self.rec.redactor.text(d.message)!r} was {d.handled}")
            if d.expected and d.handled == "accept":
                safe = not self.rec.redactor.contains_sensitive(d.message, self.params.values())
                dialog_exp = DialogExpectation(action="accept", message_regex="^" + re.escape(d.message[:60]) if safe else ".*")

        expect = await self._post_checkpoint(before_urls, before_marks, desc.frame)
        intent = {"click": f"Click {description}", "fill": f"Enter {_value_label(value)} into {description}",
                  "select": f"Choose {_value_label(value)} in {description}", "press": f"Press {args.get('key')} in {description}"}[action]
        effect = decision.effect
        self.flow.add(DraftStep(action=action, intent=intent, effect=effect, phase=phase, target=target,
                                value=value, key=str(args.get("key")) if action == "press" else None, expect=expect,
                                dialog=dialog_exp, requires_approval=effect is Effect.IRREVERSIBLE))
        self.last_frame = desc.frame
        after = await self.surface.frame_urls()
        changed = [k for k, v in after.items() if before_urls.get(k) != v]
        summary = f"OK: {intent}." + (f" Frames navigated: {changed}." if changed else " No frame navigated.")
        if notes:
            summary += " " + "; ".join(notes) + "."
        return summary

    async def _verify_target(self, target: Target, ref: str, desc: ElementDescriptor) -> Any:
        """The persisted locator must resolve to the very element the model chose."""
        try:
            res = await self.surface.resolve(target, self.params)
        except LocatorConflict as e:
            return f"ERROR: locator strategies disagree for {ref} ({e}); choose another element."
        if res is None:
            return f"ERROR: {ref} could not be re-resolved by its locators; observe again."
        same = await res.frame.evaluate("([a, r]) => a === window.__cua.byRef(r)", [res.handle, ref])
        if not same:
            return f"ERROR: locators for {ref} resolve to a different element; choose a more specific element."
        return res

    async def _post_checkpoint(self, before_urls: dict[str, str], before_marks: dict[str, list[str]], frame: FrameRef) -> Checkpoint | None:
        after_urls = await self.surface.frame_urls()
        after_marks = await self.surface.landmarks()
        conds: list[Condition] = []
        for key, url in after_urls.items():
            if before_urls.get(key) == url:
                continue
            ref = FrameRef(path=[] if key == "top" else key.split("/"))
            conds.append(UrlCondition(frame=ref, path_regex=canonical_path_regex(url)))
            text = self._safe_landmark(after_marks.get(key, []), before_marks.get(key, []))
            if text:
                conds.append(TextCondition(frame=ref, text=text))
        if not conds:
            key = frame.label()
            text = self._safe_landmark(after_marks.get(key, []), before_marks.get(key, []))
            if text:
                conds.append(TextCondition(frame=frame, text=text))
        return Checkpoint(all=conds) if conds else None

    def _safe_landmark(self, after: list[str], before: list[str]) -> str | None:
        for t in after:
            if t in before or VOLATILE.match(t) or "[" in t:
                continue
            if self.rec.redactor.contains_sensitive(t, self.params.values()):
                continue
            return t
        return None

    async def _finish(self, args: dict[str, Any]) -> str:
        status = str(args.get("status"))
        summary = str(args.get("summary", ""))
        if status != "success":
            self.finished = (status, summary)
            return "Run ended."
        missing = self.flow.missing_outputs()
        if missing:
            return f"NOT FINISHED: outputs not extracted yet: {missing}."
        if not self.flow.steps:
            return "NOT FINISHED: no actions were recorded."
        texts = await self.surface.text_state()
        conds: list[Condition] = []
        for phrase in list(args.get("success_evidence") or [])[:3]:
            phrase = str(phrase).strip()
            if not phrase or VOLATILE.match(phrase) or self.rec.redactor.contains_sensitive(phrase, self.params.values()):
                continue
            cond = TextCondition(frame=self.last_frame, text=phrase)
            if await self.surface.check(cond, self.params, texts):
                conds.append(cond)
        if self.last_frame is not None:
            url = self.surface.frame_url(self.last_frame)
            if url:
                conds.append(UrlCondition(frame=self.last_frame, path_regex=canonical_path_regex(url)))
        if not any(isinstance(c, TextCondition) for c in conds):
            return "NOT FINISHED: none of the success_evidence phrases are visible static text on the current screen. Provide exact visible headings or labels."
        self.success = Checkpoint(all=conds)
        self.finished = ("success", summary)
        self.rec.event("discovery.success_checkpoint", conditions=[c.model_dump() for c in conds])
        return "Run finished."

    async def _shot(self, label: str) -> str | None:
        try:
            return self.rec.rel(self.rec.write_bytes(f"screenshots/{label}.jpg", await self.surface.screenshot(masked=True)))
        except PlaywrightError:
            return None

    async def _human(self, reason: Reason, message: str, obs: Observation, feedback: str) -> str:
        if self.channel is None:
            self.finished = ("stuck", f"{reason}: {message} (no operator channel)")
            return "No operator available."
        res = await self.channel.escalate(reason=reason, message=message, capability=self.goal.capability_id, step=None,
                                          state_summary=obs.render(), screenshot=await self._shot(f"escalation-{reason.value}"))
        if res.decision is Decision.ABORT or res.timed_out:
            self.finished = ("aborted", f"operator aborted: {res.note}")
            return "Operator aborted the run."
        recorded = self._record_human(res.human_actions)
        return (f"A human operator took control and handed back ({res.decision}). Note: {res.note or 'none'}. "
                f"They performed {len(res.human_actions)} action(s): {recorded}. Continue from the current screen.")

    def _record_human(self, actions: list[HumanAction]) -> list[str]:
        out = []
        for a in actions:
            t = a.target
            if not t or not t.get("candidates"):
                continue
            desc = ElementDescriptor(frame=FrameRef(**t["frame"]), frame_url=t.get("frame_url", ""),
                                     info={"role": t.get("role"), "name": t.get("name"), "label": t.get("label")},
                                     candidates=t["candidates"])
            label = t.get("name") or t.get("label") or t.get("role")
            description = f'{t.get("role")} "{label}"'
            target = build_target(desc, purpose="act", params=self.params, redactor=self.rec.redactor, description=description)
            if target is None:
                continue
            if a.kind == "click":
                effect = self.policy.classify("click", t, t.get("frame_url", ""))
                self.flow.add(DraftStep(action="click", intent=f"Click {description} (operator)", effect=effect,
                                        phase="main", target=target, recorded_by="human"))
                out.append(f"clicked {description}")
            elif a.kind == "type" and a.value is not None:
                value = parse_value(a.value, inputs=self._inputs, secrets=self._secrets, raw_params=self.params)
                self.flow.add(DraftStep(action="fill", intent=f"Enter {_value_label(value)} into {description} (operator)",
                                        effect=Effect.INPUT, phase="main", target=target,
                                        value=value, recorded_by="human"))
                out.append(f"typed into {description}")
        return out


def _value_label(value: ValueExpr | None) -> str:
    if isinstance(value, ParamValue):
        return "{" + value.name + "}"
    if isinstance(value, SecretValue):
        return "{secret:" + value.name + "}"
    return repr(value.value) if value is not None else "''"
