"""Turn probe candidates into a persisted, ranked Target.

Ranking reflects what survives in back-office apps: the form field name is the vendor's server
contract (stable across tenants and branding); accessible names and visual labels are what an
operator reads (stable across releases, may vary per tenant branding); table structure beats
position; structural CSS paths are last-resort. Every persisted strategy was verified at record
time to resolve to exactly the acted-on element. Strategies that embed volatile or sensitive data
(balances, names, record values) are dropped or re-expressed through input parameters.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from cua.artifact.schema import (
    CssStrategy,
    FieldNameStrategy,
    LabelStrategy,
    LiteralValue,
    ParamValue,
    RoleNameStrategy,
    TableCellStrategy,
    Target,
)
from cua.safety.redaction import Redactor
from cua.surface.base import ElementDescriptor

VOLATILE = re.compile(r"^[\s$()\-\d.,%/:+]+$")
MAX_STRATEGIES = 5

BASE = {
    "field_name": (0.92, "form field name: server-side contract of the vendor product; survives branding and layout changes"),
    "role_name": (0.86, "role + accessible name: what an operator reads on the control; survives layout changes"),
    "table_cell": (0.8, "table column header + row key: survives row reordering and column insertion"),
    "label": (0.76, "visual label proximity (adjacent cell/text): works on legacy markup with no <label>/ARIA"),
    "css": (0.3, "structural path: last resort, breaks on any layout change; never trusted alone for conflicts"),
}


def _param_for(value: str, params: dict[str, str]) -> str | None:
    for name, raw in params.items():
        if raw and value.strip() == raw.strip():
            return name
    return None


def _embeds(value: str, params: dict[str, str]) -> bool:
    return any(raw and len(raw) >= 3 and raw in value for raw in params.values())


def build_target(
    desc: ElementDescriptor,
    *,
    purpose: Literal["act", "extract"],
    params: dict[str, str],
    redactor: Redactor,
    description: str,
) -> Target | None:
    label_rx = re.compile(redactor.sensitive_labels)
    strategies: list[Any] = []
    param_bound_cell = False
    for cand in desc.candidates:
        if cand.get("count") != 1 or not cand.get("matches"):
            continue
        s = cand["strategy"]
        kind = s["kind"]
        score, why = BASE[kind]
        if kind == "field_name":
            strategies.append(FieldNameStrategy(tag=s["tag"], name=s["name"], score=score, rationale=why))
        elif kind in ("role_name", "label"):
            text = s["name"] if kind == "role_name" else s["label"]
            if purpose == "extract" and kind == "role_name":
                continue
            if VOLATILE.match(text) or _embeds(text, params) or redactor.contains_sensitive(text):
                continue
            if kind == "role_name":
                strategies.append(RoleNameStrategy(role=s["role"], name=text, score=score, rationale=why))
            else:
                strategies.append(LabelStrategy(role=s["role"], label=text, score=score, rationale=why))
        elif kind == "table_cell":
            rk = s["row_key"]
            param = _param_for(rk, params)
            if param is not None:
                row_key: Any = ParamValue(name=param)
                score += 0.03
                why += "; row key bound to input parameter"
                param_bound_cell = True
            elif VOLATILE.match(rk) or redactor.contains_sensitive(rk) or label_rx.search(s["row_key_column"]):
                continue
            else:
                row_key = LiteralValue(value=rk)
            if not s["column"]:
                score -= 0.08
            strategies.append(TableCellStrategy(
                column=s["column"], row_key_column=s["row_key_column"], row_key=row_key,
                inner_role=s.get("inner_role"), score=round(score, 2), rationale=why,
            ))
        elif kind == "css":
            strategies.append(CssStrategy(selector=s["selector"], score=score, rationale=why))
    if param_bound_cell:
        strategies = [s for s in strategies if not (isinstance(s, TableCellStrategy) and isinstance(s.row_key, LiteralValue))]
    if purpose == "act" and param_bound_cell:
        strategies = [s for s in strategies if not isinstance(s, RoleNameStrategy | LabelStrategy)] + [
            s for s in strategies if isinstance(s, RoleNameStrategy | LabelStrategy)
        ]
        for s in strategies:
            if isinstance(s, RoleNameStrategy | LabelStrategy):
                s.score = min(s.score, 0.7)
                s.rationale += "; demoted: not scoped to the parameterised row"
    strategies.sort(key=lambda s: s.score, reverse=True)
    if not strategies:
        return None
    cell = next((s for s in strategies if isinstance(s, TableCellStrategy)), None)
    if purpose == "extract" or VOLATILE.match(description.split('"')[1] if '"' in description else ""):
        if cell is not None:
            key = cell.row_key.value if isinstance(cell.row_key, LiteralValue) else "{" + cell.row_key.name + "}"
            description = f'{desc.info["role"]} in column "{cell.column}" where {cell.row_key_column} = "{key}"'
        elif desc.info.get("label"):
            description = f'{desc.info["role"]} labelled "{desc.info["label"]}"'
        else:
            description = f'{desc.info["role"]} at {desc.frame.label()}'
    description = redactor.text(description)
    return Target(description=description, frame=desc.frame, expect_role=desc.info["role"], strategies=strategies[:MAX_STRATEGIES])
