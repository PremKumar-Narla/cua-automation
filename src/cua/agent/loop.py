"""
Goal-driven agent loop (brief §3.1): observe -> decide -> act, until the goal is
met (LLM calls `done` and the checkpoint verifies) or a stopping condition fires
(max_steps, timeout, dead-end, or escalate).

Each ACCEPTED step is appended to the artifact using the SEMANTIC target the LLM
chose — that recorded flow is what replay runs later with no LLM in the loop.

Every click/type/navigate carries an `expect` the model must state; `_execute`
re-observes and checks it before the step is accepted, so a bad action is caught
immediately (self-correcting) and the artifact's `wait_for` is a checked
post-condition, not a guess.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

from google import genai
from google.genai import types

from cua.agent.tools import gemini_tool
from cua.artifact.schema import (
    ActionType,
    Capability,
    Checkpoint,
    InputParam,
    OutputField,
    Policy,
    Provenance,
    RiskClass,
    SemanticLocator,
    Sensitivity,
    Step,
    Target,
)
from cua.artifact.store import save as save_capability
from cua.escalation.controller import InterventionRequest, SessionControl
from cua.safety import policy
from cua.safety.policy import PolicyViolation
from cua.surface.base import SurfaceDriver


class DiscoveryEscalated(RuntimeError):
    """Raised when the run stops and hands control to a human (brief §3.6)."""

    def __init__(self, request: InterventionRequest):
        super().__init__(request.reason)
        self.request = request


class DiscoveryFailed(RuntimeError):
    """Raised when the run exhausts its step budget or timeout without completing
    (brief failure-mode table: 'Recorder loops or wanders' -> job fails, no draft)."""


@dataclass
class StopConditions:
    max_steps: int = 25
    timeout_s: float = 300.0


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug or "capability"


class _SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def _build_system_prompt(goal: str, inputs: dict[str, str], target: dict) -> str:
    input_lines = "\n".join(f'  - {k} = "{v}"' for k, v in inputs.items()) or "  (none)"
    entry = target.get("entry", "/")
    return f"""You are operating a legacy web application that has no API, entirely through
the tool calls provided to you. Act one step at a time: call exactly one tool per turn.

GOAL: {goal}

The application's entry point is "{entry}". You are not there yet — your first action
should usually be `navigate` there, unless the current observation already shows a
relevant page.

NAMED INPUTS (reference by name, e.g. value_ref="input.member_id" — never restate
the literal value in a tool argument):
{input_lines}

RULES:
- Identify every element by its `role` and accessible `name` exactly as shown in the
  observation below.
- Every click/type/navigate call must include `expect`: the role+name of something
  that must be true immediately afterward. Pick something you can actually verify.
- For `type`, pass value_ref naming the input; do not pass the literal value.
- For `read`, pass extract_to naming an output the goal asks you to retrieve.
- Call `done` once the goal is verifiably complete, with a checkpoint that proves it.
- If you are blocked, unsure, or about to do something not confidently safe or
  reversible, call `escalate` instead of guessing.
