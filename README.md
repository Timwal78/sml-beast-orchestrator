# sml-beast-orchestrator

Autonomous x402-driven SEO orchestrator + BB7 backlink outreach agent for
ScriptMasterLabs verticals (MasterSheets, XRPL/x402 infrastructure).

**SML DOES NOT CUSTODY.** Every BB7 outbound payment is fire-and-forget on
the XRP Ledger. Once the demo USDC leaves the hot wallet, it's gone from
SML's control — no escrow, no clawback, no settlement window where SML
could reverse the payment. See [`docs/BB7_DESIGN.md`](docs/BB7_DESIGN.md) §5.

---

## What's in the box

| Component | Module | Status |
|---|---|---|
| SERP-Gap engine + attack angles | `sml_beast/intel/serp_gap.py` | shipped |
| JSON-LD schema factory (vertical-keyed) | `sml_beast/content/generator.py` | shipped |
| Internal cross-vertical link graph | `sml_beast/content/link_graph.py` | shipped |
| M2M backlink-target finder | `sml_beast/intel/backlink_targets.py` | shipped |
| x402 facilitator proxy | `sml_beast/adapters/x402_proxy.py` | shipped |
| Beastmode operator dashboard | `sml_beast/dashboard.py` | shipped |
| BB7 outreach guardrails (kill switch, ceiling, caps) | `sml_beast/outreach/guardrails.py` | shipped |
| BB7 atomic state machine (TOCTOU-closed) | `sml_beast/outreach/state.py` | shipped |
| BB7 XRPL client (no-custody, no-escrow) | `sml_beast/outreach/xrpl_client.py` | shipped |
| BB7 contact enricher (security.txt → humans.txt → ...) | `sml_beast/outreach/enricher.py` | shipped |
| BB7 pitch template renderer | `sml_beast/outreach/templates.py` | shipped |
| BB7 SMTP dispatcher + strict reply parser | `sml_beast/outreach/dispatcher.py` | shipped |
| BB7 observation-only verifier | `sml_beast/outreach/verifier.py` | shipped |
| BB7 IMAP reply monitor | `sml_beast/outreach/reply_monitor.py` | shipped |
| BB7 hot-wallet balance check | `sml_beast/outreach/balance.py` | shipped |
| BB7 Discord operator alerts | `sml_beast/outreach/alerts.py` | shipped |
| BB7 preflight validator | `sml_beast/outreach/preflight.py` | shipped |
| BB7 composition entrypoint | `sml_beast/outreach/agent.py` | shipped |
| BB7 operator CLI (`bb7`) | `sml_beast/outreach/opctl.py` | shipped |

---

## Install

```bash
cp .env.example .env
# fill in SERPER_API_KEY, X402_PROXY_SECRET, DASHBOARD_AUTH_TOKEN,
# BB7_XRPL_WALLET_SEED, BB7_SMTP_*, BB7_OPT_OUT_URL, BB7_OPERATOR_*
pip install -e ".[bb7]"
```

Installs three console scripts:

- `bb7` — operator control surface (`bb7 status`, `bb7 opt-out`, `bb7 kill on`, ...)
- `bb7-preflight` — validates env vars, XRPL wallet, SMTP auth, DNS, balance
- `bb7-agent` — runs one outreach cycle (used by the GitHub Actions cron)

---

## Run

### Orchestrator (BB1–BB6: SERP harvest + page generation)

```bash
python -m sml_beast.orchestrator
```

Starts the x402 facilitator proxy on `$PORT` (or 4020 locally) and one worker
thread per vertical. Workers harvest SERPs, score gaps, generate vertical-keyed
JSON-LD + MDX into `output/<vertical>/<slug>/`. Every page has
`needs_human_review: true` in its frontmatter — nothing auto-publishes.

### Outreach agent (BB7: M2M backlink placement)

```bash
bb7-preflight                          # validate config (exit 0/1/2)
bb7 review-clear-all mastersheets      # clear the manual review gate
bb7-agent                              # run one outreach cycle
bb7 status                             # see what happened
```

The agent reads `output/<vertical>/bounty_targets.json` (produced by BB4),
enriches contacts via `security.txt`/`humans.txt`/etc., reserves spend
through the atomic state machine, fires a fire-and-forget XRPL `Payment`,
SMTPs a vertical-keyed pitch with the tx hash, and records the
`Message-ID → domain` thread map for reply correlation.

---

## Architecture at a glance

