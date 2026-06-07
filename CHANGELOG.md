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
