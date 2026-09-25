"""
Safety guardrail tests (brief §3.4 / PROJECT_PLAN Milestone 4).

Covers:
  * allowlist blocks an off-domain / off-route action before it ever touches the UI
  * redact() masks literal sensitive values in arbitrary text
  * replay refuses an irreversible / requires_approval capability unless approved=True
  * a replay transcript on disk never contains a raw sensitive value that was
    typed or read during the run, even though the caller's return value does
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

from pathlib import Path

from cua.artifact.schema import ActionType, RiskClass
from cua.artifact.store import load as load_capability
from cua.replay.engine import replay
from cua.replay.result import ReplayStatus
from cua.safety.policy import PolicyViolation, check_action, load_allowlist, redact
from cua.surface.web import WebDriver

ARTIFACT_PATH = "evidence/member.read_savings_balance.v1.json"
ALLOWLIST_PATH = "config/allowlist.servicing-console.yaml"


def test_check_action_allows_permitted_route():
    allowlist = load_allowlist(ALLOWLIST_PATH)
    check_action(ActionType.NAVIGATE, "http://127.0.0.1:5000/members/search", allowlist)  # no raise


def test_check_action_blocks_off_domain():
    allowlist = load_allowlist(ALLOWLIST_PATH)
    with pytest.raises(PolicyViolation):
        check_action(ActionType.NAVIGATE, "http://evil.example.com/members/search", allowlist)


def test_check_action_blocks_off_route():
    allowlist = load_allowlist(ALLOWLIST_PATH)
    with pytest.raises(PolicyViolation):
        check_action(ActionType.NAVIGATE, "http://127.0.0.1:5000/admin/delete-everything", allowlist)


def test_check_action_blocks_disallowed_action_type():
    allowlist = load_allowlist(ALLOWLIST_PATH)
    with pytest.raises(PolicyViolation):
        check_action(ActionType.WAIT, "http://127.0.0.1:5000/members/search", allowlist)  # not in permitted_actions


def test_redact_masks_every_occurrence():
    text = "member 12345 called about balance $4,210.55; confirmed 12345 again"
    out = redact(text, ["12345", "$4,210.55"])
    assert "12345" not in out
    assert "$4,210.55" not in out
    assert out.count("[REDACTED]") == 3


def test_redact_is_a_noop_with_no_sensitive_values():
    assert redact("nothing sensitive here", []) == "nothing sensitive here"


# --- replay-level policy gate + transcript redaction (needs the live mock app) ---

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def mock_bank_url():
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "flask", "--app", "apps/mock_bank/app", "run", "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base_url = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            urllib.request.urlopen(base_url + "/members/search", timeout=0.5)
            break
        except Exception:
            time.sleep(0.1)
    else:
        proc.terminate()
        raise RuntimeError("mock bank app did not come up in time")
    yield base_url
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture
def driver():
    d = WebDriver(headed=False)
    yield d
    d.close()


@pytest.fixture
def capability():
    return load_capability(ARTIFACT_PATH)


def test_replay_blocks_irreversible_capability_without_approval(mock_bank_url, driver, capability, tmp_path):
    capability.policy.risk_class = RiskClass.IRREVERSIBLE
    result = replay(capability, {"member_id": "12345"}, driver, base_url=mock_bank_url,
                     evidence_root=tmp_path, approved=False)
    assert result.status == ReplayStatus.FAILURE
    assert result.failed_step == "policy"


def test_replay_allows_irreversible_capability_when_approved(mock_bank_url, driver, capability, tmp_path):
    capability.policy.risk_class = RiskClass.IRREVERSIBLE
    result = replay(capability, {"member_id": "12345"}, driver, base_url=mock_bank_url,
                     evidence_root=tmp_path, approved=True)
    assert result.status == ReplayStatus.SUCCESS


def test_replay_transcript_never_contains_raw_sensitive_values(mock_bank_url, driver, capability, tmp_path):
    result = replay(capability, {"member_id": "12345"}, driver, base_url=mock_bank_url, evidence_root=tmp_path)
    assert result.status == ReplayStatus.SUCCESS
    assert result.outputs["savings_balance"] == "$4,210.55"  # the real value still reaches the caller

    transcript_text = Path(result.evidence_ref, "transcript.jsonl").read_text(encoding="utf-8")
    assert "12345" not in transcript_text
    assert "$4,210.55" not in transcript_text
    assert "[REDACTED]" in transcript_text
    # sanity: the file is still valid JSONL
    for line in transcript_text.splitlines():
        json.loads(line)
