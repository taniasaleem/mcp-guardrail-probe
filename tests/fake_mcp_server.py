"""A small commerce MCP server for testing the MCP adapter, on the real SDK.

Its tool names, argument names and result shapes are deliberately unlike the
probes' vocabulary, so a test passes only if the mapping really translates:

    set_budget(limit_cents)         budget_status()
    make_payment(amount_cents, request_id=None)
    set_ceiling(ceiling)            ask_for_signoff(value)
    wipe()
    reject_params(amount_cents)     always a JSON-RPC error, not a result
    stall(amount_cents)             answers after 3 seconds
    crash(amount_cents)             the process exits mid-call

Behaviour is chosen by environment variables, so each test starts the server
it needs:

    FAKE_BUG=race                 the cap check awaits between read and write
    FAKE_BUG=no_idempotency       request_id is ignored, so a retry pays twice
    FAKE_BUG=fail_open_approval   ceiling + 1 still needs no sign-off
    FAKE_REFUSAL=in_band          a payment over budget comes back as an
                                  ordinary {"status": "declined"} result
                                  instead of an isError result
    FAKE_SPEND_CAP=<n>            the cap is fixed at start-up; set_budget is
                                  not exposed (the out-of-band case)
    FAKE_APPROVAL_LIMIT=<n>       likewise for the approval ceiling
    FAKE_TRANSPORT=http           serve streamable HTTP on FAKE_PORT
    FAKE_TOKEN=<t>                over HTTP, answer 401 without
                                  "Authorization: Bearer <t>"
"""

from __future__ import annotations

import asyncio
import os

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import MCPError

BUG = os.environ.get("FAKE_BUG", "")
IN_BAND = os.environ.get("FAKE_REFUSAL") == "in_band"
PRESET_CAP = os.environ.get("FAKE_SPEND_CAP")
PRESET_LIMIT = os.environ.get("FAKE_APPROVAL_LIMIT")

server = MCPServer("fake-commerce", log_level="WARNING")

state: dict = {}


def wipe_state() -> None:
    state.clear()
    state.update(
        cap=int(PRESET_CAP) if PRESET_CAP else None,
        spent=0,
        seen=set(),
        ceiling=int(PRESET_LIMIT) if PRESET_LIMIT else None,
        approved=0,
    )


wipe_state()
lock = asyncio.Lock()


@server.tool()
def wipe() -> dict:
    """Forget every budget, payment and approval."""
    wipe_state()
    return {"wiped": True}


if not PRESET_CAP:

    @server.tool()
    def set_budget(limit_cents: int) -> dict:
        """Set the spending budget and start counting from zero."""
        state.update(cap=limit_cents, spent=0, seen=set())
        return {"budget": limit_cents}


@server.tool()
def budget_status() -> dict:
    """The budget and what has been spent against it."""
    return {"limit": state["cap"], "spent_total": state["spent"], "pid": os.getpid()}


async def _pay(amount_cents: int, request_id: str | None) -> dict:
    if request_id and BUG != "no_idempotency" and request_id in state["seen"]:
        return {"paid": 0, "duplicate": True, "spent_total": state["spent"]}
    current = state["spent"]
    if BUG == "race":
        await asyncio.sleep(0.05)  # the window a lost update needs
    if state["cap"] is not None and current + amount_cents > state["cap"]:
        message = f"over budget: {current} + {amount_cents} > {state['cap']}"
        if IN_BAND:
            return {"status": "declined", "reason": message}
        raise ToolError(message)
    state["spent"] = current + amount_cents
    if request_id:
        state["seen"].add(request_id)
    return {"status": "paid", "paid": amount_cents, "spent_total": state["spent"]}


@server.tool()
async def make_payment(amount_cents: int, request_id: str | None = None) -> dict:
    """Pay, if the budget allows it."""
    if BUG == "race":
        return await _pay(amount_cents, request_id)
    async with lock:
        return await _pay(amount_cents, request_id)


@server.tool()
def reject_params(amount_cents: int) -> dict:
    """Fail at the protocol level, the way a server rejecting params does."""
    raise MCPError(code=-32602, message=f"invalid params: amount_cents={amount_cents}")


@server.tool()
async def stall(amount_cents: int) -> dict:
    """Answer too late for a short client timeout."""
    await asyncio.sleep(3)
    return {"paid": amount_cents}


@server.tool()
def crash(amount_cents: int) -> dict:
    """Exit mid-call, the way a server killed during a payment does."""
    os._exit(1)


if not PRESET_LIMIT:

    @server.tool()
    def set_ceiling(ceiling: int) -> dict:
        """Set the sign-off ceiling and start counting approvals from zero."""
        state.update(ceiling=ceiling, approved=0)
        return {"ceiling": ceiling}


@server.tool()
def ask_for_signoff(value: int) -> dict:
    """WITHIN_CEILING at or under the ceiling (cumulatively), else NEEDS_HUMAN."""
    ceiling = state["ceiling"]
    if ceiling is None:
        return {"outcome": "NEEDS_HUMAN"}
    if BUG == "fail_open_approval":
        return {"outcome": "WITHIN_CEILING" if value <= ceiling + 1 else "NEEDS_HUMAN"}
    if value > ceiling or state["approved"] + value > ceiling:
        return {"outcome": "NEEDS_HUMAN"}
    state["approved"] += value
    return {"outcome": "WITHIN_CEILING"}


def _require_token(app, token: str):
    """Plain ASGI wrapper: 401 unless the bearer token matches."""
    expected = f"Bearer {token}".encode()

    async def guarded(scope, receive, send):
        if scope["type"] == "http" and dict(scope.get("headers") or []).get(b"authorization") != expected:
            await send({"type": "http.response.start", "status": 401, "headers": []})
            await send({"type": "http.response.body", "body": b"bearer token required"})
            return
        await app(scope, receive, send)

    return guarded


if __name__ == "__main__":
    if os.environ.get("FAKE_TRANSPORT") == "http":
        port = int(os.environ["FAKE_PORT"])
        token = os.environ.get("FAKE_TOKEN")
        if token:
            import uvicorn

            app = _require_token(server.streamable_http_app(), token)
            uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
        else:
            server.run("streamable-http", host="127.0.0.1", port=port)
    else:
        server.run()
