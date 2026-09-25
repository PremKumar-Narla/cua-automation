"""
SurfaceDriver — THE seam (brief §3.7).

Separates "how we perceive/act on a surface" from "the recorded flow". The agent
loop and the replay engine only ever talk to this interface. Swap the surface
(web -> legacy web -> desktop) without touching a single artifact.

TODO(build-together): flesh out Observation and ResolveResult as we wire WebDriver.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from cua.artifact.schema import SemanticLocator


@dataclass
class Observation:
    """What the LLM sees each step. Bias toward a11y tree, not raw HTML."""
    url: str
    a11y_text: str                       # accessibility-tree text dump
    interactive: list[dict] = field(default_factory=list)  # [{role,name,...}]
    screenshot_ref: Optional[str] = None


@dataclass
class ResolveResult:
    ok: bool
    handle: object = None                # driver-specific element handle
    count: int = 0                       # 0 or >1 => ambiguous => detectable failure
    strategy_used: str = ""              # "role+name" | "anchored" | "fallback:css" ...


class SurfaceDriver(ABC):
    def set_evidence_dir(self, path) -> None:
        """Optional: where to save per-step screenshots. No-op unless overridden."""

    @abstractmethod
    def observe(self) -> Observation: ...

    @abstractmethod
    def resolve(self, locator: SemanticLocator) -> ResolveResult:
        """Resolve a semantic locator with priority + uniqueness check."""

    @abstractmethod
    def click(self, locator: SemanticLocator) -> None: ...

    @abstractmethod
    def type(self, locator: SemanticLocator, value: str) -> None: ...

    @abstractmethod
    def navigate(self, url: str) -> None: ...

    @abstractmethod
    def read(self, locator: SemanticLocator) -> str: ...

    @abstractmethod
    def close(self) -> None: ...
