# sml-beast-orchestrator

Autonomous x402-driven SEO orchestrator. Spins up concurrent worker threads
to scale two ScriptMasterLabs verticals in parallel:

- **MasterSheets** — Google Sheets alternative, one-time payment, BYOK AI, data sovereignty.
- **XRPL / x402 agentic infrastructure** — sub-50 ms settlement, zero API keys, M2M stablecoin rails.

## Architecture

```
+-----------------------------+
|  scripts/run_beast.py       |  CLI entry
+--------------+--------------+
               |
               v
+-----------------------------+
|  sml_beast.orchestrator     |  ThreadPoolExecutor
|   - launches x402 proxy     |
|   - launches one worker     |
|     thread per vertical     |
+------+----------------+-----+
       |                |
       v                v
+-------------+ +-----------------+
| MasterSheets| | XrplX402 Worker |   speak pure x402 (X-PAYMENT)
|   Worker    | |                 |
+------+------+ +--------+--------+
       |                 |
       +--------+--------+
                |
                v
+--------------------------------------+
|  sml_beast.adapters.x402_proxy       |  internal facilitator
|   - /x402/search  (gated)            |
|   - /.well-known/x402  (discovery)   |
+----------------+---------------------+
                 |  (HMAC-signed internal tokens; bridges to subscription
                 |   until a public x402 SERP provider ships)
                 v
+--------------------------------------+
|  Serper.dev (real subscription key)  |  live Google SERP, PAA, related
+--------------------------------------+
```

## Why a proxy

The operator approved a deliberate adapter pattern: agents speak pure x402
(`X-PAYMENT` header, `accepts[]` 402 envelope, base64 paymentPayload).
The proxy validates each envelope and fires the operator's Serper.dev key
upstream. **The SERP data is real, end to end.** Only the payment layer is
internal — and the moment a public x402 SERP provider lists on CDP Bazaar,
swap `x402_proxy._fetch_upstream` to forward the agent's `X-PAYMENT` header
directly and delete this proxy. Workers don't change.

## No fake data

If `SERPER_API_KEY` is unset, `SerperClient.__init__` raises immediately
and the orchestrator refuses to start. No placeholder SERPs, no stub
responses. Same for `X402_PROXY_SECRET` — without it, internal tokens
cannot be minted and the orchestrator aborts.

## Output discipline

Generated MDX + JSON-LD lands under `output/<vertical>/<slug>/`. Every page
has `needs_human_review: true` in frontmatter; nothing auto-deploys to
`scriptmasterlabs.com`. The operator reviews and ships.

## Run

```bash
cp .env.example .env
# fill in SERPER_API_KEY + X402_PROXY_SECRET
pip install -e .
python -m scripts.run_beast               # both verticals
python -m scripts.run_beast --only xrpl_x402
```

## Test

```bash
python -m unittest tests.test_smoke -v
```

## Repo placement

This is a standalone repository — zero coupling to SqueezeOS. No shared CI,
no shared env, no shared git history.
