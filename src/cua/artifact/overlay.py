"""Tenant overlays: specialise a base capability for one tenant without re-recording it.

The base artifact is recorded once per vendor product; an overlay carries only the tenant's
deltas (branding-driven labels, extra locator strategies, timeouts) keyed by stable step ids and
pinned to a base major version. The effective artifact is recomputed (and re-hashed) at load time,
so the base's approval covers the shared flow and the overlay is reviewed separately.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from cua.artifact.schema import Capability, LocatorStrategy


class StepPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step: str
    prepend_strategies: list[LocatorStrategy] = Field(default_factory=list)
    timeout_ms: int | None = None


class Overlay(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant: str
    capability: str
    base_major: int
    note: str = ""
    steps: list[StepPatch] = Field(default_factory=list)
    success_text_replacements: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Overlay:
        return cls.model_validate(yaml.safe_load(path.read_text()))


class OverlayError(ValueError):
    pass


def apply_overlay(base: Capability, overlay: Overlay) -> Capability:
    if overlay.capability != base.id:
        raise OverlayError(f"overlay targets {overlay.capability}, artifact is {base.id}")
    if int(base.version.split(".")[0]) != overlay.base_major:
        raise OverlayError(f"overlay pinned to major {overlay.base_major}, artifact is {base.version}")
    data = base.model_dump(mode="python")
    by_id = {s["id"]: s for s in data["steps"]}
    for patch in overlay.steps:
        step = by_id.get(patch.step)
        if step is None:
            raise OverlayError(f"overlay patches unknown step {patch.step}")
        if patch.prepend_strategies:
            if "target" not in step or step["target"] is None:
                raise OverlayError(f"step {patch.step} has no target to patch")
            step["target"]["strategies"] = [s.model_dump() for s in patch.prepend_strategies] + step["target"]["strategies"]
        if patch.timeout_ms and step.get("expect"):
            step["expect"]["timeout_ms"] = patch.timeout_ms
    for cond in data["success"]["all"]:
        if cond.get("kind") == "text" and cond.get("text") in overlay.success_text_replacements:
            cond["text"] = overlay.success_text_replacements[cond["text"]]
    data["content_hash"] = None
    effective = Capability.model_validate(data)
    effective.content_hash = effective.compute_hash()
    return effective
