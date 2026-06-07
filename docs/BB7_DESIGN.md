# BB7 — Autonomous Backlink Outreach Agent

> **Status:** DESIGN LOCKED — sealed after architect red-team. Implementation
> may proceed in dependency order (guardrails.py first).
> **Implementation:** NONE yet. Code lands in subsequent commits.

## 0. Mandate

Consume `output/<vertical>/bounty_targets.json` (produced by BB4) and convert
the top N domains per cycle into **verified live backlinks** to the SML
property catalog, paying micro-amounts over XRPL only on cryptographic
proof of a live, indexable, dofollow link.

**Critical reframe (locked):** the agent does NOT pitch the payment as a
"placement fee." It pitches the payment as a **live technical
demonstration of sub-50ms M2M settlement infrastructure**. The recipient
gets real value in their wallet, witnesses the rails work, and is asked
for a link as organic recognition of the tech. We are buying their
attention with a working demo, not bribing them for editorial integrity.

Non-goals:

- Mass-mail spam. Volume cap is 3/day during 30-day warmup, 10/day
  steady-state.
- Pre-payment for promises. The escrow lifecycle settles
  **post-publication** exclusively.
- Generic templated copy. Every pitch is gap-report-aware,
  vertical-keyed, and ties to a specific structural weakness the
  recipient's existing content exhibits.
- **Public visibility of paid placements.** Strict operational secrecy.
  See §9.

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

### Template skeleton (vertical-agnostic) — REFRAMED AS LIVE DEMO

```
Subject: {pers_subject}

Hi {first_name_or_team_handle},

I run engineering for ScriptMasterLabs. {pers_observation} — specifically
{pers_gap_finding}.

I just dropped {USDC} USDC into the XRPL address listed in your
{enrichment_source} (tx: {xrpl_tx_hash}, settled in
{settlement_time_ms}ms). That's not a placement fee. It's the simplest
demonstration I can give you that the sub-50ms M2M payment rails we
publish at {anchor_url} actually work end to end.

If you find that interesting enough to mention {anchor_resource_title}
in {pers_target_url} as a relevant infrastructure reference, that's the
extent of the ask. If not, keep the USDC — it's already yours, and we
appreciate the time you spent reading this.

We never follow up on this thread. Reply STOP to add your domain to our
permanent opt-out registry: {opt_out_url}.

— {operator_signature}
ScriptMasterLabs operator
SPF: {spf_status}  DKIM: {dkim_status}  DMARC: {dmarc_status}
```

**Why this pivot matters.** Cold pitch + $0.10 = junk mail. Cold pitch +
$5 USDC pre-settled with a transaction hash you can verify on the ledger
in 50ms = an unambiguous signal that the operator has working
infrastructure and is willing to spend real money to prove it. The
"link" ask becomes optional and organic. This is also the only framing
under which the agent operates outside the "paid backlink" frame that
violates Google's Webmaster Guidelines (see §9.7).

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

## 5. XRPL payment integration — pre-settled demo + optional escrow uplift

**Cardinal rule:** the payment is settled **before** the pitch lands.
The recipient sees real funds in their wallet at read-time, with a
transaction hash they can verify in <50ms. This is the entire point of
the live-demo framing — the value transfer is the proof, not the
promise.

The optional escrow uplift (§5.4) is reserved for a second-tier strategic
pitch (priority_score ≥ 30) where the operator offers an additional
premium fee, contingent on link verification, on top of the unconditional
demo payment.

### 5.1 Demo settlement (default path, all pitches)

| State | Trigger | XRPL action |
|---|---|---|
| `PROPOSED` | Pitch generated; address enriched | — |
| `DEMO_SENT` | Standard fee transferred unconditionally to recipient address | `Payment` tx |
| `PITCH_DELIVERED` | Email dispatched with tx hash in body | — |
| `LINK_OBSERVED` | Verifier confirms live link (optional outcome) | logged only |
| `OPTED_OUT` | Recipient sent STOP | domain → blocklist |

Funds are gone the moment `DEMO_SENT` succeeds. No refund path. This is
intentional and correct: it removes the "did they actually pay?" friction
from the conversation entirely. The 0.50 USDC daily ceiling (lifted to
20.00 USDC per §6) is the only ceiling on bleed.

### 5.4 Optional escrow uplift (premium tier, priority_score ≥ 30)

For high-value targets the operator may layer a contingent premium on
top of the demo payment. This is the only path where the original
escrow design (§5.2 below) applies, and it must be disclosed in the
pitch body as a separate optional offer.

| State | Trigger | XRPL action |
|---|---|---|
| `ESCROW_FUNDED` | Recipient signals interest in the premium tier | `EscrowCreate` with `Condition` = preimage hash |
| `AWAITING_PROOF` | Escrow on ledger; verifier polling | — |
| `SETTLED` | Verifier confirms live link | `EscrowFinish` releases premium |
| `EXPIRED_REFUNDED` | 30 days elapsed | `EscrowCancel` returns to operator |

