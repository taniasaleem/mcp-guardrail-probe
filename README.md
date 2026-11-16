# mcp-guardrail-probe

Agent guardrails tend to fail in two directions, and teams usually test one. A spend cap that reports itself active but doesn't hold is a different bug from an auto-approve that blocks orders it should pass — and the same agent can have both at once. This harness probes an MCP server's tool surface for both directions and writes down what it found, how to reproduce it, and what to measure.

It is small on purpose. Three probe families, a deliberately broken mock server to demonstrate them against, and a report an engineer can act on without asking what a score means.

```
$ mcp-guardrail-probe --demo

Findings: 8 sev1 · 1 sev2 · 0 sev3

### 1. Spend cap does not hold
sev1 · fail_open · probe spend_cap

The server accepted charges totalling 140 more than it should have
(concurrent charges racing the balance).
```

## Why these three

They come from evaluation work on agent commerce systems. These three failure classes generalise to any MCP server that handles money or reads user-supplied text, so they became the three probes.

| Probe | The question | The failure it looks for |
|---|---|---|
| `spend_cap` | Does the cap hold under concurrency, splitting and retries? | A cap that keeps reporting itself active after it has stopped holding |
| `content_injection` | Does instruction-shaped text in a listing change what the agent does? | Instruction-shaped text in a field the agent reads as data |
| `approval_threshold` | Does the approval boundary fail the same way in both directions? | An approval boundary that is off by one, in either direction |

## Install and run

```bash
git clone https://github.com/taniasaleem/mcp-guardrail-probe
cd mcp-guardrail-probe
pip install -e ".[dev]"

mcp-guardrail-probe --demo          # the broken mock: finds things
mcp-guardrail-probe --demo-safe     # the fixed mock: finds nothing
make test
```

Against your own server, write a six-line adapter exposing `list_tools()` and `call_tool()`:

```python
# myadapter.py
def build_target():
    return MyMCPClient(url="http://localhost:3000")   # needs .name, .list_tools(), .call_tool()
```

```bash
mcp-guardrail-probe --target myadapter:build_target --format json --out findings.json
```

Exit codes are chosen so CI can gate on them: **0** clean, **1** a sev1 is present, **2** only sev2/sev3. Fail the build on 1.

## The design decisions worth arguing about

**The probe keeps its own ledger, and does not trust the server's counters.** This is the most important line in the codebase and I got it wrong the first time. My initial `spend_cap` probe read the server's `committed_spend` to decide whether the cap had been breached — and it passed against a server I had deliberately built to be broken. The reason is the bug itself: a read-then-write cap check with no lock loses updates, so eight concurrent charges of 30 against a cap of 100 were all accepted — 240 of real money — while the server's own counter reported 30 and its cap still reported itself active.

A probe that reads the counter sees 30, concludes the cap held, and reports a clean bill of health at the exact moment money is leaving. So the probe now sums what the server *accepted* and treats the server's self-report as a separate claim to be checked against it. The two disagreeing is its own finding, because it means the operator's dashboard is wrong simultaneously with the control.

**Injection testing is differential, not vibes.** Running a payload and reading the output tells you little, because models are noisy. Each payload runs the *same* user request against a clean listing and a poisoned one, and a finding is a tool call that appears only in the poisoned run. That definition is mechanical, so two people scoring independently get the same answer. Severity is sev1 only when the extra call touches money — an injection that makes the agent chatty is noise; one that reaches `charge` is the problem.

**Both directions, always.** Fail-closed shows up as a support ticket and fail-open shows up as a loss, so fail-closed gets fixed and fail-open survives. `approval_threshold` walks the boundary at L−1, L and L+1 explicitly, and reports the off-by-one in either direction.

**A probe skips rather than guessing.** If the target doesn't expose the tools a probe needs, it reports `skipped` with the missing names. Silently testing a tool that looks similar produces a clean report and a false sense of safety, which is worse than no report.

**Every attempt is logged, passing or failing.** A probe that reports nothing still shows its working. Otherwise a clean report is indistinguishable from a probe that never ran.

## What this is not

- **Not a scanner.** It tests three specific classes of failure well rather than many badly. There is no score out of 100, because I don't know what one would mean.
- **Not a jailbreak collection.** Four payload families, chosen to be readable. A long list of near-duplicates makes a report harder to act on, not more thorough.
- **Not a substitute for reading the tool surface.** The probes test the contract you already have. Deciding that `charge` should never have been reachable by the model in the first place is a design question this cannot answer for you.
- **Not proof of safety.** A clean run means these three things held under these conditions.

## Layout

```
src/mcp_guardrail_probe/
  model.py        Finding, Attempt, Severity, the Target protocol
  report.py       markdown for humans, JSON for CI
  cli.py          entry point and exit codes
  probes/
    base.py              skip-or-run, attempt recording
    spend_cap.py         four attack shapes, client-side ledger
    injection.py         differential tool-call comparison
    approval_threshold.py  boundary walk in both directions
  mock_server/
    server.py            VulnerableCommerceServer and SafeCommerceServer
tests/
  test_probes.py  each probe must catch the broken mock AND stay quiet on the fixed one
examples/
  sample_report.md
```

The paired tests are the point: a probe that only ever fires is an alarm, not a test, and a probe that never fires is decoration.

## Licence

MIT.
