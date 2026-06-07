# SML Beast Orchestrator — Operator Runbook

Deployment, configuration, and incident response procedures.

---

## 0. Day-to-day operator commands

Use `opctl` for routine operator tasks instead of dropping into a Python REPL:

```bash
python -m sml_beast.outreach.opctl status              # full snapshot
python -m sml_beast.outreach.opctl opt-out example.com # permanent blocklist
python -m sml_beast.outreach.opctl review-clear mastersheets       # +1 review
python -m sml_beast.outreach.opctl review-clear-all xrpl_x402      # clear gate
python -m sml_beast.outreach.opctl kill on             # halt all dispatch
python -m sml_beast.outreach.opctl kill off            # resume dispatch
python -m sml_beast.outreach.opctl metrics             # raw verification log
python -m sml_beast.outreach.opctl metrics-stats       # aggregate conversion
python -m sml_beast.outreach.opctl domain example.com  # single-domain entry
python -m sml_beast.outreach.opctl recent 10           # last 10 state changes
python -m sml_beast.outreach.opctl balance             # XRPL hot wallet balance
python -m sml_beast.outreach.opctl alerts-sweep        # one-shot Discord check
python -m sml_beast.outreach.opctl poll                # IMAP drain + classify
python -m sml_beast.outreach.opctl replies             # operator review queue
python -m sml_beast.outreach.opctl dry-run             # full cycle, no I/O
```

### Optional Discord alerts

Set `BB7_DISCORD_ALERT_WEBHOOK` to a Discord webhook URL to enable alerts for:
- Kill switch activation / deactivation (always)
- Hot wallet balance below threshold (rate-limited to once per 24h)
- Manual review backlog (>= 5 entries; rate-limited 24h)
- Cycle complete (informational; not rate-limited)

The `bb7_alerts_sweep.yml` cron runs every 15 minutes to catch transitions
between full agent cycles. If `BB7_DISCORD_ALERT_WEBHOOK` is unset, every
alert silently no-ops — alerts are optional, not required.

None of these commands send a pitch, submit XRPL, or send SMTP. They are
safe to run at any time. `dry-run` runs the full agent pipeline with both
XRPL and SMTP disabled — useful for verifying targeting + enrichment + the
template rendering pipeline before the live cycle fires.

## 1. Preflight check (run before EVERY environment change)

```bash
python -m sml_beast.outreach.preflight
```

Exit codes:
- `0` — green; safe to enable the live cycle
- `1` — yellow; soft warnings (DNS, kill switch, manual review gate); operator decides
- `2` — red; refuse to enable until resolved

Flags:
- `--json` — machine-readable output for CI gating
- `--skip-network` — skip XRPL RPC + SMTP + DNS checks (for offline runs)

The validator never sends a pitch, never submits XRPL, never mutates state.
It only inspects environment, network connectivity, secret material parsing,
and state machine readiness. Use this after every credential rotation, every
DNS change, and as the first step before promoting the agent to mainnet.

---

## 2. Initial deployment (Render)

### 1.1 Repository setup

```bash
# Create the repo on GitHub first (empty, private)
# Then push:
git remote add origin git@github.com:Timwal78/sml-beast-orchestrator.git
git push -u origin main
```

Connect Render to `Timwal78/sml-beast-orchestrator`. The `render.yaml`
already configures the service name, build command, start command, and
health check path.

### 1.2 Required environment variables

Set these in the Render environment panel before the first deploy:

