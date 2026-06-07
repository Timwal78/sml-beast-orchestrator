# Changelog

All notable changes to sml-beast-orchestrator. Format roughly follows
Keep a Changelog (https://keepachangelog.com/) with operator-relevant
sections only.

## [Unreleased] — BB7 implementation in progress

### Sealed
- BB7 design v2 with no-custody mandate (`docs/BB7_DESIGN.md`)
  - Escrow tier removed entirely
  - Fire-and-forget Payment transactions only
  - Verifier decoupled from money movement (observation-only)

### Added (this phase)
- `sml_beast/outreach/guardrails.py` — kill switch, daily ceiling,
  pitch caps, hard blocklist enforcement
- Dashboard Bearer-token auth gate (failure-closed)
- CI/CD scaffold: ruff, mypy, GitHub Actions matrix (3.11 + 3.12),
  Dependabot weekly bumps, pip-audit advisory scan
- Project memory: `CLAUDE.md`, `SECURITY.md`, this `CHANGELOG.md`
- `pyproject.toml` rewritten with pinned deps, optional `[bb7]` and
  `[dev]` extras, ruff + mypy configuration

### Fixed
- `render.yaml` build command (referenced non-existent
  `requirements.txt`; corrected to `pip install -e ".[bb7]"`)
- Multiple ruff/mypy findings across the codebase (30 auto-fixed,
  7 hand-fixed); CI now green on all configured rules

### Added (shipping polish phase)
- Console scripts: `bb7`, `bb7-preflight`, `bb7-agent` (registered in
  `pyproject.toml [project.scripts]`). After `pip install -e ".[bb7]"`,
  operator can type `bb7 status` instead of the full `python -m ...` path.
- `tests/test_e2e_smoke.py` — true end-to-end integration test:
  bounty_targets.json → enrich → review gate → balance → XRPL → state →
  SMTP → thread map → opctl status → IMAP STOP reply → opt-out → second
  cycle blocked. Proves the integration surface holds across all 14
  outreach modules.

### Added (balance check phase)
- `outreach/balance.py` — XRPL hot wallet balance query; converts raw XRP to
  USDC-equivalent via BB7_XRP_PRICE_USDC; healthy/unhealthy classification
  vs 20 USDC threshold; degraded result on network error (no spam alerts)
- `agent.run_cycle()` pre-flight: confirmed unhealthy balance aborts cycle
  before any spend; network errors log a warning and proceed cautiously;
  injectable `balance_check_fn` for test isolation
- `preflight.check_hot_wallet_balance` — balance check is now part of the
  preflight validator
- `opctl balance` — query balance (exit 0=healthy, 1=low, 2=config error)
- `.github/workflows/bb7_balance_sweep.yml` — every 2h weekday balance check
- `agent` tests refactored to inject `balance_check_fn` (suite went from
  82s back to 2s)

### Added (alerts phase)
- `outreach/alerts.py` — Discord webhook operator alerts; types: KILL_SWITCH_
  ACTIVATED/DEACTIVATED, LOW_HOT_WALLET, MANUAL_REVIEW_BACKLOG, CYCLE_COMPLETE;
  per-type rate-limiting (24h window); injectable post_fn; failure-closed
  (no webhook env → silent no-op)
- `agent.run_cycle()` now emits CYCLE_COMPLETE + runs check_and_alert() after
  each cycle (best-effort; never breaks the cycle)
- `opctl alerts-sweep` — one-shot check for kill switch transition + backlog
- `.github/workflows/bb7_alerts_sweep.yml` — runs every 15 min; detects state
  changes between agent cycles

### Added (reply monitor phase)
- `outreach/reply_monitor.py` — IMAP poller; drains UNSEEN, classifies via
  dispatcher.parse_reply, routes OPTOUT → state.mark_opted_out (only mutation),
  ACCEPT/MANUAL_REVIEW → operator queue (append-only JSONL); thread map
  persists Message-ID → domain after each successful dispatch
- `agent.py` records dispatched Message-ID → domain in the thread map after
  each successful pitch (step 8 in the dispatch sequence)
- `opctl replies` — dump operator review queue
- `opctl poll` — one-shot IMAP poll
- `.github/workflows/bb7_reply_poll.yml` — cron polls inbox every 30min during
  business hours (UTC); runs `opctl poll`

### Added (opctl phase)
- `outreach/opctl.py` — operator control CLI; subcommands for `status`,
  `opt-out`, `review-clear`, `review-clear-all`, `kill on/off`, `metrics`,
  `metrics-stats`, `domain`, `recent`, `dry-run`. None mutate live systems
  (no XRPL submit, no SMTP send); dry-run runs the full pipeline with both
  disabled.

### Added (preflight phase)
- `outreach/preflight.py` — preflight validator; refuses to enable cycle until
  env vars / XRPL wallet / SMTP auth / DNS / output dir / kill switch /
  manual review state all check out. Exit codes: 0=green, 1=warn, 2=fail.
  Never sends a pitch, never submits XRPL, never mutates state.
- CI `preflight` job — runs offline preflight on every PR; blocks merge only
  on hard FAILs (warnings are expected on fresh checkouts)

### Added (continued this phase)
- `outreach/enricher.py` — contact discovery; security.txt → humans.txt →
  contact.txt → humans.json → author pages → sponsorship pages; 30-day TTL
  cache; role-account rejection; atomic POSIX writes
- `outreach/templates.py` — vertical-keyed pitch templates; required-field
  enforcement (TemplateMissingVariableError on any unfilled slot); attack-angle
  → observation mapping for mastersheets + xrpl_x402
- `outreach/dispatcher.py` — SMTP send via outreach@infrastructure.scriptmasterlabs.com;
  CAN-SPAM footer; strict reply parser (OPTOUT / ACCEPT / MANUAL_REVIEW);
  no LLM in acceptance path; injectable smtp_factory for test isolation
- `outreach/verifier.py` — observation-only link presence checker; FOUND /
  NOT_FOUND / SUSPICIOUS / ERROR classifications; wildcard-DNS guard;
  append-only JSONL log; conversion_stats() for dashboard
- `outreach/agent.py` — composition entrypoint; cron-friendly run_cycle();
  dry-run mode (BB7_OUTREACH_DRY_RUN=1); kill switch propagation
- Dashboard `[OUTREACH]` panel — domain counts by state, daily ceiling status,
  kill switch indicator, conversion/dofollow rates, 20-event timeline;
  state color-coded (cyan=PROPOSED, amber=DEMO_SENT, green=verified, magenta=opted-out)

### Pending (next commits)
- `RUNBOOK.md` — operator deployment + key rotation procedures

## [0.2.0] — 2026-06-07 (BB1–BB6 shipped)

### Added
- **BB1.** SERP-Gap engine (`intel/serp_gap.py`) — structural
  classification, severity scoring, PAA harvest, semantic clusters,
  vertical-keyed attack angles
- **BB2.** Vertical-keyed JSON-LD schema factory (`content/generator.py`)
  — Product + SoftwareApplication + FAQPage for MasterSheets;
  Product + Organization + TechArticle + FAQPage for IRL/x402
- **BB3.** Internal link graph (`content/link_graph.py`) —
  cross-vertical authority bleeding
- **BB4.** Backlink-target finder (`intel/backlink_targets.py`) —
  M2M bounty list with class-weighted ranking
- **BB5.** Single-service deployment — Procfile, render.yaml
- **BB6.** Beastmode operator dashboard (`dashboard.py`) — read-only,
  Beastmode aesthetic, sub-routes for state/bounty/pages
- Hybrid x402 proxy — `/api/v1/m2m/serp` route, port 4020,
  HMAC + X-PAYMENT preserved

### Architectural rulings (operator)
- Isolation: this repo is NEVER pushed to `timwal78/squeezeos`
- No demo data / no hardcoded metrics
- Strict Beastmode aesthetic enforced by guard tests
- No emojis in code/logs/commits

## [0.1.0] — initial scaffold

- Multithreaded x402 SEO orchestrator skeleton
- Worker abstract base + MasterSheets/XRPL implementations
- Canonical product briefs (`content/briefs.py`)
- Serper.dev adapter
