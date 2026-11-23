# Results: almega-mcp, in memory

The first runs of the probes against a real MCP server. Both of almega's
servers held their spend cap and their approval boundary. The one finding, a
sev3, is how almega is designed rather than a defect, so there was nothing
to report to its maintainers.

| | |
|---|---|
| Date | 2026-11-23 |
| Server | [almega-ai/almega-mcp](https://github.com/almega-ai/almega-mcp) 0.2.0 at `3163eb7`, on MCP Python SDK 1.30.0 |
| Backend | in memory (`ALMEGA_BACKEND=memory`). No network calls, no payments: everything stayed inside the server process. |
| Probe | this repository at the commit that adds this file, on MCP Python SDK 2.3.0 |
| Machine | macOS 26.5 on arm64, Python 3.12.14 on both sides |
| Mappings | [`wallet-memory.json`](wallet-memory.json), [`gate-memory.json`](gate-memory.json) |
| Reports | [`results/`](results/): one markdown and one JSON report per server |

## What happened

| Check | Wallet (`almega-wallet`) | Gate (`almega-mcp`) |
|---|---|---|
| One charge of 150 against a cap of 100 | refused, 0 accepted | refused, 0 accepted |
| Four charges of 30 in a row | 90 accepted, the fourth refused | same |
| Eight charges of 30 sent at once | 90 accepted | same |
| The same charge twice under one idempotency key | not run: `pay` takes no key | same |
| The server's own spend counter afterwards | matched the probe's ledger every time | same |
| Approval of 49, 50 and 51 against a threshold of 50 | approved, approved, escalated | same |
| Three approvals of 49 | all approved, 147 in total (sev3) | same |

Every reading started from fresh state (`fresh_state: true`, through almega's
`reset` tool), and every refused charge was refused with almega's own reason,
for example "would exceed monthly limit ($150.00 requested, $100.00 left of
$100.00)", not with an argument error. Both runs exited 2: no sev1 or sev2,
one sev3.

## What this does and does not show

- **The sev3 is the design, not a defect.** `approve_above` is a per-payment
  rule. The wallet says so in its `open_wallet` tool description ("Any single
  charge above this requires a human approval"); the gate applies the same
  rule to each payment, though its docs say less. What caps the total is
  `monthly_limit`, which the mappings set to 1,000,000 for the approval
  readings so that only the approval rule is measured. With a real
  `monthly_limit`, three payments of 49 are bounded by it.
- **The concurrent shape could not test a race here.** almega's tools are
  ordinary (not `async`) functions, and SDK 1.30's FastMCP calls those directly
  on its single event loop. So the eight charges sent at once were processed
  one after another inside the server. The cap holding is real; it is not
  evidence that almega would survive truly concurrent charges, because this
  server never sees any.
- **Idempotency was not tested**, because `pay` has no idempotency key. The
  mappings drop the probe's key, and the report lists that shape as not
  supported.
- **Only the in-memory backend was run.** Step 3 of the [run sheet](RUN-SHEET.md),
  the wallet on Stripe Issuing test mode, needs an operator's test-mode key
  and has not been run.

## Run it yourself

Steps 0 to 2 of the [run sheet](RUN-SHEET.md). The reports here were written
with `--out` outside the repository, read for local paths and keys, and then
copied into `results/`.
