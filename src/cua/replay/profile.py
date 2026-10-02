"""App profile: knowledge about a vendor product shared by every tenant and every capability on it.

Runtime exceptional states (session expiry, interstitials, "not found", permission denials,
app errors) belong to the application, not to any one recorded flow, so they live here and are
evaluated continuously while replay waits. One curated profile per vendor product (+ version range)
is reused across hundreds of tenants; tenant overlays may extend it.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from cua.artifact.schema import Condition, Target


class StateKind(StrEnum):
    BUSINESS_OUTCOME = "business_outcome"
    RECOVERABLE = "recoverable"
    HARD_FAILURE = "hard_failure"


class Recovery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["click", "reauthenticate"]
    target: Target | None = None
    max_times: int = Field(default=2, ge=1, le=5)


class KnownState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    kind: StateKind
    description: str
    match: list[Condition] = Field(min_length=1)
    code: str | None = None
    message_regex: str | None = None
    recovery: Recovery | None = None


class DialogRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    message_regex: str
    description: str


class AuthSpec(BaseModel):
    """How to recognise the sign-on screen (by content: legacy apps render it at any URL)."""

    model_config = ConfigDict(extra="forbid")
    login_text_regex: str


class AppProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    version: int
    product: str
    auth: AuthSpec
    states: list[KnownState] = Field(default_factory=list)
    benign_dialogs: list[DialogRule] = Field(default_factory=list)

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"

    @classmethod
    def load(cls, path: Path) -> AppProfile:
        return cls.model_validate(yaml.safe_load(path.read_text()))

    def business_codes(self) -> list[tuple[str, str]]:
        return [(s.code, s.description) for s in self.states if s.kind is StateKind.BUSINESS_OUTCOME and s.code]


def load_profile(ref: str, profiles_dir: Path) -> AppProfile:
    pid, _, version = ref.partition("@")
    profile = AppProfile.load(profiles_dir / f"{pid}.yaml")
    if version and str(profile.version) != version:
        raise ValueError(f"profile {pid} is at version {profile.version}, artifact pins {version}")
    return profile
