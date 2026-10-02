"""Builds a Capability from the actions that were actually executed during discovery."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from cua import __version__
from cua.agent.goal import GoalSpec
from cua.artifact.schema import (
    AppBinding,
    Capability,
    Checkpoint,
    ClickStep,
    DialogExpectation,
    Effect,
    ExtractStep,
    FillStep,
    LiteralValue,
    NavigateStep,
    OutcomeSpec,
    ParamValue,
    ParseRule,
    PressStep,
    Provenance,
    RiskSummary,
    SecretValue,
    SelectStep,
    Step,
    SurfaceSpec,
    Target,
    ValueExpr,
)
from cua.replay.profile import AppProfile

PLACEHOLDER = re.compile(r"^\{\{\s*(secret:)?([a-z][a-z0-9_]*)\s*\}\}$")
ANY_PLACEHOLDER = re.compile(r"\{\{\s*(secret:)?([a-z][a-z0-9_]*)\s*\}\}")


class RecordingError(ValueError):
    pass


def parse_value(text: str, *, inputs: set[str], secrets: set[str], raw_params: dict[str, str]) -> ValueExpr:
    m = PLACEHOLDER.match(text.strip())
    if m:
        is_secret, name = bool(m.group(1)), m.group(2)
        if is_secret:
            if name not in secrets:
                raise RecordingError(f"unknown secret {name!r}; available: {sorted(secrets)}")
            return SecretValue(name=name)
        if name not in inputs:
            raise RecordingError(f"unknown input {name!r}; available: {sorted(inputs)}")
        return ParamValue(name=name)
    if ANY_PLACEHOLDER.search(text):
        raise RecordingError("placeholders must be the entire value, e.g. '{{member_number}}' (no surrounding text)")
    for name, raw in raw_params.items():
        if raw and text.strip() == raw.strip():
            return ParamValue(name=name)
    return LiteralValue(value=text)


@dataclass
class DraftStep:
    action: str
    intent: str
    effect: Effect
    phase: str
    target: Target | None = None
    value: ValueExpr | None = None
    key: str | None = None
    url: str | None = None
    output: str | None = None
    parse: ParseRule = ParseRule.TEXT
    attribute: str = "text"
    expect: Checkpoint | None = None
    dialog: DialogExpectation | None = None
    requires_approval: bool = False
    recorded_by: str = "agent"


def infer_parse(sample: str, output_type: str) -> ParseRule:
    if output_type == "decimal" or re.fullmatch(r"\(?-?\$?[\d,]+\.\d{2}\)?", sample.strip()):
        return ParseRule.CURRENCY
    if output_type == "integer":
        return ParseRule.INTEGER
    return ParseRule.TEXT


class FlowRecorder:
    def __init__(self, goal: GoalSpec, profile: AppProfile) -> None:
        self.goal = goal
        self.profile = profile
        self.steps: list[DraftStep] = []

    def add(self, step: DraftStep) -> None:
        prev = self.steps[-1] if self.steps else None
        if (
            prev is not None
            and step.action == prev.action == "fill"
            and step.target is not None
            and prev.target is not None
            and step.target.strategies[0] == prev.target.strategies[0]
        ):
            self.steps[-1] = step
            return
        self.steps.append(step)

    @property
    def extracted(self) -> set[str]:
        return {s.output for s in self.steps if s.output}

    def missing_outputs(self) -> list[str]:
        return [o.name for o in self.goal.outputs if o.name not in self.extracted]

    def build(self, *, success: Checkpoint, run_id: str, planner: str, recorded_against: str, version: str) -> Capability:
        missing = self.missing_outputs()
        if missing:
            raise RecordingError(f"declared outputs were never extracted: {missing}")
        steps: list[Step] = [self._to_step(i, d) for i, d in enumerate(self.steps, start=1)]
        codes = {o.code: o for o in self.goal.outcomes}
        for code, desc in self.profile.business_codes():
            codes.setdefault(code, OutcomeSpec(code=code, description=desc))
        max_effect = max((s.effect for s in steps), key=lambda e: e.rank)
        cap = Capability(
            id=self.goal.capability_id,
            version=version,
            title=self.goal.title,
            description=self.goal.description,
            app=AppBinding(product=self.goal.product, profile=self.profile.ref),
            surface=SurfaceSpec(kind="web", entry_url="{base_url}" + self.goal.entry_path),
            inputs=self.goal.inputs,
            secrets=self.goal.secrets,
            outputs=self.goal.outputs,
            outcomes=list(codes.values()),
            steps=steps,
            success=success,
            risk=RiskSummary(max_effect=max_effect, approval_required_steps=[s.id for s in steps if s.requires_approval]),
            provenance=Provenance(
                recorded_at=datetime.now(UTC),
                discovery_run_id=run_id,
                planner=planner,
                goal=self.goal.goal,
                recorded_against=recorded_against,
                recorder_version=__version__,
                human_steps=sum(1 for d in self.steps if d.recorded_by == "human"),
            ),
        )
        cap.content_hash = cap.compute_hash()
        return cap

    def _to_step(self, i: int, d: DraftStep) -> Step:
        common: dict[str, Any] = {
            "id": f"s{i:02d}",
            "phase": d.phase,
            "intent": d.intent,
            "effect": d.effect,
            "expect": d.expect,
            "dialog": d.dialog,
            "requires_approval": d.requires_approval,
            "recorded_by": d.recorded_by,
        }
        if d.action == "navigate":
            return NavigateStep(url=d.url or "{base_url}/", **common)
        if d.target is None and d.action != "press":
            raise RecordingError(f"step {i} ({d.action}) has no target")
        if d.action == "click":
            return ClickStep(target=d.target, **common)  # type: ignore[arg-type]
        if d.action == "fill":
            return FillStep(target=d.target, value=d.value or LiteralValue(value=""), **common)  # type: ignore[arg-type]
        if d.action == "select":
            return SelectStep(target=d.target, value=d.value or LiteralValue(value=""), **common)  # type: ignore[arg-type]
        if d.action == "press":
            return PressStep(target=d.target, key=d.key or "Enter", **common)
        if d.action == "extract":
            return ExtractStep(target=d.target, output=d.output or "", attribute=d.attribute, parse=d.parse, **common)  # type: ignore[arg-type]
        raise RecordingError(f"unsupported action {d.action}")
