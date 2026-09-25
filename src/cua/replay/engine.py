"""
Deterministic replay engine (brief §3.3) — NO LLM in the decision loop.
This package must never import cua.agent (which pulls in the LLM client) —
that boundary is what lets a test assert zero model calls during replay.

Contract:
  replay(capability, inputs, driver, base_url) -> ReplayResult

Determinism strategy:
  * semantic locator resolution in priority order + uniqueness check (brief §3.7):
    a locator resolving to 0 or >1 elements is a detectable hard failure, never a guess
  * recorded wait_for post-conditions, polled with a timeout, instead of fixed sleeps
  * on_conditions checked right after acting and again if wait_for times out, so a
    legitimate business outcome (e.g. "no such member") is never conflated with a crash
  * checkpoint asserted before declaring success; evidence (screenshot + a11y snapshot)
    captured on every step, same as discovery
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.parse import urljoin

from cua.artifact.schema import ActionType, Capability, OutcomeClass, RiskClass, SemanticLocator, Step
from cua.escalation.controller import SessionControl
from cua.replay.result import ReplayResult
from cua.safety import policy
from cua.surface.base import SurfaceDriver


class _SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def _validate_inputs(capability: Capability, inputs: dict[str, str]) -> None:
    missing = [param.name for param in capability.inputs if param.required and param.name not in inputs]
    if missing:
        raise ValueError(f"missing required input(s): {missing}")


def _check_condition(cond: dict, driver: SurfaceDriver) -> bool:
    """A condition is {"role","name"} | {"text_contains": str} | {"any_of": [cond, ...]}."""
    if "any_of" in cond:
        return any(_check_condition(sub_condition, driver) for sub_condition in cond["any_of"])
    if "text_contains" in cond:
        return cond["text_contains"] in driver.observe().a11y_text
    if "role" in cond and "name" in cond:
        return driver.resolve(SemanticLocator(role=cond["role"], name=cond["name"])).ok
    return False


def _wait_until(cond: dict, driver: SurfaceDriver, timeout: float = 5.0, poll: float = 0.25) -> bool:
    deadline = time.time() + timeout
    while True:
        if _check_condition(cond, driver):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(poll)


def _match_on_condition(step: Step, driver: SurfaceDriver):
    for cond in step.on_conditions:
        if _check_condition(cond.match, driver):
            return cond
    return None


def _act(step: Step, driver: SurfaceDriver, base_url: str, inputs: dict[str, str],
         sensitive_values: list[str]) -> str | None:
    """Perform the step's primary action. Returns an extracted value for READ steps."""
    if step.action == ActionType.NAVIGATE:
        path = (step.url_pattern or "/").format_map(_SafeDict(inputs))
        driver.navigate(urljoin(base_url + "/", path.lstrip("/")))
        return None
    if step.action == ActionType.CLICK:
        driver.click(step.target)
        return None
    if step.action == ActionType.TYPE:
        input_name = (step.value_ref or "").removeprefix("input.")
        if input_name not in inputs:
            raise KeyError(input_name)
        value = inputs[input_name]
        driver.type(step.target, value)
        sensitive_values.append(value)
        return None
    if step.action == ActionType.READ:
        value = driver.read(step.target)
        sensitive_values.append(value)
        return value
    if step.action == ActionType.ASSERT:
        resolved = driver.resolve(step.target)
        if not resolved.ok:
            raise LookupError(f"assert failed: resolved to {resolved.count} elements")
        return None
    if step.action == ActionType.WAIT:
        return None
    raise ValueError(f"unknown action {step.action}")