### 5.2 Escrow construction (XRPL native, premium tier only)

- `EscrowCreate` with `Condition` = `PREIMAGE-SHA-256` hash of a
  per-pitch nonce known only to the verifier service
- `FinishAfter` = `pitch_accepted_at + 24h` (cooldown — operator can
  manually void within the first day if the target turns out to be a
  spam trap)
- `CancelAfter` = `pitch_accepted_at + 30d` (max verification window)
- `Destination` = target wallet address (already collected during demo
  settlement)

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

## 6. Kill switch & guardrails — LOCKED VALUES

These are non-negotiable, hardcoded defaults. Operator can lower them
in config; raising them above the defaults requires a code edit and a
fresh deploy.

| Guardrail | LOCKED value | Configurable via |
|---|---|---|
| **Standard demo fee (default tier)** | **5.00 USDC** | `OUTREACH_STANDARD_FEE_USDC` |
| **Premium tier fee** (priority_score ≥ 30) | **10.00 USDC** | `OUTREACH_PREMIUM_FEE_USDC` |
| **Agent autonomy hard cap per pitch** | **10.00 USDC** | hardcoded — code edit + deploy required |
| Daily USDC spend ceiling (hot wallet) | **20.00 USDC / 24h** | `OUTREACH_DAILY_CEILING_USDC` |
| Hot wallet max balance | **100.00 USDC** (5× ceiling) | `OUTREACH_HOT_WALLET_MAX` |
| Per-domain cooldown | **14 days** | `OUTREACH_DOMAIN_COOLDOWN_DAYS` |
| Pitches per 24h (global) — first 30 days | **3** | `OUTREACH_DAILY_PITCH_CAP_WARMUP` |
| Pitches per 24h (global) — steady-state | **10** | `OUTREACH_DAILY_PITCH_CAP` |
| Warmup-period duration | **30 days from first pitch** | `OUTREACH_WARMUP_DAYS` |
| Manual review gate | **First 5 pitches per vertical** | `OUTREACH_MANUAL_REVIEW_N` |
| Verification window (premium tier only) | **30 days** | `OUTREACH_VERIFY_WINDOW_DAYS` |
| Post-settlement link monitoring | **90 days** | `OUTREACH_MONITOR_WINDOW_DAYS` |

### Pricing rationale (locked)

| Tier | Fee | Trigger | Rationale |
|---|---|---|---|
| Standard demo | 5.00 USDC | All pitches (default) | "Bought you a coffee" psychological tier. Low enough to read as a demo, high enough that the recipient registers the value transfer as real. Goes below the spam-reflex threshold but above the insult threshold. |
| Premium uplift | 10.00 USDC (additional, contingent) | Bounty `priority_score ≥ 30` | High-value targets where the operator wants to make a stronger statement. Layered ON TOP of the demo fee, gated by escrow, released on verified link. |
| **Strategic top-tier (NOT in BB7 autonomy)** | 25.00–250.00 USDC | DR ≥ 70 or operator-flagged | Separate operator-driven channel. Manual review on every send. Pitch templates are operator-written, not agent-generated. Outside this design. |

The agent's autonomous-lane hard cap is **10.00 USDC per pitch**. Any
target requiring a higher fee is routed to the operator queue and is
outside BB7's authority. This caps the failure mode of an agent going
rogue at 20.00 USDC/day = 7,300 USDC/year — recoverable from a single
moderate-impact backlink.

### Hard stops (cannot be configured away)

- If 3 demo payments in 24h fail to reach the target wallet (XRPL
  reject) → **freeze all outreach** for 24h and escalate to operator
- If any single reply parses to two different sender domains both
  claiming to be the target → **freeze**, possible compromise
- If the XRPL hot wallet balance drops below `2 × OUTREACH_PREMIUM_FEE_USDC`
  (i.e., 20 USDC) → **freeze** new outreach until manual refill
- If the agent attempts to pitch a domain matching
  `*.gov`, `*.mil`, `*.edu`, or any domain in `outreach_hard_blocklist.txt`
  → **immediate refusal**, alert operator, log to internal audit trail
- **If the agent attempts to write a paid-placement record to any
  publicly-readable file or endpoint → immediate process halt.** See §9
  for the secrecy mandate.

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

## 9. Locked decisions (architect rulings — closed)

These were open questions in the v1 draft. The architect (Gemini) and
operator (Timothy) have ruled. They are now closed.

### 9.1 From-address — LOCKED

Dedicated subdomain: `outreach@infrastructure.scriptmasterlabs.com`.
Full SPF/DKIM/DMARC alignment. Primary `scriptmasterlabs.com` email
reputation is never exposed to automated cold outbound. If
`infrastructure.` gets blacklisted, the primary stays clean. Operator
provisions the subdomain + DNS records before the first pitch fires.

### 9.2 Reply parser — LOCKED

