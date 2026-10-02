"""File-backed artifact registry: artifacts/<capability_id>/<semver>.json.

Artifacts are immutable per version. Re-recording with an identical behavioural contract
(content_hash) reuses the version; any contract change bumps the minor version and resets
status to draft, so approval always applies to exactly the reviewed contract.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from cua.artifact.schema import Approval, Capability, Status


class ArtifactError(Exception):
    pass


def _semver_key(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, cap_id: str, version: str) -> Path:
        return self.root / cap_id / f"{version}.json"

    def versions(self, cap_id: str) -> list[str]:
        d = self.root / cap_id
        if not d.is_dir():
            return []
        return sorted((p.stem for p in d.glob("*.json") if p.stem.count(".") == 2), key=_semver_key)

    def next_version(self, cap_id: str, content_hash: str) -> tuple[str, bool]:
        versions = self.versions(cap_id)
        if not versions:
            return "1.0.0", True
        latest = self.load(f"{cap_id}@{versions[-1]}")
        if latest.content_hash == content_hash:
            return latest.version, False
        major, minor, _ = _semver_key(latest.version)
        return f"{major}.{minor + 1}.0", True

    def save(self, cap: Capability) -> Path:
        cap.content_hash = cap.compute_hash()
        path = self._path(cap.id, cap.version)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cap.model_dump(mode="json", exclude_none=True), indent=2) + "\n")
        return path

    def load(self, ref: str) -> Capability:
        cap_id, _, version = ref.partition("@")
        if not version:
            versions = self.versions(cap_id)
            if not versions:
                raise ArtifactError(f"no artifact {cap_id!r} in {self.root}")
            approved = [v for v in versions if json.loads(self._path(cap_id, v).read_text()).get("status") == "approved"]
            version = (approved or versions)[-1]
        path = self._path(cap_id, version)
        if not path.exists():
            raise ArtifactError(f"no artifact {cap_id}@{version}")
        cap = Capability.model_validate_json(path.read_text())
        if cap.content_hash != cap.compute_hash():
            raise ArtifactError(f"{cap.ref}: content_hash mismatch; artifact modified after recording/approval")
        return cap

    def load_file(self, path: Path) -> Capability:
        cap = Capability.model_validate_json(path.read_text())
        if cap.content_hash and cap.content_hash != cap.compute_hash():
            raise ArtifactError(f"{path}: content_hash mismatch")
        return cap

    def approve(self, ref: str, approver: str, note: str = "") -> Capability:
        cap = self.load(ref)
        cap.status = Status.APPROVED
        cap.approval = Approval(approved_by=approver, approved_at=datetime.now(UTC), note=note)
        self.save(cap)
        return cap

    def all(self) -> list[Capability]:
        if not self.root.is_dir():
            return []
        out = []
        for d in sorted(p for p in self.root.iterdir() if p.is_dir()):
            versions = self.versions(d.name)
            if versions:
                out.append(self.load(d.name))
        return out
