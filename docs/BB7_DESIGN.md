# BB7 — Autonomous Backlink Outreach Agent

> **Status:** DESIGN — awaiting red-team review (Gemini) + operator approval (Timothy).
> **Implementation:** NONE. No code in this commit. Sign-off required before BB7 lands in `sml_beast/`.

## 0. Mandate

Consume `output/<vertical>/bounty_targets.json` (produced by BB4) and convert
the top N domains per cycle into **verified live backlinks** to the SML
property catalog, paying placement fees over x402 / XRPL only on
cryptographic proof of a live, indexable, dofollow link.

Non-goals:

- Mass-mail spam. The agent must pitch fewer, better targets — institutional
  outreach at agent scale, not consumer scale.
- Pre-payment for promises. The escrow lifecycle settles **post-publication**
  exclusively.
- Generic templated copy. Every pitch is gap-report-aware and vertical-keyed.

## 1. Threat model & ethical perimeter

| Threat | Mitigation |
|---|---|
| Spam-list categorization by mail providers | Volume cap (≤10 pitches/day total, ≤1/domain/14d), reputable from-address (SML operator's domain with SPF/DKIM/DMARC aligned), no link shorteners, no tracking pixels |
| CAN-SPAM / GDPR / PECR violations | Every email carries (a) physical operator address, (b) clear identification as a placement offer, (c) one-click opt-out that adds the domain to a permanent blocklist; agent never emails EU/UK contacts without an existing public placement offer page |
| Adversarial site responds with fake "link added" | Cryptographic verification loop (§5) — no settlement on operator's word alone |
| Wildcard DNS returning fake 200s on every check | Verification fetches the exact pitched URL and parses the DOM for the canonical `<a href>`; presence of the link in random sub-paths is treated as a red flag and triggers a manual review |
| Compromised agent or stolen secret bleeding the XRPL wallet | Daily-spend kill switch (§6), per-domain rate limit, hot-wallet float capped at 5× daily-ceiling — bulk funds in a cold operator wallet |
| Reputation damage from a single bad pitch | Manual review gate for the first 5 outreach attempts per vertical; mandatory operator dry-run sample after every prompt template change |

The agent operates under a **standing presumption of refusal**. Any
ambiguity in target eligibility, pitch tone, or verification signal
escalates to the operator queue rather than proceeding.

## 2. Architecture overview

```
output/<vertical>/bounty_targets.json
            │
            ▼
   ┌─────────────────────┐
   │  Target Enricher    │   /humans.txt, /.well-known/security.txt,
   │  (§3)               │   /.well-known/contact.txt — NOT mailto:
   └─────────┬───────────┘
             ▼
   ┌─────────────────────┐
   │  Eligibility Filter │   blocklist, rate-limit, freshness check
   └─────────┬───────────┘
             ▼
   ┌─────────────────────┐
   │  Pitch Generator    │   vertical-keyed template + gap-report
   │  (§4)               │   personalization
   └─────────┬───────────┘
             ▼
   ┌─────────────────────┐
   │  Manual Review Gate │   first N per vertical; opt-out after N=5
   └─────────┬───────────┘
             ▼
   ┌─────────────────────┐    [reject]   ┌────────────────────┐
   │  Send Pitch         │ ────────────► │ Mark refused, log  │
   └─────────┬───────────┘               └────────────────────┘
        [accepted]
             ▼
   ┌─────────────────────┐
   │  Escrow Commit      │   XRPL conditional escrow (§5)
   │  (§5)               │   condition = SHA-256(verification payload)
   └─────────┬───────────┘
             ▼
   ┌─────────────────────┐    [no link in 30d]   ┌──────────────┐
   │  Verification Loop  │ ───────────────────►  │ EscrowCancel │
   │  (§5.3)             │                       │ refund       │
   └─────────┬───────────┘                       └──────────────┘
        [link verified]
             ▼
   ┌─────────────────────┐
   │  EscrowFinish       │   placement fee settled to target wallet
   └─────────────────────┘
```

State persists to `output/<vertical>/outreach_state.json` (per-domain
lifecycle log) and `output/<vertical>/escrows.json` (active XRPL escrows
with sequence numbers + fulfillment hashes). The state files are the
source of truth — restart-safe; the in-memory queue is rebuilt on boot.

## 3. Target enrichment

Standard SEO bots scrape `mailto:` from footers and get filtered by spam
heuristics. This agent targets developer-centric endpoints that imply
the contact understands M2M protocols, which lifts conversion and avoids
the consumer-spam classification.

### Source priority (highest → lowest)

1. **`/.well-known/security.txt`** — RFC 9116; `Contact:` field is the
   canonical developer-reachable email. Sites that publish this opt into
   programmatic contact by definition.
2. **`/humans.txt`** — Convention from humanstxt.org; `Contact:` /
   `Author:` lines. Implies a maintained, developer-aware site.
3. **`/.well-known/contact.txt`** — Emerging convention; treat as 1-tier.
4. **`/humans.json`** — Rarer but structured; parse `contacts[].email`.
5. **Author profile pages** (`/about`, `/team`, `/authors/<slug>`) — last
   resort; parse only when the page declares an email that's clearly a
   public business address (not a personal account).
6. **`mailto:` footer scrape** — **DEPRIORITIZED**. Only used when the
   target has a public sponsorship/advertising page that explicitly
   invites cold outreach (e.g., `/advertise`, `/sponsor`, `/partner`).

### Eligibility filter — applied AFTER enrichment

- Domain not in `outreach_blocklist.txt`
- Last outreach to this domain ≥ 14 days ago (or never)
- Target email passes basic syntactic check and is not a role-based
  account on the spam-trap list (`abuse@`, `postmaster@`, `noreply@`)
- Domain's bounty `priority_score` ≥ `OUTREACH_MIN_PRIORITY` (default 6)
- Manual override flag respected in either direction

### Caching

Enrichment results land in `output/enrichment_cache/<domain>.json` with
a 30-day TTL. Re-enrichment is forced when the domain re-enters the
bounty list with a meaningfully different SERP signature.

## 4. Pitch generation

Two templates — one per vertical — drawing from the same canonical brief
the page generator already uses, with personalization from the gap report
for the keyword(s) that surfaced this domain.

### Template skeleton (vertical-agnostic)

```
Subject: {pers_subject}

Hi {first_name_or_team_handle},

I run engineering for ScriptMasterLabs. {pers_observation} — specifically
{pers_gap_finding}.

We publish {anchor_resource_title} ({anchor_url}) which covers exactly
that gap from the {vertical_perspective} angle. Would you consider linking
to it from {pers_target_url}?

To make it worth your time we settle a placement fee on the XRPL ledger
the moment the link goes live and our verification crawler confirms it
(usually within 24h of publication). Standard offer: {USDC} USDC,
non-negotiable, no follow-up emails.

If this isn't a fit, reply STOP and we won't reach out again. Our public
opt-out registry: {opt_out_url}.

— {operator_signature}
ScriptMasterLabs operator
SPF: {spf_status}  DKIM: {dkim_status}  DMARC: {dmarc_status}
```

### Per-vertical personalization

**MasterSheets:** `pers_observation` draws from the attack-angle codes
in the gap report — e.g., "I noticed your roundup ranks Google Sheets
and Excel side-by-side without addressing data sovereignty." The anchor
resource is the highest-priority MasterSheets page.

**IRL / x402:** `pers_observation` draws from the structural classification
— e.g., "Your post on x402 settlement focuses on the Coinbase facilitator;
I think your readers would benefit from the dual-chain XRPL + Xahau path
which is where institutional micropayments are actually clearing today."
The anchor resource is the highest-priority IRL/x402 page.

All template variables are required; the generator refuses to send a
pitch with any unfilled slot. No silent fallbacks.

## 5. x402 / XRPL payment integration — escrow model

**Cardinal rule:** the agent never pre-pays for a promise. Settlement is
conditional on cryptographically-verified link publication.

### 5.1 Lifecycle

| State | Trigger | XRPL action |
|---|---|---|
| `PROPOSED` | Pitch generated, not yet sent | — |
| `SENT` | Pitch dispatched | — |
| `ACCEPTED` | Target replies "yes" via reply parser or operator-confirmed | `EscrowCreate` with full placement fee |
| `AWAITING_PROOF` | Escrow on ledger; verifier polling | — |
| `SETTLED` | Verifier confirms live link | `EscrowFinish` releases funds to target wallet |
| `EXPIRED_REFUNDED` | 30 days elapsed without verification | `EscrowCancel` returns funds to operator wallet |
| `MANUALLY_VOIDED` | Operator killed the pitch | `EscrowCancel` |

### 5.2 Escrow construction (XRPL native)

- `EscrowCreate` with `Condition` = `PREIMAGE-SHA-256` hash of a
  per-pitch nonce known only to the verifier service
- `FinishAfter` = `pitch_accepted_at + 24h` (cooldown — operator can
  manually void within the first day if the target turns out to be a
  spam trap)
- `CancelAfter` = `pitch_accepted_at + 30d` (max verification window)
- `Destination` = target wallet address (collected during the acceptance
  reply or, if absent, a generic SML-operated payout address the target
  can claim by signing a message from the same email used in acceptance)

When verification succeeds the verifier reveals the preimage, the agent
submits `EscrowFinish` with the fulfillment, and the ledger releases.

**Why XRPL native (not the SqueezeOS in-memory `/api/settlement`):** the
SqueezeOS settlement engine is explicitly in-memory (per `CLAUDE.md`) and
resets on every redeploy. The 30-day verification window requires
persistent on-ledger state.

### 5.3 Verification loop

A daily cron task (`verify_outreach.py`):

1. Loads all `AWAITING_PROOF` escrows from `outreach_state.json`
2. For each pitched URL, fetches the live page (respecting `robots.txt`
   and a 10s timeout)
3. Parses the DOM and walks every `<a>` element
4. Confirms the link exists at the exact target slug from the pitch
5. Confirms `rel` does **not** contain `nofollow`, `sponsored`, or `ugc`
6. Confirms the anchor text matches one of the approved variants from
   the pitch (within a Levenshtein distance of 3 — exact match preferred)
7. Records the verification payload `{url, href, rel, anchor, ts}` and
   computes `SHA-256(payload || nonce)`
8. If the hash matches the escrow's `Condition`, submits `EscrowFinish`

If verification fails on day N, the loop retries on day N+1 with
exponential backoff up to the `CancelAfter` deadline. Three consecutive
4xx responses move the pitch to `MANUALLY_VOIDED` for operator review
(possible site migration; the human decides).

### 5.4 Anti-gaming

- Verifier fetches from a residential-IP proxy pool to detect cloaking
- Verifier checks Google's `site:<domain>` index after 7 days — if the
  page isn't indexed (target may have noindex'd it post-payment), flag
  for manual review
