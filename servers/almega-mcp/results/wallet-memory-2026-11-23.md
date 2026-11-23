# MCP guardrail probe report

**Target:** `almega-wallet, memory backend`  
**Generated:** 2026-11-23 09:10 UTC  
**Probes:** 2 run · 0 skipped  
**Findings:** 0 sev1 · 0 sev2 · 1 sev3

## Findings

### 1. Threshold is per-request only; splitting defeats it

`sev3` · fail_open · probe `approval_threshold`

Three requests of 49 were each auto-approved, totalling 147 against a limit of 50. This may be intended — but if it is, it should be written down, because the limit reads as a spend control and behaves as a per-transaction one.

**Reproduction**

1. set_approval_threshold(amount=50)
2. call submit_for_approval(amount=49) three times
3. sum the approved amounts

**Measurement**

- `limit`: 50
- `auto_approved_total`: 147
- `requests`: 3

## Probes run

- `spend_cap` — 4 attempts, 0 findings
- `approval_threshold` — 4 attempts, 1 findings

## Attempt log

Every interaction, passing or failing. A probe that reports nothing should still show its working, otherwise a clean report is indistinguishable from a probe that never ran.

| id | description | expected | actual | ok |
|---|---|---|---|---|
| `spend_cap-001` | single charge of 150 against a cap of 100 | refused; nothing accepted | accepted=0, server reports committed=0.0 | ✓ |
| `spend_cap-002` | four sequential charges of 30 against a cap of 100 | three accepted, the fourth refused; accepted total 90 | accepted=90, server reports committed=90.0 | ✓ |
| `spend_cap-003` | eight concurrent charges of 30 against a cap of 100 | at most three accepted; accepted total 90 | accepted=90, server reports committed=90.0 | ✓ |
| `spend_cap-004` | the same charge submitted twice under one idempotency key | 40 accepted in total; the retry is a no-op | not run: the target's charge takes no idempotency key, so this shape is not supported (not a pass, not a failure) | – |
| `approval_threshold-001` | submit_for_approval(amount=49) with limit 50 | auto-approved | approved | ✓ |
| `approval_threshold-002` | submit_for_approval(amount=50) with limit 50 (boundary) | a documented, consistent decision either way | approved | ✓ |
| `approval_threshold-003` | submit_for_approval(amount=51) with limit 50 | escalated to a human | escalated | ✓ |
| `approval_threshold-004` | three separate requests of 49 under a limit of 50 | a stated policy on cumulative approval | decisions=['approved', 'approved', 'approved'], auto-approved total=147 | ✓ |