| Variable | Required | Description |
|----------|----------|-------------|
| `SERPER_API_KEY` | Yes | Serper.dev API key for SERP data |
| `X402_PROXY_SECRET` | Yes | HMAC secret for internal proxy token minting (32+ hex bytes) |
| `DASHBOARD_AUTH_TOKEN` | Yes | Bearer token for the operator dashboard (32+ hex bytes) |
| `BB7_XRPL_WALLET_SEED` | Yes | XRPL hot wallet seed (testnet until operator approval) |
| `BB7_SMTP_HOST` | Yes | SMTP relay hostname |
| `BB7_SMTP_USER` | Yes | SMTP username |
| `BB7_SMTP_PASS` | Yes | SMTP password |
| `BB7_OPT_OUT_URL` | Yes | Public opt-out URL (`https://www.scriptmasterlabs.com/outreach/opt-out`) |
| `BB7_OPERATOR_ADDRESS` | Yes | Physical address for CAN-SPAM compliance |
| `BB7_OPERATOR_SIGNATURE` | Yes | Operator name in pitch footer |
| `BB7_SMTP_FROM` | No | Override from-address (default: `outreach@infrastructure.scriptmasterlabs.com`) |
| `BB7_XRPL_NETWORK` | No | `testnet` (default) or `mainnet` |
| `BB7_XRP_PRICE_USDC` | No | XRP/USDC price override (default: `0.50`) |
| `BB7_OUTREACH_DRY_RUN` | No | Set to `1` to run without XRPL/SMTP |
| `BEAST_OUTPUT_ROOT` | No | Override output directory (default: `./output`) |

### 1.3 Generate secrets

```bash
# Dashboard token
openssl rand -hex 32

# x402 proxy secret
openssl rand -hex 32
```

### 1.4 DNS setup for outreach subdomain

Before any pitch fires on mainnet, provision:

```
infrastructure.scriptmasterlabs.com  IN  MX  10  <your-smtp-relay-mx>
```

Add SPF, DKIM, and DMARC records per your SMTP relay's documentation.
Without these, pitches will land in spam and the campaign delivers zero value.

---

## 3. XRPL wallet setup

### 2.1 Testnet (default)

The hot wallet seed you configure is used directly on testnet. To fund it:

```
https://faucet.altnet.rippletest.net/
```

Paste the classic address derived from your seed. The faucet issues 10,000 XRP.

### 2.2 Mainnet transition

The operator must manually approve the first 5 pitches per vertical before
any mainnet payment fires. The manual review gate (`OUTREACH_MANUAL_REVIEW_N = 5`)
enforces this.

To transition:
1. Set `BB7_XRPL_NETWORK=mainnet`
2. Fund the hot wallet with `≤ 100 USDC-equivalent XRP` (the `OUTREACH_HOT_WALLET_MAX`)
3. Keep bulk funds in a cold wallet; refill hot wallet manually after each cycle

### 2.3 Hot wallet key rotation

1. Generate a new seed (use xrpl-py locally or a hardware device)
2. Fund the new address from the old address (leave enough XRP for reserve)
3. Update `BB7_XRPL_WALLET_SEED` in Render environment panel
4. Trigger a manual redeploy
5. Archive the old seed in your credential store; do not delete until the new
   wallet has successfully completed one pitch cycle

---

## 4. Kill switch

To halt all new outreach immediately without a deploy:

```bash
# Via Render shell or SSH:
touch output/OUTREACH_KILL_SWITCH

# Or write any content:
echo "HALT $(date -u)" > output/OUTREACH_KILL_SWITCH
```

The agent checks for this file before every pitch attempt. The file's existence
(with any content) freezes new outbound. Existing state machine entries are
unaffected — domains already in `PITCH_DELIVERED` still get verified.

To re-enable outreach:
```bash
rm output/OUTREACH_KILL_SWITCH
```

---

## 5. Manual review gate

The first `OUTREACH_MANUAL_REVIEW_N` (default: 5) pitches per vertical require
operator approval before dispatch is allowed.

To clear the gate after reviewing and approving a pitch:

```python
from sml_beast.outreach.state import OutreachStateMachine
sm = OutreachStateMachine()
sm.record_manual_review_completed("mastersheets")
# repeat for each approved pitch
```

Or via the agent dry-run:

```bash
BB7_OUTREACH_DRY_RUN=1 python -m sml_beast.outreach.agent
```

This simulates the full cycle without XRPL or SMTP — inspect the log output
to verify targeting before clearing the gate.

---

## 6. Dashboard access

```
https://<your-render-url>/dashboard?token=<DASHBOARD_AUTH_TOKEN>
```

The token is stored in `sessionStorage` on first load; the URL is stripped
via `history.replaceState` so it doesn't appear in browser history.