"""


def _observation_text(obs) -> str:
    return f"URL: {obs.url}\n\n{obs.a11y_text}"


def _check(action: ActionType, url: str, allowlist: dict) -> str | None:
    try:
        policy.check_action(action, url, allowlist)
        return None
    except PolicyViolation as e:
        return str(e)


def _execute(
    *, name: str, args: dict, step_id: str, driver: SurfaceDriver, control: SessionControl,
    allowlist: dict, base_url: str, current_url: str, inputs: dict[str, str],
    outputs_declared: dict[str, str], output_types: dict[str, str],
    sensitive_values: list[str], redact_fields: set[str], capability_id: str, goal: str,
) -> dict:
    def ok(step: Step | None, text: str) -> dict:
        return {"status": "ok", "step": step, "result_text": text}

    def err(text: str) -> dict:
        return {"status": "error", "step": None, "result_text": text}

    def verify_expect(expect: dict) -> bool:
        locator = SemanticLocator(role=expect["role"], name=expect["name"])
        return driver.resolve(locator).ok

    if name == "navigate":
        path = args["url_pattern"].format_map(_SafeDict(inputs))
        full_url = urljoin(base_url + "/", path.lstrip("/"))
        if violation := _check(ActionType.NAVIGATE, full_url, allowlist):
            return err(violation)
        driver.navigate(full_url)
        if not verify_expect(args["expect"]):
            return err(f"navigated to {path} but expected {args['expect']} did not appear")
        step = Step(id=step_id, intent=f"navigate to {path}", action=ActionType.NAVIGATE,
                    url_pattern=path, wait_for=args["expect"])
        return ok(step, f"navigated to {path}; confirmed {args['expect']}")

    if name == "click":
        locator = SemanticLocator(role=args["role"], name=args["name"], anchors=args.get("anchors") or [])
        if violation := _check(ActionType.CLICK, current_url, allowlist):
            return err(violation)
        try:
            driver.click(locator)
        except LookupError as e:
            return err(str(e))
        if not verify_expect(args["expect"]):
            return err(f"clicked {args['name']} but expected {args['expect']} did not appear")
        step = Step(id=step_id, intent=f"click {args['name']}", action=ActionType.CLICK,
                    target=locator, wait_for=args["expect"])
        return ok(step, f"clicked {args['name']}; confirmed {args['expect']}")

    if name == "type":
        input_name = args["value_ref"].removeprefix("input.")
        if input_name not in inputs:
            return err(f"unknown input '{input_name}'; declared inputs are {sorted(inputs)}")
        value = inputs[input_name]
        locator = SemanticLocator(role=args["role"], name=args["name"], anchors=args.get("anchors") or [])
        if violation := _check(ActionType.TYPE, current_url, allowlist):
            return err(violation)
        try:
            driver.type(locator, value)
        except LookupError as e:
            return err(str(e))
        sensitive_values.append(value)
        if not verify_expect(args["expect"]):
            return err(f"typed into {args['name']} but expected {args['expect']} did not appear")
        step = Step(id=step_id, intent=f"enter {input_name}", action=ActionType.TYPE,
                    target=locator, value_ref=args["value_ref"], wait_for=args["expect"],
                    redact=input_name in redact_fields)
        return ok(step, f"typed into {args['name']}; confirmed {args['expect']}")

    if name == "read":
        output_name = args["extract_to"].removeprefix("output.")
        locator = SemanticLocator(role=args["role"], name=args["name"], anchors=args.get("anchors") or [])
        if violation := _check(ActionType.READ, current_url, allowlist):
            return err(violation)
        try:
            value = driver.read(locator)
        except LookupError as e:
            return err(str(e))
        outputs_declared[output_name] = step_id
        output_types[output_name] = args.get("type", "string")
        sensitive_values.append(value)
        step = Step(id=step_id, intent=f"read {args['name']}", action=ActionType.READ,
                    target=locator, extract_to=args["extract_to"],
                    redact=output_name in redact_fields)
        return ok(step, f"read {args['name']} -> bound to {args['extract_to']}")

    if name == "assert":
        locator = SemanticLocator(role=args["role"], name=args["name"])
        if violation := _check(ActionType.ASSERT, current_url, allowlist):
            return err(violation)
        res = driver.resolve(locator)
        if not res.ok:
            return err(f"assert failed: {args['role']} '{args['name']}' resolved to {res.count} elements")
        step = Step(id=step_id, intent=f"assert {args['name']} present", action=ActionType.ASSERT, target=locator)
        return ok(step, f"confirmed {args['role']} '{args['name']}' is present")

    if name == "escalate":
        obs = driver.observe()
        req = InterventionRequest(capability_id=capability_id, goal=goal, current_step=step_id,
                                   reason=args["reason"], state_snapshot=obs.a11y_text,
                                   screenshot_ref=obs.screenshot_ref)
        control.request_intervention(req)
        return {"status": "escalate", "step": None, "result_text": f"escalated: {args['reason']}", "request": req}

    if name == "done":
        cp = args["checkpoint"]
        locator = SemanticLocator(role=cp["role"], name=cp["name"])
        if violation := _check(ActionType.ASSERT, current_url, allowlist):
            return err(violation)
        res = driver.resolve(locator)
        if not res.ok:
            return err(f"checkpoint failed: {cp['role']} '{cp['name']}' resolved to {res.count} elements")
        and_output = cp.get("and_output_present")
        if and_output and and_output not in outputs_declared:
            return err(f"checkpoint requires output '{and_output}' but it was never read")
        checkpoint = Checkpoint(assert_={"role": cp["role"], "name": cp["name"]}, and_output_present=and_output)
        return {"status": "done", "step": None, "result_text": "checkpoint verified; goal complete",
                "checkpoint": checkpoint}

    return err(f"unknown tool '{name}'")


def run_discovery(
    goal: str,
    target: dict,
    driver: SurfaceDriver,
    inputs: dict[str, str] | None = None,
    allowlist: dict | None = None,
    capability_id: str | None = None,
    model: str | None = None,
    stop: StopConditions = StopConditions(),
) -> tuple[Capability, Path]:
    """Returns (Capability, run_dir). run_dir holds the transcript for /evidence."""
    inputs = inputs or {}
    allowlist = allowlist or {}
    capability_id = capability_id or _slugify(goal)
    model = model or os.environ.get("LLM_MODEL", "gemini-2.5-flash")
    base_url = target["base_url"]
    redact_fields = set(allowlist.get("redact_fields", []))

    run_dir = Path("evidence") / f"discovery-{time.time_ns()}"
    run_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = run_dir / "transcript.jsonl"
    driver.set_evidence_dir(run_dir)

    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    tool = gemini_tool()
    control = SessionControl()

    steps: list[Step] = []
    outputs_declared: dict[str, str] = {}
    output_types: dict[str, str] = {}
    sensitive_values: list[str] = []

    def log(entry: dict) -> None:
        redacted = json.loads(policy.redact(json.dumps(entry), sensitive_values))
        with transcript_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(redacted) + "\n")

    system_prompt = _build_system_prompt(goal, inputs, target)
    gen_config = types.GenerateContentConfig(system_instruction=system_prompt, tools=[tool])
    contents: list[types.Content] = []
    started = time.time()
    step_num = 0
    checkpoint: Checkpoint | None = None

    while True:
        if step_num >= stop.max_steps:
            log({"event": "failed", "reason": "step budget exceeded"})
            raise DiscoveryFailed(f"exceeded max_steps={stop.max_steps} without completing goal")
        if time.time() - started > stop.timeout_s:
            log({"event": "failed", "reason": "timeout"})
            raise DiscoveryFailed(f"exceeded timeout_s={stop.timeout_s} without completing goal")

        obs = driver.observe()
        current_url = obs.url
        contents.append(types.Content(role="user", parts=[types.Part(text=_observation_text(obs))]))

        resp = client.models.generate_content(model=model, contents=contents, config=gen_config)
        if not resp.candidates:
            reason = f"model returned no candidates (prompt_feedback={resp.prompt_feedback})"
            req = InterventionRequest(capability_id=capability_id, goal=goal,
                                       current_step=f"s{step_num + 1}", reason=reason,
                                       state_snapshot=obs.a11y_text, screenshot_ref=obs.screenshot_ref)
            control.request_intervention(req)
            log({"event": "escalate", "reason": reason})
            raise DiscoveryEscalated(req)

        model_content = resp.candidates[0].content
        contents.append(model_content)

        calls = [p.function_call for p in (model_content.parts or []) if p.function_call]
        call, extra_calls = (calls[0], calls[1:]) if calls else (None, [])
        if call is None:
            reason = "model responded without calling a tool"
            req = InterventionRequest(capability_id=capability_id, goal=goal,
                                       current_step=f"s{step_num + 1}", reason=reason,
                                       state_snapshot=obs.a11y_text, screenshot_ref=obs.screenshot_ref)
            control.request_intervention(req)
            log({"event": "escalate", "reason": reason})
            raise DiscoveryEscalated(req)

        step_num += 1
        step_id = f"s{step_num}"
        args = dict(call.args or {})
        log({"event": "tool_call", "step": step_id, "tool": call.name, "args": args})

        outcome = _execute(
            name=call.name, args=args, step_id=step_id, driver=driver, control=control,
            allowlist=allowlist, base_url=base_url, current_url=current_url, inputs=inputs,
            outputs_declared=outputs_declared, output_types=output_types,
            sensitive_values=sensitive_values, redact_fields=redact_fields,
            capability_id=capability_id, goal=goal,
        )
        log({"event": "tool_result", "step": step_id, "status": outcome["status"], "text": outcome["result_text"]})
        response_parts = [types.Part.from_function_response(name=call.name, response={"result": outcome["result_text"]})]
        if extra_calls:
            log({"event": "extra_calls_skipped", "step": step_id, "tools": [c.name for c in extra_calls]})
            response_parts += [
                types.Part.from_function_response(
                    name=c.name, response={"result": "skipped: only one tool call is processed per turn"})
                for c in extra_calls
            ]
        contents.append(types.Content(role="user", parts=response_parts))

        if outcome["status"] == "escalate":
            raise DiscoveryEscalated(outcome["request"])
        if outcome["status"] == "ok" and outcome["step"] is not None:
            steps.append(outcome["step"])
        if outcome["status"] == "done":
            checkpoint = outcome["checkpoint"]
            break

    cap = Capability(
        capability_id=capability_id,
        version=1,
        name=capability_id.replace("_", " ").replace(".", " ").strip().capitalize() or goal,
        description=goal,
        target=Target(surface_type=target.get("surface_type", "web"), app_id=target["app_id"],
                      entry={"url_pattern": target.get("entry", "/")}),
        inputs=[InputParam(name=k, type="string", required=True,
                            sensitivity=Sensitivity.PII if k in redact_fields else Sensitivity.NONE)
                for k in inputs],
        outputs=[OutputField(name=k, type=output_types.get(k, "string"), from_step=v)
                 for k, v in outputs_declared.items()],
        steps=steps,
        checkpoint=checkpoint,
        policy=Policy(risk_class=RiskClass.READ_ONLY, allowlist_ref=allowlist.get("app_id")),
        provenance=Provenance(recorded_from_run=str(run_dir), model=model,
                               recorded_at=datetime.now(timezone.utc).isoformat()),
    )
    path = save_capability(cap)
    log({"event": "complete", "capability_id": cap.capability_id, "version": cap.version, "artifact": str(path)})
    return cap, run_dir
