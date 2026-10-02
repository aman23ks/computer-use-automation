from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from cua.artifact.locators import build_target
from cua.artifact.overlay import Overlay, OverlayError, apply_overlay
from cua.artifact.recorder import RecordingError, parse_value
from cua.artifact.schema import (
    Capability,
    Checkpoint,
    Effect,
    FrameRef,
    LiteralValue,
    OutputType,
    ParamSpec,
    ParamType,
    ParamValue,
    ParseRule,
    SecretValue,
    Sensitivity,
    Status,
    TableCellStrategy,
    TextCondition,
)
from cua.replay.engine import parse_output
from cua.replay.params import InputError, validate_inputs
from cua.safety.policy import Policy, Verdict
from cua.safety.redaction import Redactor
from cua.surface.base import ElementDescriptor
from cua.urls import canonical_path_regex


def cap_dict(**over: Any) -> dict[str, Any]:
    target = {"description": "textbox", "frame": {"path": ["main"]}, "expect_role": "textbox",
              "strategies": [{"kind": "field_name", "tag": "input", "name": "q1", "score": 0.9}]}
    cell = {"description": "cell", "frame": {"path": ["main"]}, "expect_role": "cell",
            "strategies": [{"kind": "css", "selector": "td", "score": 0.3}]}
    d: dict[str, Any] = {
        "id": "coreone.member.test", "version": "1.0.0", "title": "t", "description": "d",
        "app": {"product": "p", "profile": "coreone-teller@1"}, "surface": {"entry_url": "{base_url}/"},
        "inputs": [{"name": "member_number", "type": "string", "description": "m", "sensitivity": "pii"}],
        "outputs": [{"name": "bal", "type": "decimal", "description": "b"}],
        "steps": [
            {"id": "s01", "action": "fill", "intent": "i", "effect": "input", "target": target, "value": {"kind": "param", "name": "member_number"}},
            {"id": "s02", "action": "extract", "intent": "i", "effect": "read", "target": cell, "output": "bal"},
        ],
        "success": {"all": [{"kind": "text", "text": "Member Detail"}]},
        "risk": {"max_effect": "input"},
        "provenance": {"recorded_at": datetime.now(UTC).isoformat(), "discovery_run_id": "r", "planner": "scripted",
                       "goal": "g", "recorded_against": "t", "recorder_version": "0"},
    }
    d.update(over)
    return d


class TestSchema:
    def test_valid_capability_and_hash_ignores_review_state(self) -> None:
        cap = Capability.model_validate(cap_dict())
        h = cap.compute_hash()
        cap.status = Status.DEPRECATED
        assert cap.compute_hash() == h

    def test_undeclared_param_rejected(self) -> None:
        d = cap_dict()
        d["steps"][0]["value"] = {"kind": "param", "name": "nope"}
        with pytest.raises(ValidationError, match="undeclared input"):
            Capability.model_validate(d)

    def test_every_output_must_be_extracted(self) -> None:
        d = cap_dict()
        d["steps"] = d["steps"][:1]
        with pytest.raises(ValidationError, match="extracted exactly once"):
            Capability.model_validate(d)

    def test_approval_flag_only_on_irreversible(self) -> None:
        d = cap_dict()
        d["steps"][0]["requires_approval"] = True
        with pytest.raises(ValidationError, match="irreversible"):
            Capability.model_validate(d)

    def test_risk_summary_cannot_understate(self) -> None:
        d = cap_dict()
        d["steps"][0]["effect"] = "write"
        with pytest.raises(ValidationError, match="understates"):
            Capability.model_validate(d)

    def test_pii_examples_never_persisted(self) -> None:
        with pytest.raises(ValidationError):
            ParamSpec(name="ssn", type=ParamType.STRING, description="x", sensitivity=Sensitivity.PII, example="900-12-3456")

    def test_approved_needs_record(self) -> None:
        with pytest.raises(ValidationError, match="approval record"):
            Capability.model_validate(cap_dict(status="approved"))

    def test_tool_definition(self) -> None:
        tool = Capability.model_validate(cap_dict()).tool_definition()
        assert tool["name"] == "coreone__member__test"
        assert tool["input_schema"]["required"] == ["member_number"]
        assert tool["input_schema"]["additionalProperties"] is False


