# Security Policy

## Supported versions

Only the latest commit on `main`. This is a private operator tool;
no LTS branches.

## Reporting a vulnerability

Email security@scriptmasterlabs.com (PGP key on
`https://www.scriptmasterlabs.com/.well-known/security.txt`).

**Do NOT** open a public GitHub issue for security findings. Coordinated
disclosure window: 90 days from acknowledged report unless mutually
agreed otherwise.

What to include:
- Reproduction steps (commit hash + minimal POC)
- Affected component (proxy / dashboard / worker / BB7 module)
- Severity assessment (your CVSS or descriptive)
- Suggested remediation (optional but appreciated)

Acknowledgment SLA: 72 hours. Fix SLA: 7 days for HIGH/CRITICAL, 30
days for MEDIUM, 90 days for LOW.

## Threat model — what this codebase guards against

### In scope

- **Proxy abuse.** Forged `X-PAYMENT` envelopes are rejected via
  constant-time HMAC verification. Expired tokens rejected via the `exp`
  claim. Replay is mitigated by the short-TTL nonce (default 1h).
- **Dashboard data exfiltration.** All `/dashboard` + `/api/dashboard/*`
  routes are gated by Bearer token (`DASHBOARD_AUTH_TOKEN`).
  Failure-closed: without the env var, the routes don't register at all.
  Constant-time token comparison.
- **Output-tree path traversal.** Vertical names are whitelisted in
  `dashboard._safe_vertical()`. Unknown verticals return 404.
- **Hot-wallet drain (BB7).** Hard daily ceiling (20 USDC), hot-wallet
  max (100 USDC), per-pitch autonomy cap (5 USDC), per-domain cooldown
  (14 days), kill-switch file (`output/OUTREACH_KILL_SWITCH`). Worst
  case bleed is bounded at 20 USDC / 24h.
- **Adversarial reply parsing (BB7).** Strict regex only on the
  acceptance path. No LLM in the money-movement classifier. Reduces
  hallucinated-yes risk to zero.
- **Pitch-target compromise (BB7).** `.gov`, `.mil`, `.edu` blanket
  refusal. Custom blocklist file (`outreach_hard_blocklist.txt`).
  Manual review gate on the first 5 pitches per vertical.

### Out of scope

- **Render account compromise.** If the operator's Render dashboard
  credentials are stolen, the env vars (including `X402_PROXY_SECRET`
  and `BB7_XRPL_WALLET_SEED`) are exposed. Use 2FA on Render.
- **GitHub repo compromise.** Same risk surface as any private repo.
  Use SSH keys, 2FA, branch protection.
- **Upstream Serper.dev compromise.** We trust the SERP results
  returned. If Serper served adversarial content, the gap engine could
  be tricked into generating attack-angle text targeted at the
  attacker's choice. Out of our control.
- **XRPL ledger censorship.** Out of our control by design — XRPL is
  the substrate.

## Hot/cold wallet split (BB7)

The BB7 agent operates a **hot wallet only**. Cold reserve lives in a
separate operator-controlled wallet that the agent has no signing key
for. The hot wallet is refilled manually from cold; the agent never
sweeps in either direction.

Hard caps (see `outreach/guardrails.py`):
- `OUTREACH_HOT_WALLET_MAX_USDC = 100.00` (refusal floor)
- `OUTREACH_DAILY_CEILING_USDC = 20.00` (24h spend cap)
- `OUTREACH_STANDARD_FEE_USDC = 5.00` (per-pitch)
- `OUTREACH_MAX_AUTONOMY_FEE_USDC = 5.00` (agent's per-pitch hard cap)

If the hot wallet drops below 2× the standard fee (10 USDC), new
outreach freezes until manual refill. This prevents pulling from cold
reserves without operator action.

## Secret handling

| Secret | Where it lives | Rotation cadence |
|---|---|---|
| `SERPER_API_KEY` | Render env vars (sync:false) | On compromise |
| `X402_PROXY_SECRET` | Render env vars (sync:false) | Quarterly |
| `DASHBOARD_AUTH_TOKEN` | Render env vars (sync:false) | Monthly |
| `BB7_XRPL_WALLET_SEED` | Render env vars (sync:false) | Per-deploy if any compromise suspected |
| `BB7_SMTP_PASS` | Render env vars (sync:false) | Per SMTP provider policy |

NEVER commit any of these. `.env.example` documents them with empty
values only.

## Rotation procedure

1. Generate a new value (`openssl rand -hex 32` for HMAC/token-style)
2. Update the Render env panel
3. Render auto-redeploys
4. Verify health: `curl https://.../health` returns 200
5. Verify dashboard auth: open `?token=<new>` succeeds, `?token=<old>` 401s

## Public surfaces — what's externally reachable

| Route | Auth | Notes |
|---|---|---|
| `GET /health` | None | Liveness probe. Returns minimal metadata (network, price, total calls). |
| `GET /.well-known/x402` | None | Bazaar discovery. Public by protocol design. |
| `POST /api/v1/m2m/serp` | HMAC `X-PAYMENT` envelope | Proxy gate; rejects on missing/invalid/expired. |
| `GET /dashboard` | Bearer token | Failure-closed. `?token=<>` first-load convenience. |
| `GET /api/dashboard/*` | Bearer token (header only) | Query-param shortcut does NOT apply here. |

Everything else returns Flask's default 404.

## Out-of-band alerting

- **GitHub Dependabot** — weekly PRs for dependency bumps; security
  alerts as they fire
- **GitHub Actions** — CI failures notify on push/PR
- **Render** — deploy failures + health-check failures notify

There is no PagerDuty / Slack / SMS integration. Operator monitors via
GitHub + Render dashboards. This is acceptable for the current scale.
