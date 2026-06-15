# sml-beast-orchestrator — Operator Memory

> Project memory for AI-assisted sessions. Read this before making any change.
> Last sealed: 2026-06-07 (commit `66e98ef`, BB1–BB6 + BB7-guardrails shipped).

## What this is

A standalone autonomous SEO orchestrator that:

1. Pulls live SERPs via an internal x402-protocol facilitator-proxy
   (HMAC-gated; speaks Bazaar-compatible `X-PAYMENT` envelope)
2. Runs each SERP through a structural gap analyzer (BB1) that classifies
   incumbents, extracts PAA, harvests semantic clusters, and emits
   vertical-keyed competitive attack angles
3. Generates MDX landing pages + vertical-keyed JSON-LD schemas (BB2)
   with cross-vertical contextual link blocks (BB3)
4. Harvests external backlink-target domains into a ranked
   bounty-targets.json per vertical (BB4)
5. Surfaces all of the above on a read-only operator dashboard (BB6)
   gated by Bearer-token auth (failure-closed)
6. **BB7 (in flight):** autonomous outreach agent — sends fire-and-forget
   demo Payments to enriched contacts, dispatches pitch emails, observes
   conversion via verifier loop. NEVER holds funds. NEVER escrows.

## NON-NEGOTIABLE OPERATOR RULINGS

Any change that conflicts with these must be flagged to the operator
BEFORE shipping. These have been locked across multiple turns and
override default behavior.

### 1. ISOLATION — this repo is NOT `timwal78/squeezeos`

This is `Timwal78/sml-beast-orchestrator`, a standalone weapon. It must
NEVER share a remote, a Render service group, a CI/CD pipeline, or a
GitHub Actions secrets scope with the SqueezeOS trading engine. The
service name in `render.yaml` is `sml-beast-orchestrator` precisely
because cross-contamination of the trading engine's deployment pipeline
is a hard non-starter.

**If you find a `timwal78/squeezeos` remote on this working tree, stop.
Do not push. Surface to the operator.**

### 2. NO CUSTODY

SML does not custody funds — not for customers, not for backlink
targets, not for the BB7 outreach agent. This was the operator's
emphatic ruling (twice in one message) and overrides the v1 BB7 design.

Practical implications:
- BB7 sends unconditional `Payment` transactions only. No escrow.
- No `EscrowCreate` / `EscrowFinish` / `EscrowCancel` codepaths.
- No state machine where SML controls fund release/refund.
- Once a payment leaves the hot wallet it is gone from SML's control.
- Verifier exists ONLY for conversion analytics — never gates money.

See `docs/BB7_DESIGN.md` §5 for the full no-custody payment model.
The v1 escrow design is documented as REMOVED for clarity.

### 3. NO PUBLIC LEDGER OF PAID PLACEMENTS

BB7 ships a public opt-out registry at
`https://www.scriptmasterlabs.com/outreach/opt-out` (CAN-SPAM
compliance, good faith). It DOES NOT ship a public list of domains
that received demo payments. Publishing that set is operationally
equivalent to handing Google's webspam team a signed confession.

Code-level enforcement:
- Dashboard auth gate (failure-closed) protects all paid-placement data
- Internal-only path convention: `output/_internal/outreach_ledger.jsonl`
- Files under `_internal/` are explicitly excluded from any dashboard
  enumeration route or future public serializer

### 4. NO DEMO DATA / NO HARDCODED METRICS

If live data isn't available, return `"Awaiting Data"` or a real error.
Never invent numbers, never insert placeholder ticker symbols, never
emit `Math.random()`-driven progress bars that look real.

Mirrors the SqueezeOS Developer Manifesto rule (this isn't SqueezeOS
but the same operator). Tests fail if introduced.

### 5. FAILURE-CLOSED AUTH

Every externally-reachable surface that surfaces operator data requires
auth. If the auth env var is unset, the routes do NOT register (Flask
returns its default 404). There is no soft-fallback path that serves
operator data open.

Currently enforced on:
- `/dashboard` + `/api/dashboard/*` (Bearer via `DASHBOARD_AUTH_TOKEN`)

When BB7 outreach panel lands, it inherits the same gate.

### 6. STRICT BEASTMODE AESTHETIC

The operator dashboard has zero-tolerance aesthetic rules: pure jet
black backgrounds (`#000000` / `#050505` / `#0a0a0a`), neon accents
only (cyan / magenta / electric green / amber), JetBrains Mono via
Google Fonts, sharp corners (`border-radius: 0 !important` enforced
globally), terminal-style layout. NO grays. NO bootstrap. NO rounded
corners. 8 aesthetic guard tests in `tests/test_dashboard.py` will
fail loudly if anyone slips in `#cccccc`, `#888`, bootstrap classes,
or `border-radius: <anything other than 0>`.

### 7. NO EMOJIS IN CODE / LOGS / COMMITS

Project convention: no emojis unless the operator explicitly requests
them. Operator grep workflows + log-forwarding pipes don't play nice
with leading/trailing emojis in critical lines.

## Architecture invariants

### Service topology

Single Render web service running one Python process that hosts:
- Flask x402 facilitator-proxy on `0.0.0.0:$PORT`
- Operator dashboard on the same Flask app (gated by `DASHBOARD_AUTH_TOKEN`)
- N worker threads (one per vertical), each pulling SERPs over `127.0.0.1:$PORT`