class TestRedaction:
    def test_patterns_and_registered_values(self) -> None:
        r = Redactor()
        r.register("100234", Sensitivity.PII, "member_number")
        r.register("hunter2!", Sensitivity.SECRET, "pw")
        r.register("x", Sensitivity.SECRET, "short")
        out = r.text("member 100234 ssn 900-12-3456 bal $1,234.56 pw hunter2! mail a@b.co x")
        assert "100234" not in out and "900-12-3456" not in out and "1,234.56" not in out and "hunter2" not in out
        assert "[PII:member_number]" in out and "[SECRET:pw]" in out and "[SSN]" in out and "[AMOUNT]" in out
        assert out.endswith(" x")

    def test_recursive(self) -> None:
        r = Redactor()
        assert r.obj({"a": ["(555) 010-2233", {"b": "ok"}]}) == {"a": ["[PHONE]", {"b": "ok"}]}


class TestPolicy:
    def policy(self) -> Policy:
        return Policy(allowed_origins=["http://x"], allowed_paths=["^/app/"], allowed_actions=["click", "fill", "navigate"],
                      risk_rules=[{"effect": "irreversible", "reason": "posts", "name_regex": "(?i)^submit"}])

    def test_allowlist(self) -> None:
        p = self.policy()
        assert p.url_allowed("http://x/app/a")[0]
        assert not p.url_allowed("http://evil/app/a")[0]
        assert not p.url_allowed("http://x/__faults")[0]

    def test_decisions(self) -> None:
        p = self.policy()
        assert p.decide("click", {"name": "Submit"}, "http://x/app/r", mode="replay").verdict is Verdict.REQUIRE_APPROVAL
        assert p.decide("extract", {}, "http://x/app/r", mode="replay").verdict is Verdict.DENY
        post = p.decide("click", {"name": "Continue", "submits": True, "form": "post"}, "http://x/app/r", mode="replay")
        assert post.verdict is Verdict.ALLOW and post.effect is Effect.WRITE
        assert p.decide("click", {"name": "Search", "submits": True, "form": "get"}, "http://x/app/r", mode="replay").effect is Effect.NAVIGATE


class TestInputs:
    specs = [
        ParamSpec(name="n", type=ParamType.STRING, description="", pattern=r"^\d{6}$"),
        ParamSpec(name="amt", type=ParamType.DECIMAL, description=""),
        ParamSpec(name="kind", type=ParamType.ENUM, description="", enum=["A", "B"], required=False),
    ]

    def test_valid_and_normalised(self) -> None:
        assert validate_inputs(self.specs, {"n": "100234", "amt": "1,000.50"}) == {"n": "100234", "amt": "1000.50"}

    def test_problems_collected(self) -> None:
        with pytest.raises(InputError) as e:
            validate_inputs(self.specs, {"n": "12", "amt": "x", "kind": "C", "extra": "1"})
        assert len(e.value.problems) == 4


def test_parse_output() -> None:
    assert parse_output("$1,234.56", ParseRule.CURRENCY, OutputType.DECIMAL) == "1234.56"
    assert parse_output("($12,650.40)", ParseRule.CURRENCY, OutputType.DECIMAL) == "-12650.40"
    assert parse_output(" 42 ", ParseRule.INTEGER, OutputType.INTEGER) == 42


