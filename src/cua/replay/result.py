"""Replay result contract returned to the calling agent.

Exactly one of: outputs (succeeded), outcome (business_outcome: a legitimate answer such as
"no such member"), approval (needs_approval: stopped before an irreversible step), or error
(failed: with step, expected vs observed, and evidence paths).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class RunStatus(StrEnum):
    SUCCEEDED = "succeeded"
    BUSINESS_OUTCOME = "business_outcome"
    NEEDS_APPROVAL = "needs_approval"
    FAILED = "failed"


class ErrorCode(StrEnum):
    INPUT_INVALID = "INPUT_INVALID"
    SECRET_UNAVAILABLE = "SECRET_UNAVAILABLE"
    ARTIFACT_NOT_APPROVED = "ARTIFACT_NOT_APPROVED"
    POLICY_VIOLATION = "POLICY_VIOLATION"
    ARTIFACT_POLICY_MISMATCH = "ARTIFACT_POLICY_MISMATCH"
    NAVIGATION_FAILED = "NAVIGATION_FAILED"
    LOCATOR_NOT_FOUND = "LOCATOR_NOT_FOUND"
    LOCATOR_CONFLICT = "LOCATOR_CONFLICT"
    ACTION_FAILED = "ACTION_FAILED"
    CHECKPOINT_TIMEOUT = "CHECKPOINT_TIMEOUT"
    SUCCESS_CHECK_FAILED = "SUCCESS_CHECK_FAILED"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"
    UNEXPECTED_DIALOG = "UNEXPECTED_DIALOG"
    APP_ERROR = "APP_ERROR"
    RECOVERY_EXHAUSTED = "RECOVERY_EXHAUSTED"
    HUMAN_ABORTED = "HUMAN_ABORTED"
    HUMAN_TIMEOUT = "HUMAN_TIMEOUT"
    HANDBACK_UNRESOLVED = "HANDBACK_UNRESOLVED"
    APPROVAL_DENIED = "APPROVAL_DENIED"


RETRYABLE = {ErrorCode.NAVIGATION_FAILED, ErrorCode.CHECKPOINT_TIMEOUT, ErrorCode.APP_ERROR, ErrorCode.HUMAN_TIMEOUT}


class Failure(BaseModel):
    code: ErrorCode
    message: str
    step_id: str | None = None
    step_intent: str | None = None
    expected: str | None = None
    observed: str | None = None
    retryable: bool = False
    evidence: dict[str, str] = Field(default_factory=dict)


class Outcome(BaseModel):
    code: str
    message: str
    step_id: str | None = None


class ApprovalRequest(BaseModel):
    step_id: str
    step_intent: str
    reason: str


class StepReport(BaseModel):
    step_id: str
    action: str
    status: Literal["ok", "failed", "skipped_by_handback"]
    resolved_by: str | None = None
    degraded: bool = False
    attempts: int = 1
    duration_ms: int = 0


class RecoveryReport(BaseModel):
    state_id: str
    step_id: str
    action: str


class ReplayResult(BaseModel):
    run_id: str
    capability: str
    tenant: str | None = None
    status: RunStatus
    outputs: dict[str, Any] = Field(default_factory=dict)
    outcome: Outcome | None = None
    approval: ApprovalRequest | None = None
    error: Failure | None = None
    steps: list[StepReport] = Field(default_factory=list)
    recoveries: list[RecoveryReport] = Field(default_factory=list)
    interventions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    duration_ms: int = 0
    evidence_dir: str | None = None