def replay(
    capability: Capability,
    inputs: dict[str, str],
    driver: SurfaceDriver,
    base_url: str,
    control: SessionControl | None = None,
    step_timeout: float = 5.0,
    evidence_root: Path = Path("evidence"),
    approved: bool = False,
) -> ReplayResult:
    _validate_inputs(capability, inputs)

    run_dir = evidence_root / f"replay-{time.time_ns()}"
    run_dir.mkdir(parents=True, exist_ok=True)
    driver.set_evidence_dir(run_dir)
    transcript_path = run_dir / "transcript.jsonl"
    sensitive_values: list[str] = []

    def log(entry: dict) -> None:
        # Redact at capture time: sensitive_values grows as steps run, so earlier
        # entries are re-checked too — nothing typed or read ever lands raw on disk.
        redacted = json.loads(policy.redact(json.dumps(entry), sensitive_values))
        with transcript_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(redacted) + "\n")

    log({"event": "start", "capability_id": capability.capability_id, "version": capability.version})

    needs_approval = capability.policy.requires_approval or capability.policy.risk_class == RiskClass.IRREVERSIBLE
    if needs_approval and not approved:
        reason = (f"risk_class={capability.policy.risk_class.value}, "
                  f"requires_approval={capability.policy.requires_approval}")
        log({"event": "blocked", "reason": reason})
        return ReplayResult.failure(
            step="policy", expected="approved=True for an irreversible/gated capability",
            observed=reason, evidence_ref=str(run_dir),
        )

    outputs: dict[str, str] = {}
    for step in capability.steps:
        if control is not None:
            control.guard()

        try:
            value = _act(step, driver, base_url, inputs, sensitive_values)
        except (LookupError, KeyError) as error:
            matched = _match_on_condition(step, driver)
            if matched and matched.classify == OutcomeClass.BUSINESS_OUTCOME:
                log({"event": "business_outcome", "step": step.id, "outcome": matched.outcome})
                return ReplayResult.business(matched.outcome, evidence_ref=str(run_dir))
            log({"event": "failure", "step": step.id, "reason": str(error)})
            return ReplayResult.failure(
                step=step.id, expected=f"{step.action.value} to resolve/act", observed=str(error),
                evidence_ref=str(run_dir),
            )

        if step.action == ActionType.READ and step.extract_to:
            outputs[step.extract_to.removeprefix("output.")] = value

        matched = _match_on_condition(step, driver)
        if matched and matched.classify == OutcomeClass.BUSINESS_OUTCOME:
            log({"event": "business_outcome", "step": step.id, "outcome": matched.outcome})
            return ReplayResult.business(matched.outcome, evidence_ref=str(run_dir))

        if step.wait_for is not None and not _wait_until(step.wait_for, driver, timeout=step_timeout):
            matched = _match_on_condition(step, driver)
            if matched and matched.classify == OutcomeClass.BUSINESS_OUTCOME:
                log({"event": "business_outcome", "step": step.id, "outcome": matched.outcome})
                return ReplayResult.business(matched.outcome, evidence_ref=str(run_dir))
            log({"event": "failure", "step": step.id, "reason": "wait_for timed out"})
            return ReplayResult.failure(
                step=step.id, expected=str(step.wait_for), observed=driver.observe().a11y_text,
                evidence_ref=str(run_dir),
            )

        log({"event": "step_ok", "step": step.id})

    if not _check_condition(capability.checkpoint.assert_, driver):
        log({"event": "failure", "step": "checkpoint", "reason": "checkpoint assertion failed"})
        return ReplayResult.failure(
            step="checkpoint", expected=str(capability.checkpoint.assert_),
            observed=driver.observe().a11y_text, evidence_ref=str(run_dir),
        )
    and_output = capability.checkpoint.and_output_present
    if and_output and and_output not in outputs:
        log({"event": "failure", "step": "checkpoint", "reason": f"output '{and_output}' was never read"})
        return ReplayResult.failure(
            step="checkpoint", expected=f"output '{and_output}' present", observed=str(outputs),
            evidence_ref=str(run_dir),
        )

    log({"event": "success", "outputs": outputs})
    return ReplayResult.success(outputs, evidence_ref=str(run_dir))
