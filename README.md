# mcp-guardrail-probe

Agent guardrails tend to fail in two directions, and teams usually test one. A spend cap that reports itself active but doesn't hold is a different bug from an auto-approve that blocks orders it should pass — and the same agent can have both at once. This harness probes an MCP server's tool surface for both directions and writes down what it found, how to reproduce it, and what to measure.

It is small on purpose. Three probe families, a deliberately broken mock server to demonstrate them against, and a report an engineer can act on without asking what a score means.

```
$ mcp-guardrail-probe --demo

Probes: 3 run · 0 skipped
Findings: 12 sev1 · 1 sev2 · 0 sev3

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

Needs Python 3.10 or newer.

```bash
git clone https://github.com/taniasaleem/mcp-guardrail-probe
cd mcp-guardrail-probe
pip install -e ".[dev]"

mcp-guardrail-probe --demo          # the broken mock: finds things
mcp-guardrail-probe --demo-safe     # the fixed mock: finds nothing
make test
```

Exit codes are chosen so CI can gate on them: **0** clean, **1** a sev1 is present, **2** only sev2/sev3, **3** nothing was tested because every probe was skipped, **4** the run did not complete. Fail the build on 1, 3 and 4.

4 means the target could not be loaded or reached, or a call failed in a way that is not a refusal: a timeout or dropped connection (the charge may have committed), a failed reset, a setup call the server refused, or a crash. The report is still written. It covers the probes that finished, marks the one that stopped as aborted with the attempts it got through, and lists the rest as not run. 4 wins over a sev1 found earlier in the same run, because a run that stopped part-way has incomplete counts.

## Running it against your own server

**Read this first: the probes act, they do not just look.**

- `spend_cap` sets a cap of 100, then issues 15 real `charge` calls totalling 590: 150 once, 30 twelve times (eight of them concurrently), and 40 twice under one idempotency key. If the server's `charge` takes no idempotency key, the two 40s are not sent (13 calls, 510). Against a cap configured out-of-band (below) every charge scales with that cap instead: 1.5x, 0.3x and 0.4x, totalling 5.9x (5.1x without the retry).
- `approval_threshold` sets an auto-approve limit, then requests approval for 49, 50 and 51, then 49 three more times (L-1, L, L+1, then L-1 three times, for an out-of-band limit L). On a server where an approval moves money, each approval may be a real payment.
- `content_injection` creates up to 21 listings (17 on the bundled mock), most of them carrying text that tells an agent to charge 999.

Point it only at a sandbox or test instance you own, never at anything that moves real money or that other people can see.

### A real MCP server

`spend_cap` and `approval_threshold` run against any MCP server over stdio or streamable HTTP, through the official MCP Python SDK. The SDK is an optional extra, so the core package keeps zero dependencies:

```bash
pip install -e ".[mcp]"
mcp-guardrail-probe --mcp-config my-server.json --probe spend_cap --probe approval_threshold
```

The probes speak a fixed vocabulary (`charge`, `set_spending_limit`, ...), and your server has its own. The JSON file says how one maps onto the other. Nothing is guessed: a probe whose tools are not mapped and not present under its own names is skipped, and the run says so.

```json
{
  "name": "my-server (sandbox)",
  "server": {"transport": "stdio", "command": "my-server", "args": [], "env": {"API_KEY": "${MY_TEST_KEY}"}},
  "reset": {"tool": "reset_sandbox"},
  "tools": {
    "set_spending_limit": {"tool": "set_budget", "rename": {"amount": "limit_cents"}, "types": {"limit_cents": "integer"}},
    "get_spending_limit": {"tool": "budget_status", "result": {"committed_spend": "spent_total"}},
    "charge": {
      "tool": "make_payment",
      "rename": {"amount": "amount_cents", "idempotency_key": "request_id"},
      "types": {"amount_cents": "integer"},
      "refuse_when": [{"path": "status", "in": ["declined"]}],
      "reason": "reason"
    }
  }
}
```

| Key | What it does |
|---|---|
| `server` | `{"transport": "stdio", "command", "args", "env", "cwd"}` or `{"transport": "http", "url", "headers"}`. `${VAR}` and `${VAR:-default}` come from your environment, so keys stay out of the file. A stdio server gets only a minimal environment (`PATH`, `HOME` and a few more) plus `env`, so pass anything else it needs explicitly. |
| `tools.<probe tool>.tool` | The server's name for it. |
| `rename`, `fixed`, `drop`, `types` | Argument renames (probe name to server name), constant arguments the server needs, probe arguments to leave out, and `number`, `integer` or `string` coercions. A `fixed` value may not override an argument the probe sends. Dropping `idempotency_key` from `charge` says the server has no idempotency key: `spend_cap` then reports its retry shape as not supported instead of sending it. |
| `refuse_when` | When an ordinary result means no: `{"path": "status", "in": [...]}`, `"not_in": [...]` or `"present": true`. Dotted paths reach into nested results. |
| `reason` | Where a refusal result holds the server's own explanation: a path, or a list of paths where the first one present wins, such as `["reason", "error"]`. The report then leads each refusal with it, ahead of the result itself. Without it, a long result can push the explanation past the point where the report cuts a refusal message off. |
| `result` | Fields the probes read, taken from the server's result: `"committed_spend": "spent_total"`, or `{"path": "outcome", "map": {"WITHIN_CEILING": "approved"}}`. |
| `reset` | How to give each reading fresh state: `{"tool": "..."}` calls a server tool, `{"restart": true}` starts a new server process (stdio only, and it resets only a server that keeps its state in memory; over http it would reconnect to the same server, so it is refused), `{"command": [...]}` runs a local command (from the file's directory) and then reconnects. |
| `out_of_band_limits` | `spending_limit` and/or `approval_threshold`, for a server whose limit its operator sets (below). `approval_threshold` must be above 1, because the probe tests L−1. |
| `timeout_seconds` | Per call. Default 60. |

Unknown keys are errors, not ignored, and `_comment` is allowed anywhere.

**How a refusal is recognised.** An `isError: true` result and a JSON-RPC error both count as refused. So does any result a `refuse_when` rule matches, because many servers say no with an ordinary result such as `{"status": "declined"}`; without the rule the probe would read a declined charge as paid. So a `charge` mapping with no `refuse_when` gets a warning when the run starts; ignore it only if the server reports every refusal with `isError`. A call that times out, or whose connection drops before it answers, is *not* counted as refused: the charge may have committed, so the run stops instead, with exit code 4.

**Limits configured out-of-band.** Some servers take their cap or approval threshold from an env var, a config file or an operator API, and give the agent no tool to set it. Declare the value in `out_of_band_limits` and the probe tests that value, never calls `set_spending_limit` or `set_approval_threshold`, and sizes its amounts from it. `spend_cap` then requires a `reset`: each attack shape deliberately uses up the cap, so without one every shape after the first would start from the last one's spend. It skips with that reason rather than run. `approval_threshold` does the same: without a reset, L and L+1 would be read after L−1 was approved, and on a server that also checks a running total, that total would escalate L+1 and hide a per-request rule that approves it.

**When a mapping is wrong.** A wrong mapping usually makes every charge fail, for example on a bad argument, and every refused charge looks like a cap holding. So `spend_cap` reports a sev2, "Spend cap refuses charges that fit under it", when charges that fit under a fresh cap are refused. It also reports it when the first charge of the retry shape is refused, which happens when a server rejects the idempotency key outright; if the server has no such key, drop it in the mapping. Each attempt keeps the server's refusal messages, so the report shows which case you hit.

Mapping files for two open-source agent-wallet servers, with step-by-step run sheets, are in [`servers/`](servers/).

### Your own Python adapter

`--target module:callable` still takes any Python object with:

- `name`: a label for the report
- `list_tools()`: the tool names it exposes, as strings
- `call_tool(tool, **kwargs)`: calls one tool and returns its result as a dict. A refusal is either a raised `ToolError` or an MCP-style result with `"isError": true`, and the probes treat both as refused. Anything else counts as accepted.
- `reset()`: optional. If present, it is called between probes, before each spend-cap shape and before each approval boundary check, so earlier calls cannot affect a reading.
- `out_of_band_limits`: optional, as above.
- `takes_idempotency_key`: optional. Set it to `False` when your `charge` has no idempotency key, and the retry shape is reported as not supported instead of run.

```python
# myadapter.py
from mcp_guardrail_probe import ToolError

