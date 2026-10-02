"""Per-run evidence: redacted structured event log, masked screenshots, redacted DOM text snapshots."""

from __future__ import annotations

import json
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cua.safety.redaction import Redactor


def new_run_id(kind: str) -> str:
    return f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{kind}-{secrets.token_hex(3)}"


class RunRecorder:
    def __init__(self, root: Path, kind: str, redactor: Redactor, run_id: str | None = None) -> None:
        self.run_id = run_id or new_run_id(kind)
        self.kind = kind
        self.dir = root / self.run_id
        self.redactor = redactor
        self._seq = 0
        self._t0 = time.monotonic()
        (self.dir / "screenshots").mkdir(parents=True, exist_ok=True)
        (self.dir / "dom").mkdir(exist_ok=True)
        self._log = (self.dir / "events.jsonl").open("a", encoding="utf-8")

    def event(self, type_: str, **fields: Any) -> dict[str, Any]:
        self._seq += 1
        record = {
            "seq": self._seq,
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "t_ms": int((time.monotonic() - self._t0) * 1000),
            "run_id": self.run_id,
            "type": type_,
            **self.redactor.obj(fields),
        }
        self._log.write(json.dumps(record, default=str) + "\n")
        self._log.flush()
        return record

    def path(self, *parts: str) -> Path:
        return self.dir.joinpath(*parts)

    def rel(self, p: Path) -> str:
        return str(p.relative_to(self.dir))

    def write_json(self, name: str, obj: Any, *, redact: bool = True) -> Path:
        p = self.dir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.redactor.obj(obj) if redact else obj, indent=2, default=str) + "\n")
        return p

    def write_text(self, name: str, text: str) -> Path:
        p = self.dir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.redactor.text(text))
        return p

    def write_bytes(self, name: str, data: bytes) -> Path:
        p = self.dir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def close(self) -> None:
        self._log.close()
