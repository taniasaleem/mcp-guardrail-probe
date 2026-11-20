"""How McpTarget opens and closes sessions, with a stand-in for the SDK client.

The real SDK is not needed: the target talks to the SDK only through the
Client class that _require_sdk returns, so these tests swap in one whose
close they control. That makes timing that is rare against a real server
(a session that takes longer to close than the target waits) happen on cue.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from mcp_guardrail_probe import mcp_target
from mcp_guardrail_probe.mcp_target import McpConfig, McpTarget

HTTP = {"transport": "http", "url": "http://127.0.0.1:9/mcp"}


def _client_class(close_delays, shielded=False, tools=("pay",)):
    """A Client whose n-th instance takes close_delays[n] seconds to close.

    shielded: the close ignores cancellation and finishes anyway, as a
    transport whose cleanup is shielded does.
    """
    delays = list(close_delays)

    class StandInClient:
        def __init__(self, server, read_timeout_seconds=None, cache=None):
            self.delay = delays.pop(0) if delays else 0.0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            end = time.monotonic() + self.delay
            while True:
                try:
                    await asyncio.sleep(max(0.0, end - time.monotonic()))
                    return
                except asyncio.CancelledError:
                    if not shielded:
                        raise

        async def list_tools(self, cursor=None):
            return SimpleNamespace(
                tools=[SimpleNamespace(name=n) for n in tools], next_cursor=None
            )

    return StandInClient


@pytest.fixture
def short_waits(monkeypatch):
    monkeypatch.setattr(mcp_target, "_CLOSE_WAIT_SECONDS", 0.2)
    monkeypatch.setattr(mcp_target, "_CANCEL_WAIT_SECONDS", 0.2)


def _target(monkeypatch, client_class, **config):
    monkeypatch.setattr(mcp_target, "_require_sdk", lambda: (client_class, None, Exception))
    return McpTarget(McpConfig.from_dict({"name": "stand-in", "server": HTTP, **config}))


def test_an_old_session_closing_late_cannot_clear_the_new_client(monkeypatch, short_waits):
    target = _target(monkeypatch, _client_class([1.0, 0.0], shielded=True))
    try:
        first = target._client
        target._disconnect()  # gives up waiting after 0.2s; the old close runs on
        target._connect()
        second = target._client
        assert second is not None and second is not first
        time.sleep(1.5)  # the old session has finished closing by now
        assert target._client is second
    finally:
        target.close()


def test_a_session_that_will_not_close_in_time_is_cancelled(monkeypatch, short_waits):
    target = _target(monkeypatch, _client_class([5.0, 0.0]))
    try:
        owner = target._owner
        started = time.monotonic()
        target._disconnect()
        assert owner.done(), "the old session was left running beside the next one"
        assert time.monotonic() - started < 2
    finally:
        target.close()


NO_RULE_WARNING = "tools.charge has no refuse_when"


def test_a_charge_mapping_with_no_refuse_when_is_warned_about(monkeypatch, capsys):
    tools = {"charge": {"tool": "pay"}}
    _target(monkeypatch, _client_class([]), tools=tools).close()
    assert NO_RULE_WARNING in capsys.readouterr().err


def test_no_warning_when_the_charge_has_a_refuse_when_rule(monkeypatch, capsys):
    rule = [{"path": "status", "in": ["declined"]}]
    tools = {"charge": {"tool": "pay", "refuse_when": rule}}
    _target(monkeypatch, _client_class([]), tools=tools).close()
    assert NO_RULE_WARNING not in capsys.readouterr().err


def test_no_warning_when_the_server_has_nothing_to_charge_with(monkeypatch, capsys):
    tools = {"charge": {"tool": "pay"}}
    _target(monkeypatch, _client_class([], tools=("list",)), tools=tools).close()
    assert NO_RULE_WARNING not in capsys.readouterr().err