- Verifier re-checks weekly for 90 days post-settlement; if the link is
  removed within that window the domain is added to the permanent
  blocklist and noted in a public `link-rot-registry.json` (defensive
  publishing — discourages take-and-bail behavior)

## 6. Kill switch & guardrails

These are non-negotiable, hardcoded defaults. Operator can lower them
in config; raising them above the defaults requires a code edit and a
fresh deploy.

| Guardrail | Default | Configurable via |
|---|---|---|
| Daily USDC spend ceiling (hot wallet) | **0.50 USDC / 24h** | `OUTREACH_DAILY_CEILING_USDC` |
| Hot wallet max balance | **2.50 USDC** (5× ceiling) | `OUTREACH_HOT_WALLET_MAX` |
| Per-domain cooldown | **14 days** | `OUTREACH_DOMAIN_COOLDOWN_DAYS` |
| Pitches per 24h (global) | **10** | `OUTREACH_DAILY_PITCH_CAP` |
| Standard placement fee | **0.10 USDC** | `OUTREACH_STANDARD_FEE_USDC` |
| Max placement fee (single pitch) | **0.50 USDC** (matches daily ceiling) | hardcoded — code edit required |
| Manual review gate | **First 5 pitches per vertical** | `OUTREACH_MANUAL_REVIEW_N` |
| Verification window | **30 days** | `OUTREACH_VERIFY_WINDOW_DAYS` |
| Post-settlement monitoring | **90 days** | `OUTREACH_MONITOR_WINDOW_DAYS` |

