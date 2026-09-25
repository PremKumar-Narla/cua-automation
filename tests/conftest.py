"""Shared pytest fixtures: a real mock bank app + a real (headless) browser driver."""
from __future__ import annotations

import socket
import subprocess
import sys
import time
import urllib.request

import pytest

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
def driver():
    d = WebDriver(headed=False)
    yield d
    d.close()
