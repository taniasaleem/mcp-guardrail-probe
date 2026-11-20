# Run sheet: agent-wallet

Server: [DaveForan/agent-wallet](https://github.com/DaveForan/agent-wallet),
read at commit `61f2616`. **It has no licence file**, so run it from your own
checkout and copy none of its code into this repository. `reset-daemon.sh`
drives it from outside and edits none of its files.

This sheet runs its daemon (`src/daemon.ts`), which serves MCP over
streamable HTTP on port 4024 and an operator control API on 4023. The probes
talk only to the MCP port. The reset script uses the control API, as an
operator would, to grant the probe one mandate.

Both limits are out-of-band, and no agent tool sets them:

| Mapping | Limit | Where it comes from |
|---|---|---|
| `spend-cap.json` | 100 cents | the lifetime cap of mandate `probe`, which `reset-daemon.sh spend-cap` grants |
| `approval.json` | 100 cents | the daemon's auto-approve threshold (tiered mode), set in `src/daemon.ts` |

Its cap is lifetime spend against a mandate. So before every reading,
`reset-daemon.sh` stops the daemon, deletes its database, starts a new one and
grants the mandate again. The mappings do this through `reset.command`. The
amounts are integer cents.

Do the steps in order. Step 1 sends nothing outside your machine. Step 2
issues cards in a Stripe test-mode account.

## 0. Set up (once)

Node 24 or newer (its `engines` field):

```bash
git clone https://github.com/DaveForan/agent-wallet ~/src/agent-wallet
```

```bash
git -C ~/src/agent-wallet checkout 61f2616
```

```bash
cd ~/src/agent-wallet && npm install --ignore-scripts
```

At this commit `npm ci` refuses to install, because `package-lock.json` is out
of sync with `package.json`. `npm install` works but rewrites the lock file in
your checkout.

Then, in this repository, with the probe's environment active:

```bash
pip install -e ".[mcp]"
```

```bash
export AGENT_WALLET_DIR=~/src/agent-wallet NODE="$(command -v node)"
```

```bash
mkdir -p ~/mcp-guardrail-probe-runs
```

Ports 4022, 4023 and 4024 must be free. To use others, export
`AGENT_WALLET_MCP_PORT`, `AGENT_WALLET_PAY_PORT` and
`AGENT_WALLET_CONTROL_PORT`; the script and the mappings both read them. The
daemon's state and log go to `$PROBE_STATE_DIR` (default
`$TMPDIR/agent-wallet-probe`). The log includes the throwaway control token
the script generates.

## 1. Dry run with no Stripe key: plumbing only

```bash
unset STRIPE_SECRET_KEY
```

```bash
bash servers/agent-wallet/reset-daemon.sh spend-cap
```

```bash
mcp-guardrail-probe --mcp-config servers/agent-wallet/spend-cap.json --probe spend_cap --out ~/mcp-guardrail-probe-runs/agent-wallet-dry-spend.md
```

```bash
bash servers/agent-wallet/reset-daemon.sh approval
```

```bash
mcp-guardrail-probe --mcp-config servers/agent-wallet/approval.json --probe approval_threshold --out ~/mcp-guardrail-probe-runs/agent-wallet-dry-approval.md
```

With no key, any payment the policy allows ends `failed` at the Stripe rail
before any network call ("stripe rail has no API key"). Expect "Spend cap
refuses charges that fit under it" and "Auto-approve blocks below its own
limit", both with that reason in the attempts. Here they prove only that the
transport, mapping and reset work. The readings of the policy itself (denied
for over the cap, `pending_approval` above the threshold) are real. Every
reading should show `fresh_state: true`.

The first command starts the daemon, because the probe connects before its
first reset.

## 2. Stripe Issuing test mode (the operator's call)

Only with a Stripe **test-mode** key, on an account with Issuing enabled in
test mode. agent-wallet's Stripe rail itself accepts any key; the reset script
refuses one that does not start with `sk_test_`.

Without `STRIPE_ISSUING_CARDHOLDER`, the rail creates a new cardholder in each
daemon process that settles a payment, which is up to five per probe run. To avoid that, create one
test cardholder in the dashboard and export its id.

```bash
export STRIPE_SECRET_KEY=sk_test_... STRIPE_ISSUING_CARDHOLDER=ich_...
```

```bash
bash servers/agent-wallet/reset-daemon.sh spend-cap
```

```bash
mcp-guardrail-probe --mcp-config servers/agent-wallet/spend-cap.json --probe spend_cap --out ~/mcp-guardrail-probe-runs/agent-wallet-stripe-spend.md
```

```bash
bash servers/agent-wallet/reset-daemon.sh approval
```

```bash
mcp-guardrail-probe --mcp-config servers/agent-wallet/approval.json --probe approval_threshold --out ~/mcp-guardrail-probe-runs/agent-wallet-stripe-approval.md
```

```bash
bash servers/agent-wallet/reset-daemon.sh stop
```

What it creates, from reading the code (not yet run): one single-use virtual
card for each payment the policy settles. Each card's spend limit is the
payment's amount, and it cancels itself after one authorization. No merchant
is charged; issuing the card is the whole settlement. `spend_cap` sends 15
requests (150; 30 four times; 30 eight times at once; 40 twice) and
`approval_threshold` sends 6 (99, 100, 101, then 99 three times), all in
cents. Requests that are denied or left pending issue no card. A pending
approval is wiped with the database at the next reset, and nobody approves it.

## 3. After the runs

- Compare each report with the expected readings in the audit notes, which
  are kept outside this repository.
- If a defect reproduces, tell agent-wallet's maintainer before the result is
  published anywhere.
- The exit code is 1 whenever there is a sev1. That is the probe's verdict on
  the server. It does not mean the run failed.