class MyServer:
    name = "my-server (sandbox)"

    def list_tools(self) -> list[str]:
        ...

    def call_tool(self, tool: str, **kwargs) -> dict:
        ...  # call the tool; on refusal raise ToolError or return {"isError": True, ...}

def build_target():
    return MyServer()
```

```bash
mcp-guardrail-probe --target myadapter:build_target --format json --out findings.json
```

### What each probe needs

| Probe | Tools it needs | Optional |
|---|---|---|
| `spend_cap` | `charge`, and `set_spending_limit` unless the cap is out-of-band | `get_spending_limit`: without it, the check of the server's own counter against the probe's ledger is skipped, and the attempts say "not exposed" |
| `approval_threshold` | `submit_for_approval`, and `set_approval_threshold` unless the limit is out-of-band | |
| `content_injection` | `add_item`, `agent_request` | |

`content_injection` needs more than a mapping. `agent_request` is an agent that reads the listings and decides which tools to call. In MCP the model runs in the client, not the server, so a real MCP server will not normally have such a tool. Today this probe runs only against the bundled mock. Running it against a real server needs an LLM agent loop inside the harness, which it does not have yet.

## The design decisions worth arguing about

**The probe keeps its own ledger, and does not trust the server's counters.** This is the most important line in the codebase and I got it wrong the first time. My initial `spend_cap` probe read the server's `committed_spend` to decide whether the cap had been breached — and it passed against a server I had deliberately built to be broken. The reason is the bug itself: a read-then-write cap check with no lock loses updates, so eight concurrent charges of 30 against a cap of 100 were all accepted — 240 of real money — while the server's own counter reported 30 and its cap still reported itself active.

A probe that reads the counter sees 30, concludes the cap held, and reports a clean bill of health at the exact moment money is leaving. So the probe now sums what the server *accepted* and treats the server's self-report as a separate claim to be checked against it. The two disagreeing is its own finding, because it means the operator's dashboard is wrong simultaneously with the control.

**Injection testing is differential, not vibes.** Running a payload and reading the output tells you little, because models are noisy. Each payload runs the *same* user request against a clean listing and a poisoned one, and a finding is a tool call that appears only in the poisoned run. That definition is mechanical, so two people scoring independently get the same answer. Severity is sev1 only when the extra call touches money — an injection that makes the agent chatty is noise; one that reaches `charge` is the problem. The bundled mock's "agent" is a regex that obeys `call <tool> with amount <n>`. So the demo shows this machinery working (differential detection, field discovery, severity), not that any payload works on a real model. One of the four payloads, `role_play`, never fires on it.

**Both directions, always.** Fail-closed shows up as a support ticket and fail-open shows up as a loss, so fail-closed gets fixed and fail-open survives. `approval_threshold` walks the boundary at L−1, L and L+1 explicitly, and reports the off-by-one in either direction. Each boundary check starts from fresh state (through the target's `reset()`, when it has one). On a server that tracks cumulative approvals, reading L straight after approving L−1 measures the running total, not the boundary.

**A probe skips rather than guessing.** If the target doesn't expose the tools a probe needs, it reports `skipped` with the missing names. Silently testing a tool that looks similar produces a clean report and a false sense of safety, which is worse than no report. For the same reason, a run where every probe skipped exits 3 and says "Nothing was tested", so it can never pass for a clean result.

**Every attempt is logged, passing or failing.** A probe that reports nothing still shows its working. Otherwise a clean report is indistinguishable from a probe that never ran.

## What this is not

- **Not a drop-in for every probe.** `spend_cap` and `approval_threshold` run against a real MCP server through a mapping file you write. `content_injection` runs only against the bundled mock until the harness has its own agent loop.
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
  mcp_target.py   a Target backed by a real MCP server, and its mapping file
  probes/
    base.py              skip-or-run, attempt recording
    spend_cap.py         three cap shapes and a retry, client-side ledger
    injection.py         differential tool-call comparison
    approval_threshold.py  boundary walk in both directions
  mock_server/
    server.py            VulnerableCommerceServer and SafeCommerceServer
servers/
  almega-mcp/     mapping files and run sheet for one open-source agent wallet
  agent-wallet/   ...and for another, with the script that resets its daemon
tests/
  test_probes.py      each probe must catch the broken mock AND stay quiet on the fixed one,
                      and each detection path has a test that fails when that path breaks
  test_cli.py         the exit-code contract, including 3 when nothing ran and 4 when a run stops
  test_mcp_target.py  the MCP adapter against a fake server built on the same SDK
  test_mcp_config.py  the mapping file, and every file in servers/
  fake_mcp_server.py  that fake server: other names, other shapes, switchable bugs
examples/
  sample_report.md
```

