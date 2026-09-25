"""
Safety & policy guardrails (brief §3.4).

Enforced at a single choke-point so it cannot be bypassed:
  * allowlist  — permitted domains/route patterns + permitted action types
  * risk class — read_only/reversible safe; irreversible handled conservatively
  * redaction  — never persist secrets/PII into artifacts or logs
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import yaml

from cua.artifact.schema import ActionType, RiskClass

SAFE_ACTIONS = {ActionType.NAVIGATE, ActionType.READ, ActionType.WAIT, ActionType.ASSERT}
RISKY_ACTIONS = {ActionType.CLICK, ActionType.TYPE}  # can cause writes; inspect target/intent


class PolicyViolation(Exception):
    ...


def load_allowlist(path: str | Path) -> dict:
    return yaml.safe_load(Path(path).read_text())


def _route_matches(pattern: str, path: str) -> bool:
    """":id"-style segments in `pattern` match any single path segment."""
    pat_parts = pattern.strip("/").split("/")
    path_parts = path.strip("/").split("/")
    if len(pat_parts) != len(path_parts):
        return False
    return all(p.startswith(":") or p == seg for p, seg in zip(pat_parts, path_parts))


def check_action(action: ActionType, url: str, allowlist: dict) -> None:
    """Raise PolicyViolation if the action/route is outside the allowlist."""
    permitted_actions = allowlist.get("permitted_actions", [])
    if action.value not in permitted_actions:
        raise PolicyViolation(f"action '{action.value}' is not in the permitted_actions allowlist")

    parsed = urlparse(url)
    domain = parsed.hostname or ""
    permitted_domains = allowlist.get("permitted_domains", [])
    if domain not in permitted_domains:
        raise PolicyViolation(f"domain '{domain}' is not in the permitted_domains allowlist")

    permitted_routes = allowlist.get("permitted_routes", [])
    if not any(_route_matches(r, parsed.path) for r in permitted_routes):
        raise PolicyViolation(f"route '{parsed.path}' is not in the permitted_routes allowlist")


def is_irreversible(risk: RiskClass) -> bool:
    return risk == RiskClass.IRREVERSIBLE


def redact(text: str, values: list[str]) -> str:
    """Mask each sensitive literal value found in `text` before it is logged/persisted."""
    out = text
    for value in values:
        if value:
            out = out.replace(value, "[REDACTED]")
    return out
