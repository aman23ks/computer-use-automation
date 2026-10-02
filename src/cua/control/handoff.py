"""Control transfer between automation and a human operator on the same live session.

State machine (one per live session):

    AUTOMATION --escalate()--> PENDING --claim()--> HUMAN --resolve()--> AUTOMATION
                                  |                    |
                                  +---- timeout -------+--> AUTOMATION (decision=abort)

Exactly one party controls the session at a time. The automation blocks inside escalate()
and never touches the surface until control returns; the operator's surface calls require the
lease token issued by claim(). Every transition and every human action is written to the run log.
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from cua.evidence import RunRecorder


class Controller(StrEnum):
    AUTOMATION = "automation"
    PENDING = "pending_human"
    HUMAN = "human"


class Reason(StrEnum):
    APPROVAL_REQUIRED = "approval_required"
    AGENT_STUCK = "agent_stuck"
    AGENT_REQUESTED = "agent_requested"
    POLICY_BLOCKED = "policy_blocked"
    REPLAY_FAILURE = "replay_failure"
    PLANNER_ERROR = "planner_error"


class Decision(StrEnum):
    RESUME = "resume"
    APPROVE = "approve"
    DENY = "deny"
    ABORT = "abort"


class ControlError(Exception):
    pass


@dataclass
class HumanAction:
    kind: str
    at: str
    target: dict[str, Any] | None
    value: str | None = None


@dataclass
class Intervention:
    id: str
    run_id: str
    run_kind: str
    capability: str
    reason: Reason
    message: str
    step: str | None
    state_summary: str
    screenshot: str | None
    created_at: str
    status: str = "pending"
    claimed_by: str | None = None
    decision: Decision | None = None
    note: str = ""
    human_actions: list[HumanAction] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        d["human_actions"] = [asdict(a) for a in self.human_actions]
        return d


@dataclass
class Resolution:
    decision: Decision
    note: str
    operator: str | None
    human_actions: list[HumanAction]
    timed_out: bool = False


class InterventionSink(Protocol):
    async def notify(self, intervention: Intervention) -> None: ...


class StderrSink:
    def __init__(self, console_url: str | None) -> None:
        self.console_url = console_url

    async def notify(self, intervention: Intervention) -> None:
        import sys

        where = f" -> operator console: {self.console_url}" if self.console_url else ""
        print(f"\n[INTERVENTION {intervention.id}] {intervention.reason}: {intervention.message}{where}\n", file=sys.stderr)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class ControlChannel:
    def __init__(self, recorder: RunRecorder, sinks: list[InterventionSink] | None = None, timeout_s: int = 900) -> None:
        self.recorder = recorder
        self.sinks = sinks or []
        self.timeout_s = timeout_s
        self.controller = Controller.AUTOMATION
        self.current: Intervention | None = None
        self.history: list[Intervention] = []
        self._lease: str | None = None
        self._done = asyncio.Event()
        self._resolution: Resolution | None = None

    def state(self) -> dict[str, Any]:
        return {
            "run_id": self.recorder.run_id,
            "controller": self.controller.value,
            "intervention": self.current.public() if self.current else None,
            "history": [i.id for i in self.history],
        }

    def assert_automation(self) -> None:
        if self.controller is not Controller.AUTOMATION:
            raise ControlError(f"automation attempted to act while controller={self.controller}")

    async def escalate(
        self, *, reason: Reason, message: str, capability: str, step: str | None, state_summary: str, screenshot: str | None
    ) -> Resolution:
        self.assert_automation()
        intervention = Intervention(
            id=f"iv-{secrets.token_hex(3)}",
            run_id=self.recorder.run_id,
            run_kind=self.recorder.kind,
            capability=capability,
            reason=reason,
            message=message,
            step=step,
            state_summary=state_summary,
            screenshot=screenshot,
            created_at=_now(),
        )
        self.current = intervention
        self.controller = Controller.PENDING
        self._done.clear()
        self._resolution = None
        self.recorder.event("control.escalated", intervention=intervention.id, reason=reason.value, message=message, step=step)
        self._persist()
        for sink in self.sinks:
            await sink.notify(intervention)
        try:
            await asyncio.wait_for(self._done.wait(), timeout=self.timeout_s)
            assert self._resolution is not None
            resolution = self._resolution
        except TimeoutError:
            resolution = Resolution(Decision.ABORT, "no operator response before timeout", None, intervention.human_actions, True)
            intervention.status = "expired"
            intervention.decision = Decision.ABORT
            self.recorder.event("control.timeout", intervention=intervention.id)
        self.controller = Controller.AUTOMATION
        self._lease = None
        self.history.append(intervention)
        self.current = None
        self.recorder.event("control.returned", intervention=intervention.id, to="automation", decision=resolution.decision.value)
        self._persist(intervention)
        return resolution

    def claim(self, intervention_id: str, operator: str) -> str:
        iv = self._require(intervention_id)
        if self.controller is not Controller.PENDING:
            raise ControlError(f"cannot claim while controller={self.controller}")
        self._lease = secrets.token_urlsafe(16)
        self.controller = Controller.HUMAN
        iv.status = "claimed"
        iv.claimed_by = operator
        self.recorder.event("control.claimed", intervention=iv.id, operator=operator, to="human")
        self._persist()
        return self._lease

    def require_lease(self, lease: str | None) -> Intervention:
        if self.controller is not Controller.HUMAN or not lease or not secrets.compare_digest(lease, self._lease or ""):
            raise ControlError("no valid operator lease for this session")
        assert self.current is not None
        return self.current

    def record_human(self, lease: str | None, action: HumanAction) -> None:
        iv = self.require_lease(lease)
        iv.human_actions.append(action)
        self.recorder.event("human.action", intervention=iv.id, operator=iv.claimed_by, **asdict(action))

    def add_human_actions(self, actions: list[HumanAction]) -> None:
        if self.current is None:
            return
        for a in actions:
            self.current.human_actions.append(a)
            self.recorder.event("human.action", intervention=self.current.id, operator=self.current.claimed_by, source="live_window", **asdict(a))

    def resolve(self, intervention_id: str, lease: str | None, decision: Decision, note: str = "") -> None:
        iv = self.require_lease(lease)
        if iv.id != intervention_id:
            raise ControlError("lease does not belong to this intervention")
        if iv.reason is Reason.APPROVAL_REQUIRED and decision is Decision.RESUME:
            raise ControlError("approval interventions must be resolved with approve or deny")
        iv.status = "resolved"
        iv.decision = decision
        iv.note = note
        self._resolution = Resolution(decision, note, iv.claimed_by, list(iv.human_actions))
        self.recorder.event("control.resolved", intervention=iv.id, decision=decision.value, note=note, operator=iv.claimed_by)
        self._done.set()

    def _require(self, intervention_id: str) -> Intervention:
        if self.current is None or self.current.id != intervention_id:
            raise ControlError(f"no pending intervention {intervention_id}")
        return self.current

    def _persist(self, iv: Intervention | None = None) -> None:
        iv = iv or self.current
        if iv is not None:
            self.recorder.write_json(f"interventions/{iv.id}.json", iv.public())
