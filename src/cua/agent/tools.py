"""
Action tool surface for the LLM (brief §3.1).

The LLM emits ONE typed action per tool-call turn. Every recorded action maps 1:1
to a Step in the artifact, so discovery and replay share vocabulary.

Two conventions carry the design's key decisions straight into the schema:
  * `value_ref` / `extract_to` name a *logical* input/output ("input.member_id",
    not the literal "12345") — this is what lets the compiler-in-the-loop record a
    reusable capability instead of guessing which literal values are parameters.
  * `expect` on click/type/navigate is the model's own prediction of what should be
    true after the action lands. The loop re-observes and verifies it before
    accepting the step, so a bad click is caught immediately (self-correcting) and
    the artifact's `wait_for` is a *checked* post-condition, not a guess.
"""
from __future__ import annotations

from google.genai import types

_LOCATOR_PROPS = {
    "role": {"type": "string", "description": "ARIA role, e.g. button, textbox, link, heading, cell."},
    "name": {"type": "string", "description": "Accessible name / visible label, exactly as shown in the observation."},
    "anchors": {
        "type": "array", "items": {"type": "string"},
        "description": "Optional free-text notes on where this element lives, e.g. \"within form 'Member Search'\". Aids a human reviewer; not required for resolution.",
    },
}

_EXPECT_PROP = {
    "type": "object",
    "description": "What must be true after this action for it to count as landed, e.g. {\"role\": \"heading\", \"name\": \"Member Detail\"}.",
    "properties": {
        "role": {"type": "string"},
        "name": {"type": "string"},
    },
    "required": ["role", "name"],
}

AGENT_TOOLS = [
    {
        "name": "navigate",
        "description": "Go to a path on the target app (relative to its base URL). Use {input_name} to splice in a named input, e.g. \"/members/{member_id}\".",
        "input_schema": {
            "type": "object",
            "properties": {
                "url_pattern": {"type": "string"},
                "expect": _EXPECT_PROP,
            },
            "required": ["url_pattern", "expect"],
        },
    },
    {
        "name": "click",
        "description": "Click a control identified by role + accessible name.",
        "input_schema": {
            "type": "object",
            "properties": {**_LOCATOR_PROPS, "expect": _EXPECT_PROP},
            "required": ["role", "name", "expect"],
        },
    },
    {
        "name": "type",
        "description": "Fill a field by naming the logical input it corresponds to, e.g. value_ref=\"input.member_id\" — never pass the literal value here.",
        "input_schema": {
            "type": "object",
            "properties": {
                **_LOCATOR_PROPS,
                "value_ref": {"type": "string", "description": "\"input.<name>\", referencing one of the task's named inputs."},
                "expect": _EXPECT_PROP,
            },
            "required": ["role", "name", "value_ref", "expect"],
        },
    },
    {
        "name": "read",
        "description": "Read the text of an element and bind it to a named output, e.g. extract_to=\"output.savings_balance\".",
        "input_schema": {
            "type": "object",
            "properties": {
                **_LOCATOR_PROPS,
                "extract_to": {"type": "string", "description": "\"output.<name>\" for the value being extracted."},
                "type": {
                    "type": "string", "default": "string",
                    "description": "Semantic type of the value, e.g. string, money, date.",
                },
            },
            "required": ["role", "name", "extract_to"],
        },
    },
    {
        "name": "assert",
        "description": "Verify an element is present without acting on it (a standalone checkpoint, not tied to a prior action).",
        "input_schema": {
            "type": "object",
            "properties": _LOCATOR_PROPS,
            "required": ["role", "name"],
        },
    },
    {
        "name": "escalate",
        "description": "Stop and hand control to a human: use this when stuck, blocked, or facing an action you should not take unattended.",
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    },
    {
        "name": "done",
        "description": "Declare the goal complete. `checkpoint` is the final state that proves it; and_output_present names an output that must have been read.",
        "input_schema": {
            "type": "object",
            "properties": {
                "checkpoint": {
                    "type": "object",
                    "properties": {
                        "role": {"type": "string"},
                        "name": {"type": "string"},
                        "and_output_present": {"type": "string"},
                    },
                    "required": ["role", "name"],
                },
            },
            "required": ["checkpoint"],
        },
    },
]


def gemini_tool() -> types.Tool:
    """AGENT_TOOLS is plain JSON Schema, which the Gemini SDK accepts directly via
    parameters_json_schema — one conversion point, one source of truth for the tools."""
    return types.Tool(function_declarations=[
        types.FunctionDeclaration(name=t["name"], description=t["description"],
                                   parameters_json_schema=t["input_schema"])
        for t in AGENT_TOOLS
    ])
