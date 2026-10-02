from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from cua.artifact.schema import CAPABILITY_ID, OutcomeSpec, OutputSpec, ParamSpec, SecretRef


class GoalSpec(BaseModel):
    """Discovery input: what capability to learn, its intended contract, and where."""

    model_config = ConfigDict(extra="forbid")
    capability_id: str = Field(pattern=CAPABILITY_ID)
    title: str
    description: str
    goal: str = Field(description="Natural-language goal; may reference inputs as {name}")
    product: str
    profile: str
    entry_path: str = "/"
    inputs: list[ParamSpec] = Field(default_factory=list)
    secrets: list[SecretRef] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)
    outcomes: list[OutcomeSpec] = Field(default_factory=list)
    max_steps: int | None = None

    @classmethod
    def load(cls, path: Path) -> GoalSpec:
        return cls.model_validate(yaml.safe_load(path.read_text()))

    def goal_for_model(self) -> str:
        """The goal as shown to the model: inputs are referenced by placeholder, never by value."""
        text = self.goal
        for p in self.inputs:
            text = text.replace("{" + p.name + "}", "{{" + p.name + "}}")
        return text
