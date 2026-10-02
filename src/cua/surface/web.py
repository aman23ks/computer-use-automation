"""WebSurface: Playwright transport + the in-page probe (probe.js) for perception and targeting.

Playwright is used as a transport (frames, input events, screenshots, network interception);
element identification never relies on the app's DOM hygiene. The probe derives roles,
accessible names, visual labels and table structure the way an operator reads the screen.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from pathlib import Path
from typing import Any

from playwright.async_api import (
    Browser,
    BrowserContext,
    Dialog,
    Frame,
    Page,
    Playwright,
    Route,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError

from cua.artifact.schema import (
    Checkpoint,
    Condition,
    DialogExpectation,
    ElementCondition,
    FrameRef,
    LiteralValue,
    ParamValue,
    TableCellStrategy,
    Target,
    TextCondition,
    UrlCondition,
)
from cua.safety.policy import Policy
from cua.safety.redaction import Redactor
from cua.surface.base import DialogEvent, ElementDescriptor, FrameSnapshot, Observation, Resolution
from cua.urls import canonical_path_regex, path_and_query

PROBE_JS = (Path(__file__).parent / "probe.js").read_text()
SEMANTIC_SCORE = 0.6


class SurfaceError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class LocatorConflict(SurfaceError):
    def __init__(self, attempts: list[tuple[str, int]]) -> None:
        super().__init__("LOCATOR_CONFLICT", f"semantic strategies resolved to different elements: {attempts}")
        self.attempts = attempts


def bind_strategy(strategy: Any, bind: dict[str, str]) -> dict[str, Any]:
    payload: dict[str, Any] = strategy.model_dump(mode="json", exclude={"score", "rationale"})
    if isinstance(strategy, TableCellStrategy):
        rk = strategy.row_key
        if isinstance(rk, LiteralValue):
            payload["row_key"] = rk.value
        elif isinstance(rk, ParamValue):
            payload["row_key"] = bind[rk.name]
        else:
            raise SurfaceError("INVALID_ARTIFACT", "secrets cannot be used inside locators")
    return payload


class WebSurface:
    kind = "web"

    def __init__(
        self,
        policy: Policy,
        redactor: Redactor,
        *,
        headless: bool = True,
        viewport: tuple[int, int] = (1280, 800),
        action_timeout_ms: int = 5_000,
        navigation_timeout_ms: int = 15_000,
    ) -> None:
        self.policy = policy
        self.redactor = redactor
        self.headless = headless
        self.viewport = viewport
        self.action_timeout_ms = action_timeout_ms
        self.navigation_timeout_ms = navigation_timeout_ms
        self.lock = asyncio.Lock()
        self.blocked_requests: list[str] = []
        self.human_events: list[dict[str, Any]] = []
        self._recording_human = False
        self._console_busy = False
        self._dialog_expectation: DialogExpectation | None = None
        self._dialogs: list[DialogEvent] = []
        self._last_frames: list[Frame] = []
        self.last_attempts: list[tuple[str, int]] = []
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    async def __aenter__(self) -> WebSurface:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("surface not started")
        return self._page

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=self.headless)
        self._context = await self._browser.new_context(
            viewport={"width": self.viewport[0], "height": self.viewport[1]}, java_script_enabled=True
        )
        await self._context.add_init_script(script=PROBE_JS)
        await self._context.expose_binding("__cuaHuman", self._on_human_event)
        await self._context.route("**/*", self._guard)
        self._page = await self._context.new_page()
        self._page.on("dialog", self._on_dialog)

    async def close(self) -> None:
        for closer in (self._context, self._browser):
            if closer is not None:
                with contextlib.suppress(PlaywrightError):
                    await closer.close()
        if self._pw is not None:
            await self._pw.stop()

    async def _guard(self, route: Route) -> None:
        url = route.request.url
        ok, _ = self.policy.url_allowed(url)
        if ok:
            await route.continue_()
        else:
            self.blocked_requests.append(self.redactor.text(url))
            await route.abort("blockedbyclient")

    async def _on_dialog(self, dialog: Dialog) -> None:
        message = dialog.message
        exp = self._dialog_expectation
        if exp is not None and re.search(exp.message_regex, message):
            await (dialog.accept() if exp.action == "accept" else dialog.dismiss())
            self._dialogs.append(DialogEvent(dialog.type, message, exp.action, True))
            return
        if dialog.type == "alert":
            await dialog.accept()
            handled = "acknowledged"
        else:
            await dialog.dismiss()
            handled = "dismissed"
        self._dialogs.append(DialogEvent(dialog.type, message, handled, False))

    def expect_dialog(self, expectation: DialogExpectation | None) -> None:
        self._dialog_expectation = expectation

    def drain_dialogs(self) -> list[DialogEvent]:
        out, self._dialogs = self._dialogs, []
        return out

    def _on_human_event(self, source: dict[str, Any], entry: dict[str, Any]) -> None:
        if not self._recording_human or self._console_busy:
            return
        frame: Frame = source["frame"]
        self.human_events.append({"frame": self.frame_ref(frame).model_dump(), "frame_url": frame.url, **entry})

    def set_human_recording(self, on: bool) -> None:
        self._recording_human = on
        if on:
            self.human_events = []

    def frame_path(self, frame: Frame) -> list[str]:
        path: list[str] = []
        f: Frame | None = frame
        while f is not None and f.parent_frame is not None:
            siblings = f.parent_frame.child_frames
            path.insert(0, f.name or f"@{siblings.index(f)}")
            f = f.parent_frame
        return path

    def frame_ref(self, frame: Frame) -> FrameRef:
        return FrameRef(path=self.frame_path(frame), url_regex=canonical_path_regex(frame.url))

    def find_frame(self, ref: FrameRef | None) -> Frame | None:
        frames = [f for f in self.page.frames if not f.is_detached()]
        if ref is None:
            return self.page.main_frame
        for f in frames:
            if self.frame_path(f) == ref.path:
                return f
        if ref.url_regex:
            for f in frames:
                if re.search(ref.url_regex, path_and_query(f.url)):
                    return f
        return None

    def frame_url(self, frame: FrameRef) -> str | None:
        f = self.find_frame(frame)
        return f.url if f else None

    async def _eval(self, frame: Frame, expr: str, arg: Any = None) -> Any:
        try:
            return await frame.evaluate(expr, arg)
        except PlaywrightError:
            return None

    async def observe(self, *, screenshot: bool) -> Observation:
        frames = [f for f in self.page.frames if not f.is_detached()]
        label_rx = self.redactor.js_label_regex()
        next_ref = 1
        snaps: list[FrameSnapshot] = []
        elements: dict[str, dict[str, Any]] = {}
        ref_frames: dict[str, int] = {}
        for i, f in enumerate(frames):
            snap = await self._eval(f, "([n, l]) => window.__cua ? window.__cua.snapshot(n, l) : null", [next_ref, label_rx])
            if not snap:
                continue
            next_ref = snap["next"]
            for el in snap["elements"]:
                el["frame"] = self.frame_path(f)
                elements[el["ref"]] = el
                ref_frames[el["ref"]] = i
            snaps.append(FrameSnapshot(
                index=i,
                path=self.frame_path(f),
                url=f.url,
                title=self.redactor.text(snap["title"]),
                lines=[self.redactor.text(line) for line in snap["lines"]],
                landmarks=[self.redactor.text(t) for t in snap["landmarks"]],
            ))
        self._last_frames = frames
        shot = await self.screenshot(masked=True) if screenshot else None
        dialogs = [f"{d.kind}: {self.redactor.text(d.message)!r} -> {d.handled}" for d in self._dialogs]
        return Observation(url=self.page.url, frames=snaps, elements=elements, ref_frames=ref_frames, dialogs=dialogs, screenshot=shot)

    async def describe_ref(self, ref: str, obs: Observation) -> ElementDescriptor:
        idx = obs.ref_frames.get(ref)
        if idx is None or idx >= len(self._last_frames):
            raise SurfaceError("UNKNOWN_REF", f"element ref {ref} is not in the latest observation")
        frame = self._last_frames[idx]
        data = await self._eval(frame, "(r) => window.__cua.describeRef(r)", ref)
        if not data:
            raise SurfaceError("STALE_REF", f"element ref {ref} is no longer attached; observe again")
        return ElementDescriptor(frame=self.frame_ref(frame), frame_url=frame.url, info=data["info"], candidates=data["candidates"])

    async def resolve(self, target: Target, bind: dict[str, str]) -> Resolution | None:
        frame = self.find_frame(target.frame)
        attempts: list[tuple[str, int]] = []
        self.last_attempts = attempts
        if frame is None:
            attempts.append((f"frame:{target.frame.label()}", 0))
            return None
        first: Resolution | None = None
        first_score = 0.0
        for i, strategy in enumerate(target.strategies):
            if first is not None and (first_score < SEMANTIC_SCORE or strategy.score < SEMANTIC_SCORE):
                break
            try:
                handle = await frame.evaluate_handle(
                    "(s) => { const r = window.__cua.resolve(s); return r.length === 1 ? r[0] : r.length; }",
                    bind_strategy(strategy, bind),
                )
            except PlaywrightError:
                attempts.append((strategy.kind, 0))
                return None
            el = handle.as_element()
            if el is None:
                attempts.append((strategy.kind, int(await handle.json_value())))
                continue
            role = await el.evaluate("(e) => window.__cua.roleOf(e)")
            if role != target.expect_role:
                attempts.append((strategy.kind, -1))
                continue
            attempts.append((strategy.kind, 1))
            if first is None:
                first = Resolution(el, frame, i, strategy.kind, attempts)
                first_score = strategy.score
                continue
            if not await frame.evaluate("([a, b]) => a === b", [first.handle, el]):
                raise LocatorConflict(attempts)
            break
        if first is not None:
            first.attempts = attempts
        return first

    async def click(self, res: Resolution) -> None:
        await res.handle.click(timeout=self.action_timeout_ms)

    async def fill(self, res: Resolution, value: str) -> None:
        await res.handle.fill(value, timeout=self.action_timeout_ms)

    async def select(self, res: Resolution, option: str) -> None:
        await res.handle.select_option(label=option, timeout=self.action_timeout_ms)

    async def press(self, key: str, res: Resolution | None) -> None:
        if res is not None:
            await res.handle.press(key, timeout=self.action_timeout_ms)
        else:
            await self.page.keyboard.press(key)

    async def read(self, res: Resolution, attribute: str) -> str:
        return str(await res.handle.evaluate("(e, a) => window.__cua.read(e, a)", attribute))

    async def navigate(self, url: str) -> None:
        ok, why = self.policy.url_allowed(url)
        if not ok:
            raise SurfaceError("POLICY_VIOLATION", why)
        await self.page.goto(url, wait_until="load", timeout=self.navigation_timeout_ms)

    async def text_state(self) -> dict[Frame, str]:
        """Visible text of every frame, fetched once per poll (one evaluate per frame)."""
        out: dict[Frame, str] = {}
        for f in self.page.frames:
            if f.is_detached():
                continue
            text = await self._eval(f, "() => document.body ? document.body.innerText : ''")
            out[f] = " ".join(str(text or "").split())
        return out

    async def check(self, cond: Condition, bind: dict[str, str], texts: dict[Frame, str] | None = None) -> bool:
        if isinstance(cond, UrlCondition):
            f = self.find_frame(cond.frame)
            return f is not None and re.search(cond.path_regex, path_and_query(f.url)) is not None
        if isinstance(cond, TextCondition):
            texts = texts if texts is not None else await self.text_state()
            if cond.frame is not None:
                f = self.find_frame(cond.frame)
                pool = [texts.get(f, "")] if f is not None else []
            else:
                pool = list(texts.values())
            if cond.regex is not None:
                rx = re.compile(cond.regex, re.I)
                hit = any(rx.search(t) for t in pool)
            else:
                needle = " ".join((cond.text or "").split()).lower()
                hit = any(needle in t.lower() for t in pool)
            return hit if cond.present else not hit
        if isinstance(cond, ElementCondition):
            try:
                return await self.resolve(cond.target, bind) is not None
            except LocatorConflict:
                return False
        return False

    async def check_all(
        self, cp: Checkpoint, bind: dict[str, str], texts: dict[Frame, str] | None = None
    ) -> tuple[bool, list[str]]:
        texts = texts if texts is not None else await self.text_state()
        failed = [describe_condition(c) for c in cp.all if not await self.check(c, bind, texts)]
        return not failed, failed

    async def settle(self, timeout_ms: int = 5_000, quiet_ms: int = 300) -> None:
        """Wait until every frame is loaded and frame URLs have been stable for quiet_ms."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_ms / 1000
        last: list[str] | None = None
        stable_since = loop.time()
        while loop.time() < deadline:
            frames = [f for f in self.page.frames if not f.is_detached()]
            urls = [f.url for f in frames]
            states = [await self._eval(f, "() => document.readyState") for f in frames]
            now = loop.time()
            if urls != last:
                last, stable_since = urls, now
            elif all(st == "complete" for st in states) and now - stable_since >= quiet_ms / 1000:
                return
            await asyncio.sleep(0.05)

    async def frame_urls(self) -> dict[str, str]:
        return {"/".join(self.frame_path(f)) or "top": f.url for f in self.page.frames if not f.is_detached()}

    async def landmarks(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for f in self.page.frames:
            if f.is_detached():
                continue
            snap = await self._eval(f, "() => window.__cua ? window.__cua.landmarks() : []")
            out["/".join(self.frame_path(f)) or "top"] = [self.redactor.text(t) for t in (snap or [])]
        return out

    async def match_text(self, regex: str, frame: FrameRef | None) -> str | None:
        frames = [self.find_frame(frame)] if frame else [f for f in self.page.frames if not f.is_detached()]
        for f in frames:
            if f is None:
                continue
            m = await self._eval(f, "(r) => window.__cua ? window.__cua.matchText(r) : null", regex)
            if m:
                return str(m)
        return None

    async def screenshot(self, *, masked: bool) -> bytes:
        frames = [f for f in self.page.frames if not f.is_detached()]
        if masked:
            args = [self.redactor.js_patterns(), self.redactor.sensitive_values, self.redactor.js_label_regex()]
            for f in frames:
                await self._eval(f, "([p, v, l]) => window.__cua && window.__cua.markSensitive(p, v, l)", args)
        try:
            return await self.page.screenshot(
                type="jpeg",
                quality=60,
                animations="disabled",
                mask=[f.locator("[data-cua-mask]") for f in frames] if masked else None,
                mask_color="#1a1a1a",
            )
        finally:
            if masked:
                for f in frames:
                    await self._eval(f, "() => window.__cua && window.__cua.clearMasks()")

    async def dom_text(self) -> str:
        return (await self.observe(screenshot=False)).render(max_lines_per_frame=400)

    async def _frame_at(self, x: float, y: float) -> tuple[Frame, float, float]:
        best: tuple[int, Frame, float, float] = (0, self.page.main_frame, x, y)
        for f in self.page.frames:
            if f.parent_frame is None or f.is_detached():
                continue
            try:
                box = await (await f.frame_element()).bounding_box()
            except PlaywrightError:
                continue
            if box and box["x"] <= x < box["x"] + box["width"] and box["y"] <= y < box["y"] + box["height"]:
                depth = len(self.frame_path(f))
                if depth > best[0]:
                    best = (depth, f, x - box["x"], y - box["y"])
        return best[1], best[2], best[3]

    async def describe_at(self, x: float, y: float) -> ElementDescriptor | None:
        frame, lx, ly = await self._frame_at(x, y)
        data = await self._eval(frame, "([x, y]) => window.__cua.describeAt(x, y)", [lx, ly])
        if not data:
            return None
        return ElementDescriptor(frame=self.frame_ref(frame), frame_url=frame.url, info=data["info"], candidates=data["candidates"])

    async def describe_focused(self) -> ElementDescriptor | None:
        for f in self.page.frames:
            data = await self._eval(f, "() => window.__cua ? window.__cua.describeActive() : null")
            if data:
                return ElementDescriptor(frame=self.frame_ref(f), frame_url=f.url, info=data["info"], candidates=data["candidates"])
        return None

    async def console_click(self, x: float, y: float) -> ElementDescriptor | None:
        self._console_busy = True
        try:
            desc = await self.describe_at(x, y)
            await self.page.mouse.click(x, y)
            await asyncio.sleep(0.05)
            return desc
        finally:
            self._console_busy = False

    async def console_type(self, text: str) -> ElementDescriptor | None:
        desc = await self.describe_focused()
        await self.page.keyboard.type(text)
        return desc

    async def console_press(self, key: str) -> ElementDescriptor | None:
        desc = await self.describe_focused()
        await self.page.keyboard.press(key)
        return desc


def describe_condition(c: Condition) -> str:
    if isinstance(c, UrlCondition):
        return f"url[{c.frame.label()}] ~ {c.path_regex}"
    if isinstance(c, TextCondition):
        where = c.frame.label() if c.frame else "any frame"
        what = c.text if c.text is not None else f"/{c.regex}/"
        return f"text {'present' if c.present else 'absent'} in {where}: {what!r}"
    return f"element {c.target.description!r} resolvable"
