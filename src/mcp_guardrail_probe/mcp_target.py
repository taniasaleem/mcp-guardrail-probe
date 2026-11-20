"""A Target backed by a real MCP server, over stdio or streamable HTTP.

    pip install -e ".[mcp]"
    mcp-guardrail-probe --mcp-config servers/my-server.json

The probes speak a fixed vocabulary (`charge`, `set_spending_limit`,
`submit_for_approval`, ...). A real server has its own names, its own argument
shapes, and its own way of saying no. The mapping file bridges the two, and it
is written down rather than guessed: a probe that quietly tested a tool that
merely looked similar would produce a clean report about the wrong thing.

Three ways a real server says no, all of which become a `ToolError` here so
the probes count them as refusals:

  * an `isError: true` result, which is how MCP reports a failed tool call;
  * a JSON-RPC error (unknown tool, invalid params);
  * an ordinary result that means no, such as `{"status": "denied"}`. That one
    needs a `refuse_when` rule, because only the mapping knows what "denied"
    looks like on this server.

A call whose outcome is unknown is *not* a refusal: one that timed out, or
whose connection dropped before the answer came back. The charge may have
committed, so counting it as refused could hide an overspend. It raises
`McpTargetError` and stops the run. Any other JSON-RPC error is an answer
from a live server that declined the call, and counts as a refusal.

The SDK is imported only when a target connects, so the core package keeps
its zero dependencies.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .model import ToolError
from .probes import ALL_PROBES

#: Every tool name a probe asks for. A mapping for anything else is a typo.
PROBE_TOOLS = frozenset(
    tool
    for probe in ALL_PROBES.values()
    for tool in (*probe.required_tools, *probe.optional_tools)
)

#: JSON-RPC error codes that mean "no answer", not "no". From the MCP spec's
#: SDK conventions (mcp_types.jsonrpc), kept here so this module imports
#: without the SDK.
_CONNECTION_CLOSED = -32000
_REQUEST_TIMEOUT = -32001

#: How long a closing session gets before it is cancelled, and how long the
#: cancellation gets to land. Module constants so tests can shorten them.
_CLOSE_WAIT_SECONDS = 15.0
_CANCEL_WAIT_SECONDS = 5.0

LIMIT_KEYS = frozenset({"spending_limit", "approval_threshold"})

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_TYPES = frozenset({"number", "integer", "string"})
_MISSING = object()


class ConfigError(ValueError):
    """The mapping file is malformed or refers to something that is not there."""


class McpTargetError(RuntimeError):
    """Something went wrong that is not a refusal: a timeout, a failed reset."""


# -- config ------------------------------------------------------------


def _expand(value: Any) -> Any:
    """Substitute ${VAR} or ${VAR:-default} from the environment.

    Secrets and machine paths stay out of the file. An unset variable with no
    default is an error, never an empty string: an empty API key or command
    path fails later and less clearly.
    """
    if isinstance(value, str):

        def sub(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            if name in os.environ:
                return os.environ[name]
            if default is not None:
                return default
            raise ConfigError(f"refers to ${{{name}}}, which is not set in the environment")

        return _VAR.sub(sub, value)
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


def _check_keys(where: str, data: Any, allowed: set[str]) -> dict[str, Any]:
    """Reject unknown keys, so a typo fails loudly instead of being ignored.

    `_comment` is allowed anywhere: JSON has no comments, and a mapping file
    should say why each choice was made.
    """
    if not isinstance(data, dict):
        raise ConfigError(f"{where} must be an object")
    unknown = set(data) - allowed - {"_comment"}
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {sorted(unknown)}; allowed: {sorted(allowed)}")
    return data


@dataclass(frozen=True)
class RefuseRule:
    """When an ordinary result means the server said no."""

    path: str
    present: bool = False
    in_: tuple[Any, ...] | None = None
    not_in: tuple[Any, ...] | None = None

    @classmethod
    def parse(cls, where: str, data: Any) -> RefuseRule:
        data = _check_keys(where, data, {"path", "present", "in", "not_in"})
        if not isinstance(data.get("path"), str):
            raise ConfigError(f"{where}: 'path' is required")
        tests = [k for k in ("present", "in", "not_in") if k in data]
        if len(tests) != 1:
            raise ConfigError(f"{where}: give exactly one of 'present', 'in', 'not_in'")
        if "present" in data and data["present"] is not True:
            raise ConfigError(f"{where}: 'present' must be true")
        for key in ("in", "not_in"):
            if key in data and not isinstance(data[key], list):
                raise ConfigError(f"{where}: '{key}' must be a list")
        return cls(
            path=data["path"],
            present="present" in data,
            in_=tuple(data["in"]) if "in" in data else None,
            not_in=tuple(data["not_in"]) if "not_in" in data else None,
        )

    def refuses(self, result: dict[str, Any]) -> str | None:
        """A reason if this rule says the result is a refusal, else None."""
        value = _get_path(result, self.path)
        if self.present:
            return None if value is _MISSING or value is None else f"{self.path} is present"
        if self.in_ is not None:
            return f"{self.path}={value!r}" if _json_in(value, self.in_) else None
        # not_in: a missing field counts as a refusal. If the field the mapping
        # names is not there, the mapping is wrong, and the safe reading of a
        # charge we cannot interpret is "not accepted" plus a visible reason,
        # which the spend probe reports as failing closed.
        if value is _MISSING:
            return f"{self.path} is missing from the result"
        return None if _json_in(value, self.not_in or ()) else f"{self.path}={value!r}"


def _json_in(value: Any, options: tuple[Any, ...]) -> bool:
    """Membership as JSON sees it: true is not 1, though Python says it is."""
    for option in options:
        if isinstance(value, bool) or isinstance(option, bool):
            if isinstance(value, bool) and isinstance(option, bool) and value == option:
                return True
        elif value == option:
            return True
    return False


def _map_key(value: Any) -> str:
    """A JSON value as a config author writes it as an object key.

    JSON object keys are strings, so a map written {"true": ...}, {"null": ...}
    or {"2": ...} has to match the values true, null and 2. str() gives
    "True" and "None", which no one writes in a JSON file.
    """
    return value if isinstance(value, str) else json.dumps(value)


@dataclass(frozen=True)
class ResultField:
    path: str
    map: dict[str, Any] | None = None


@dataclass(frozen=True)
class ToolMapping:
    """How one probe tool reaches one server tool."""

    tool: str
    rename: dict[str, str] = field(default_factory=dict)
    fixed: dict[str, Any] = field(default_factory=dict)
    drop: frozenset[str] = frozenset()
    types: dict[str, str] = field(default_factory=dict)
    refuse_when: tuple[RefuseRule, ...] = ()
    result: dict[str, ResultField] = field(default_factory=dict)

    @classmethod
    def parse(cls, probe_tool: str, data: Any) -> ToolMapping:
        where = f"tools.{probe_tool}"
        data = _check_keys(
            where, data, {"tool", "rename", "fixed", "drop", "types", "refuse_when", "result"}
        )
        types = dict(data.get("types", {}))
        bad = {k: v for k, v in types.items() if v not in _TYPES}
        if bad:
            raise ConfigError(f"{where}.types: {bad}; each must be one of {sorted(_TYPES)}")
        result: dict[str, ResultField] = {}
        for name, spec in dict(data.get("result", {})).items():
            if isinstance(spec, str):
                result[name] = ResultField(path=spec)
            else:
                spec = _check_keys(f"{where}.result.{name}", spec, {"path", "map"})
                if not isinstance(spec.get("path"), str):
                    raise ConfigError(f"{where}.result.{name}: 'path' is required")
                result[name] = ResultField(path=spec["path"], map=spec.get("map"))
        return cls(
            tool=str(data.get("tool", probe_tool)),
            rename=dict(data.get("rename", {})),
            fixed=dict(data.get("fixed", {})),
            drop=frozenset(data.get("drop", [])),
            types=types,
            refuse_when=tuple(
                RefuseRule.parse(f"{where}.refuse_when[{i}]", rule)
                for i, rule in enumerate(data.get("refuse_when", []))
            ),
            result=result,
        )

    def arguments(self, probe_tool: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        args: dict[str, Any] = {}
        for key, value in kwargs.items():
            if key in self.drop:
                continue
            args[self.rename.get(key, key)] = value
        clash = set(args) & set(self.fixed)
        if clash:
            # A fixed value that overrode a probe argument would quietly turn
            # every charge into the same charge.
            raise ConfigError(
                f"tools.{probe_tool}.fixed sets {sorted(clash)}, which the probe also sends"
            )
        args.update(self.fixed)
        for key, kind in self.types.items():
            if key in args:
                args[key] = _coerce(probe_tool, key, args[key], kind)
        return args

    def interpret(self, result: dict[str, Any]) -> dict[str, Any]:
        """Raise ToolError if this result is a refusal; otherwise map its fields."""
        for rule in self.refuse_when:
            reason = rule.refuses(result)
            if reason:
                detail = json.dumps(result, default=str)
                if len(detail) > 300:
                    detail = detail[:297] + "..."
                raise ToolError(f"{self.tool} refused ({reason}): {detail}", result)
        out = dict(result)
        for name, spec in self.result.items():
            value = _get_path(result, spec.path)
            if value is _MISSING:
                continue
            if spec.map is not None:
                value = spec.map.get(_map_key(value), value)
            out[name] = value
        return out


def _coerce(probe_tool: str, key: str, value: Any, kind: str) -> Any:
    if kind == "string":
        return str(value)
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ConfigError(f"tools.{probe_tool}.types.{key}: {value!r} is not a number") from None
    if kind == "integer":
        if number != number.to_integral_value():
            raise ConfigError(
                f"tools.{probe_tool}.types.{key}: {value!r} is not a whole number. "
                "The probes size amounts from the limit (x1.5, x0.3, x0.4, L-1, L+1); "
                "for a server that counts whole units, use a limit that is a multiple of 10"
            )
        return int(number)
    return int(number) if number == number.to_integral_value() else float(number)


def _get_path(data: Any, path: str) -> Any:
    for part in path.split("."):
        if isinstance(data, dict) and part in data:
            data = data[part]
        elif isinstance(data, list) and part.isdigit() and int(part) < len(data):
            data = data[int(part)]
        else:
            return _MISSING
    return data


@dataclass(frozen=True)
class McpConfig:
    name: str
    transport: str
    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = 60.0
    reset: dict[str, Any] | None = None
    out_of_band_limits: dict[str, Decimal] = field(default_factory=dict)
    tools: dict[str, ToolMapping] = field(default_factory=dict)
    base_dir: Path = Path(".")

    @property
    def takes_idempotency_key(self) -> bool:
        """False when the charge mapping drops idempotency_key.

        Dropping it is how a mapping says the server has none: the key never
        reaches the server, so the spend probe's retry shape would test
        nothing, and is reported as not supported instead.
        """
        charge = self.tools.get("charge")
        return charge is None or "idempotency_key" not in charge.drop

    @classmethod
    def from_file(cls, path: str | os.PathLike[str]) -> McpConfig:
        path = Path(path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ConfigError(f"no such file: {path}") from None
        except json.JSONDecodeError as exc:
            raise ConfigError(f"not valid JSON: {exc}") from None
        return cls.from_dict(raw, base_dir=path.resolve().parent)

    @classmethod
    def from_dict(cls, raw: Any, base_dir: Path = Path(".")) -> McpConfig:
        raw = _check_keys(
            "config",
            raw,
            {"name", "server", "timeout_seconds", "reset", "out_of_band_limits", "tools"},
        )
        server = _check_keys(
            "server",
            _expand(raw.get("server")),
            {"transport", "command", "args", "env", "cwd", "url", "headers"},
        )
        transport = server.get("transport")
        if transport == "stdio":
            if not isinstance(server.get("command"), str):
                raise ConfigError("server.command is required for stdio")
            for key in ("url", "headers"):
                if key in server:
                    raise ConfigError(f"server.{key} applies to http, not stdio")
        elif transport == "http":
            if not isinstance(server.get("url"), str):
                raise ConfigError("server.url is required for http")
            for key in ("command", "args", "env", "cwd"):
                if key in server:
                    raise ConfigError(f"server.{key} applies to stdio, not http")
        else:
            raise ConfigError("server.transport must be 'stdio' or 'http'")

        reset = raw.get("reset")
        if reset is not None:
            reset = _check_keys("reset", _expand(reset), {"tool", "args", "restart", "command"})
            kinds = [k for k in ("tool", "restart", "command") if k in reset]
            if len(kinds) != 1:
                raise ConfigError("reset: give exactly one of 'tool', 'restart', 'command'")
            if "args" in reset and "tool" not in reset:
                raise ConfigError("reset.args goes with reset.tool")
            if "restart" in reset and reset["restart"] is not True:
                raise ConfigError("reset.restart must be true")
            if "restart" in reset and transport == "http":
                # Over http a restart only reopens the session; the server and
                # its state carry on, yet every reading would claim fresh state.
                raise ConfigError(
                    "reset.restart only works for stdio, where it starts a new server "
                    "process. Over http it would reconnect to the same server, whose "
                    "state carries over. Use reset.tool, or reset.command to restart "
                    "the server yourself"
                )
            if "command" in reset and not (
                isinstance(reset["command"], list) and reset["command"]
            ):
                raise ConfigError("reset.command must be a non-empty list, e.g. [\"./reset.sh\"]")

        limits_raw = _check_keys("out_of_band_limits", raw.get("out_of_band_limits", {}), set(LIMIT_KEYS))
        limits: dict[str, Decimal] = {}
        for key, value in limits_raw.items():
            try:
                limits[key] = Decimal(str(value))
            except InvalidOperation:
                raise ConfigError(f"out_of_band_limits.{key}: {value!r} is not a number") from None
            if limits[key] <= 0:
                raise ConfigError(f"out_of_band_limits.{key} must be positive")
        if limits.get("approval_threshold", 2) <= 1:
            # approval_threshold tests L-1; the probe would skip at run time.
            raise ConfigError(
                "out_of_band_limits.approval_threshold must be above 1: the probe "
                "tests L-1, L and L+1, and L-1 must be an amount above zero"
            )

        tools_raw = raw.get("tools", {})
        if not isinstance(tools_raw, dict):
            raise ConfigError("tools must be an object")
        unknown = set(tools_raw) - PROBE_TOOLS
        if unknown:
            raise ConfigError(
                f"tools: {sorted(unknown)} is not a tool any probe uses; "
                f"map one of {sorted(PROBE_TOOLS)}"
            )
        tools = {name: ToolMapping.parse(name, _expand(spec)) for name, spec in tools_raw.items()}

        default_name = (
            f"mcp-stdio:{server['command']}" if transport == "stdio" else f"mcp-http:{server['url']}"
        )
        return cls(
            name=str(raw.get("name", default_name)),
            transport=transport,
            command=server.get("command"),
            args=tuple(str(a) for a in server.get("args", [])),
            env={str(k): str(v) for k, v in server.get("env", {}).items()},
            cwd=server.get("cwd"),
            url=server.get("url"),
            headers={str(k): str(v) for k, v in server.get("headers", {}).items()},
            timeout_seconds=float(raw.get("timeout_seconds", 60.0)),
            reset=reset,
            out_of_band_limits=limits,
            tools=tools,
            base_dir=base_dir,
        )


# -- target ------------------------------------------------------------


def _require_sdk() -> Any:
    try:
        import mcp  # noqa: F401
        from mcp import Client, StdioServerParameters
        from mcp.shared.exceptions import MCPError
    except ImportError as exc:
        raise McpTargetError(
            "running against an MCP server needs the MCP Python SDK: "
            "pip install -e '.[mcp]'  (or: pip install 'mcp>=2.3,<3')"
        ) from exc
    return Client, StdioServerParameters, MCPError


class McpTarget:
    """A probe Target that drives a real MCP server through the official SDK.

    The SDK is async; the Target protocol is not, and the spend probe calls it
    from eight threads at once. So one background event loop owns the session,
    and every call is submitted to it. Concurrent calls stay concurrent on the
    wire: eight in-flight requests on one session, which is what a cap check
    with no lock fails under.
    """

    def __init__(self, config: McpConfig) -> None:
        self.config = config
        self.name = config.name
        self.out_of_band_limits = dict(config.out_of_band_limits)
        self.takes_idempotency_key = config.takes_idempotency_key
        if config.reset is not None:
            # Only a target that can really reset says it can. Probes look for
            # reset() to decide whether a reading started from fresh state.
            self.reset = self._reset
        self._sdk = _require_sdk()
        self._server_tools: list[str] = []
        self._client: Any = None
        self._stop: asyncio.Event | None = None
        self._owner: asyncio.Task[None] | None = None
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="mcp-target", daemon=True
        )
        self._thread.start()
        try:
            self._connect()
            self._warn_unmapped()
            self._warn_no_refusal_rule()
        except BaseException:
            self.close()
            raise

    @classmethod
    def from_file(cls, path: str | os.PathLike[str]) -> McpTarget:
        return cls(McpConfig.from_file(path))

    # -- Target protocol -----------------------------------------------

    def list_tools(self) -> list[str]:
        """Probe tool names this server can serve, through the mapping or as-is."""
        available = set(self._server_tools)
        names = []
        for probe_tool in sorted(PROBE_TOOLS):
            mapping = self.config.tools.get(probe_tool)
            server_tool = mapping.tool if mapping else probe_tool
            if server_tool in available:
                names.append(probe_tool)
        return names

    def call_tool(self, tool: str, **kwargs: Any) -> dict[str, Any]:
        mapping = self.config.tools.get(tool) or ToolMapping(tool=tool)
        args = mapping.arguments(tool, kwargs)
        result = self._call(mapping.tool, args)
        return mapping.interpret(result)

    # -- lifecycle -----------------------------------------------------

    def close(self) -> None:
        if self._loop.is_closed():
            return
        try:
            self._disconnect()
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=10)
            if not self._thread.is_alive():
                self._loop.close()

    def __enter__(self) -> McpTarget:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _reset(self) -> None:
        reset = self.config.reset or {}
        if "tool" in reset:
            try:
                self._call(str(reset["tool"]), dict(reset.get("args", {})))
            except ToolError as exc:
                raise McpTargetError(f"reset tool {reset['tool']!r} failed: {exc}") from exc
            return
        if "command" in reset:
            self._disconnect()
            command = [str(part) for part in reset["command"]]
            try:
                done = subprocess.run(
                    command,
                    cwd=self.config.base_dir,
                    capture_output=True,
                    text=True,
                    timeout=max(self.config.timeout_seconds, 120.0),
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise McpTargetError(f"reset command {command} failed: {exc}") from exc
            output = (done.stdout + done.stderr).strip()
            if done.returncode != 0:
                raise McpTargetError(
                    f"reset command {command} exited {done.returncode}: {output[-500:]}"
                )
            if output:
                print(output, file=sys.stderr)
            self._connect()
            return
        # restart: a new stdio server process (from_dict refuses it for http).
        self._disconnect()
        self._connect()

    # -- plumbing ------------------------------------------------------

    def _submit(self, coro: Any, timeout: float) -> Any:
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise McpTargetError(f"no answer from the MCP server within {timeout:.0f}s") from None

    def _transport(self) -> Any:
        _, StdioServerParameters, _ = self._sdk
        cfg = self.config
        if cfg.transport == "stdio":
            cwd = None
            if cfg.cwd:
                cwd = Path(cfg.cwd)
                if not cwd.is_absolute():
                    cwd = cfg.base_dir / cwd
            return StdioServerParameters(
                command=cfg.command or "", args=list(cfg.args), env=cfg.env or None, cwd=cwd
            )
        if not cfg.headers:
            return cfg.url
        return None  # built inside the loop, see _own_session

    async def _own_session(
        self, ready: concurrent.futures.Future[list[str]], stop: asyncio.Event
    ) -> None:
        """Hold one session open until told to stop.

        The SDK's context managers must be entered and exited in the same task,
        so a single long-lived task owns the session and every call borrows it.
        """
        Client, _, _ = self._sdk
        client = None
        try:
            transport = self._transport()
            async with _http_client(self.config) as http:
                server = transport if transport is not None else _streamable(self.config, http)
                async with Client(
                    server, read_timeout_seconds=self.config.timeout_seconds, cache=None
                ) as client:
                    names: list[str] = []
                    cursor = None
                    while True:
                        listing = await client.list_tools(cursor=cursor)
                        names.extend(tool.name for tool in listing.tools)
                        cursor = listing.next_cursor
                        if not cursor:
                            break
                    self._client = client
                    ready.set_result(names)
                    await stop.wait()
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(exc)
            if not isinstance(exc, Exception):
                raise
        finally:
            # Only clear our own client. A session slow to close can finish
            # after reset() has connected the next one, and must not wipe it.
            if client is not None and self._client is client:
                self._client = None

    def _connect(self) -> None:
        ready: concurrent.futures.Future[list[str]] = concurrent.futures.Future()

        async def start() -> None:
            self._stop = asyncio.Event()
            self._owner = asyncio.ensure_future(self._own_session(ready, self._stop))

        self._submit(start(), timeout=10)
        # Starting a server can take longer than answering one call does.
        wait = max(self.config.timeout_seconds, 30.0)
        try:
            self._server_tools = ready.result(timeout=wait)
        except concurrent.futures.TimeoutError:
            raise McpTargetError(f"could not connect to {self.name} within {wait:.0f}s") from None
        except Exception as exc:
            raise McpTargetError(f"could not connect to {self.name}: {exc!r}") from exc

    def _disconnect(self) -> None:
        stop, owner = self._stop, self._owner
        self._stop = self._owner = None
        if stop is None or owner is None:
            return

        async def finish() -> None:
            stop.set()
            done, _ = await asyncio.wait({owner}, timeout=_CLOSE_WAIT_SECONDS)
            if not done:
                # Cancel a session that will not close in time, rather than
                # leave it running beside the next one.
                owner.cancel()
                await asyncio.wait({owner}, timeout=_CANCEL_WAIT_SECONDS)

        self._submit(finish(), timeout=_CLOSE_WAIT_SECONDS + _CANCEL_WAIT_SECONDS + 5)

    def _call(self, server_tool: str, args: dict[str, Any]) -> dict[str, Any]:
        _, _, MCPError = self._sdk

        async def call() -> Any:
            client = self._client
            if client is None:
                raise McpTargetError(f"not connected to {self.name}")
            return await client.call_tool(
                server_tool, args, read_timeout_seconds=self.config.timeout_seconds
            )

        try:
            result = self._submit(call(), timeout=self.config.timeout_seconds + 10)
        except MCPError as exc:
            if exc.code == _REQUEST_TIMEOUT:
                raise McpTargetError(
                    f"{server_tool} did not answer within {self.config.timeout_seconds:.0f}s; "
                    "a charge may still have committed, so this is not counted as a refusal"
                ) from exc
            if exc.code == _CONNECTION_CLOSED:
                raise McpTargetError(
                    f"the connection closed before {server_tool} answered; "
                    "a charge may still have committed, so this is not counted as a refusal"
                ) from exc
            raise ToolError(f"{server_tool}: {exc.message}", {"code": exc.code}) from exc
        return parse_result(server_tool, result)

    def _warn_unmapped(self) -> None:
        available = set(self._server_tools)
        for probe_tool, mapping in sorted(self.config.tools.items()):
            if mapping.tool not in available:
                print(
                    f"mcp-guardrail-probe: {self.name}: mapping {probe_tool} -> {mapping.tool}, "
                    f"but the server exposes no tool {mapping.tool!r} "
                    f"(it has: {', '.join(sorted(available)) or 'none'})",
                    file=sys.stderr,
                )

    def _warn_no_refusal_rule(self) -> None:
        """Say so when a declined charge could only be recognised by isError.

        Without refuse_when, an ordinary result that means no (JSON such as
        {"status": "declined"}, or plain text) counts as accepted. The probe
        does not guess the other way: counting results it cannot read as
        refused could hide charges that went through, while counting them as
        accepted errs toward a loud sev1 that someone will look at.
        """
        if "charge" not in self.list_tools():
            return
        mapping = self.config.tools.get("charge") or ToolMapping(tool="charge")
        if mapping.refuse_when:
            return
        print(
            f"mcp-guardrail-probe: {self.name}: tools.charge has no refuse_when, so only "
            "isError results and JSON-RPC errors count as refused. A charge this server "
            "declines with an ordinary result (such as {\"status\": \"declined\"} or "
            "plain text) will be counted as accepted. That is fine if the server reports "
            "every refusal with isError.",
            file=sys.stderr,
        )


def parse_result(server_tool: str, result: Any) -> dict[str, Any]:
    """Turn a CallToolResult into the dict the probes read.

    JSON in the text content is the common case (it is what most servers send,
    and what the spec asks structured results to repeat). Failing that, the
    structured content. Failing that, the text itself under "text".
    """
    texts = [
        str(getattr(block, "text", ""))
        for block in (getattr(result, "content", None) or [])
        if getattr(block, "type", None) == "text"
    ]
    if getattr(result, "is_error", False):
        message = " ".join(t for t in texts if t).strip() or f"{server_tool} returned isError"
        raise ToolError(message, {"isError": True, "content": [{"type": "text", "text": t} for t in texts]})
    if len(texts) == 1:
        try:
            value = json.loads(texts[0])
        except ValueError:
            value = _MISSING
        if value is not _MISSING:
            return value if isinstance(value, dict) else {"result": value}
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return dict(structured)
    return {"text": "\n".join(texts)}


class _NullAsyncContext:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: Any) -> None:
        return None


def _http_client(config: McpConfig) -> Any:
    """An httpx client carrying the configured headers, or nothing."""
    if config.transport != "http" or not config.headers:
        return _NullAsyncContext()
    import httpx2

    return httpx2.AsyncClient(headers=config.headers, timeout=httpx2.Timeout(30.0, read=300.0))


def _streamable(config: McpConfig, http: Any) -> Any:
    from mcp.client.streamable_http import streamable_http_client

    return streamable_http_client(config.url or "", http_client=http)
