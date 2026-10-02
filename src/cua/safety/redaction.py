"""Redaction for everything that leaves process memory: logs, artifacts, evidence, and model input.

Three layers, applied in order:
1. Registered values: exact runtime values of secrets / PII / confidential params and outputs.
2. Patterns: format-detectable identifiers (SSN, card numbers, email, phone, dates).
3. Label-aware masking in the page probe (cells next to "SSN:", "Name" columns, ...), handled in probe.js
   via `sensitive_labels`, because free-text PII such as names cannot be detected by format.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from cua.artifact.schema import Sensitivity

DEFAULT_PATTERNS: dict[str, str] = {
    "ssn": r"\b\d{3}-\d{2}-\d{4}\b",
    "card": r"\b(?:\d{4}[ -]){3}\d{4}\b",
    "email": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
    "phone": r"\(\d{3}\)\s?\d{3}-\d{4}",
    "date": r"\b\d{2}/\d{2}/\d{4}\b",
    "amount": r"\(?-?\$\d[\d,]*\.\d{2}\)?",
}
DEFAULT_SENSITIVE_LABELS = r"(?i)^(name|member|ssn|social security|tax id|date of birth|dob|phone|email|address)\b"


def mask_value(value: str, sensitivity: Sensitivity, label: str) -> str:
    if sensitivity is Sensitivity.SECRET:
        return f"[SECRET:{label}]"
    if sensitivity is Sensitivity.PII:
        return f"[PII:{label}]"
    return f"[{sensitivity.value.upper()}:{label}]"


@dataclass
class Redactor:
    patterns: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_PATTERNS))
    sensitive_labels: str = DEFAULT_SENSITIVE_LABELS
    _values: dict[str, str] = field(default_factory=dict)
    _compiled: list[tuple[str, re.Pattern[str]]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._compiled = [(name, re.compile(rx)) for name, rx in self.patterns.items()]

    def register(self, value: object, sensitivity: Sensitivity, label: str) -> None:
        text = str(value) if value is not None else ""
        if sensitivity is Sensitivity.PUBLIC or len(text) < 3:
            return
        self._values[text] = mask_value(text, sensitivity, label)

    @property
    def sensitive_values(self) -> list[str]:
        return list(self._values)

    def text(self, s: str) -> str:
        for raw in sorted(self._values, key=len, reverse=True):
            s = s.replace(raw, self._values[raw])
        for name, rx in self._compiled:
            s = rx.sub(f"[{name.upper()}]", s)
        return s

    def obj(self, o: Any) -> Any:
        if isinstance(o, str):
            return self.text(o)
        if isinstance(o, dict):
            return {k: self.obj(v) for k, v in o.items()}
        if isinstance(o, list | tuple):
            return [self.obj(v) for v in o]
        return o

    def js_patterns(self) -> list[str]:
        return list(self.patterns.values())

    def js_label_regex(self) -> str:
        return self.sensitive_labels.removeprefix("(?i)")

    def contains_sensitive(self, s: str, extra: Iterable[str] = ()) -> bool:
        return self.text(s) != s or any(v and v in s for v in extra)
