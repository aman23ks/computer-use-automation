"""Capability artifact schema (v1).

A capability is the unit an AI agent invokes: a typed contract (inputs, secrets, outputs,
business outcomes, risk) plus the deterministic flow that fulfils it. The flow is decoupled
from the model transcript: every target carries ranked, surface-level locator strategies that
were each proven to resolve uniquely to the acted-on element at recording time.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION: Literal["1.0"] = "1.0"
IDENT = r"^[a-z][a-z0-9_]*$"
CAPABILITY_ID = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$"
SEMVER = r"^\d+\.\d+\.\d+$"


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False, use_enum_values=False)


class Sensitivity(StrEnum):
    PUBLIC = "public"
    CONFIDENTIAL = "confidential"
    PII = "pii"
    SECRET = "secret"

    @property
    def masked_in_logs(self) -> bool:
        return self is not Sensitivity.PUBLIC


class ParamType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"
    ENUM = "enum"


class Effect(StrEnum):
    READ = "read"
    INPUT = "input"
    NAVIGATE = "navigate"
    WRITE = "write"
    IRREVERSIBLE = "irreversible"

    @property
    def rank(self) -> int:
        return list(Effect).index(self)

    @property
    def mutates(self) -> bool:
        return self in (Effect.WRITE, Effect.IRREVERSIBLE)


class Status(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    DEPRECATED = "deprecated"


class ParamSpec(Model):
    name: str = Field(pattern=IDENT)
    type: ParamType
    description: str
    required: bool = True
    pattern: str | None = None
    enum: list[str] | None = None
    sensitivity: Sensitivity = Sensitivity.PUBLIC
    example: str | None = None

    @model_validator(mode="after")
    def _check(self) -> ParamSpec:
        if self.type is ParamType.ENUM and not self.enum:
            raise ValueError(f"enum param {self.name!r} needs 'enum' values")
        if self.pattern:
            re.compile(self.pattern)
        if self.example is not None and self.sensitivity in (Sensitivity.SECRET, Sensitivity.PII):
            raise ValueError(f"param {self.name!r} is {self.sensitivity}; examples must not be persisted")
        return self

    def json_schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = {"description": self.description}
        if self.type is ParamType.ENUM:
            schema |= {"type": "string", "enum": self.enum}
        elif self.type is ParamType.INTEGER:
            schema["type"] = "integer"
        elif self.type is ParamType.BOOLEAN:
            schema["type"] = "boolean"
        else:
            schema["type"] = "string"
            if self.type is ParamType.DECIMAL:
                schema.setdefault("pattern", r"^-?\d+(\.\d+)?$")
        if self.pattern:
            schema["pattern"] = self.pattern
        return schema


class SecretRef(Model):
    name: str = Field(pattern=IDENT)
    description: str


class OutputType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    DECIMAL = "decimal"


class OutputSpec(Model):
    name: str = Field(pattern=IDENT)
    type: OutputType
    description: str
    sensitivity: Sensitivity = Sensitivity.CONFIDENTIAL


class OutcomeSpec(Model):
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    description: str


class LiteralValue(Model):
    kind: Literal["literal"] = "literal"
    value: str


class ParamValue(Model):
    kind: Literal["param"] = "param"
    name: str = Field(pattern=IDENT)


class SecretValue(Model):
    kind: Literal["secret"] = "secret"
    name: str = Field(pattern=IDENT)


ValueExpr = Annotated[LiteralValue | ParamValue | SecretValue, Field(discriminator="kind")]


class FrameRef(Model):
    """Frame addressed by its name path from the top document; url_regex is a fallback."""

    path: list[str] = Field(default_factory=list)
    url_regex: str | None = None

    def label(self) -> str:
        return "/".join(self.path) or "top"


class _Strategy(Model):
    score: float = Field(ge=0, le=1)
    rationale: str = ""


class FieldNameStrategy(_Strategy):
    kind: Literal["field_name"] = "field_name"
    tag: str
    name: str


class RoleNameStrategy(_Strategy):
    kind: Literal["role_name"] = "role_name"
    role: str
    name: str


class LabelStrategy(_Strategy):
    kind: Literal["label"] = "label"
    role: str
    label: str


class TableCellStrategy(_Strategy):
    kind: Literal["table_cell"] = "table_cell"
    column: str
    row_key_column: str
    row_key: ValueExpr
    inner_role: str | None = None


class TextStrategy(_Strategy):
    kind: Literal["text"] = "text"
    role: str
    text: str


class CssStrategy(_Strategy):
    kind: Literal["css"] = "css"
    selector: str


LocatorStrategy = Annotated[
    FieldNameStrategy | RoleNameStrategy | LabelStrategy | TableCellStrategy | TextStrategy | CssStrategy,
    Field(discriminator="kind"),
]


class Target(Model):
    description: str
    frame: FrameRef
    expect_role: str
    strategies: list[LocatorStrategy] = Field(min_length=1)

    @field_validator("strategies")
    @classmethod
    def _ranked(cls, v: list[Any]) -> list[Any]:
        return sorted(v, key=lambda s: s.score, reverse=True)


class UrlCondition(Model):
    kind: Literal["url"] = "url"
    frame: FrameRef
    path_regex: str


class TextCondition(Model):
    kind: Literal["text"] = "text"
    frame: FrameRef | None = None
    text: str | None = None
    regex: str | None = None
    present: bool = True

    @model_validator(mode="after")
    def _one(self) -> TextCondition:
        if (self.text is None) == (self.regex is None):
            raise ValueError("text condition needs exactly one of 'text' or 'regex'")
        return self


class ElementCondition(Model):
    kind: Literal["element"] = "element"
    target: Target


Condition = Annotated[UrlCondition | TextCondition | ElementCondition, Field(discriminator="kind")]


class Checkpoint(Model):
    all: list[Condition] = Field(min_length=1)
    timeout_ms: int = Field(default=10_000, ge=100, le=120_000)


class RetryPolicy(Model):
    attempts: int = Field(default=2, ge=1, le=5)
    backoff_ms: int = Field(default=500, ge=0, le=10_000)


class DialogExpectation(Model):
    action: Literal["accept", "dismiss"]
    message_regex: str


class ParseRule(StrEnum):
    TEXT = "text"
    INTEGER = "integer"
    CURRENCY = "currency"


class _Step(Model):
    id: str = Field(pattern=r"^s\d{2,3}$")
    phase: Literal["auth", "main"] = "main"
    intent: str
    effect: Effect
    expect: Checkpoint | None = None
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    dialog: DialogExpectation | None = None
    requires_approval: bool = False
    recorded_by: Literal["agent", "human", "author"] = "agent"


class NavigateStep(_Step):
    action: Literal["navigate"] = "navigate"
    url: str


class ClickStep(_Step):
    action: Literal["click"] = "click"
    target: Target


class FillStep(_Step):
    action: Literal["fill"] = "fill"
    target: Target
    value: ValueExpr


class SelectStep(_Step):
    action: Literal["select"] = "select"
    target: Target
    value: ValueExpr


class PressStep(_Step):
    action: Literal["press"] = "press"
    target: Target | None = None
    key: str


class ExtractStep(_Step):
    action: Literal["extract"] = "extract"
    target: Target
    output: str = Field(pattern=IDENT)
    attribute: Literal["text", "value"] = "text"
    parse: ParseRule = ParseRule.TEXT


Step = Annotated[
    NavigateStep | ClickStep | FillStep | SelectStep | PressStep | ExtractStep, Field(discriminator="action")
]


class AppBinding(Model):
    product: str
    profile: str = Field(description="App profile id@version: shared runtime-state detectors for this vendor product")
    product_version: str | None = None


class SurfaceSpec(Model):
    kind: Literal["web", "legacy_web", "desktop"] = "web"
    entry_url: str = Field(description="Template; {base_url} is bound per tenant, never supplied by the caller")


class Provenance(Model):
    recorded_at: datetime
    discovery_run_id: str
    planner: str
    goal: str
    recorded_against: str
    recorder_version: str
    human_steps: int = 0


class Approval(Model):
    approved_by: str
    approved_at: datetime
    note: str = ""


class RiskSummary(Model):
    max_effect: Effect
    approval_required_steps: list[str] = Field(default_factory=list)


class Capability(Model):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    id: str = Field(pattern=CAPABILITY_ID)
    version: str = Field(pattern=SEMVER)
    status: Status = Status.DRAFT
    title: str
    description: str
    app: AppBinding
    surface: SurfaceSpec
    inputs: list[ParamSpec] = Field(default_factory=list)
    secrets: list[SecretRef] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)
    outcomes: list[OutcomeSpec] = Field(default_factory=list)
    steps: list[Step] = Field(min_length=1)
    success: Checkpoint
    risk: RiskSummary
    provenance: Provenance
    approval: Approval | None = None
    content_hash: str | None = None

    @model_validator(mode="after")
    def _referential_integrity(self) -> Capability:
        ids = [s.id for s in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("step ids must be unique")
        inputs = {p.name for p in self.inputs}
        secrets = {s.name for s in self.secrets}
        outputs = {o.name for o in self.outputs}
        extracted: list[str] = []
        for step in self.steps:
            for ref in _value_refs(step):
                if isinstance(ref, ParamValue) and ref.name not in inputs:
                    raise ValueError(f"{step.id}: undeclared input {ref.name!r}")
                if isinstance(ref, SecretValue) and ref.name not in secrets:
                    raise ValueError(f"{step.id}: undeclared secret {ref.name!r}")
            if isinstance(step, ExtractStep):
                extracted.append(step.output)
            if step.requires_approval and step.effect is not Effect.IRREVERSIBLE:
                raise ValueError(f"{step.id}: only irreversible steps carry requires_approval")
        if sorted(extracted) != sorted(outputs):
            raise ValueError(f"every declared output must be extracted exactly once (declared={sorted(outputs)}, extracted={sorted(extracted)})")
        max_effect = max((s.effect for s in self.steps), key=lambda e: e.rank)
        if self.risk.max_effect.rank < max_effect.rank:
            raise ValueError(f"risk.max_effect understates steps ({max_effect})")
        if self.status is Status.APPROVED and self.approval is None:
            raise ValueError("approved capabilities need an approval record")
        return self

    def compute_hash(self) -> str:
        """Hash of the behavioural contract; excludes status/approval/provenance so review state doesn't churn it."""
        body = self.model_dump(mode="json", exclude={"status", "approval", "provenance", "content_hash"})
        return "sha256:" + hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"

    def tool_definition(self) -> dict[str, Any]:
        required = [p.name for p in self.inputs if p.required]
        outcome_codes = ", ".join(o.code for o in self.outcomes) or "none"
        return {
            "name": self.id.replace(".", "__"),
            "description": (
                f"{self.title}. {self.description} Returns outputs "
                f"{[o.name for o in self.outputs]} on success; business outcomes: {outcome_codes}. "
                f"Max effect: {self.risk.max_effect}."
            ),
            "input_schema": {
                "type": "object",
                "properties": {p.name: p.json_schema() for p in self.inputs},
                "required": required,
                "additionalProperties": False,
            },
        }


def _value_refs(step: Any) -> list[Any]:
    refs: list[Any] = []
    if isinstance(step, FillStep | SelectStep):
        refs.append(step.value)
    target = getattr(step, "target", None)
    if target is not None:
        refs.extend(s.row_key for s in target.strategies if isinstance(s, TableCellStrategy))
    return refs


def export_json_schema() -> dict[str, Any]:
    return Capability.model_json_schema()
