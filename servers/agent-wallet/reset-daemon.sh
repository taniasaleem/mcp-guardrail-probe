#!/usr/bin/env bash
# A fresh agent-wallet daemon for one probe reading.
#
#   bash reset-daemon.sh spend-cap   # mandate "probe", cap 100 cents
#   bash reset-daemon.sh approval    # mandate "probe", cap 5000 cents
#   bash reset-daemon.sh stop
#
# Stops the daemon this script started last, deletes its database, starts a
# new one, and grants the probe one mandate through the operator control API.
# The mapping files run it before every reading (reset.command), because
# agent-wallet's cap is lifetime spend against a mandate: without a fresh
# database, each attack shape would start with the last one's spend.
#
# It uses agent-wallet as published. It edits none of its files and copies
# none of its code. Policy (auto-approve threshold 100 cents, hard limit 5000
# cents, mandate required) is what src/daemon.ts sets; only the mandate is ours.
#
# Needs:
#   AGENT_WALLET_DIR   an agent-wallet checkout with `npm install` done
#   NODE               Node >= 24 (default: node on PATH)
#   STRIPE_SECRET_KEY  optional; refused unless it starts with sk_test_
# Optional: PROBE_STATE_DIR, AGENT_WALLET_{MCP,PAY,CONTROL}_PORT.
set -euo pipefail

profile="${1:?usage: reset-daemon.sh spend-cap|approval|stop}"
state="${PROBE_STATE_DIR:-${TMPDIR:-/tmp}/agent-wallet-probe}"
mcp_port="${AGENT_WALLET_MCP_PORT:-4024}"
pay_port="${AGENT_WALLET_PAY_PORT:-4022}"
control_port="${AGENT_WALLET_CONTROL_PORT:-4023}"
mkdir -p "$state"

stop_daemon() {
  if [[ -f "$state/daemon.pid" ]]; then
    local pid
    pid="$(cat "$state/daemon.pid")"
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid"
      for _ in $(seq 1 50); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.1
      done
    fi
    rm -f "$state/daemon.pid"
  fi
}

case "$profile" in
  spend-cap) cap=100 ;;  # must equal out_of_band_limits.spending_limit in spend-cap.json
  approval) cap=5000 ;;  # high enough that only the auto-approve tier decides
  stop)
    stop_daemon
    exit 0
    ;;
  *)
    echo "unknown profile: $profile (spend-cap, approval or stop)" >&2
    exit 2
    ;;
esac

: "${AGENT_WALLET_DIR:?set AGENT_WALLET_DIR to your agent-wallet checkout}"
if [[ -n "${STRIPE_SECRET_KEY:-}" && "$STRIPE_SECRET_KEY" != sk_test_* ]]; then
  # agent-wallet's Stripe rail accepts any key, so the check lives here.
  echo "refusing: STRIPE_SECRET_KEY is not a Stripe test-mode key (sk_test_...)" >&2
  exit 1
fi

stop_daemon
rm -f "$state"/wallet.db "$state"/wallet.db-*
token="$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')"

(
  cd "$AGENT_WALLET_DIR"
  AGENT_WALLET_DB="$state/wallet.db" \
    AGENT_WALLET_CONTROL_TOKEN="$token" \
    AGENT_WALLET_MCP_PORT="$mcp_port" \
    AGENT_WALLET_PAY_PORT="$pay_port" \
    AGENT_WALLET_CONTROL_PORT="$control_port" \
    nohup "${NODE:-node}" src/daemon.ts >>"$state/daemon.log" 2>&1 &
  echo $! >"$state/daemon.pid"
)

control="http://127.0.0.1:$control_port"
up=""
for _ in $(seq 1 100); do
  if curl -sf -H "Authorization: Bearer $token" "$control/status" >/dev/null 2>&1; then
    up=1
    break
  fi
  sleep 0.1
done
if [[ -z "$up" ]]; then
  echo "agent-wallet daemon did not start; see $state/daemon.log" >&2
  exit 1
fi

curl -sf -X POST "$control/mandates" \
  -H "Authorization: Bearer $token" \
  -H "Content-Type: application/json" \
  -d "{\"id\":\"probe\",\"grantedBy\":\"mcp-guardrail-probe\",\"cap\":{\"amount\":\"$cap\",\"currency\":\"USD\"},\"rails\":[\"stripe\"]}" \
  >/dev/null

for _ in $(seq 1 50); do
  if (exec 3<>"/dev/tcp/127.0.0.1/$mcp_port") 2>/dev/null; then
    break
  fi
  sleep 0.1
done
echo "agent-wallet daemon up: profile $profile, mandate cap $cap cents, state in $state" >&2
