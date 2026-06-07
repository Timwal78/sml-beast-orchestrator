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

### Pending (next commits)
- `outreach/state.py` — per-domain cooldown, manual review counter,
  warmup auto-detect, restart-safe persistence
- `outreach/xrpl_client.py` — `send_payment(dst, amt) -> tx_hash` only
- `outreach/enricher.py` — security.txt / humans.txt source priority
- `outreach/templates.py` — vertical-keyed pitch templates
- `outreach/dispatcher.py` — SMTP send with SPF/DKIM/DMARC alignment
- `outreach/verifier.py` — observation-only link presence checker
- `outreach/agent.py` — composition entrypoint
- Dashboard outreach panel (BB6 extension)
- `RUNBOOK.md` — operator deployment + rotation procedures

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
