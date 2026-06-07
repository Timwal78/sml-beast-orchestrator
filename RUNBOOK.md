# SML Beast Orchestrator — Operator Runbook

Deployment, configuration, and incident response procedures.

---

## 1. Initial deployment (Render)

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

## 2. XRPL wallet setup

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

## 3. Kill switch

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

## 4. Manual review gate

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

## 5. Dashboard access

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

## 6. Opt-out handling

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

## 7. State file recovery

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

## 8. SMTP credentials rotation

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

## 9. Cron / scheduling

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

## 10. Escalation checklist

| Symptom | Action |
|---------|--------|
| Hot wallet balance below `2 × 5.00 = 10 USDC-eq` | Refill from cold wallet; do NOT lower the pitch cap |
| 3+ XRPL payment failures in 24h | Activate kill switch; check hot wallet balance + XRPL testnet status |
| Bounce rate > 2% during warmup | Activate kill switch; audit the enrichment source quality |
| Reply classified MANUAL_REVIEW unexpectedly | Review the raw reply in logs; update `parse_reply` if a new acceptance pattern is needed |
| State file corrupt on restart | Fresh state auto-loaded; check Render disk persistence settings |
| Dashboard returning 401 | Token mismatch; regenerate and update `DASHBOARD_AUTH_TOKEN` |
