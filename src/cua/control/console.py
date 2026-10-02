"""Minimal operator console (deliberately bare; a production console would be a co-browsing UI).

Exposes the pending intervention and the *same* live browser session the automation was using:
a live (unmasked, never persisted) screenshot view, click-at-point, type, key press, and
claim / resolve. All session endpoints require the lease from claim().
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from aiohttp import web

from cua.control.handoff import ControlChannel, ControlError, Decision, HumanAction
from cua.surface.base import ElementDescriptor
from cua.surface.web import WebSurface

CHANNEL: web.AppKey[ControlChannel] = web.AppKey("channel", ControlChannel)
SURFACE: web.AppKey[WebSurface] = web.AppKey("surface", WebSurface)


def _target(desc: ElementDescriptor | None) -> dict[str, Any] | None:
    if desc is None:
        return None
    info = desc.info
    return {
        "frame": desc.frame.model_dump(),
        "frame_url": desc.frame_url,
        "role": info.get("role"),
        "name": info.get("name"),
        "label": info.get("label"),
        "field": info.get("field"),
        "secret": bool(info.get("secret")),
        "form": info.get("form"),
        "submits": info.get("submits"),
        "candidates": desc.candidates,
    }


def _lease(request: web.Request) -> str | None:
    return request.headers.get("X-Lease") or request.query.get("lease")


async def _json(request: web.Request) -> dict[str, Any]:
    try:
        return dict(await request.json())
    except json.JSONDecodeError:
        return {}


@web.middleware
async def control_errors(request: web.Request, handler: Any) -> web.StreamResponse:
    try:
        return await handler(request)  # type: ignore[no-any-return]
    except ControlError as exc:
        return web.json_response({"error": str(exc)}, status=409)


async def state(request: web.Request) -> web.Response:
    return web.json_response(request.app[CHANNEL].state())


async def claim(request: web.Request) -> web.Response:
    body = await _json(request)
    lease = request.app[CHANNEL].claim(request.match_info["iid"], str(body.get("operator", "operator")))
    surface = request.app[SURFACE]
    surface.set_human_recording(not surface.headless)
    return web.json_response({"lease": lease})


async def resolve(request: web.Request) -> web.Response:
    body = await _json(request)
    channel = request.app[CHANNEL]
    surface = request.app[SURFACE]
    channel.require_lease(_lease(request))
    live = [HumanAction(kind=e["type"], at=_now(), target={"frame": e["frame"], "candidates": e["candidates"], **e["info"]}, value=e.get("value"))
            for e in surface.human_events]
    surface.set_human_recording(False)
    channel.add_human_actions(live)
    channel.resolve(request.match_info["iid"], _lease(request), Decision(str(body.get("decision"))), str(body.get("note", "")))
    return web.json_response({"ok": True})


async def screenshot(request: web.Request) -> web.Response:
    request.app[CHANNEL].require_lease(_lease(request))
    surface = request.app[SURFACE]
    async with surface.lock:
        shot = await surface.screenshot(masked=False)
    return web.Response(body=shot, content_type="image/jpeg", headers={"Cache-Control": "no-store"})


async def elements(request: web.Request) -> web.Response:
    request.app[CHANNEL].require_lease(_lease(request))
    surface = request.app[SURFACE]
    async with surface.lock:
        obs = await surface.observe(screenshot=False)
        out = []
        for ref, el in obs.elements.items():
            frame = surface._last_frames[obs.ref_frames[ref]]
            offset = {"x": 0.0, "y": 0.0}
            if frame.parent_frame is not None:
                box = await (await frame.frame_element()).bounding_box()
                offset = {"x": box["x"], "y": box["y"]} if box else offset
            b = el["bbox"]
            out.append({"ref": ref, "role": el["role"], "name": el["name"], "label": el.get("label"),
                        "center": {"x": offset["x"] + b["x"] + b["width"] / 2, "y": offset["y"] + b["y"] + b["height"] / 2}})
    return web.json_response({"text": obs.render(), "elements": out})


async def click(request: web.Request) -> web.Response:
    channel = request.app[CHANNEL]
    lease = _lease(request)
    channel.require_lease(lease)
    body = await _json(request)
    surface = request.app[SURFACE]
    async with surface.lock:
        desc = await surface.console_click(float(body["x"]), float(body["y"]))
    channel.record_human(lease, HumanAction(kind="click", at=_now(), target=_target(desc)))
    return web.json_response({"ok": True, "target": _target(desc)})


async def type_text(request: web.Request) -> web.Response:
    channel = request.app[CHANNEL]
    lease = _lease(request)
    channel.require_lease(lease)
    body = await _json(request)
    surface = request.app[SURFACE]
    async with surface.lock:
        desc = await surface.console_type(str(body["text"]))
    secret = bool(desc and desc.info.get("secret"))
    channel.record_human(lease, HumanAction(kind="type", at=_now(), target=_target(desc), value=None if secret else str(body["text"])))
    return web.json_response({"ok": True})


async def press(request: web.Request) -> web.Response:
    channel = request.app[CHANNEL]
    lease = _lease(request)
    channel.require_lease(lease)
    body = await _json(request)
    surface = request.app[SURFACE]
    async with surface.lock:
        desc = await surface.console_press(str(body["key"]))
    channel.record_human(lease, HumanAction(kind="press", at=_now(), target=_target(desc), value=str(body["key"])))
    return web.json_response({"ok": True})


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


INDEX_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>CUA Operator Console</title>
<style>
body{font:14px system-ui,sans-serif;margin:16px;background:#f6f6f4;color:#222}
.card{background:#fff;border:1px solid #ddd;border-radius:6px;padding:12px;margin-bottom:12px}
#live{border:1px solid #999;cursor:crosshair;max-width:100%}
button{margin-right:6px;padding:6px 10px} .pill{padding:2px 8px;border-radius:10px;background:#eee}
.human{background:#ffe2b8} .automation{background:#cfe8cf} .pending_human{background:#ffd0d0}
</style></head><body>
<h2>Operator Console</h2>
<div class="card">Controller: <span id="ctl" class="pill">-</span> &nbsp; Run: <code id="run"></code></div>
<div class="card" id="iv">No pending intervention.</div>
<div class="card" id="session" style="display:none">
  <div style="margin-bottom:8px">
    <input id="txt" placeholder="type into focused field" size="30"><button onclick="typeText()">Type</button>
    <button onclick="press('Enter')">Enter</button><button onclick="press('Tab')">Tab</button>
    <button onclick="resolveIv('resume')">Hand back to automation</button>
    <button onclick="resolveIv('approve')">Approve step</button><button onclick="resolveIv('deny')">Deny step</button>
    <button onclick="resolveIv('abort')">Abort run</button>
  </div>
  <img id="live" width="1280" height="800" onclick="clickAt(event)">
</div>
<script>
let lease=null, ivId=null;
async function j(url,opts){const r=await fetch(url,opts);return r.json();}
function hdr(){return {'Content-Type':'application/json','X-Lease':lease||''};}
async function refresh(){
  const s=await j('/api/state'); const c=document.getElementById('ctl');
  c.textContent=s.controller; c.className='pill '+s.controller; document.getElementById('run').textContent=s.run_id;
  const iv=s.intervention; const box=document.getElementById('iv');
  if(!iv){box.textContent='No pending intervention.'; document.getElementById('session').style.display='none'; lease=null; return;}
  ivId=iv.id;
  box.innerHTML='<b>'+iv.id+'</b> ['+iv.reason+'] step='+(iv.step||'-')+'<br>'+iv.message+
    '<pre style="white-space:pre-wrap;max-height:180px;overflow:auto">'+iv.state_summary.replace(/</g,'&lt;')+'</pre>'+
    (lease?'':'<button onclick="claim()">Take control of live session</button>');
  if(lease){document.getElementById('session').style.display='block';
    document.getElementById('live').src='/api/session/screenshot?lease='+encodeURIComponent(lease)+'&t='+Date.now();}
}
async function claim(){const r=await j('/api/interventions/'+ivId+'/claim',{method:'POST',headers:hdr(),body:JSON.stringify({operator:'console-operator'})}); lease=r.lease; refresh();}
async function clickAt(e){const img=e.target;const sx=1280/img.clientWidth, sy=800/img.clientHeight;
  await j('/api/session/click',{method:'POST',headers:hdr(),body:JSON.stringify({x:e.offsetX*sx,y:e.offsetY*sy})}); setTimeout(refresh,400);}
async function typeText(){const t=document.getElementById('txt'); await j('/api/session/type',{method:'POST',headers:hdr(),body:JSON.stringify({text:t.value})}); t.value=''; setTimeout(refresh,300);}
async function press(k){await j('/api/session/press',{method:'POST',headers:hdr(),body:JSON.stringify({key:k})}); setTimeout(refresh,400);}
async function resolveIv(d){const note=prompt('Note for the run log (optional)')||''; const r=await j('/api/interventions/'+ivId+'/resolve',{method:'POST',headers:hdr(),body:JSON.stringify({decision:d,note})}); if(r.error) alert(r.error); lease=null; refresh();}
setInterval(refresh,1500); refresh();
</script></body></html>"""


async def index(request: web.Request) -> web.Response:
    return web.Response(text=INDEX_HTML, content_type="text/html")


def create_console(channel: ControlChannel, surface: WebSurface) -> web.Application:
    app = web.Application(middlewares=[control_errors])
    app[CHANNEL] = channel
    app[SURFACE] = surface
    app.add_routes([
        web.get("/", index),
        web.get("/api/state", state),
        web.post("/api/interventions/{iid}/claim", claim),
        web.post("/api/interventions/{iid}/resolve", resolve),
        web.get("/api/session/screenshot", screenshot),
        web.get("/api/session/elements", elements),
        web.post("/api/session/click", click),
        web.post("/api/session/type", type_text),
        web.post("/api/session/press", press),
    ])
    return app


class ConsoleServer:
    def __init__(self, channel: ControlChannel, surface: WebSurface, host: str = "127.0.0.1", port: int = 8765) -> None:
        self.app = create_console(channel, surface)
        self.host, self.port = host, port
        self._runner: web.AppRunner | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    async def __aenter__(self) -> ConsoleServer:
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        await web.TCPSite(self._runner, self.host, self.port).start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

