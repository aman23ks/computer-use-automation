from __future__ import annotations

import os
import re
from decimal import Decimal, InvalidOperation

from cua.artifact.schema import ParamSpec, ParamType, SecretRef


class InputError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def validate_inputs(specs: list[ParamSpec], raw: dict[str, str]) -> dict[str, str]:
    """Validate and normalise caller inputs before touching the UI; returns canonical string forms."""
    problems: list[str] = []
    known = {s.name for s in specs}
    problems.extend(f"unknown input {k!r}" for k in raw if k not in known)
    out: dict[str, str] = {}
    for spec in specs:
        value = raw.get(spec.name)
        if value is None or value == "":
            if spec.required:
                problems.append(f"{spec.name}: required")
            continue
        value = str(value).strip()
        if spec.type is ParamType.INTEGER and not re.fullmatch(r"-?\d+", value):
            problems.append(f"{spec.name}: expected integer")
            continue
        if spec.type is ParamType.DECIMAL:
            try:
                value = str(Decimal(value.replace(",", "")))
            except InvalidOperation:
                problems.append(f"{spec.name}: expected decimal")
                continue
        if spec.type is ParamType.BOOLEAN:
            if value.lower() not in ("true", "false"):
                problems.append(f"{spec.name}: expected true/false")
                continue
            value = value.lower()
        if spec.type is ParamType.ENUM and value not in (spec.enum or []):
            problems.append(f"{spec.name}: expected one of {spec.enum}")
            continue
        if spec.pattern and not re.fullmatch(spec.pattern, value):
            problems.append(f"{spec.name}: does not match {spec.pattern}")
            continue
        out[spec.name] = value
    if problems:
        raise InputError(problems)
    return out


class SecretUnavailable(KeyError):
    pass


class EnvSecretProvider:
    """Resolves secret refs from CUA_SECRET_<NAME>. Production: a vault client behind the same interface."""

    prefix = "CUA_SECRET_"

    def get(self, ref: SecretRef) -> str:
        value = os.environ.get(self.prefix + ref.name.upper())
        if not value:
            raise SecretUnavailable(ref.name)
        return value
