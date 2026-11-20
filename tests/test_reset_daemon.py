"""servers/agent-wallet/reset-daemon.sh, with a stand-in for the daemon.

The stand-in is what NODE runs in place of agent-wallet's src/daemon.ts: a
few lines of Python that answer the control API (status, mandates) and, unless
told not to, open the MCP port. Nothing of agent-wallet runs or is copied.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "servers" / "agent-wallet" / "reset-daemon.sh"

STAND_IN = r'''
import http.server, os, socket, sys, threading

def port(name):
    return int(os.environ[name])

if os.environ.get("STAND_IN_SERVE_MCP") == "1":
    mcp = socket.socket()
    mcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    mcp.bind(("127.0.0.1", port("AGENT_WALLET_MCP_PORT")))
    mcp.listen()
    threading.Thread(target=lambda: [mcp.accept()[0].close() for _ in iter(int, 1)], daemon=True).start()

class Control(http.server.BaseHTTPRequestHandler):
    def _ok(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")
    def do_GET(self):
        self._ok()
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self._ok()
    def log_message(self, *args):
        pass

http.server.HTTPServer(("127.0.0.1", port("AGENT_WALLET_CONTROL_PORT")), Control).serve_forever()
'''


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def daemon_env(tmp_path):
    if shutil.which("bash") is None or shutil.which("curl") is None:
        pytest.skip("needs bash and curl")
    checkout = tmp_path / "agent-wallet"
    (checkout / "src").mkdir(parents=True)
    (checkout / "src" / "daemon.ts").write_text(STAND_IN)
    env = {
        **os.environ,
        "AGENT_WALLET_DIR": str(checkout),
        "NODE": sys.executable,
        "PROBE_STATE_DIR": str(tmp_path / "state"),
        "AGENT_WALLET_MCP_PORT": str(_free_port()),
        "AGENT_WALLET_PAY_PORT": str(_free_port()),
        "AGENT_WALLET_CONTROL_PORT": str(_free_port()),
    }
    env.pop("STRIPE_SECRET_KEY", None)
    yield env
    subprocess.run(["bash", str(SCRIPT), "stop"], env=env, timeout=30)


def _reset(env):
    return subprocess.run(
        ["bash", str(SCRIPT), "spend-cap"], env=env, capture_output=True, text=True, timeout=60
    )


def test_a_daemon_serving_mcp_is_reported_up(daemon_env):
    done = _reset({**daemon_env, "STAND_IN_SERVE_MCP": "1"})
    assert done.returncode == 0, done.stderr
    assert "agent-wallet daemon up" in done.stderr


def test_a_daemon_not_serving_mcp_fails_the_reset(daemon_env):
    """Before the fix the wait fell through, printed "daemon up" and exited 0."""
    done = _reset({**daemon_env, "STAND_IN_SERVE_MCP": "0"})
    assert done.returncode == 1
    assert "not serving MCP on port" in done.stderr
    assert "daemon.log" in done.stderr
    assert "daemon up" not in done.stderr


def test_a_live_stripe_key_is_refused(daemon_env):
    done = _reset({**daemon_env, "STRIPE_SECRET_KEY": "sk_live_placeholder"})
    assert done.returncode == 1
    assert "not a Stripe test-mode key" in done.stderr
