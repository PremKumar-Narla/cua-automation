"""
Artifact schema — the reusable, agent-invocable capability.

This is the focal point of the whole project (brief §3.2). It is a *contract*,
not just a step list: typed inputs, typed outputs, per-step semantic locators,
recognized branch outcomes, a checkpoint, and a policy/risk block.

Design rules baked in here (be ready to defend each):
  * Targets are SEMANTIC (role + accessible name + anchors), never raw CSS as the
    primary signal. CSS is an explicit, ranked fallback only. This is what lets the
    same artifact drive a legacy web app or (in design) a desktop a11y tree.
  * The artifact stores value *references* and output *shapes*, never literal PII /
    balances / secrets. Regulated data never lands on disk in an artifact.
  * Versioned + provenance-tagged, but decoupled from the raw model transcript
    (transcript lives in /evidence, not inline).
"""
from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


# --- perception / targeting -------------------------------------------------

class Fallback(BaseModel):
    """Last-resort locator. Used only if semantic signals fail to resolve uniquely."""
    strategy: Literal["css", "xpath", "coordinates"]
    value: str


class SemanticLocator(BaseModel):
    """
    How a control is identified. Resolution order on replay:
        role + name  ->  role + name + anchors  ->  fallbacks
    A locator that resolves to 0 or >1 elements is a DETECTABLE hard failure,
    never a random pick. (See replay/engine.py.)
    """
    role: Optional[str] = None          # button | textbox | link | cell | heading ...
    name: Optional[str] = None          # accessible name / visible label
    anchors: list[str] = Field(default_factory=list)   # e.g. "within form 'Member Search'"
    fallbacks: list[Fallback] = Field(default_factory=list)
    robustness_note: Optional[str] = None   # your reasoning, for the human reviewer


# --- inputs / outputs -------------------------------------------------------

class Sensitivity(str, Enum):
    NONE = "none"
    PII = "pii"
    SECRET = "secret"


class InputParam(BaseModel):
    name: str
    type: str                           # "string" | "money" | "date" | ...
    required: bool = True
    sensitivity: Sensitivity = Sensitivity.NONE
    description: Optional[str] = None


class OutputField(BaseModel):
    name: str
    type: str
    from_step: str                      # step id whose `read` produced this value


# --- steps ------------------------------------------------------------------

class ActionType(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE = "type"
    READ = "read"
    WAIT = "wait"
    ASSERT = "assert"


class OutcomeClass(str, Enum):
    BUSINESS_OUTCOME = "business_outcome"   # expected; caller must know ("no such member")
    RECOVERABLE = "recoverable"             # auto-handle and continue
    # (hard failures are not declared here — they're the default when nothing matches)


class BranchCondition(BaseModel):
    """
    A recognized exceptional state AT THIS STEP. This is where the error taxonomy
    lives *in the contract itself*, so replay classifies deliberately instead of guessing.
    """
    match: dict                         # e.g. {"text_contains": "No such member"}
    classify: OutcomeClass
    outcome: Optional[str] = None       # business outcome code, e.g. "MEMBER_NOT_FOUND"
    recovery: Optional[str] = None      # recovery routine name, e.g. "reauth_then_retry"


class Step(BaseModel):
    id: str
    intent: str                         # human-readable ("enter the member id")
    action: ActionType
    target: Optional[SemanticLocator] = None
    url_pattern: Optional[str] = None   # for NAVIGATE
    value_ref: Optional[str] = None     # references a typed input, e.g. "input.member_id"
    extract_to: Optional[str] = None    # for READ, e.g. "output.savings_balance"
    wait_for: Optional[dict] = None     # post-condition proving the step landed
    on_conditions: list[BranchCondition] = Field(default_factory=list)
    redact: bool = False                # value is sensitive -> never log raw


# --- top-level capability ---------------------------------------------------

class RiskClass(str, Enum):
    READ_ONLY = "read_only"
    REVERSIBLE_WRITE = "reversible_write"
    IRREVERSIBLE = "irreversible"


class Target(BaseModel):
    surface_type: Literal["web", "legacy_web", "desktop"] = "web"
    app_id: str
    vendor_base: Optional[str] = None   # basis for cross-tenant reuse, e.g. "vendor-x@2024"
    entry: dict                         # {"url_pattern": "/members/search"}


class Policy(BaseModel):
    risk_class: RiskClass = RiskClass.READ_ONLY
    requires_approval: bool = False     # gate for unattended replay (stretch: draft->approved)
    allowlist_ref: Optional[str] = None


class Provenance(BaseModel):
    recorded_from_run: Optional[str] = None
    model: Optional[str] = None
    recorded_at: Optional[str] = None
    # NOTE: raw transcript intentionally NOT stored here — see /evidence.


class Checkpoint(BaseModel):
    assert_: dict = Field(alias="assert")   # {"role": "heading", "name": "Member Detail"}
    and_output_present: Optional[str] = None

    model_config = {"populate_by_name": True}


class Capability(BaseModel):
    """A saved, versioned, reviewable capability an AI agent can call by name."""
    schema_version: str = "1.0"
    capability_id: str
    version: int = 1
    name: str
    description: str

    target: Target
    inputs: list[InputParam] = Field(default_factory=list)
    outputs: list[OutputField] = Field(default_factory=list)
    steps: list[Step]
    checkpoint: Checkpoint
    policy: Policy = Field(default_factory=Policy)
    provenance: Provenance = Field(default_factory=Provenance)

    def input_names(self) -> set[str]:
        return {param.name for param in self.inputs}
