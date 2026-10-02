"""Deterministic replay: the production execution path. No model is consulted.

Every wait is a race between (a) the expected state (next target resolvable / step checkpoint)
and (b) the app profile's known exceptional states, polled from one batched text snapshot.
Nothing sleeps blindly. Outcomes are classified into business outcomes (returned to the caller),
recoverable states (handled, logged, bounded), and hard failures (stop with expected vs observed
and evidence). Optionally escalates to a human on the same live session and resumes at the
first step whose preconditions hold after hand-back.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import Counter
from decimal import Decimal, InvalidOperation
from typing import Any

from playwright.async_api import Error as PlaywrightError

from cua.artifact.schema import (
    Capability,
    Checkpoint,
    ClickStep,
    ExtractStep,
    FillStep,
    LiteralValue,
    NavigateStep,
    OutputType,
    ParamValue,
    ParseRule,
    PressStep,
    SecretValue,
    SelectStep,
    Sensitivity,
    Step,
    Target,
    ValueExpr,
)
from cua.control.handoff import ControlChannel, Decision, Reason
from cua.evidence import RunRecorder
from cua.replay.params import EnvSecretProvider, InputError, SecretUnavailable, validate_inputs
from cua.replay.profile import AppProfile, KnownState, StateKind
from cua.replay.result import (
    RETRYABLE,
    ApprovalRequest,
    ErrorCode,
    Failure,
    Outcome,
    RecoveryReport,
    ReplayResult,
    RunStatus,
    StepReport,
)
from cua.safety.policy import Policy, Verdict
from cua.surface.base import Resolution
from cua.surface.web import LocatorConflict, SurfaceError, WebSurface

POLL_S = 0.15
TARGET_WAIT_MS = 10_000


class _Stop(Exception):
    pass


class BusinessOutcomeHit(_Stop):
    def __init__(self, outcome: Outcome) -> None:
        self.outcome = outcome


class StepFailed(_Stop):
    def __init__(self, failure: Failure) -> None:
        self.failure = failure


class NeedsReauth(_Stop):
    def __init__(self, state_id: str) -> None:
        self.state_id = state_id


class ApprovalNeeded(_Stop):
    def __init__(self, request: ApprovalRequest) -> None:
        self.request = request


def parse_output(raw: str, rule: ParseRule, out_type: OutputType) -> Any:
    text = raw.strip()
    if rule is ParseRule.CURRENCY:
        negative = (text.startswith("(") and text.endswith(")")) or text.startswith("-")
        digits = re.sub(r"[^\d.]", "", text)
        value = Decimal(digits)
        return str(-value if negative else value)
    if rule is ParseRule.INTEGER or out_type is OutputType.INTEGER:
        return int(re.sub(r"[^\d-]", "", text))
    if out_type is OutputType.DECIMAL:
        return str(Decimal(text.replace(",", "")))
    return text


class ReplayEngine:
    def __init__(
        self,
        capability: Capability,
        profile: AppProfile,
        policy: Policy,
        surface: WebSurface,
        recorder: RunRecorder,
        *,
        base_url: str,
        channel: ControlChannel | None = None,
        authorize_irreversible: bool = False,
        escalate: bool = False,
        tenant: str | None = None,
        secrets: EnvSecretProvider | None = None,
    ) -> None:
        self.cap = capability
        self.profile = profile
        self.policy = policy
        self.surface = surface
        self.rec = recorder
        self.base_url = base_url.rstrip("/")
        self.channel = channel
        self.authorize_irreversible = authorize_irreversible
        self.escalate = escalate and channel is not None
        self.tenant = tenant
        self.secrets = secrets or EnvSecretProvider()
        self.bind: dict[str, str] = {}
        self._secret_values: dict[str, str] = {}
        self.outputs: dict[str, Any] = {}
        self.reports: list[StepReport] = []
        self.recoveries: list[RecoveryReport] = []
        self.warnings: list[str] = []
        self.interventions: list[str] = []
        self._recovery_counts: Counter[str] = Counter()
        self._approved: set[str] = set()
        self._reauthenticating = False
        self._t0 = time.monotonic()

    async def run(self, raw_inputs: dict[str, str]) -> ReplayResult:
        self.rec.event("replay.start", capability=self.cap.ref, tenant=self.tenant, content_hash=self.cap.content_hash,
                       status=self.cap.status.value, inputs=sorted(raw_inputs))
        try:
            result = await self._run(raw_inputs)
        except StepFailed as f:
            result = await self._fail(f.failure)
        except BusinessOutcomeHit as hit:
            result = self._result(RunStatus.BUSINESS_OUTCOME, outcome=hit.outcome)
        except (NeedsReauth, ApprovalNeeded) as stop:
            result = await self._fail(Failure(code=ErrorCode.RECOVERY_EXHAUSTED, message=f"unexpected {type(stop).__name__} outside a step"))
        result.duration_ms = int((time.monotonic() - self._t0) * 1000)
        result.evidence_dir = str(self.rec.dir)
        self.rec.event("replay.result", status=result.status.value, outputs=result.outputs,
                       outcome=result.outcome.model_dump() if result.outcome else None,
                       error=result.error.model_dump() if result.error else None)
        self.rec.write_json("result.json", result.model_dump(mode="json"))
        return result

    def _result(self, status: RunStatus, **kw: Any) -> ReplayResult:
        return ReplayResult(
            run_id=self.rec.run_id, capability=self.cap.ref, tenant=self.tenant, status=status,
            steps=self.reports, recoveries=self.recoveries, warnings=self.warnings + [
                f"blocked request: {u}" for u in self.surface.blocked_requests
            ], interventions=self.interventions, **kw,
        )

    async def _run(self, raw_inputs: dict[str, str]) -> ReplayResult:
        try:
            self.bind = validate_inputs(self.cap.inputs, raw_inputs)
        except InputError as e:
            raise StepFailed(Failure(code=ErrorCode.INPUT_INVALID, message=str(e), expected="inputs matching the capability contract",
                                     observed="; ".join(e.problems))) from e
        for spec in self.cap.inputs:
            if spec.name in self.bind:
                self.rec.redactor.register(self.bind[spec.name], spec.sensitivity, spec.name)
        for ref in self.cap.secrets:
            try:
                value = self.secrets.get(ref)
            except SecretUnavailable as e:
                raise StepFailed(Failure(code=ErrorCode.SECRET_UNAVAILABLE, message=f"secret {ref.name!r} is not configured")) from e
            self._secret_values[ref.name] = value
            self.rec.redactor.register(value, Sensitivity.SECRET, ref.name)
        for step in self.cap.steps:
            if step.action not in self.policy.allowed_actions:
                raise StepFailed(Failure(code=ErrorCode.POLICY_VIOLATION, step_id=step.id, step_intent=step.intent,
                                         message=f"action {step.action!r} is not allowlisted"))

        await self._navigate(self.cap.surface.entry_url.replace("{base_url}", self.base_url), step_id=None)
        steps = self.cap.steps
        i = 0
        mutated = False
        while i < len(steps):
            step = steps[i]
            try:
                self.reports.append(await self._execute(step))
                mutated = mutated or (step.phase == "main" and step.effect.mutates)
                i += 1
            except BusinessOutcomeHit as hit:
                self.rec.event("replay.business_outcome", **hit.outcome.model_dump())
                await self._capture("outcome")
                return self._result(RunStatus.BUSINESS_OUTCOME, outcome=hit.outcome)
            except NeedsReauth as r:
                if mutated:
                    raise StepFailed(Failure(code=ErrorCode.RECOVERY_EXHAUSTED, step_id=step.id, step_intent=step.intent,
                                             message="session expired after a state-changing step; restarting is not safe",
                                             observed=r.state_id)) from r
                self.recoveries.append(RecoveryReport(state_id=r.state_id, step_id=step.id, action="reauthenticate_and_restart"))
                self.rec.event("recovery.reauthenticate", state=r.state_id, at_step=step.id)
                self._reauthenticating = True
                i = 0
            except ApprovalNeeded as a:
                if not self.escalate:
                    self.rec.event("replay.needs_approval", **a.request.model_dump())
                    await self._capture(f"approval-{step.id}")
                    return self._result(RunStatus.NEEDS_APPROVAL, approval=a.request)
                decision = await self._escalate(Reason.APPROVAL_REQUIRED, a.request.reason, step)
                if decision is Decision.APPROVE:
                    self._approved.add(step.id)
                    continue
                code = ErrorCode.APPROVAL_DENIED if decision is Decision.DENY else ErrorCode.HUMAN_ABORTED
                raise StepFailed(Failure(code=code, step_id=step.id, step_intent=step.intent, message=f"operator decision: {decision}")) from a
            except StepFailed as f:
                if not self.escalate or f.failure.code in (ErrorCode.POLICY_VIOLATION, ErrorCode.ARTIFACT_POLICY_MISMATCH):
                    raise
                decision = await self._escalate(Reason.REPLAY_FAILURE, f"{f.failure.code}: {f.failure.message}", step)
                if decision is not Decision.RESUME:
                    code = ErrorCode.HUMAN_TIMEOUT if decision is Decision.ABORT and self._last_timed_out else ErrorCode.HUMAN_ABORTED
                    raise StepFailed(f.failure.model_copy(update={"code": code, "message": f"{f.failure.message} (operator: {decision})"})) from f
                j = await self._resume_point(i)
                if j is None:
                    raise StepFailed(Failure(code=ErrorCode.HANDBACK_UNRESOLVED, step_id=step.id, step_intent=step.intent,
                                             message="after hand-back no step's preconditions hold",
                                             observed=str(await self.surface.frame_urls()))) from f
                for skipped in steps[i:j]:
                    self.reports.append(StepReport(step_id=skipped.id, action=skipped.action, status="skipped_by_handback"))
                self.rec.event("replay.resumed", from_step=step.id, resume_at=steps[j].id if j < len(steps) else "success_check")
                i = j

        await self._wait_for(self.cap.success, None, ErrorCode.SUCCESS_CHECK_FAILED)
        missing = [o.name for o in self.cap.outputs if o.name not in self.outputs]
        if missing:
            raise StepFailed(Failure(code=ErrorCode.EXTRACTION_FAILED, message=f"outputs not extracted: {missing}"))
        await self._capture("success")
        return self._result(RunStatus.SUCCEEDED, outputs=self.outputs)

    _last_timed_out = False

    async def _escalate(self, reason: Reason, message: str, step: Step) -> Decision:
        assert self.channel is not None
        shot = await self._capture(f"escalation-{step.id}")
        res = await self.channel.escalate(
            reason=reason, message=message, capability=self.cap.ref, step=f"{step.id}: {step.intent}",
            state_summary=await self.surface.dom_text(), screenshot=shot.get("screenshot"),
        )
        self._last_timed_out = res.timed_out
        if self.channel.history:
            self.interventions.append(self.channel.history[-1].id)
        return res.decision

    async def _resume_point(self, i: int) -> int | None:
        steps = self.cap.steps
        await self.surface.settle()
        if await self._target_ready(steps[i]):
            return i
        texts = await self.surface.text_state()
        for j in range(len(steps) - 1, i - 1, -1):
            cp = steps[j].expect
            if cp is not None and (await self.surface.check_all(cp, self.bind, texts))[0]:
                return j + 1
        for j in range(i + 1, len(steps)):
            if await self._target_ready(steps[j]):
                return j
        return None

    async def _target_ready(self, step: Step) -> bool:
        target = getattr(step, "target", None)
        if target is None:
            return False
        try:
            return await self.surface.resolve(target, self.bind) is not None
        except LocatorConflict:
            return False

    def _value(self, expr: ValueExpr) -> str:
        if isinstance(expr, LiteralValue):
            return expr.value
        if isinstance(expr, ParamValue):
            return self.bind.get(expr.name, "")
        if isinstance(expr, SecretValue):
            return self._secret_values[expr.name]
        raise TypeError(expr)

    async def _navigate(self, url: str, step_id: str | None) -> None:
        decision = self.policy.decide("navigate", None, url, mode="replay")
        if decision.verdict is not Verdict.ALLOW:
            raise StepFailed(Failure(code=ErrorCode.POLICY_VIOLATION, step_id=step_id, message=decision.reason))
        for attempt in range(1, 4):
            try:
                await self.surface.navigate(url)
                await self.surface.settle()
                self.rec.event("navigate", url=url, attempt=attempt)
                return
            except (PlaywrightError, SurfaceError) as e:
                self.rec.event("navigate.error", url=url, attempt=attempt, error=str(e).splitlines()[0])
                if attempt == 3:
                    raise StepFailed(Failure(code=ErrorCode.NAVIGATION_FAILED, step_id=step_id, message=str(e).splitlines()[0],
                                             expected=f"load {url}", retryable=True)) from e
                await asyncio.sleep(0.5 * attempt)

    async def _execute(self, step: Step) -> StepReport:
        t0 = time.monotonic()
        if step.phase == "main":
            self._reauthenticating = False
        self.rec.event("step.start", step=step.id, action=step.action, intent=step.intent, effect=step.effect.value)
        if isinstance(step, NavigateStep):
            await self._navigate(step.url.replace("{base_url}", self.base_url), step.id)
            return StepReport(step_id=step.id, action=step.action, status="ok", duration_ms=int((time.monotonic() - t0) * 1000))

        if isinstance(step, PressStep) and step.target is None:
            await self.surface.press(step.key, None)
            if step.expect is not None:
                await self._wait_for(step.expect, step, ErrorCode.CHECKPOINT_TIMEOUT)
            return StepReport(step_id=step.id, action=step.action, status="ok", duration_ms=int((time.monotonic() - t0) * 1000))

        res = await self._await_target(step)
        await self._gate(step, res)
        attempts = 0
        for attempt in range(1, step.retry.attempts + 1):
            attempts = attempt
            try:
                if self.channel:
                    self.channel.assert_automation()
                await self._act(step, res)
                break
            except PlaywrightError as e:
                self.rec.event("step.action_error", step=step.id, attempt=attempt, error=str(e).splitlines()[0])
                if attempt == step.retry.attempts:
                    raise StepFailed(Failure(code=ErrorCode.ACTION_FAILED, step_id=step.id, step_intent=step.intent,
                                             message=str(e).splitlines()[0], expected=f"{step.action} on {step.target.description}",  # type: ignore[union-attr]
                                             retryable=True)) from e
                await asyncio.sleep(step.retry.backoff_ms / 1000)
                res = await self._await_target(step)

        if step.expect is not None:
            await self._wait_for(step.expect, step, ErrorCode.CHECKPOINT_TIMEOUT)
        else:
            await self.surface.settle()
        self.surface.expect_dialog(None)
        self._check_dialogs(step)
        degraded = res.strategy_index > 0
        report = StepReport(step_id=step.id, action=step.action, status="ok", resolved_by=res.strategy_kind, degraded=degraded,
                            attempts=attempts, duration_ms=int((time.monotonic() - t0) * 1000))
        self.rec.event("step.ok", step=step.id, resolved_by=res.strategy_kind, degraded=degraded, attempts=attempts)
        return report

    async def _gate(self, step: Step, res: Resolution) -> None:
        info = await res.handle.evaluate("(e) => window.__cua.describeHandle(e).info")
        decision = self.policy.decide(step.action, info, res.frame.url, mode="replay")
        self.rec.event("policy.decision", step=step.id, verdict=decision.verdict.value, effect=decision.effect.value, reason=decision.reason)
        if decision.verdict is Verdict.DENY:
            raise StepFailed(Failure(code=ErrorCode.POLICY_VIOLATION, step_id=step.id, step_intent=step.intent, message=decision.reason))
        risky = decision.verdict is Verdict.REQUIRE_APPROVAL
        if risky and not step.requires_approval:
            raise StepFailed(Failure(code=ErrorCode.ARTIFACT_POLICY_MISMATCH, step_id=step.id, step_intent=step.intent,
                                     message="policy classifies this step irreversible but the artifact does not declare it",
                                     expected="requires_approval: true", observed=decision.reason))
        if (risky or step.requires_approval) and not (self.authorize_irreversible or step.id in self._approved):
            raise ApprovalNeeded(ApprovalRequest(step_id=step.id, step_intent=step.intent, reason=decision.reason if risky else "artifact marks step irreversible"))

    async def _act(self, step: Step, res: Resolution) -> None:
        if isinstance(step, ClickStep):
            self.surface.expect_dialog(step.dialog)
            await self.surface.click(res)
        elif isinstance(step, FillStep):
            await self.surface.fill(res, self._value(step.value))
        elif isinstance(step, SelectStep):
            await self.surface.select(res, self._value(step.value))
        elif isinstance(step, PressStep):
            await self.surface.press(step.key, res)
        elif isinstance(step, ExtractStep):
            raw = await self.surface.read(res, step.attribute)
            spec = next(o for o in self.cap.outputs if o.name == step.output)
            try:
                value = parse_output(raw, step.parse, spec.type)
            except (InvalidOperation, ValueError) as e:
                raise StepFailed(Failure(code=ErrorCode.EXTRACTION_FAILED, step_id=step.id, step_intent=step.intent,
                                         message=f"could not parse {step.output} as {step.parse}",
                                         observed=self.rec.redactor.text(raw))) from e
            self.rec.redactor.register(raw, spec.sensitivity, step.output)
            self.rec.redactor.register(value, spec.sensitivity, step.output)
            self.outputs[step.output] = value
            self.rec.event("extract", step=step.id, output=step.output, value=value)

    async def _await_target(self, step: Step) -> Resolution:
        target = getattr(step, "target", None)
        if not isinstance(target, Target):
            raise StepFailed(Failure(code=ErrorCode.LOCATOR_NOT_FOUND, step_id=step.id, message="step has no target"))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + TARGET_WAIT_MS / 1000
        while True:
            if await self._detect(step):
                deadline = loop.time() + TARGET_WAIT_MS / 1000
            self._check_dialogs(step)
            try:
                res = await self.surface.resolve(target, self.bind)
            except LocatorConflict as c:
                raise StepFailed(Failure(code=ErrorCode.LOCATOR_CONFLICT, step_id=step.id, step_intent=step.intent,
                                         message=str(c), expected=target.description, observed=str(c.attempts))) from c
            if res is not None:
                if res.strategy_index > 0:
                    msg = f"{step.id}: locator degraded to {res.strategy_kind} (attempts {res.attempts}); possible UI/tenant drift"
                    self.warnings.append(msg)
                    self.rec.event("locator.degraded", step=step.id, attempts=res.attempts)
                return res
            if loop.time() > deadline:
                raise StepFailed(Failure(
                    code=ErrorCode.LOCATOR_NOT_FOUND, step_id=step.id, step_intent=step.intent,
                    message=f"could not uniquely resolve {target.description!r} within {TARGET_WAIT_MS} ms",
                    expected=f"{target.expect_role} in frame {target.frame.label()} via {[s.kind for s in target.strategies]}",
                    observed=f"attempts={self.surface.last_attempts} frames={await self.surface.frame_urls()} "
                             f"headings={await self.surface.landmarks()}",
                ))
            await asyncio.sleep(POLL_S)

    async def _wait_for(self, cp: Checkpoint, step: Step | None, code: ErrorCode) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + cp.timeout_ms / 1000
        while True:
            if await self._detect(step):
                deadline = loop.time() + cp.timeout_ms / 1000
            self._check_dialogs(step)
            ok, failed = await self.surface.check_all(cp, self.bind)
            if ok:
                return
            if loop.time() > deadline:
                raise StepFailed(Failure(
                    code=code, step_id=step.id if step else None, step_intent=step.intent if step else "success checkpoint",
                    message=f"checkpoint not reached within {cp.timeout_ms} ms", expected="; ".join(failed),
                    observed=f"frames={await self.surface.frame_urls()} headings={await self.surface.landmarks()}",
                ))
            await asyncio.sleep(POLL_S)

    async def _detect(self, step: Step | None) -> bool:
        """Evaluate profile states against one text snapshot. Returns True if a recovery was performed."""
        texts = await self.surface.text_state()
        for state in self.profile.states:
            reauth = state.recovery is not None and state.recovery.kind == "reauthenticate"
            if reauth and self._reauthenticating:
                continue
            if not all([await self.surface.check(c, self.bind, texts) for c in state.match]):
                continue
            message = state.description
            if state.message_regex:
                message = await self.surface.match_text(state.message_regex, None) or message
            step_id = step.id if step else None
            self.rec.event("state.detected", state=state.id, kind=state.kind.value, code=state.code, message=message, step=step_id)
            if state.kind is StateKind.BUSINESS_OUTCOME:
                raise BusinessOutcomeHit(Outcome(code=state.code or state.id.upper(), message=message, step_id=step_id))
            if state.kind is StateKind.HARD_FAILURE:
                raise StepFailed(Failure(code=ErrorCode.APP_ERROR, step_id=step_id, step_intent=step.intent if step else None,
                                         message=message, observed=state.id, retryable=ErrorCode.APP_ERROR in RETRYABLE))
            await self._recover(state, step)
            return True
        return False

    async def _recover(self, state: KnownState, step: Step | None) -> None:
        recovery = state.recovery
        step_id = step.id if step else "success"
        self._recovery_counts[state.id] += 1
        if recovery is None or self._recovery_counts[state.id] > recovery.max_times:
            raise StepFailed(Failure(code=ErrorCode.RECOVERY_EXHAUSTED, step_id=step_id, message=f"state {state.id} persisted after recovery",
                                     observed=state.description))
        if recovery.kind == "reauthenticate":
            raise NeedsReauth(state.id)
        assert recovery.target is not None
        res = await self.surface.resolve(recovery.target, self.bind)
        if res is None:
            self._recovery_counts[state.id] -= 1
            return
        await self.surface.click(res)
        await self.surface.settle()
        self.recoveries.append(RecoveryReport(state_id=state.id, step_id=step_id, action=f"click {recovery.target.description}"))
        self.rec.event("recovery.applied", state=state.id, step=step_id, action="click", target=recovery.target.description)

    def _check_dialogs(self, step: Step | None) -> None:
        step_id = step.id if step else "success"
        for ev in self.surface.drain_dialogs():
            message = self.rec.redactor.text(ev.message)
            if ev.expected:
                self.rec.event("dialog.expected", step=step_id, kind=ev.kind, message=message, handled=ev.handled)
                continue
            rule = next((r for r in self.profile.benign_dialogs if re.search(r.message_regex, ev.message)), None)
            if rule is not None and ev.kind == "alert":
                self.recoveries.append(RecoveryReport(state_id=rule.id, step_id=step_id, action="acknowledged alert"))
                self.rec.event("recovery.applied", state=rule.id, step=step_id, action="acknowledge_alert", message=message)
                continue
            self.rec.event("dialog.unexpected", step=step_id, kind=ev.kind, message=message, handled=ev.handled)
            raise StepFailed(Failure(code=ErrorCode.UNEXPECTED_DIALOG, step_id=step_id, step_intent=step.intent if step else None,
                                     message=f"unexpected {ev.kind} dialog ({ev.handled}, the safe default)", observed=message))

    async def _capture(self, label: str) -> dict[str, str]:
        out: dict[str, str] = {}
        try:
            shot = await self.surface.screenshot(masked=True)
            out["screenshot"] = self.rec.rel(self.rec.write_bytes(f"screenshots/{label}.jpg", shot))
            out["dom"] = self.rec.rel(self.rec.write_text(f"dom/{label}.txt", await self.surface.dom_text()))
        except PlaywrightError as e:
            out["capture_error"] = str(e).splitlines()[0]
        return out

    async def _fail(self, failure: Failure) -> ReplayResult:
        if failure.code not in (ErrorCode.INPUT_INVALID, ErrorCode.SECRET_UNAVAILABLE) and self.surface._page is not None:
            failure.evidence = await self._capture(f"failure-{failure.step_id or 'run'}")
        failure.retryable = failure.retryable or failure.code in RETRYABLE
        failure.message = self.rec.redactor.text(failure.message)
        failure.observed = self.rec.redactor.text(failure.observed) if failure.observed else None
        self.rec.event("replay.failure", **failure.model_dump())
        return self._result(RunStatus.FAILED, error=failure)