### Hard stops (cannot be configured away)

- If 3 escrows in 24h fail verification → **freeze all outreach** for 7d
  and escalate to operator queue
- If any single pitch generates 2+ reply addresses from different
  domains claiming to be the target → **freeze**, possible compromise
- If the XRPL wallet balance drops below `2 × OUTREACH_STANDARD_FEE_USDC`
  → **freeze** new outreach until manual refill
- If the agent attempts to pitch a domain matching
  `*.gov`, `*.mil`, `*.edu`, or any domain in `outreach_hard_blocklist.txt`
  → **immediate refusal**, alert operator, log to public audit trail

### Kill switch primitive

Single file at `output/OUTREACH_KILL_SWITCH`. If it exists with any
content, the agent refuses to start any new pitch loop. Verification
loop continues (so in-flight escrows still settle / refund), but no new
outbound activity. Operator drops the file to shut everything down
instantly without a deploy.

## 7. Dashboard surface (BB6 extension)

New panel: `[OUTREACH]` — magenta-bordered, between the proxy state panel
and the per-vertical panels.

```
[OUTREACH] // M2M PLACEMENT
  Pitches sent (24h):     7 / 10
  Awaiting reply:        12
  Awaiting verification:  5
  Verified live (lifetime): 31
  USDC committed (escrow): 0.40
  USDC settled (lifetime): 3.10
  USDC refunded:          0.20
  Daily ceiling status:   OK (0.10 / 0.50 used)
  Kill switch:            INACTIVE
```