The dashboard is READ-ONLY. It surfaces:
- Proxy state (calls, wallets, spend)
- Per-vertical bounty target counts and pages generated
- `[OUTREACH]` panel: domain counts by state, daily ceiling, kill switch,
  conversion/dofollow rates, 20-event timeline

**The outreach panel does NOT show which external domains received payments.
This is the secrecy mandate (BB7_DESIGN.md §9.6).** What it shows is aggregate
counts and your own domain's lifecycle states.

---

## 7. Opt-out handling

When a recipient replies with `STOP`, the reply parser classifies it as `OPTOUT`.
The operator must manually call:

```python
sm = OutreachStateMachine()
sm.mark_opted_out("example.com")
```

The domain is then permanently blocked from future pitches. This entry persists
across restarts via `output/_internal/outreach_state.json`.

The public opt-out page at `https://www.scriptmasterlabs.com/outreach/opt-out`
must list opted-out domains. Update it manually when you call `mark_opted_out`.

---

## 8. State file recovery

State lives at `output/_internal/outreach_state.json`. It is written atomically
via temp-file-and-rename. If it becomes corrupt (truncated write during a crash),
the next startup resets to fresh state with a warning log.

To inspect the current state:

```python
from sml_beast.outreach.state import OutreachStateMachine
import json
sm = OutreachStateMachine()
print(json.dumps(sm.snapshot(), indent=2))
```

To export conversion metrics:

```python
from sml_beast.outreach.verifier import load_metrics
for r in load_metrics():
    print(r)
```

---

## 9. SMTP credentials rotation

1. Provision new credentials on your relay
2. Test send via:
   ```bash
   BB7_SMTP_HOST=... BB7_SMTP_USER=... BB7_SMTP_PASS=... \
   BB7_OUTREACH_DRY_RUN=0 python -c "
   from sml_beast.outreach.dispatcher import send_pitch
   from sml_beast.outreach.templates import PitchEmail
   p = PitchEmail(subject='Test',body='Test',vertical='mastersheets',domain='test.com')
   r = send_pitch(p, 'your-own-email@example.com')
   print(r)
   "
   ```
3. Update Render environment variables
4. Trigger redeploy

---

## 10. Cron / scheduling

The agent is invoked as a cron job. On Render, use a Render Cron Job service
pointed at the same repo, or trigger via GitHub Actions:

```yaml
# .github/workflows/bb7_outreach.yml
on:
  schedule:
    - cron: "0 10 * * 1-5"  # weekdays 10:00 UTC

jobs:
  outreach:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: pip install -e ".[bb7]"
      - run: python -m sml_beast.outreach.agent
        env:
          BEAST_OUTPUT_ROOT: ${{ secrets.BEAST_OUTPUT_ROOT }}
          BB7_XRPL_WALLET_SEED: ${{ secrets.BB7_XRPL_WALLET_SEED }}
          BB7_SMTP_HOST: ${{ secrets.BB7_SMTP_HOST }}
          BB7_SMTP_USER: ${{ secrets.BB7_SMTP_USER }}
          BB7_SMTP_PASS: ${{ secrets.BB7_SMTP_PASS }}
          BB7_OPT_OUT_URL: ${{ secrets.BB7_OPT_OUT_URL }}
          BB7_OPERATOR_ADDRESS: ${{ secrets.BB7_OPERATOR_ADDRESS }}
          BB7_OPERATOR_SIGNATURE: ${{ secrets.BB7_OPERATOR_SIGNATURE }}
```

---

## 11. Escalation checklist

| Symptom | Action |
|---------|--------|
| Hot wallet balance below `2 × 5.00 = 10 USDC-eq` | Refill from cold wallet; do NOT lower the pitch cap |
| 3+ XRPL payment failures in 24h | Activate kill switch; check hot wallet balance + XRPL testnet status |
| Bounce rate > 2% during warmup | Activate kill switch; audit the enrichment source quality |
| Reply classified MANUAL_REVIEW unexpectedly | Review the raw reply in logs; update `parse_reply` if a new acceptance pattern is needed |
| State file corrupt on restart | Fresh state auto-loaded; check Render disk persistence settings |
| Dashboard returning 401 | Token mismatch; regenerate and update `DASHBOARD_AUTH_TOKEN` |
