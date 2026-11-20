# Run sheet: almega-mcp

Server: [almega-ai/almega-mcp](https://github.com/almega-ai/almega-mcp), read
at commit `3163eb7`. MIT-licensed. It ships two MCP servers, and this sheet
runs both:

| File | Server | Console script | Backend |
|---|---|---|---|
| `wallet-memory.json` | the wallet (`almega_mcp.py`) | `almega-wallet` | in memory |
| `gate-memory.json` | the gate (`almega_gate.py`), the entry the MCP Registry lists | `almega-mcp` | in memory, the only one it has |
| `wallet-stripe-test.json` | the wallet | `almega-wallet` | Stripe Issuing, test mode |

Both servers take their cap and approval threshold from a tool call
(`open_wallet` or `set_frame`), so no limit is out-of-band here, and both
offer a `reset` tool, which the mappings use before every reading.

Do the steps in order. Steps 1 and 2 send nothing outside your machine.
Step 3 creates objects in a Stripe test-mode account.

## 0. Set up (once)

almega imports `mcp.server.fastmcp`, which the MCP Python SDK removed in 2.0,
and its requirements (`mcp[cli]>=1.0.0`) do not stop pip installing 2.x. So
give it a virtualenv of its own with `mcp<2`, separate from the probe's (which
needs `mcp>=2.3`):

```bash
git clone https://github.com/almega-ai/almega-mcp ~/src/almega-mcp
```

```bash
git -C ~/src/almega-mcp checkout 3163eb7
```

```bash
python3 -m venv ~/venvs/almega && ~/venvs/almega/bin/pip install -e ~/src/almega-mcp "mcp<2"
```

```bash
~/venvs/almega/bin/python -c "import importlib.metadata as m; print(m.version('mcp'))"
```

The last line should print a 1.x version. Then, in this repository, with the
probe's own environment active:

```bash
pip install -e ".[mcp]"
```

```bash
export ALMEGA_VENV=~/venvs/almega
```

```bash
mkdir -p ~/mcp-guardrail-probe-runs
```

## 1. The wallet, in memory

```bash
mcp-guardrail-probe --mcp-config servers/almega-mcp/wallet-memory.json --probe spend_cap --probe approval_threshold --out ~/mcp-guardrail-probe-runs/almega-wallet-memory.md
```

```bash
mcp-guardrail-probe --mcp-config servers/almega-mcp/wallet-memory.json --probe spend_cap --probe approval_threshold --format json --out ~/mcp-guardrail-probe-runs/almega-wallet-memory.json
```

What it does: 9 `reset` calls and 7 `open_wallet` calls (a fresh wallet
before every reading), 13 `pay` calls for
`spend_cap` (150; 30 four times; 30 eight times at once) and 6 for
`approval_threshold` (49, 50, 51, then 49 three times). All of it stays inside
the server process. The server logs its requests to your terminal on stderr.

`spend_cap`'s retry shape (one charge sent twice under one idempotency key)
does not run here. `pay` has no idempotency parameter, so the mappings drop
the probe's key, and the attempt log lists that shape as not supported, marked
`–`. It is neither a pass nor a finding.

First, read the attempts: every reading should show `fresh_state: true`, and
`refusal_reasons` should be the server's own words ("would exceed monthly
limit"), not an argument error. If the report has "Spend cap refuses charges
that fit under it", the mapping is wrong for this checkout, so stop and fix it
before going on.

## 2. The gate, in memory

```bash
mcp-guardrail-probe --mcp-config servers/almega-mcp/gate-memory.json --probe spend_cap --probe approval_threshold --out ~/mcp-guardrail-probe-runs/almega-gate-memory.md
```

Same calls through `set_frame`, `pay` and `get_frame`. Nothing leaves the
process.

## 3. The wallet, on Stripe Issuing test mode (the operator's call)

Only with a Stripe **test-mode** key (`sk_test_...`), on an account with
Issuing enabled in test mode. almega refuses any other key. Its code creates
cardholders with a French billing address and EUR cards, so an account whose
Issuing runs in another country may reject them. If it does, every
`open_wallet` fails and the run stops with that error and exit code 4.
Nothing will have been spent.

```bash
export STRIPE_SECRET_KEY=sk_test_...
```

```bash
mcp-guardrail-probe --mcp-config servers/almega-mcp/wallet-stripe-test.json --probe spend_cap --probe approval_threshold --out ~/mcp-guardrail-probe-runs/almega-wallet-stripe-test.md
```

What it creates in the test account, from reading the code (not yet run):
one Issuing cardholder and one virtual card per `open_wallet` (7, one per
reading), and one test authorization (through Stripe's test helpers) per
payment the server approves. almega's `reset` forgets only its own index, so
these objects stay in the test account afterwards. Delete them in the
dashboard if you want them gone.

Each approved `pay` waits for Stripe inside the server, so this run is much
slower than steps 1 and 2. A call that takes longer than `timeout_seconds`
(60) stops the run rather than counting as a refusal.

## 4. After the runs

- Compare each report with the expected readings in the audit notes, which
  are kept outside this repository.
- If a defect reproduces, tell almega's maintainers before the result is
  published anywhere.
- The exit code is 1 whenever there is a sev1. That is the probe's verdict on
  the server. It does not mean the run failed.
- Exit code 4 means the run stopped part-way: a timeout, a dropped
  connection, or a setup call such as `open_wallet` that the server refused.
  The report is still written; it says where the run stopped, and its counts
  are not a reading of the server.