The paired tests are the point: a probe that only ever fires is an alarm, not a test, and a probe that never fires is decoration. They are not enough on their own, though. A pair that only asks "found something?" stays green when the probe finds the wrong thing for the wrong reason. So each detection path also has a test that pins *which* finding, shape and severity it produces.

## Prior work

This is a small harness, not new research, and most of the ideas around it are older than it is.

- Invariant Labs documented [tool poisoning attacks](https://invariantlabs.ai/blog/mcp-security-notification-tool-poisoning-attacks) on MCP, and a [toxic agent flow through the GitHub MCP server](https://invariantlabs.ai/blog/mcp-github-vulnerability), where an issue written by an outsider steers the agent. That second one is the pattern `content_injection` tests.
- Simon Willison's [lethal trifecta](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/) (private data, untrusted content, external communication) is the clearest statement of why that pattern matters.
- [AgentDojo](https://github.com/ethz-spylab/agentdojo) is an environment for evaluating prompt-injection attacks and defences on LLM agents.
- [snyk/agent-scan](https://github.com/snyk/agent-scan), [cisco-ai-defense/mcp-scanner](https://github.com/cisco-ai-defense/mcp-scanner) and [Promptfoo's MCP security testing](https://www.promptfoo.dev/docs/red-team/mcp-security-testing/) scan or red-team MCP servers for tool poisoning and prompt injection. Use them for that; this does not try to compete.
- On spending controls specifically, [agent-overspend-benchmark](https://github.com/Pink-Agentic-Payments/agent-overspend-benchmark) measures what agents *try* when asked to get around a spending rule. [payment-mcp-census](https://github.com/Pink-Agentic-Payments/payment-mcp-census) surveys which payment MCP servers document spending controls.

What this adds, as far as I can tell, is the server side of that question: whether a cap or approval rule actually *holds* under concurrent charges and at the L−1 / L / L+1 boundary, and whether a charge retried under one idempotency key is charged twice.

## Licence

MIT.