Workers reach the proxy over loopback. The proxy is the only egress
point that touches the operator's Serper.dev key.

### Data flow per keyword

```
serp(kw) -> backlinks.ingest()        [BB4: bounty harvest, always]
         -> analyze(vertical)         [BB1: gap + attack angles]
         -> priority gate             [skip if score < MIN_PRIORITY]
         -> synthesize_page_brief()   [overlay canonical + gap]
              -> write_page()
                   -> MDX body (value props, attack angles,
                                PAA, cross-link, related, CTA)
                   -> schema.jsonld (vertical-keyed)
[end of silo]
         -> backlinks.flush()         [BB4: write bounty_targets.json]
```

### In-memory stores reset on restart

- Proxy ledger (`_ledger` in `x402_proxy.py`)
- All caches in `serp_gap.py`
- `BacklinkTargetFinder` accumulator (flushed to disk at silo end)

This is intentional. Persistent state belongs in the output tree, not
in process memory.

### `_internal/` convention

Anything under `output/<vertical>/_internal/` is operator-only.
Dashboard routes refuse to serve files matching `_*`. BB7 outreach
ledgers + escrow records (when they existed, RIP) all live there.

### Path resolution

`BEAST_OUTPUT_ROOT` env var is the single source of truth for the
output tree location. All three modules (`orchestrator`, `dashboard`,
`outreach/guardrails`) honor it via lazy `_output_root()` helpers so
tests can override per-case. Hardcoded `Path("output/...")` is a bug.

### Hybrid x402 proxy

The proxy route is `/api/v1/m2m/serp` on port `4020` (locally) or
`$PORT` (on Render). HMAC validation + `X-PAYMENT` envelope are
NON-NEGOTIABLE — never downgrade these for any reason.

## Module ownership

| Module | Owns | Does NOT own |
|---|---|---|
| `intel/serp_gap.py` | Incumbent classification, gap scoring, attack angles, PAA harvest | The proxy fetch (workers) |
| `intel/backlink_targets.py` | Domain harvest, scoring, ranking, JSON serialization | Pitch dispatch (BB7) |
| `content/generator.py` | MDX body, frontmatter, JSON-LD factory | Gap analysis |
| `content/link_graph.py` | Cross-vertical contextual link block | All else |
| `workers/base.py` | Per-keyword lifecycle, lock-stepping ingest -> analyze -> generate | Vertical-specific config |
| `adapters/x402_proxy.py` | Wire protocol, ledger mutation, dashboard mounting | Business logic |
| `adapters/serper.py` | Upstream Serper.dev HTTP, error normalization | Anything else |
| `dashboard.py` | Read-only UI + JSON endpoints, auth gate, Beastmode aesthetic | State mutation |
| `outreach/guardrails.py` | Kill switch, fee/pitch caps, blocklist, daily ledger | Per-domain state |
| `outreach/state.py` (pending) | Per-domain cooldown, manual review counter, warmup auto-detect, restart-safe persistence | Money movement |
| `outreach/xrpl_client.py` (pending) | `send_payment(dst, amt) -> tx_hash` ONLY. No escrow primitives. | Anything else |

## Testing discipline

- All tests are `unittest`-based, not pytest. Don't rewrite.
- Discover via `python -m unittest discover -s tests`
- Required env for the suite: `SERPER_API_KEY=test SERPER_API_KEY=test`
  (just dummy values; the proxy never makes outbound calls in tests)
- Aesthetic guard tests in `test_dashboard.py` enforce the Beastmode
  palette + corner rules — DO NOT bypass them
- Concurrency tests verify `_ledger_lock` actually serializes
- When adding a module: ship tests in the same commit, not later

## CI gates

GitHub Actions runs on every push + PR:
1. `ruff check` — lint
2. `ruff format --check` — format
3. `python -m mypy sml_beast` — type check (via -m so stubs resolve)
4. `python -m unittest discover -s tests` — matrix 3.11 + 3.12
5. `pip-audit` — advisory only, doesn't block merges

Merging is gated on jobs 1–4 (audit is advisory).

## Common pitfalls

- **Don't** add `requirements.txt`. Deps live in `pyproject.toml` only.
  `render.yaml` builds via `pip install -e ".[bb7]"`.
- **Don't** silently rewrite operator-supplied code. Surface deltas
  explicitly in the commit message.
- **Don't** add public-facing routes without auth.
- **Don't** assume Render disk persists across deploys. Output tree is
  ephemeral unless an explicit persistent disk is configured (it isn't).
- **Don't** push a commit with a failing `mypy sml_beast` or
  `ruff check sml_beast tests`.

## Glossary

- **BB1–BB7** — "Building Block" numbering for the architecture turns.
  See commit messages and `docs/BB7_DESIGN.md`.
- **Beastmode** — the operator's aesthetic + posture mandate. Strict.
- **Bounty list** — `output/<vertical>/bounty_targets.json`, ranked
  external domains for backlink placement (BB4 output, BB7 input).
- **Cross-link block** — the cross-vertical contextual link injected
  by `link_graph.render_cross_link_block()` per BB3.
- **Hot wallet** — the BB7 outreach agent's XRPL operational wallet.
  Hard-capped at 100 USDC (5x daily ceiling). Cold reserve in a
  separate operator-controlled wallet.
- **Bazaar** — Coinbase's CDP x402 discovery protocol. Our proxy is
  Bazaar-compatible (see `/.well-known/x402`).
