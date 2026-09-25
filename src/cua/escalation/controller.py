"""
Control-transfer model (brief §3.6) — the load-bearing piece of escalation.

There is ONE source of truth for who is in control of a live session: `controller`.
Automation blocks while controller == HUMAN. This is what makes "pause, cede
control, resume on the SAME session" real rather than a TODO.
"""
from __future__ import annotations

import time
from enum import Enum
from typing import Callable, Optional

from pydantic import BaseModel, Field


class Controller(str, Enum):
    AGENT = "agent"
    HUMAN = "human"
    NONE = "none"


class InterventionRequest(BaseModel):
    """Carries enough context for a human to act (brief §3.6)."""
    capability_id: str
    goal: str
    current_step: str
    reason: str                       # WHY it stopped (stuck / disallowed / risky / hard-fail)
    state_snapshot: Optional[str] = None   # text/a11y snapshot of the surface
    screenshot_ref: Optional[str] = None
    raised_at: float = Field(default_factory=time.time)


class SessionControl:
    """
    Owns the controller-of-record for one live session and the pause/resume seam.

    The automation loop calls `guard()` before each action; if a human has taken
    over, `guard()` blocks until control is handed back. A real operator console is
    out of scope (mocked): handback can be signalled by CLI, an endpoint, or a test.
    """

    def __init__(self, on_handoff: Optional[Callable[[InterventionRequest], None]] = None):
        self.controller: Controller = Controller.AGENT
        self.pending: Optional[InterventionRequest] = None
        self.human_actions: list[dict] = []      # what the human did, for the record
        self._on_handoff = on_handoff

    def request_intervention(self, req: InterventionRequest) -> None:
        """Agent is stuck -> pause automation and cede control to a human."""
        self.pending = req
        self.controller = Controller.HUMAN
        if self._on_handoff:
            self._on_handoff(req)                # notify the (mock) operator surface

    def record_human_action(self, action: dict) -> None:
        self.human_actions.append({"ts": time.time(), **action})

    def hand_back(self) -> None:
        """Human signals done -> return control so the run can resume."""
        self.controller = Controller.AGENT
        self.pending = None

    def guard(self, poll_seconds: float = 0.5, timeout: Optional[float] = None) -> None:
        """
        Block while a human holds control. Called by the agent/replay loop before
        each action so control transfer is enforced at a single choke-point.
        """
        waited = 0.0
        while self.controller == Controller.HUMAN:
            time.sleep(poll_seconds)
            waited += poll_seconds
            if timeout is not None and waited >= timeout:
                raise TimeoutError("Timed out waiting for human hand-back")
