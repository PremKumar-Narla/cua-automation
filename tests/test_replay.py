"""
Replay engine tests (brief §7 / PROJECT_PLAN Milestone 2).

Spins up the real mock bank Flask app as a subprocess and drives it with a real
Playwright browser — these are integration tests, not mocks, because the whole
point of the replay engine is that it works against a real UI.

Covers:
  * success path returns typed outputs
  * a declared business outcome (no such member) is BUSINESS_OUTCOME, not a crash
  * UI drift (element no longer resolves) fails closed with evidence, never a guess
  * replay is deterministic: same inputs -> same outputs, run twice
  * zero LLM calls: cua.replay.engine never imports the LLM client (google.genai)
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time
import urllib.request

import pytest

from cua.artifact.store import load as load_capability
from cua.replay.engine import replay
from cua.replay.result import ReplayStatus
from cua.surface.web import WebDriver

# The committed example lives under evidence/ (artifacts/*.json is gitignored —
# it's runtime output from `cua discover`, not something a fresh clone has).
ARTIFACT_PATH = "evidence/member.read_savings_balance.v1.json"


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


def test_replay_success_returns_typed_outputs(mock_bank_url, driver, capability, tmp_path):
    result = replay(capability, {"member_id": "12345"}, driver, base_url=mock_bank_url, evidence_root=tmp_path)
    assert result.status == ReplayStatus.SUCCESS
    assert result.outputs == {"savings_balance": "$4,210.55"}
    assert result.evidence_ref is not None


def test_replay_business_outcome_is_not_a_failure(mock_bank_url, driver, capability, tmp_path):
    result = replay(capability, {"member_id": "00000"}, driver, base_url=mock_bank_url, evidence_root=tmp_path)
    assert result.status == ReplayStatus.BUSINESS_OUTCOME
    assert result.status != ReplayStatus.FAILURE
    assert result.outcome_code == "MEMBER_NOT_FOUND"


def test_replay_fails_closed_on_drift(mock_bank_url, driver, capability, tmp_path):
    capability.steps[2].target.name = "Submit Query"  # simulate a renamed/removed button
    result = replay(capability, {"member_id": "12345"}, driver, base_url=mock_bank_url, evidence_root=tmp_path)
    assert result.status == ReplayStatus.FAILURE
    assert result.failed_step == "s3"
    assert "0 elements" in result.observed
    assert result.evidence_ref is not None


def test_replay_missing_required_input_raises(mock_bank_url, driver, capability, tmp_path):
    with pytest.raises(ValueError):
        replay(capability, {}, driver, base_url=mock_bank_url, evidence_root=tmp_path)


def test_replay_is_deterministic(mock_bank_url, capability, tmp_path):
    outputs = []
    for _ in range(2):
        d = WebDriver(headed=False)
        try:
            result = replay(capability, {"member_id": "12345"}, d, base_url=mock_bank_url, evidence_root=tmp_path)
        finally:
            d.close()
        outputs.append(result.outputs)
    assert outputs[0] == outputs[1] == {"savings_balance": "$4,210.55"}


def test_replay_engine_never_imports_the_llm_client():
    assert "google.genai" not in sys.modules
    assert "cua.agent" not in sys.modules
