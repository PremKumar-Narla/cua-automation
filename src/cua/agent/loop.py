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
    input_lines = "\n".join(f'  - {name} = "{value}"' for name, value in inputs.items()) or "  (none)"
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


def _human_takeover(req: InterventionRequest, control: SessionControl, driver: SurfaceDriver, log) -> None:
    """Pause in place and let a human act in the SAME live (headed) browser session.
    Blocks synchronously on the terminal — this is the mocked operator console
    (brief §3.6 allows mocking the console UI, but the pause/resume mechanism and
    controller state must be real, which this is: `control.controller` genuinely
    flips to HUMAN and the discovery loop genuinely blocks until hand-back)."""
    control.request_intervention(req)
    log({"event": "escalate", "reason": req.reason, "step": req.current_step})
    print(f"\n[ESCALATED] {req.reason}")
    print(f"  capability={req.capability_id!r} step={req.current_step} goal={req.goal!r}")
    print("  The browser window is open — take over and do whatever's needed.")
    try:
        input("  Press Enter once you're done, to hand control back to the agent... ")
    except EOFError:
        log({"event": "escalate_abandoned", "reason": "no interactive terminal to hand off to"})
        raise DiscoveryEscalated(req)

    after = driver.observe()
    control.record_human_action({"observation_after_url": after.url})
    control.hand_back()
    log({"event": "human_handback", "observation_after_url": after.url})
    print("  Control handed back to the agent; resuming.\n")


def _generate_with_retry(client, model: str, contents, gen_config, log,
                          max_attempts: int = 4, base_delay: float = 2.0, rate_limit_delay: float = 40.0):
    """Transient LLM API overload (5xx) is common and expected — retry with
    exponential backoff. A 429 (free-tier rate limit — 5 requests/minute is easy
    for a single discovery run to hit on its own) waits long enough to cross into
    the next quota window rather than backing off briefly. Any other client error
    (e.g. a bad model name) means the request itself is wrong, not the server —
    never retried."""
    for attempt in range(1, max_attempts + 1):
        try:
            return client.models.generate_content(model=model, contents=contents, config=gen_config)
        except genai.errors.ServerError as error:
            if attempt == max_attempts:
                raise
            delay = base_delay * (2 ** (attempt - 1))
            log({"event": "llm_retry", "attempt": attempt, "reason": str(error), "delay_s": delay})
            time.sleep(delay)
        except genai.errors.ClientError as error:
            if error.code != 429 or attempt == max_attempts:
                raise
            log({"event": "llm_retry_rate_limited", "attempt": attempt, "reason": str(error),
                 "delay_s": rate_limit_delay})
            time.sleep(rate_limit_delay)


def _check(action: ActionType, url: str, allowlist: dict) -> str | None:
    try:
        policy.check_action(action, url, allowlist)
        return None
    except PolicyViolation as error:
        return str(error)


