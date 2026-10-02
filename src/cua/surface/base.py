"""Surface abstraction: the seam between "how we perceive/act" and "the recorded flow".

The recorded flow (artifact) speaks only in surface-neutral terms: frames/windows, roles,
accessible names, visual labels, table structure. A Surface implementation maps those onto a
concrete technology. WebSurface (Playwright + in-page probe) is implemented; a desktop surface
would implement the same protocol over UI Automation / AX API trees, and a pixel-only surface over
screenshots + a grounding model, emitting the same ElementInfo/candidate structures.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Protocol

from cua.artifact.schema import Checkpoint, Condition, DialogExpectation, FrameRef, Target


@dataclass
class FrameSnapshot:
    index: int
    path: list[str]
    url: str
    title: str
    lines: list[str]
    landmarks: list[str]


@dataclass
class Observation:
    url: str
    frames: list[FrameSnapshot]
    elements: dict[str, dict[str, Any]]
    ref_frames: dict[str, int]
    dialogs: list[str] = field(default_factory=list)
    screenshot: bytes | None = None

    def render(self, max_lines_per_frame: int = 160) -> str:
        out = [f"Top URL: {self.url}"]
        for f in self.frames:
            if not f.lines and not f.path:
                continue
            head = f'== frame "{"/".join(f.path) or "top"}" url={_path_of(f.url)} title={f.title!r}'
            if f.landmarks:
                head += f" headings={f.landmarks}"
            out.append(head)
            lines = f.lines[:max_lines_per_frame]
            out.extend(f"  {line}" for line in lines)
            if len(f.lines) > max_lines_per_frame:
                out.append(f"  ... ({len(f.lines) - max_lines_per_frame} more lines truncated)")
        if self.dialogs:
            out.append(f"Dialogs handled since last action: {self.dialogs}")
        return "\n".join(out)

    def signature(self) -> str:
        h = hashlib.sha256()
        for f in self.frames:
            h.update(_path_of(f.url).encode())
            h.update("\n".join(f.lines).encode())
        return h.hexdigest()[:16]


def _path_of(url: str) -> str:
    from urllib.parse import urlsplit

    p = urlsplit(url)
    return p.path + (f"?{p.query}" if p.query else "")


@dataclass
class Resolution:
    handle: Any
    frame: Any
    strategy_index: int
    strategy_kind: str
    attempts: list[tuple[str, int]]


@dataclass
class ElementDescriptor:
    frame: FrameRef
    frame_url: str
    info: dict[str, Any]
    candidates: list[dict[str, Any]]


@dataclass
class DialogEvent:
    kind: str
    message: str
    handled: str
    expected: bool


class Surface(Protocol):
    kind: str

    async def observe(self, *, screenshot: bool) -> Observation: ...
    async def describe_ref(self, ref: str, obs: Observation) -> ElementDescriptor: ...
    async def resolve(self, target: Target, bind: dict[str, str]) -> Resolution | None: ...
    async def click(self, res: Resolution) -> None: ...
    async def fill(self, res: Resolution, value: str) -> None: ...
    async def select(self, res: Resolution, option: str) -> None: ...
    async def press(self, key: str, res: Resolution | None) -> None: ...
    async def read(self, res: Resolution, attribute: str) -> str: ...
    async def navigate(self, url: str) -> None: ...
    async def check(self, cond: Condition, bind: dict[str, str]) -> bool: ...
    async def check_all(self, cp: Checkpoint, bind: dict[str, str]) -> tuple[bool, list[str]]: ...
    async def match_text(self, regex: str, frame: FrameRef | None) -> str | None: ...
    async def screenshot(self, *, masked: bool) -> bytes: ...
    async def dom_text(self) -> str: ...
    def expect_dialog(self, expectation: DialogExpectation | None) -> None: ...
    def drain_dialogs(self) -> list[DialogEvent]: ...
    def frame_url(self, frame: FrameRef) -> str | None: ...
