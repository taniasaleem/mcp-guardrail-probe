"""Command line entry point.

    mcp-guardrail-probe --demo
    mcp-guardrail-probe --demo-safe
    mcp-guardrail-probe --target mymodule:build_target --format json
    mcp-guardrail-probe --mcp-config servers/my-server.json

Exit codes are chosen so CI can gate on them: 0 clean, 1 sev1 present,
2 only sev2/sev3, 3 nothing was tested (every probe skipped), 4 the run did
not complete (the target could not be loaded or reached, or a call failed in
a way that is not a refusal). A build should fail on 1, 3 and 4, and probably
warn on 2.
"""

from __future__ import annotations

import argparse
import importlib
import sys
import traceback
from typing import Any, NoReturn

from .mcp_target import ConfigError, McpTargetError
from .model import ProbeResult, Target, ToolError
from .probes import ALL_PROBES
from .report import summarise, to_json, to_markdown

#: The run did not complete. Never 1: a crash must not read as "sev1 found".
EXIT_RUN_ABORTED = 4

#: Failures with a message that says enough on its own. Anything else also
#: aborts the run, with its traceback, because it is a bug in an adapter or
#: here.
_EXPECTED_FAILURES = (ToolError, McpTargetError, ConfigError)


def _abort(message: str) -> NoReturn:
    print(f"mcp-guardrail-probe: {message}", file=sys.stderr)
    raise SystemExit(EXIT_RUN_ABORTED)


def load_target(spec: str) -> Target:
    """Load a target from 'module:callable'.

    The callable takes no arguments and returns something satisfying Target.
    Keeping this a plain import rather than a config file means the adapter
    for a real server is ordinary code the user can debug.
    """
    if ":" not in spec:
        _abort(f"--target must look like 'module:callable', got {spec!r}")
    module_name, attr = spec.split(":", 1)
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        _abort(f"could not import {module_name!r}: {exc}")
    try:
        factory = getattr(module, attr)
    except AttributeError:
        _abort(f"{module_name!r} has no attribute {attr!r}")
    try:
        target = factory()
    except Exception as exc:
        traceback.print_exc()
        _abort(f"{spec} raised {type(exc).__name__}: {exc}")
    if not isinstance(target, Target):
        _abort(
            f"{spec} returned {type(target).__name__}, which does not provide "
            "list_tools() and call_tool()"
        )
    return target


def load_mcp_target(path: str) -> Target:
    """Connect to a real MCP server described by a JSON mapping file."""
    from .mcp_target import McpTarget

    try:
        return McpTarget.from_file(path)
    except (ConfigError, McpTargetError) as exc:
        _abort(f"{path}: {exc}")


def run(target: Target, probe_names: list[str]) -> list[ProbeResult]:
    """Run each probe in turn, and stop at the first one that fails.

    A failure is anything a probe raises: a timeout or dropped connection
    (the charge may have committed), a failed reset, a setup call the server
    refused, a mapping that cannot express an amount, or a bug. The probe is
    recorded as aborted with the attempts it finished, the probes after it as
    not run, and exit_code() returns 4. The target's state is unknown after a
    failure, so nothing else runs against it.
    """
    for name in probe_names:
        if name not in ALL_PROBES:
            raise SystemExit(
                f"unknown probe {name!r}; available: {', '.join(ALL_PROBES)}"
            )
    results: list[ProbeResult] = []
    for i, name in enumerate(probe_names):
        probe = ALL_PROBES[name]()
        try:
            if callable(getattr(target, "reset", None)):
                target.reset()  # type: ignore[attr-defined]
            results.append(probe.run(target))
        except Exception as exc:
            if not isinstance(exc, _EXPECTED_FAILURES):
                traceback.print_exc()
            reason = f"{type(exc).__name__}: {exc}"
            print(f"mcp-guardrail-probe: run aborted during {name}: {reason}", file=sys.stderr)
            results.append(
                ProbeResult(
                    probe=name,
                    target=target.name,
                    aborted_reason=reason,
                    attempts=probe.attempts,
                )
            )
            results += [
                ProbeResult(
                    probe=rest,
                    target=target.name,
                    skipped_reason=f"not run: the run stopped during {name}",
                )
                for rest in probe_names[i + 1 :]
            ]
            break
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mcp-guardrail-probe",
        description="Red-team an MCP server's tool surface for guardrail failures.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--target",
        help=(
            "module:callable returning a Target. The probes make real tool "
            "calls, including charges: point this only at a sandbox you own"
        ),
    )
    source.add_argument(
        "--mcp-config",
        metavar="FILE",
        help=(
            "JSON file describing a real MCP server and how its tools map onto "
            "the probes' (needs the [mcp] extra). Same warning as --target"
        ),
    )
    source.add_argument(
        "--demo", action="store_true", help="run against the bundled vulnerable mock"
    )
    source.add_argument(
        "--demo-safe", action="store_true", help="run against the bundled fixed mock"
    )
    parser.add_argument(
        "--probe",
        action="append",
        dest="probes",
        choices=list(ALL_PROBES),
        help="run only this probe (repeatable); default is all",
    )
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--out", help="write the report here instead of stdout")
    args = parser.parse_args(argv)

    if args.demo or args.demo_safe:
        from .mock_server import SafeCommerceServer, VulnerableCommerceServer

        target: Any = SafeCommerceServer() if args.demo_safe else VulnerableCommerceServer()
    elif args.mcp_config:
        target = load_mcp_target(args.mcp_config)
    else:
        target = load_target(args.target)

    try:
        results = run(target, args.probes or list(ALL_PROBES))
    finally:
        close = getattr(target, "close", None)
        if callable(close):
            try:
                close()
            except Exception as exc:
                # The readings are complete; a server slow to shut down does
                # not change them, so it must not change the exit code either.
                print(f"mcp-guardrail-probe: closing the target failed: {exc}", file=sys.stderr)
    rendered = to_json(results) if args.format == "json" else to_markdown(results)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(rendered + "\n")
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(rendered)

    return exit_code(results)


def exit_code(results: list[ProbeResult]) -> int:
    """0 clean, 1 sev1 present, 2 only sev2/sev3, 3 nothing was tested,
    4 the run did not complete.

    3 exists so a target exposing none of the tools the probes need cannot
    pass a CI gate. With every probe skipped there are no findings, which
    would otherwise come back as 0, the same signal as a clean run.

    4 wins over everything else, a sev1 included: the counts of a run that
    stopped part-way are not the server's counts. It is never 1, which is
    what an uncaught exception would have exited with.
    """
    if any(r.aborted for r in results):
        return EXIT_RUN_ABORTED
    counts = summarise(results)
    if counts["sev1"]:
        return 1
    if counts["sev2"] or counts["sev3"]:
        return 2
    if results and all(r.skipped for r in results):
        return 3
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