Tabular sub-panel: most recent 20 outreach events with state, domain,
vertical, and timestamp. Color-coded by state (cyan = SENT, amber =
AWAITING_PROOF, green = SETTLED, magenta = REFUNDED / VOIDED).

Aesthetic stays Beastmode — same palette guard tests will apply to the
new panel markup.

## 8. Implementation scope (when greenlit)

| Module | Purpose |
|---|---|
| `sml_beast/outreach/__init__.py` | Public surface |
| `sml_beast/outreach/enricher.py` | Source priority pipeline (§3) |
| `sml_beast/outreach/templates.py` | Vertical-keyed pitch templates (§4) |
| `sml_beast/outreach/escrow.py` | XRPL `EscrowCreate` / `Finish` / `Cancel` wrappers |
| `sml_beast/outreach/verifier.py` | Daily verification loop (§5.3) |
| `sml_beast/outreach/state.py` | Restart-safe per-domain state machine |
| `sml_beast/outreach/guardrails.py` | All caps, kill switch, hard stops (§6) |
| `sml_beast/outreach/agent.py` | Orchestrator entrypoint; cron-friendly |
| `tests/test_outreach_*.py` | Per-module tests; **full mock of XRPL** — no live ledger calls in unit tests |

Estimated stack-rank by risk:

1. **escrow.py** — highest blast radius; ledger writes; build last
2. **guardrails.py** — gates everything else; build first, lock with tests
3. **state.py** — restart-safe persistence; build second
4. **enricher.py + templates.py + verifier.py** — pure functions; parallel
5. **agent.py** — composition; trivial once everything below it is tested

## 9. Open questions for operator review

1. **From-address.** Use a fresh `outreach@scriptmasterlabs.com` mailbox or
   ride the operator's existing personal address? Recommend fresh + full
   SPF/DKIM/DMARC alignment so reputation is segregated.

2. **Reply parser** — natural-language acceptance classification. LLM-based
   (cheap, fast, occasionally wrong) or strict-regex / explicit-keyword
   (`YES / AGREE / ACCEPT`)? Recommend strict + LLM fallback flagged for
   manual review.

3. **First-cycle target count.** Hold to a daily cap of 3 (not the
   default 10) for the first 30 days while reputation builds? Recommend
   yes.

4. **Cold wallet** — should the operator hold the cold reserve on XRPL,
   Xahau, or split? Splitting hedges against single-chain outages but
   doubles operational overhead. Recommend XRPL-only for v1, Xahau
   migration once volume justifies it.

5. **Public opt-out registry.** Hosted at
   `https://www.scriptmasterlabs.com/outreach/opt-out`? The CAN-SPAM
   "one-click" requirement is satisfied by a `mailto:` link in the
   pitch, but a public registry is good faith and lifts deliverability.

6. **Audit trail.** Should every settled placement be published to a
   public JSON feed (`/outreach/settled.json`) for credibility? Risk:
   reveals which domains accept paid placements, possibly impacting
   their editorial standing. Recommend an opt-in flag during acceptance.

## 10. What this design explicitly is NOT

- **Not a link farm.** Volume cap is 10/day global; placement fees are
  visible on-ledger; opt-out is one click.
- **Not a guarantee.** Verification can fail. Escrow refunds are the
  feature, not a bug.
- **Not a replacement for editorial outreach.** The agent handles the
  long tail of low-priority targets at scale; the operator still
  personally pitches the top 5 strategic domains per quarter.
- **Not retroactive.** The agent does not pay for backlinks that already
  exist. It only commits funds for placements caused by its outbound
  pitch.

---

**Red-team this aggressively.** Specific angles to probe:

- Can a target accept the pitch, get the escrow created, publish a real
  link, collect the fee, then remove the link 24h later? (Yes —
  mitigated only by the public link-rot registry and permanent blocklist.
  Is that sufficient?)
- What's the legal status of paying for backlinks under Google's
  Webmaster Guidelines? (It violates them; Google reserves the right to
  deindex the destination page. The operator must accept this risk and
  potentially use `rel="sponsored"` — which defeats the purpose. **This
  is the elephant in the room and needs explicit operator decision.**)
- What's the reply-parser failure mode that leaks budget? (False-positive
  acceptance → escrow created on a non-acceptance → 30-day refund cycle
  ties up capital. Mitigated by reply-parser strict mode + manual gate.)
- Is the 0.50 USDC/day ceiling realistic, or is it so low that BB7 can
  never recover its development cost? (Open question — depends on
  acceptance rate and lifetime value of a single high-DR backlink.)

When you've torn this apart, we lock the design and I'll build it
module-by-module in dependency order, with `guardrails.py` shipping
first and `escrow.py` shipping last.