```
                                   ┌──────────────────┐
              SERP harvest          │  bounty_targets  │
              (BB1 + BB4)    ───►   │     .json        │
                                    └────────┬─────────┘
                                             │
        ┌────────────────────────────────────┼────────────────────────────┐
        │  BB7 outreach cycle (cron / bb7-agent)                          │
        │                                                                  │
        │  enrich ─► review gate ─► atomic_reserve ─► XRPL ─► SMTP ─► state│
        │     │            │              │            │       │           │
        │   security.txt  +1/5          ceiling +    fire-   thread map    │
        │   humans.txt    on send       autonomy    forget   M-ID→domain   │
        │   etc.                        cap                                 │
        │                                                                  │
        └────────────────┬──────────────────────────┬──────────────────────┘
                         │                          │
                         ▼                          ▼
              ┌────────────────────┐     ┌─────────────────────┐
              │  IMAP reply poller │     │  90d link verifier  │
              │  STOP → opt-out    │     │  observation only;  │
              │  YES → operator    │     │  no money movement  │
              │  queue (JSONL)     │     └─────────────────────┘
              └────────────────────┘
                         │
                         ▼
              ┌────────────────────┐
              │  Discord alerts +  │
              │  Beastmode panel   │
              └────────────────────┘
```

---

## Operator commands

```bash
bb7 status                    # full state snapshot (counts, ledger, kill switch)
bb7 balance                   # XRPL hot wallet balance + USDC-equivalent
bb7 opt-out example.com       # permanent blocklist
bb7 review-clear mastersheets # record one manual review completed
bb7 review-clear-all xrpl_x402
bb7 kill on                   # halt all dispatch immediately
bb7 kill off                  # resume
bb7 metrics                   # conversion_metrics.jsonl dump
bb7 metrics-stats             # aggregate conversion rate
bb7 domain example.com        # single-domain lifecycle entry
bb7 recent 10                 # last 10 state changes
bb7 replies                   # operator review queue
bb7 poll                      # one-shot IMAP drain + classify
bb7 alerts-sweep              # kill switch + backlog Discord check
bb7 dry-run                   # full cycle, no XRPL or SMTP
```

Or via Makefile shortcuts (see [`Makefile`](Makefile)):

```bash
make test       make lint       make preflight    make dry-run
make balance    make status     make replies      make ci
```

---

## Cron schedule (GitHub Actions)

| Workflow | Schedule | Purpose |
|---|---|---|
| `bb7_outreach.yml` | weekdays 10:00 UTC | Run one full outreach cycle |
| `bb7_reply_poll.yml` | `*/30 13-22 * * 1-5` | IMAP poll + classify |
| `bb7_alerts_sweep.yml` | every 15 min | Kill switch + backlog check |
| `bb7_balance_sweep.yml` | every 2h weekdays | Hot wallet balance check |
| `ci.yml` | every push/PR | Lint + mypy + tests + preflight + pip-audit |

---

## Hard rules (non-negotiable)

1. **No demo data.** If `SERPER_API_KEY` is unset, the orchestrator refuses
   to start. If `BB7_XRPL_WALLET_SEED` is unset, BB7 refuses to instantiate.
2. **No custody.** BB7 issues only `Payment` transactions. No `EscrowCreate`.
   No `EscrowFinish`. No `EscrowCancel`. A code-level guard test
   (`NoCustodyArchitectureTests`) fails loudly if anyone reintroduces them.
3. **Failure-closed everywhere.** Missing `DASHBOARD_AUTH_TOKEN` → dashboard
   routes don't register (404, not 401). Missing `BB7_DISCORD_ALERT_WEBHOOK`
   → alerts silently no-op (zero spam). Manual review gate ACTIVE by default.
4. **Strict reply parsing.** Acceptance regex is the only acceptance path.
   No LLM in the money-movement loop. Ambiguous replies go to the operator
   queue, not to a follow-up Payment.

---

## Quality gates

```bash
make ci   # equivalent to: make lint && make typecheck && make test
```

- **ruff** — `E F I B UP S PLE RUF` rule selection, line length 100, py311 target
- **mypy** — `no_implicit_optional`, `check_untyped_defs`, `warn_unused_ignores`
- **unittest** — matrix 3.11 + 3.12 on every PR
- **pip-audit** — advisory CVE scan (continue-on-error in CI; operator triages weekly)
- **End-to-end smoke test** — `tests/test_e2e_smoke.py` exercises the full pipeline
  in one shot (enrich → XRPL → state → SMTP → IMAP STOP → opt-out → second-cycle block)

---

## Repo placement

Standalone repo — zero coupling to SqueezeOS. No shared CI, no shared env,
no shared git history. See [`CLAUDE.md`](CLAUDE.md) and
[`docs/BB7_DESIGN.md`](docs/BB7_DESIGN.md) for the architectural mandates.

For operator deployment, key rotation, and incident response, see
[`RUNBOOK.md`](RUNBOOK.md).
