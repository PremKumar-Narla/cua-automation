"""
WebDriver — Playwright implementation of SurfaceDriver.

Resolution priority for every SemanticLocator, enforced in `resolve()`:
    role + name  ->  role + name anchored inside a named container  ->  fallbacks
A locator that resolves to 0 or >1 elements is a DETECTABLE hard failure — never a
guess (brief §3.7 / design decision: "exactly one match", never "first match").

Observation is text, not pixels: `Locator.aria_snapshot()` (default mode) renders
"- role \"name\"" lines that map 1:1 onto SemanticLocator(role, name), so the model
reads the same vocabulary it acts in. Screenshots are captured alongside purely as
evidence (brief §3.7), never as the targeting signal.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from playwright.sync_api import Locator, Page, sync_playwright

from cua.artifact.schema import SemanticLocator
from cua.surface.base import Observation, ResolveResult, SurfaceDriver

# Matches an aria_snapshot line like:  - textbox "Member ID" [level=2]
_SNAPSHOT_LINE = re.compile(r'-\s*([a-zA-Z][\w-]*)\s+"([^"]*)"')


def _parse_interactive(snapshot_text: str) -> list[dict]:
    """Flatten an aria_snapshot text dump into [{role, name}, ...]."""
    return [
        {"role": m.group(1), "name": m.group(2)}
        for m in _SNAPSHOT_LINE.finditer(snapshot_text)
    ]


class WebDriver(SurfaceDriver):
    def __init__(self, headed: bool = True, evidence_dir: Optional[str | Path] = None):
        self.headed = headed
        self.evidence_dir = Path(evidence_dir) if evidence_dir else None
        if self.evidence_dir:
            self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self._shot_count = 0

        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(headless=not headed)
        self.context = self.browser.new_context()
        self.page: Page = self.context.new_page()

    def set_evidence_dir(self, path: str | Path) -> None:
        self.evidence_dir = Path(path)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self._shot_count = 0

    # -- observation ----------------------------------------------------

    def observe(self) -> Observation:
        snapshot = self.page.locator("body").aria_snapshot()
        screenshot_ref = None
        if self.evidence_dir:
            self._shot_count += 1
            path = self.evidence_dir / f"step-{self._shot_count:02d}.png"
            self.page.screenshot(path=str(path))
            screenshot_ref = str(path)
        return Observation(
            url=self.page.url,
            a11y_text=snapshot,
            interactive=_parse_interactive(snapshot),
            screenshot_ref=screenshot_ref,
        )

    # -- resolution -------------------------------------------------------

    def resolve(self, locator: SemanticLocator) -> ResolveResult:
        if locator.role and locator.name:
            candidate = self.page.get_by_role(locator.role, name=locator.name)
            count = candidate.count()
            if count == 1:
                return ResolveResult(ok=True, handle=candidate, count=1, strategy_used="role+name")

            for anchor in locator.anchors:
                anchored = self._resolve_anchored(locator, anchor)
                if anchored is not None:
                    return ResolveResult(ok=True, handle=anchored, count=1, strategy_used="anchored")

        else:
            count = 0

        for fb in locator.fallbacks:
            resolved = self._resolve_fallback(fb)
            if resolved is not None:
                return resolved

        return ResolveResult(ok=False, count=count, strategy_used="none")

    def _resolve_anchored(self, locator: SemanticLocator, anchor: str) -> Optional[Locator]:
        """Scope role+name search to the nearest container that also contains `anchor` text."""
        container = self.page.locator(
            "xpath=//*[self::form or self::section or self::fieldset or self::div]"
            f"[.//*[contains(normalize-space(text()), {self._xpath_literal(anchor)})]]"
        ).last
        if container.count() == 0:
            return None
        candidate = container.get_by_role(locator.role, name=locator.name)
        return candidate if candidate.count() == 1 else None

    @staticmethod
    def _xpath_literal(text: str) -> str:
        if '"' not in text:
            return f'"{text}"'
        return "concat(" + ", '\"', ".join(f'"{part}"' for part in text.split('"')) + ")"

    def _resolve_fallback(self, fb) -> Optional[ResolveResult]:
        if fb.strategy == "css":
            candidate = self.page.locator(fb.value)
        elif fb.strategy == "xpath":
            candidate = self.page.locator(f"xpath={fb.value}")
        else:  # coordinates — last resort, cannot verify uniqueness
            x_str, y_str = fb.value.split(",")
            return ResolveResult(
                ok=True, handle=(float(x_str), float(y_str)), count=1,
                strategy_used="fallback:coordinates",
            )
        count = candidate.count()
        if count == 1:
            return ResolveResult(ok=True, handle=candidate, count=1, strategy_used=f"fallback:{fb.strategy}")
        return None

    # -- actions ------------------------------------------------------------

    def _require(self, locator: SemanticLocator) -> Locator | tuple:
        res = self.resolve(locator)
        if not res.ok:
            raise LookupError(
                f"locator role={locator.role!r} name={locator.name!r} "
                f"resolved to {res.count} elements (need exactly 1)"
            )
        return res.handle

    def click(self, locator: SemanticLocator) -> None:
        handle = self._require(locator)
        if isinstance(handle, tuple):
            self.page.mouse.click(*handle)
        else:
            handle.click()

    def type(self, locator: SemanticLocator, value: str) -> None:
        handle = self._require(locator)
        if isinstance(handle, tuple):
            self.page.mouse.click(*handle)
            self.page.keyboard.type(value)
        else:
            handle.fill(value)

    def navigate(self, url: str) -> None:
        self.page.goto(url)

    def read(self, locator: SemanticLocator) -> str:
        handle = self._require(locator)
        if isinstance(handle, tuple):
            raise LookupError("cannot read from a coordinates-only fallback locator")
        if locator.role in ("textbox", "combobox", "searchbox"):
            return handle.input_value()
        return (handle.text_content() or "").strip()

    def close(self) -> None:
        self.context.close()
        self.browser.close()
        self._pw.stop()