def _execute(
    *, name: str, args: dict, step_id: str, driver: SurfaceDriver,
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

    def landed_text(landed: bool, expect: dict) -> str:
        if landed:
            return f"confirmed {expect}"
        return (f"NOTE: expected {expect} but that was not found afterward — the action itself "
                f"still happened. Check the observation below; if something looks wrong, correct "
                f"course, otherwise continue.")

    if name == "navigate":
        path = args["url_pattern"].format_map(_SafeDict(inputs))
        full_url = urljoin(base_url + "/", path.lstrip("/"))
        if violation := _check(ActionType.NAVIGATE, full_url, allowlist):
            return err(violation)
        driver.navigate(full_url)
        landed = verify_expect(args["expect"])
        # Only trust the model's predicted post-state as a replay wait_for if it was
        # actually observed — an unverified guess baked into the capability would make
        # replay fail on the same wrong assumption every time.
        step = Step(id=step_id, intent=f"navigate to {path}", action=ActionType.NAVIGATE,
                    url_pattern=path, wait_for=args["expect"] if landed else None)
        return ok(step, f"navigated to {path}; {landed_text(landed, args['expect'])}")

    if name == "click":
        locator = SemanticLocator(role=args["role"], name=args["name"], anchors=args.get("anchors") or [])
        if violation := _check(ActionType.CLICK, current_url, allowlist):
            return err(violation)
        try:
            driver.click(locator)
        except LookupError as error:
            return err(str(error))
        landed = verify_expect(args["expect"])
        step = Step(id=step_id, intent=f"click {args['name']}", action=ActionType.CLICK,
                    target=locator, wait_for=args["expect"] if landed else None)
        return ok(step, f"clicked {args['name']}; {landed_text(landed, args['expect'])}")

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
        except LookupError as error:
            return err(str(error))
        sensitive_values.append(value)
        landed = verify_expect(args["expect"])
        step = Step(id=step_id, intent=f"enter {input_name}", action=ActionType.TYPE,
                    target=locator, value_ref=args["value_ref"], wait_for=args["expect"] if landed else None,
                    redact=input_name in redact_fields)
        return ok(step, f"typed into {args['name']}; {landed_text(landed, args['expect'])}")

    if name == "read":
        output_name = args["extract_to"].removeprefix("output.")
        locator = SemanticLocator(role=args["role"], name=args["name"], anchors=args.get("anchors") or [])
        if violation := _check(ActionType.READ, current_url, allowlist):
            return err(violation)
        try:
            value = driver.read(locator)
        except LookupError as error:
            return err(str(error))
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
        resolved = driver.resolve(locator)
        if not resolved.ok:
            return err(f"assert failed: {args['role']} '{args['name']}' resolved to {resolved.count} elements")
        step = Step(id=step_id, intent=f"assert {args['name']} present", action=ActionType.ASSERT, target=locator)
        return ok(step, f"confirmed {args['role']} '{args['name']}' is present")

    if name == "escalate":
        obs = driver.observe()
        req = InterventionRequest(capability_id=capability_id, goal=goal, current_step=step_id,
                                   reason=args["reason"], state_snapshot=obs.a11y_text,
                                   screenshot_ref=obs.screenshot_ref)
        # request_intervention() happens centrally in _human_takeover(), which also
        # blocks for the actual hand-back — see run_discovery's main loop.
        return {"status": "escalate", "step": None, "result_text": f"escalated: {args['reason']}", "request": req}

    if name == "done":
        checkpoint_args = args["checkpoint"]
        locator = SemanticLocator(role=checkpoint_args["role"], name=checkpoint_args["name"])
        if violation := _check(ActionType.ASSERT, current_url, allowlist):
            return err(violation)
        resolved = driver.resolve(locator)
        if not resolved.ok:
            return err(f"checkpoint failed: {checkpoint_args['role']} '{checkpoint_args['name']}' "
                       f"resolved to {resolved.count} elements")
        and_output = checkpoint_args.get("and_output_present")
        if and_output:
            and_output = and_output.removeprefix("output.")  # models pass either form
        if and_output and and_output not in outputs_declared:
            return err(f"checkpoint requires output '{and_output}' but it was never read")
        checkpoint = Checkpoint(assert_={"role": checkpoint_args["role"], "name": checkpoint_args["name"]},
                                 and_output_present=and_output)
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
    model = model or os.environ.get("LLM_MODEL", "gemini-3.8-flash")
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
    consecutive_errors = 0
    MAX_CONSECUTIVE_ERRORS = 3
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

        resp = _generate_with_retry(client, model, contents, gen_config, log)
        if not resp.candidates:
            reason = f"model returned no candidates (prompt_feedback={resp.prompt_feedback})"
            req = InterventionRequest(capability_id=capability_id, goal=goal,
                                       current_step=f"s{step_num + 1}", reason=reason,
                                       state_snapshot=obs.a11y_text, screenshot_ref=obs.screenshot_ref)
            pause_started = time.time()
            _human_takeover(req, control, driver, log)
            started += time.time() - pause_started  # don't burn the run's budget on human time
            continue

        model_content = resp.candidates[0].content
        contents.append(model_content)

        calls = [part.function_call for part in (model_content.parts or []) if part.function_call]
        call, extra_calls = (calls[0], calls[1:]) if calls else (None, [])
        if call is None:
            reason = "model responded without calling a tool"
            req = InterventionRequest(capability_id=capability_id, goal=goal,
                                       current_step=f"s{step_num + 1}", reason=reason,
                                       state_snapshot=obs.a11y_text, screenshot_ref=obs.screenshot_ref)
            pause_started = time.time()
            _human_takeover(req, control, driver, log)
            started += time.time() - pause_started
            continue

        step_num += 1
        step_id = f"s{step_num}"
        args = dict(call.args or {})
        log({"event": "tool_call", "step": step_id, "tool": call.name, "args": args})

        outcome = _execute(
            name=call.name, args=args, step_id=step_id, driver=driver,
            allowlist=allowlist, base_url=base_url, current_url=current_url, inputs=inputs,
            outputs_declared=outputs_declared, output_types=output_types,
            sensitive_values=sensitive_values, redact_fields=redact_fields,
            capability_id=capability_id, goal=goal,
        )
        log({"event": "tool_result", "step": step_id, "status": outcome["status"], "text": outcome["result_text"]})
        response_parts = [types.Part.from_function_response(name=call.name, response={"result": outcome["result_text"]})]
        if extra_calls:
            log({"event": "extra_calls_skipped", "step": step_id,
                 "tools": [extra_call.name for extra_call in extra_calls]})
            response_parts += [
                types.Part.from_function_response(
                    name=extra_call.name,
                    response={"result": "skipped: only one tool call is processed per turn"})
                for extra_call in extra_calls
            ]
        contents.append(types.Content(role="user", parts=response_parts))

        if outcome["status"] == "escalate":
            pause_started = time.time()
            _human_takeover(outcome["request"], control, driver, log)
            started += time.time() - pause_started
            consecutive_errors = 0
            continue
        if outcome["status"] == "error":
            consecutive_errors += 1
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                req = InterventionRequest(
                    capability_id=capability_id, goal=goal, current_step=step_id,
                    reason=f"{consecutive_errors} consecutive failed actions — the agent looks stuck",
                    state_snapshot=driver.observe().a11y_text)
                pause_started = time.time()
                _human_takeover(req, control, driver, log)
                started += time.time() - pause_started
                consecutive_errors = 0
            continue
        consecutive_errors = 0
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
        inputs=[InputParam(name=input_name, type="string", required=True,
                            sensitivity=Sensitivity.PII if input_name in redact_fields else Sensitivity.NONE)
                for input_name in inputs],
        outputs=[OutputField(name=output_name, type=output_types.get(output_name, "string"), from_step=source_step_id)
                 for output_name, source_step_id in outputs_declared.items()],
        steps=steps,
        checkpoint=checkpoint,
        policy=Policy(risk_class=RiskClass.READ_ONLY, allowlist_ref=allowlist.get("app_id")),
        provenance=Provenance(recorded_from_run=str(run_dir), model=model,
                               recorded_at=datetime.now(timezone.utc).isoformat()),
    )
    path = save_capability(cap)
    log({"event": "complete", "capability_id": cap.capability_id, "version": cap.version, "artifact": str(path)})
    return cap, run_dir