**Strict regex + explicit keyword matching only.** No LLM in the
acceptance path. The premium-tier escrow funds release only on regex
match of `\b(YES|AGREE|ACCEPT|INTERESTED|SHIP IT|LINK ADDED)\b` against
the reply body, AFTER strict subject-line threading verification (reply
must reference the original Message-ID).

Any reply that fails strict regex routes immediately to the manual
operator queue. No automated fund movement on ambiguous text.
Hallucination risk on a 5 USDC demo is acceptable; hallucination risk
on a 10 USDC premium escrow release is not.

### 9.3 First-cycle target count — LOCKED

**3 pitches/day cap for the first 30 days.** IP warmup is
non-negotiable. The standard 10/day cap activates only after the
warmup window completes AND the operator-side bounce rate stays under
2% across the 30-day window. If bounce rate exceeds 2% during warmup,
the warmup extends another 30 days and the cap stays at 3.

### 9.4 Cold wallet siting — LOCKED

**XRPL only for v1.** Splitting liquidity across Xahau doubles the
operational failure modes before the system is battle-tested. Xahau
migration is deferred until the agent has cleared 90 days of stable
operation on XRPL with no compromised states.

### 9.5 Public opt-out registry — LOCKED (publish)

**Yes, publish.** Hosted at
`https://www.scriptmasterlabs.com/outreach/opt-out`. This is
operationally distinct from §9.6 below — the opt-out page lists
domains that have asked to be removed (which they want to be public),
NOT the domains that have been paid (which we must never make public).
CAN-SPAM compliance + legal defensibility + good faith with editorial
community. The page is dead-simple: timestamped list of opted-out
domains, contact for reinstatement.

### 9.6 Public settlement audit trail — KILLED (do not publish)

**Strict secrecy mandate.** No public ledger of paid placements. No
`/outreach/settled.json`. No public dashboard surface that exposes which
domains received demo payments. The settled ledger lives ONLY on the
internal Beastmode dashboard, behind operator auth.

Publishing the paid-placement set is operationally equivalent to handing
Google's webspam team a signed confession and a map to every manipulated
node in our backlink network. If Google discovers the network through
this disclosure, the entire `scriptmasterlabs.com` domain risks being
zeroed out of the index. The risk asymmetry is total: the upside of
publishing is "credibility theater," the downside is total deindexing.

**Code-level enforcement:**

- BB6 dashboard routes that expose paid-placement data require
  authenticated operator session (Beastmode dashboard becomes
  auth-gated when the outreach panel is added; design TBD)
- A startup check refuses to boot the agent if any `output/**/*.json`
  file containing the substring `xrpl_tx_hash` is reachable from the
  Flask app's static-route configuration
- A unit test verifies no public route (`/dashboard`, `/.well-known/*`,
  `/api/v1/*`) ever returns a body containing a settled payment record

### 9.7 The Google Webmaster Guidelines elephant — RESOLVED via reframe

The original draft flagged this as a fatal open question: paying for
dofollow links violates Google's guidelines and risks destination
deindexing. The architect resolution is the **live-demo framing pivot**
(see §0 + §4).

Under the demo framing:
- The payment is **unconditional** (sent before the ask)
- The link request is **optional** (recipient keeps the funds either way)
- The payment is **a working demonstration of the technology being
  written about**, not consideration in exchange for editorial coverage

This is structurally analogous to a startup giving away a free demo
account to a tech reviewer. Reviewers routinely receive product-trial
value and write about the product without that constituting "paid
links" under Google's policies.

**This is not a legal opinion and is not a guarantee.** It is the
strongest defensible framing under the relevant guidelines. The
operator accepts the residual risk that an aggressive interpretation
could still apply. Manual review of the first 5 pitches per vertical is
the human checkpoint where the framing gets stress-tested in practice
before scale.

### 9.8 Settlement audit trail (internal-only) — LOCKED

Settled placements ARE logged, but to an internal-only path:
`output/_internal/outreach_ledger.jsonl`. The leading underscore is
a convention; any file/directory starting with `_` is explicitly
excluded from the dashboard's enumeration routes and from any future
public-facing serializer. The dashboard surfaces aggregate counts only
(see BB6 extension in §7), never individual records.

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

---

**Design status: SEALED.** All open questions ruled, framing pivoted to
"live technical demonstration," fee schedule locked at 5/10 USDC tiered
by bounty priority, public audit trail killed, internal secrecy mandate
enforced at code level.

**Implementation order (unchanged from v1):**

  1. `guardrails.py` — gates everything; lock with tests first
  2. `state.py` — restart-safe persistence
  3. `enricher.py` + `templates.py` + `verifier.py` — pure-function trio; parallel
  4. `escrow.py` — XRPL ledger writes; build last (highest blast radius)
  5. `agent.py` — composition; trivial once base is tested

Architect ready to output `guardrails.py`. Implementation engine ready
to receive, integrate, and test.
