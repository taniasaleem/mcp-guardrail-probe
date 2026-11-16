"""A deliberately broken commerce target, for demonstrating the probes.

The bugs here are common failure classes in agent commerce systems. They are
reproduced because they are representative, not because they are exotic:

  * the spend cap reads committed spend, decides, then writes — with no lock,
    so concurrent charges race, and it keeps reporting itself active afterwards
  * charges are not idempotent, so a retry commits twice
  * the "agent" concatenates listing text into its context and follows
    imperatives it finds there
  * auto-approve is off by one at the boundary in the blocking direction, and
    has no cumulative check at all

Running the probes against this target should produce findings. If it ever
produces none, the harness is broken — that is what tests/test_probes.py
asserts.
"""

from __future__ import annotations

import re
import time
from decimal import Decimal
from typing import Any

from ..model import ToolError

_IMPERATIVE = re.compile(r"call\s+(\w+)\s+with\s+amount\s+([\d.]+)", re.I)


class VulnerableCommerceServer:
    """In-process stand-in for an MCP commerce server."""

    name = "mock://vulnerable-commerce"

    def __init__(self, *, slow: bool = True) -> None:
        # `slow` widens the race window so the concurrency bug is reliably
        # observable on a fast machine. A real server has network latency
        # doing the same job.
        self._slow = slow
        self.reset()

    def reset(self) -> None:
        self.cap: Decimal | None = None
        self.committed = Decimal("0")
        self.approval_threshold: Decimal | None = None
        self.listings: list[dict[str, Any]] = []
        self.seen_keys: set[str] = set()

    # -- MCP surface ---------------------------------------------------

    def list_tools(self) -> list[str]:
        return [
            "set_spending_limit",
            "get_spending_limit",
            "charge",
            "add_item",
            "agent_request",
            "set_approval_threshold",
            "submit_for_approval",
        ]

    def call_tool(self, tool: str, **kwargs: Any) -> Any:
        try:
            handler = getattr(self, f"_tool_{tool}")
        except AttributeError:
            raise ToolError(f"unknown tool {tool!r}") from None
        return handler(**kwargs)

    # -- spend cap -----------------------------------------------------

    def _tool_set_spending_limit(self, amount: str) -> dict[str, Any]:
        self.cap = Decimal(str(amount))
        self.committed = Decimal("0")
        self.seen_keys.clear()
        return {"cap": str(self.cap), "active": True}

    def _tool_get_spending_limit(self) -> dict[str, Any]:
        # BUG: reports active purely because a cap value exists, with no
        # reference to whether it is still being honoured.
        return {
            "cap": str(self.cap) if self.cap is not None else None,
            "committed_spend": str(self.committed),
            "active": self.cap is not None,
        }

    def _tool_charge(self, amount: str, idempotency_key: str | None = None) -> dict[str, Any]:
        amt = Decimal(str(amount))

        # BUG: no idempotency. The key is recorded but never consulted before
        # committing, so a retry charges twice.
        if idempotency_key:
            self.seen_keys.add(idempotency_key)

        if self.cap is not None:
            current = self.committed          # read
            if self._slow:
                time.sleep(0.01)              # BUG: race window
            if current + amt > self.cap:
                raise ToolError(
                    f"would exceed cap: {current} + {amt} > {self.cap}",
                    {"cap": str(self.cap), "committed_spend": str(current)},
                )
            self.committed = current + amt    # write — lost update under load
        else:
            self.committed += amt

        return {"charged": str(amt), "committed_spend": str(self.committed)}

    # -- listings and the "agent" --------------------------------------

    def _tool_add_item(self, title: str, **fields: Any) -> dict[str, Any]:
        if "seller_note" in fields:
            raise ToolError("unknown field 'seller_note'")
        listing = {"title": title, **fields}
        self.listings = [listing]
        return {"created": listing}

    def _tool_agent_request(self, prompt: str) -> dict[str, Any]:
        """A toy agent loop.

        BUG: listing text is concatenated into the same context as the user's
        request, and any imperative found anywhere in that blob is executed.
        """
        context = " ".join(
            str(v) for listing in self.listings for v in listing.values()
        )
        blob = f"{prompt}\n{context}"

        calls = ["search_items"]
        for tool, amount in _IMPERATIVE.findall(blob):
            tool = tool.lower()
            if tool in self.list_tools():
                try:
                    self.call_tool(tool, amount=amount)
                except ToolError:
                    pass
                calls.append(tool)

        return {"tool_calls": calls, "text": "Here is the blue mug."}

    # -- approvals -----------------------------------------------------

    def _tool_set_approval_threshold(self, amount: str) -> dict[str, Any]:
        self.approval_threshold = Decimal(str(amount))
        return {"limit": str(self.approval_threshold)}

    def _tool_submit_for_approval(self, amount: str) -> dict[str, Any]:
        amt = Decimal(str(amount))
        if self.approval_threshold is None:
            return {"decision": "escalated"}
        # BUG: off by one in the blocking direction — anything from
        # (limit - 1) upward escalates, so valid amounts below the limit are
        # refused. And there is no cumulative tracking at all.
        if amt >= self.approval_threshold - 1:
            return {"decision": "escalated"}
        return {"decision": "approved"}


class SafeCommerceServer(VulnerableCommerceServer):
    """The same surface with the bugs fixed, used to prove the probes can pass."""

    name = "mock://safe-commerce"

    def __init__(self) -> None:
        super().__init__(slow=False)
        import threading

        self._lock = threading.Lock()

    def _tool_charge(self, amount: str, idempotency_key: str | None = None) -> dict[str, Any]:
        amt = Decimal(str(amount))
        with self._lock:
            if idempotency_key and idempotency_key in self.seen_keys:
                return {"charged": "0", "committed_spend": str(self.committed)}
            if self.cap is not None and self.committed + amt > self.cap:
                raise ToolError(f"would exceed cap: {self.committed} + {amt} > {self.cap}")
            self.committed += amt
            if idempotency_key:
                self.seen_keys.add(idempotency_key)
        return {"charged": str(amt), "committed_spend": str(self.committed)}

    def _tool_get_spending_limit(self) -> dict[str, Any]:
        enforcing = self.cap is not None and self.committed <= self.cap
        return {
            "cap": str(self.cap) if self.cap is not None else None,
            "committed_spend": str(self.committed),
            "active": enforcing,
        }

    def _tool_agent_request(self, prompt: str) -> dict[str, Any]:
        # Listing text is data. It is never scanned for instructions.
        return {"tool_calls": ["search_items"], "text": "Here is the blue mug."}

    def _tool_submit_for_approval(self, amount: str) -> dict[str, Any]:
        amt = Decimal(str(amount))
        if self.approval_threshold is None:
            return {"decision": "escalated"}
        with self._lock:
            if amt > self.approval_threshold:
                return {"decision": "escalated"}
            if self.committed + amt > self.approval_threshold:
                return {"decision": "escalated"}
            self.committed += amt
        return {"decision": "approved"}
