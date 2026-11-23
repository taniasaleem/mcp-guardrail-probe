"""Report rendering.

Two formats, for two readers. Markdown is for the engineer who has to fix it,
so every finding carries reproduction steps and the attempt log. JSON is for
CI, so a pipeline can fail a build on `sev1 > 0` without parsing prose.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .model import ProbeResult, Severity

_ORDER = {Severity.SEV1: 0, Severity.SEV2: 1, Severity.SEV3: 2}


def _cell(text: str) -> str:
    """Text for one attempt-log cell. It can quote a server, so a | must not
    add a column and a line break must not start a row."""
    return " ".join(str(text).split("\n")).replace("\r", " ").replace("|", "\\|")


def summarise(results: list[ProbeResult]) -> dict[str, int]:
    counts = {s.value: 0 for s in Severity}
    for result in results:
        for finding in result.findings:
            counts[finding.severity.value] += 1
    return counts


def probe_counts(results: list[ProbeResult]) -> dict[str, int]:
    skipped = sum(1 for r in results if r.skipped)
    aborted = sum(1 for r in results if r.aborted)
    return {"run": len(results) - skipped - aborted, "skipped": skipped, "aborted": aborted}


def to_json(results: list[ProbeResult]) -> str:
    return json.dumps(
        {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "summary": summarise(results),
            "probes": probe_counts(results),
            "results": [r.to_dict() for r in results],
        },
        indent=2,
        default=str,
    )


def to_markdown(results: list[ProbeResult]) -> str:
    counts = summarise(results)
    probes = probe_counts(results)
    target = next((r.target for r in results), "unknown")
    aborted = [r for r in results if r.aborted]
    probe_line = f"**Probes:** {probes['run']} run · {probes['skipped']} skipped"
    if aborted:
        probe_line += f" · {probes['aborted']} aborted"
    lines: list[str] = [
        "# MCP guardrail probe report",
        "",
        f"**Target:** `{target}`  ",
        f"**Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}  ",
        probe_line + "  ",
        f"**Findings:** {counts['sev1']} sev1 · {counts['sev2']} sev2 · {counts['sev3']} sev3",
        "",
    ]

    if aborted:
        lines += [
            f"> **This run did not complete.** `{aborted[0].probe}` stopped part-way: "
            f"{aborted[0].aborted_reason}. The findings below come only from the "
            "probes that finished; the aborted probe's attempt log shows how far it "
            "got. This is not a clean result.",
            "",
        ]

    if counts["sev1"]:
        lines += [
            "> A sev1 means a control that should have held did not. "
            "There is no margin at which that is acceptable, because the next "
            "caller chooses the margin.",
            "",
        ]

    findings = [f for r in results for f in r.findings]
    findings.sort(key=lambda f: _ORDER[f.severity])

    if not findings and aborted:
        lines += ["No findings from the probes that finished.", ""]
    elif not findings and results and probes["run"] == 0:
        lines += [
            "**Nothing was tested.** Every probe was skipped because the target "
            "does not expose the tools it needs (see below). This is not a clean "
            "result.",
            "",
        ]
    elif not findings and probes["skipped"]:
        lines += [
            f"No findings from the probes that ran. {probes['skipped']} of "
            f"{len(results)} probes were skipped (see below).",
            "",
        ]
    elif not findings:
        lines += ["No findings. Every probe that ran completed without a failure.", ""]
    else:
        lines += ["## Findings", ""]
        for i, f in enumerate(findings, 1):
            direction = f" · {f.direction.value}" if f.direction else ""
            lines += [
                f"### {i}. {f.title}",
                "",
                f"`{f.severity.value}`{direction} · probe `{f.probe}`",
                "",
                f.summary,
                "",
                "**Reproduction**",
                "",
            ]
            lines += [f"{n}. {step}" for n, step in enumerate(f.reproduction, 1)]
            lines += ["", "**Measurement**", ""]
            lines += [f"- `{k}`: {v}" for k, v in f.metric.items()]
            lines += [""]

    lines += ["## Probes run", ""]
    for r in results:
        if r.skipped:
            lines.append(f"- `{r.probe}` — **skipped**: {r.skipped_reason}")
        elif r.aborted:
            lines.append(
                f"- `{r.probe}` — **aborted** after {len(r.attempts)} attempts: "
                f"{r.aborted_reason}"
            )
        else:
            lines.append(
                f"- `{r.probe}` — {len(r.attempts)} attempts, {len(r.findings)} findings"
            )
    lines.append("")

    attempts = [(r.probe, a) for r in results for a in r.attempts]
    if attempts:
        lines += [
            "## Attempt log",
            "",
            "Every interaction, passing or failing. A probe that reports nothing "
            "should still show its working, otherwise a clean report is "
            "indistinguishable from a probe that never ran.",
            "",
            "| id | description | expected | actual | ok |",
            "|---|---|---|---|---|",
        ]
        for probe, a in attempts:
            ok = "–" if a.passed is None else "✓" if a.passed else "✗"
            lines.append(
                f"| `{a.attempt_id}` | {_cell(a.description)} | {_cell(a.expected)} "
                f"| {_cell(a.actual)} | {ok} |"
            )
        lines.append("")

    return "\n".join(lines)
