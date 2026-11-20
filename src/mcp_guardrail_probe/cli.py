"""Command line entry point.

    mcp-guardrail-probe --demo
    mcp-guardrail-probe --demo-safe
    mcp-guardrail-probe --target mymodule:build_target --format json

Exit codes are chosen so CI can gate on them: 0 clean, 1 sev1 present,
2 only sev2/sev3, 3 nothing was tested (every probe skipped). A build
should fail on 1 and 3, and probably warn on 2.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from typing import Any

from .model import ProbeResult, Target
from .probes import ALL_PROBES
from .report import summarise, to_json, to_markdown


def load_target(spec: str) -> Target:
    """Load a target from 'module:callable'.

    The callable takes no arguments and returns something satisfying Target.
    Keeping this a plain import rather than a config file means the adapter
    for a real server is ordinary code the user can debug.
    """
    if ":" not in spec:
        raise SystemExit(f"--target must look like 'module:callable', got {spec!r}")
    module_name, attr = spec.split(":", 1)
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise SystemExit(f"could not import {module_name!r}: {exc}") from None
    try:
        factory = getattr(module, attr)
    except AttributeError:
        raise SystemExit(f"{module_name!r} has no attribute {attr!r}") from None
    target = factory()
    if not isinstance(target, Target):
        raise SystemExit(
            f"{spec} returned {type(target).__name__}, which does not provide "
            "list_tools() and call_tool()"
        )
    return target


def run(target: Target, probe_names: list[str]) -> list[ProbeResult]:
    results: list[ProbeResult] = []
    for name in probe_names:
        probe_cls = ALL_PROBES.get(name)
        if probe_cls is None:
            raise SystemExit(
                f"unknown probe {name!r}; available: {', '.join(ALL_PROBES)}"
            )
        if hasattr(target, "reset"):
            target.reset()  # type: ignore[attr-defined]
        results.append(probe_cls().run(target))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mcp-guardrail-probe",
        description="Red-team an MCP server's tool surface for guardrail failures.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--target", help="module:callable returning a Target")
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
    else:
        target = load_target(args.target)

    results = run(target, args.probes or list(ALL_PROBES))
    rendered = to_json(results) if args.format == "json" else to_markdown(results)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(rendered + "\n")
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(rendered)

    return exit_code(results)


def exit_code(results: list[ProbeResult]) -> int:
    """0 clean, 1 sev1 present, 2 only sev2/sev3, 3 nothing was tested.

    3 exists so a target exposing none of the tools the probes need cannot
    pass a CI gate. With every probe skipped there are no findings, which
    would otherwise come back as 0, the same signal as a clean run.
    """
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
