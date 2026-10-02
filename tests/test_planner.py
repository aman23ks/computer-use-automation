from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from cua.agent.planner import ClaudePlanner, Turn
from cua.evidence import RunRecorder
from cua.safety.redaction import Redactor
from cua.surface.base import FrameSnapshot, Observation

from .conftest import goal


class StubMessages:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.responses.pop(0)


def response(*blocks: Any, stop: str = "tool_use") -> Any:
    usage = SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0)
    return SimpleNamespace(content=list(blocks), stop_reason=stop, usage=usage, model="claude-opus-5-5")


def tool_use(id_: str, name: str, args: dict[str, Any]) -> Any:
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=args,
                           model_dump=lambda **_: {"type": "tool_use", "id": id_, "name": name, "input": args})


def text(t: str) -> Any:
    return SimpleNamespace(type="text", text=t, model_dump=lambda **_: {"type": "text", "text": t})


def obs() -> Observation:
    return Observation(url="http://h/", frames=[FrameSnapshot(0, ["main"], "http://h/app", "t", ["[e1 button \"Go\"]"], [])],
                       elements={"e1": {"ref": "e1", "role": "button", "name": "Go"}}, ref_frames={"e1": 0}, screenshot=b"\xff\xd8")


async def test_claude_planner_request_shape_and_threading(tmp_path: Path) -> None:
    stub = StubMessages([
        response(text("thinking out loud"), stop="end_turn"),
        response(tool_use("tu_1", "click", {"ref": "e1", "accept_dialog": False, "rationale": "go"})),
        response(tool_use("tu_2", "finish", {"status": "success", "summary": "", "success_evidence": ["Done"]})),
    ])
    client = SimpleNamespace(beta=SimpleNamespace(messages=stub))
    rec = RunRecorder(tmp_path, "discovery", Redactor())
    planner = ClaudePlanner(goal("read_primary_savings_balance"), "http://h", 10, rec, client=client)  # type: ignore[arg-type]

    first = await planner.decide(Turn(obs(), "start", 1, 9))
    assert first.tool == "click" and first.args["ref"] == "e1"
    req = stub.calls[0]
    assert req["model"] == "claude-opus-5-5"
    assert req["thinking"] == {"type": "adaptive"} and req["output_config"]["effort"] == "medium"
    assert req["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    assert all(t["strict"] and t["input_schema"]["additionalProperties"] is False for t in req["tools"])
    assert "{{member_number}}" in req["messages"][0]["content"][0]["text"]
    assert "100234" not in str(req["messages"])
    assert stub.calls[1]["messages"][-1]["content"][0]["text"] == "Call exactly one tool to continue."

    second = await planner.decide(Turn(obs(), "OK: clicked", 2, 8))
    assert second.tool == "finish"
    result_msg = stub.calls[2]["messages"][-1]["content"][0]
    assert result_msg["type"] == "tool_result" and result_msg["tool_use_id"] == "tu_1"
    assert any(b.get("type") == "image" for b in result_msg["content"])
    assert all(m["role"] in ("user", "assistant") for m in planner.messages)
    assert {"type": "image", "omitted": True} in planner.transcript()[0]["content"]
