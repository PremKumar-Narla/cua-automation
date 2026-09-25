"""
Escalation / human-handoff tests (brief §3.6 / PROJECT_PLAN Milestone 5).

`_human_takeover` is the pause -> human-acts-in-the-same-session -> hand-back
mechanism. It's tested directly (rather than through a full `run_discovery` run)
since that needs a live LLM call; this covers the actual control-transfer
machinery without needing a Gemini key.
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

from cua.agent.loop import DiscoveryEscalated, _human_takeover
from cua.escalation.controller import Controller, InterventionRequest, SessionControl
from cua.surface.web import WebDriver


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
def driver(mock_bank_url):
    d = WebDriver(headed=False)
    d.navigate(mock_bank_url + "/members/search")
    yield d
    d.close()


def test_human_takeover_pauses_and_resumes(driver, tmp_path, monkeypatch):
    control = SessionControl()
    transcript = tmp_path / "transcript.jsonl"

    def log(entry):
        with transcript.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    assert control.controller == Controller.AGENT

    monkeypatch.setattr("builtins.input", lambda *_: "")  # simulate the human pressing Enter immediately

    req = InterventionRequest(capability_id="test.capability", goal="test goal",
                               current_step="s1", reason="agent is stuck")
    _human_takeover(req, control, driver, log)

    # control returned to the agent, not left stuck on HUMAN
    assert control.controller == Controller.AGENT
    assert control.pending is None
    # the human's presence during the pause was actually recorded
    assert len(control.human_actions) == 1

    events = [json.loads(line)["event"] for line in transcript.read_text().splitlines()]
    assert "escalate" in events
    assert "human_handback" in events


def test_human_takeover_without_a_terminal_raises(driver, tmp_path, monkeypatch):
    control = SessionControl()
    transcript = tmp_path / "transcript.jsonl"

    def log(entry):
        with transcript.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def raise_eof(*_):
        raise EOFError

    monkeypatch.setattr("builtins.input", raise_eof)

    req = InterventionRequest(capability_id="test.capability", goal="test goal",
                               current_step="s1", reason="agent is stuck")
    with pytest.raises(DiscoveryEscalated):
        _human_takeover(req, control, driver, log)

    # a non-interactive context can't hand off — it fails loudly rather than hanging forever
    assert control.controller == Controller.HUMAN
