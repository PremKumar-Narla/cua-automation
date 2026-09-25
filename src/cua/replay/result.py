"""
Replay result contract — the second focal point (brief §3.3).

The single most-flagged design mistake in this brief is conflating a legitimate
business outcome ("no such member") with a crash. This contract makes the three
classes structurally distinct so a caller can always tell them apart.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class ReplayStatus(str, Enum):
    SUCCESS = "success"                   # goal reached; typed outputs present
    BUSINESS_OUTCOME = "business_outcome" # expected, caller-relevant (NOT a failure)
    FAILURE = "failure"                   # hard failure; stop and surface debug detail


class RecoveredEvent(BaseModel):
    """A recoverable condition that was auto-handled so the run could continue."""
    step_id: str
    condition: str                        # e.g. "session_expired"
    recovery: str                         # e.g. "reauth_then_retry"


class ReplayResult(BaseModel):
    status: ReplayStatus
    outcome_code: Optional[str] = None    # populated for BUSINESS_OUTCOME
    outputs: Optional[dict] = None        # populated for SUCCESS

    # debug detail — populated for FAILURE
    failed_step: Optional[str] = None
    expected: Optional[str] = None
    observed: Optional[str] = None

    recovered_events: list[RecoveredEvent] = Field(default_factory=list)
    controller_events: list[dict] = Field(default_factory=list)  # any human handoff
    evidence_ref: Optional[str] = None    # path to structured log + screenshot/trace

    @classmethod
    def success(cls, outputs: dict, evidence_ref: str | None = None) -> "ReplayResult":
        return cls(status=ReplayStatus.SUCCESS, outputs=outputs, evidence_ref=evidence_ref)

    @classmethod
    def business(cls, code: str, evidence_ref: str | None = None) -> "ReplayResult":
        return cls(status=ReplayStatus.BUSINESS_OUTCOME, outcome_code=code,
                   evidence_ref=evidence_ref)

    @classmethod
    def failure(cls, step: str, expected: str, observed: str,
                evidence_ref: str | None = None) -> "ReplayResult":
        return cls(status=ReplayStatus.FAILURE, failed_step=step,
                   expected=expected, observed=observed, evidence_ref=evidence_ref)
