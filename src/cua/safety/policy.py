"""Execution policy: what the automation may touch and how risky each action is.

Enforced in code at three points, independent of anything the model says:
the action gate (before every discovery/replay action), the network route guard
(every request the browser makes), and the artifact loader (steps the policy forbids are rejected).
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field

from cua.artifact.schema import Effect
from cua.safety.redaction import DEFAULT_PATTERNS, DEFAULT_SENSITIVE_LABELS, Redactor

ActionKind = Literal["navigate", "click", "fill", "select", "press", "extract"]


class Verdict(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class RiskRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    effect: Effect
    reason: str
    name_regex: str | None = None
    url_regex: str | None = None
    actions: list[ActionKind] | None = None

    def matches(self, action: str, name: str, url_path: str) -> bool:
        if self.actions and action not in self.actions:
            return False
        if self.name_regex and not re.search(self.name_regex, name or ""):
            return False
        return not (self.url_regex and not re.search(self.url_regex, url_path))


class RedactionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    patterns: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_PATTERNS))
    sensitive_labels: str = DEFAULT_SENSITIVE_LABELS


class Decision(BaseModel):
    verdict: Verdict
    effect: Effect
    reason: str


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allowed_origins: list[str]
    allowed_paths: list[str]
    allowed_actions: list[ActionKind]
    risk_rules: list[RiskRule] = Field(default_factory=list)
    irreversible_in_discovery: Literal["require_approval", "deny"] = "require_approval"
    irreversible_in_replay: Literal["require_authorization", "deny"] = "require_authorization"
    max_discovery_steps: int = 30
    discovery_timeout_s: int = 600
    human_timeout_s: int = 900
    redaction: RedactionConfig = Field(default_factory=RedactionConfig)

    @classmethod
    def load(cls, path: Path) -> Policy:
        return cls.model_validate(yaml.safe_load(path.read_text()))

    def redactor(self) -> Redactor:
        return Redactor(patterns=dict(self.redaction.patterns), sensitive_labels=self.redaction.sensitive_labels)

    def url_allowed(self, url: str) -> tuple[bool, str]:
        if url.startswith(("data:", "about:blank", "javascript:")):
            return True, "inline"
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self.allowed_origins:
            return False, f"origin {origin} not in allowlist"
        if not any(re.search(p, parts.path or "/") for p in self.allowed_paths):
            return False, f"path {parts.path} not in allowlist"
        return True, "allowlisted"

    def classify(self, action: str, element: dict[str, Any] | None, url_path: str) -> Effect:
        name = (element or {}).get("name") or (element or {}).get("label") or ""
        for rule in self.risk_rules:
            if rule.matches(action, name, url_path):
                return rule.effect
        if action == "extract":
            return Effect.READ
        if action in ("fill", "select"):
            return Effect.INPUT
        if action == "navigate":
            return Effect.NAVIGATE
        submits = bool((element or {}).get("submits")) or (action == "press" and bool((element or {}).get("form")))
        if submits:
            return Effect.WRITE if (element or {}).get("form") == "post" else Effect.NAVIGATE
        return Effect.INPUT if action == "press" else Effect.NAVIGATE

    def decide(self, action: str, element: dict[str, Any] | None, url: str, *, mode: Literal["discovery", "replay"]) -> Decision:
        if action not in self.allowed_actions:
            return Decision(verdict=Verdict.DENY, effect=Effect.NAVIGATE, reason=f"action {action!r} not allowlisted")
        ok, why = self.url_allowed(url)
        if not ok:
            return Decision(verdict=Verdict.DENY, effect=Effect.NAVIGATE, reason=why)
        effect = self.classify(action, element, urlsplit(url).path)
        if effect is Effect.IRREVERSIBLE:
            deny = (self.irreversible_in_discovery if mode == "discovery" else self.irreversible_in_replay) == "deny"
            return Decision(
                verdict=Verdict.DENY if deny else Verdict.REQUIRE_APPROVAL,
                effect=effect,
                reason="irreversible action: " + next(
                    (r.reason for r in self.risk_rules if r.matches(action, (element or {}).get("name") or "", urlsplit(url).path)),
                    "classified irreversible",
                ),
            )
        return Decision(verdict=Verdict.ALLOW, effect=effect, reason=why)