def test_parse_value_placeholders() -> None:
    kw: dict[str, Any] = {"inputs": {"member_number"}, "secrets": {"pw"}, "raw_params": {"member_number": "100234"}}
    assert parse_value("{{member_number}}", **kw) == ParamValue(name="member_number")
    assert parse_value("{{secret:pw}}", **kw) == SecretValue(name="pw")
    assert parse_value("100234", **kw) == ParamValue(name="member_number")
    assert parse_value("hello", **kw) == LiteralValue(value="hello")
    with pytest.raises(RecordingError):
        parse_value("ID-{{member_number}}", **kw)
    with pytest.raises(RecordingError):
        parse_value("{{secret:other}}", **kw)


def test_canonical_path_regex() -> None:
    assert canonical_path_regex("http://h/app/member.do?mid=48213") == r"^/app/member\.do(\?.*)?$"
    rx = canonical_path_regex("http://h/item/12345/view")
    assert re.search(rx, "/item/999/view") and not re.search(rx, "/item/999/edit")


def descriptor(candidates: list[dict[str, Any]], role: str = "cell") -> ElementDescriptor:
    return ElementDescriptor(frame=FrameRef(path=["main"]), frame_url="http://h/app/x", info={"role": role, "name": "", "label": ""},
                             candidates=[{"strategy": c, "count": 1, "matches": True} for c in candidates])


class TestLocators:
    def test_param_bound_row_key_and_literal_variants_dropped(self) -> None:
        d = descriptor([
            {"kind": "role_name", "role": "link", "name": "Select"},
            {"kind": "table_cell", "column": "", "row_key_column": "Member #", "row_key": "100234", "inner_role": "link"},
            {"kind": "table_cell", "column": "", "row_key_column": "Branch", "row_key": "001 MAIN", "inner_role": "link"},
            {"kind": "table_cell", "column": "", "row_key_column": "Name", "row_key": "SMITH, JANE A", "inner_role": "link"},
            {"kind": "css", "selector": "a"},
        ], role="link")
        t = build_target(d, purpose="act", params={"member_number": "100234"}, redactor=Redactor(), description='link "Select"')
        assert t is not None
        kinds = [s.kind for s in t.strategies]
        assert kinds[0] == "table_cell" and kinds.count("table_cell") == 1
        top = t.strategies[0]
        assert isinstance(top, TableCellStrategy) and top.row_key == ParamValue(name="member_number")
        assert "SMITH" not in t.model_dump_json()

    def test_extract_never_locates_by_value(self) -> None:
        d = descriptor([
            {"kind": "role_name", "role": "cell", "name": "$1,234.56"},
            {"kind": "table_cell", "column": "Current Balance", "row_key_column": "Available", "row_key": "$1,209.56", "inner_role": None},
            {"kind": "table_cell", "column": "Current Balance", "row_key_column": "Description", "row_key": "Primary Share", "inner_role": None},
            {"kind": "css", "selector": "td"},
        ])
        t = build_target(d, purpose="extract", params={}, redactor=Redactor(), description='cell "$1,234.56"')
        assert t is not None
        assert [s.kind for s in t.strategies] == ["table_cell", "css"]
        assert "1,234" not in t.model_dump_json() and "1,209" not in t.model_dump_json()
        assert t.description == 'cell in column "Current Balance" where Description = "Primary Share"'


def test_overlay_prepends_and_pins_major() -> None:
    cap = Capability.model_validate(cap_dict())
    cap.content_hash = cap.compute_hash()
    ov = Overlay(tenant="t", capability=cap.id, base_major=1, steps=[{"step": "s01", "prepend_strategies": [
        {"kind": "label", "role": "textbox", "label": "Account Number:", "score": 0.95}]}])
    eff = apply_overlay(cap, ov)
    assert eff.steps[0].target.strategies[0].kind == "label"  # type: ignore[union-attr]
    assert eff.content_hash != cap.content_hash
    with pytest.raises(OverlayError):
        apply_overlay(cap, ov.model_copy(update={"base_major": 2}))


def test_checkpoint_text_condition_requires_one_matcher() -> None:
    with pytest.raises(ValidationError):
        TextCondition(text="a", regex="b")
    Checkpoint(all=[TextCondition(regex="ERR-\\d+")])
