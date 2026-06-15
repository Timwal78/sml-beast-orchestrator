"""
BB7 pitch templates — vertical-keyed email generation.

Every template variable is REQUIRED. `render_pitch()` raises
`TemplateMissingVariableError` for any unfilled slot — no silent
fallback. This matches the design mandate in BB7_DESIGN.md §4:
"the generator refuses to send a pitch with any unfilled slot."

Template framing: the payment is a live technical demonstration of
sub-50ms M2M settlement rails, NOT a placement fee. This is the only
framing under which the agent operates outside Google's paid-backlink
prohibition. The link ask is optional and organic.

Vertical-keyed personalization:
  mastersheets  — attack-angle codes from the gap report drive
                  pers_observation (data sovereignty, BYOK, pricing model,
                  AI lock-in, etc.)
  xrpl_x402     — structural classification + settlement framing drive
                  pers_observation (Coinbase facilitator vs dual-chain,
                  institutional clearing, x402 infra)

Public interface:
  render_pitch(vertical, context) -> PitchEmail
  observation_for_attack_angles(angles, vertical) -> str
  subject_for_vertical(vertical, site_name) -> str
"""

from dataclasses import dataclass
from typing import Any

# ── sentinel for required-field enforcement ──────────────────────────────────

_MISSING = object()

_REQUIRED_FIELDS = {
    "first_name_or_team_handle",
    "pers_observation",
    "pers_gap_finding",
    "usdc_amount",
    "enrichment_source",
    "xrpl_tx_hash",
    "settlement_time_ms",
    "anchor_url",
    "anchor_resource_title",
    "pers_target_url",
    "opt_out_url",
    "operator_signature",
}


class TemplateMissingVariableError(ValueError):
    """Raised when a required template variable is absent or empty."""


# ── result dataclass ─────────────────────────────────────────────────────────


@dataclass
class PitchEmail:
    subject: str
    body: str
    vertical: str
    domain: str

    def to_dict(self) -> dict:
        return {
            "subject": self.subject,
            "body": self.body,
            "vertical": self.vertical,
            "domain": self.domain,
        }


# ── attack-angle → observation mapping (MasterSheets) ────────────────────────

_MS_ANGLE_OBSERVATIONS: dict[str, str] = {
    "data_sovereignty": (
        "I noticed your roundup covers spreadsheet tools without addressing "
        "data sovereignty — who actually owns the data once it's in the cloud"
    ),
    "pricing_model": (
        "I noticed your piece compares spreadsheet tools on features but "
        "doesn't surface the subscription-vs-one-time ownership model, which "
        "is the axis most power users care about"
    ),
    "byok_ai": (
        "I noticed your AI-spreadsheet comparison assumes the LLM is "
        "provider-hosted — the Bring Your Own Key (BYOK) model that runs "
        "the AI against the user's own API keys doesn't appear"
    ),
    "ai_lock_in": (
        "I noticed your piece on AI-augmented spreadsheets doesn't address "
        "vendor lock-in risk once a provider controls both your data and "
        "the model weights"
    ),
    "local_control": (
        "I noticed your review focuses on cloud-native tools and doesn't "
        "mention local-first or self-hostable alternatives"
    ),
    "privacy_gap": (
        "I noticed your spreadsheet guide doesn't surface the privacy "
        "implications of storing sensitive business data on third-party "
        "infrastructure"
    ),
}

_MS_DEFAULT_OBSERVATION = (
    "I noticed your content covers productivity and data tools "
    "without surfacing the data-sovereignty trade-off that drives "
    "a significant share of tool-switching decisions today"
)

# ── attack-angle → observation mapping (IRL / x402) ──────────────────────────

_X402_ANGLE_OBSERVATIONS: dict[str, str] = {
    "coinbase_facilitator": (
        "Your post on x402 settlement focuses on the Coinbase facilitator; "
        "I think your readers would benefit from the dual-chain XRPL + Xahau "
        "path, which is where institutional micropayments are actually clearing today"
    ),
    "api_key_friction": (
        "I noticed your AI agent architecture relies on static API keys — "
        "the HTTP 402 Payment Required model eliminates that friction entirely "
        "(agents pay per call with no corporate credit line required)"
    ),
    "settlement_latency": (
        "Your piece compares payment rails without citing ledger finality — "
        "the sub-50ms settlement time on XRPL is a structural differentiator "
        "that changes the economics of per-call agent billing"
    ),
    "legacy_rails": (
        "Your coverage of agent-to-agent payments still routes through "
        "legacy Web2 billing; the sovereign x402 path on-ledger has no "
        "intermediary credit card processor in the loop"
    ),
    "m2m_infrastructure": (
        "I noticed your M2M payment write-up doesn't cover the on-ledger "
        "settlement path — where the payment receipt is a cryptographic "
        "proof on the ledger, not a database row on a third-party server"
    ),
}

_X402_DEFAULT_OBSERVATION = (
    "I noticed your coverage of machine-to-machine payment infrastructure "
    "doesn't surface the on-ledger settlement path on XRPL + Xahau, which "
    "is where institutional M2M clearing is moving today"
)

_ANGLE_MAPS: dict[str, dict[str, str]] = {
    "mastersheets": _MS_ANGLE_OBSERVATIONS,
    "xrpl_x402": _X402_ANGLE_OBSERVATIONS,
}

_DEFAULTS: dict[str, str] = {
    "mastersheets": _MS_DEFAULT_OBSERVATION,
    "xrpl_x402": _X402_DEFAULT_OBSERVATION,
}

# ── subject lines ─────────────────────────────────────────────────────────────

_SUBJECT_TEMPLATES: dict[str, str] = {
    "mastersheets": (
        "Re: {site_name} — live XRPL settlement demo + infrastructure mention"
    ),
    "xrpl_x402": (
        "Re: {site_name} — sub-50ms XRPL settlement proof (tx enclosed)"
    ),
}


def subject_for_vertical(vertical: str, site_name: str) -> str:
    """Return the subject line for the given vertical, personalized to the site."""
    tmpl = _SUBJECT_TEMPLATES.get(vertical)
    if not tmpl:
        raise ValueError(f"Unknown vertical for subject: {vertical!r}")
    return tmpl.format(site_name=site_name)


# ── observation builder ───────────────────────────────────────────────────────


def observation_for_attack_angles(angles: list[str], vertical: str) -> str:
    """Return the first matching observation string for the given attack angles.

    Tries each angle in list order; returns the vertical's default observation
    when no angle matches the known map. Never returns an empty string."""
    angle_map = _ANGLE_MAPS.get(vertical, {})
    for angle in angles:
        if angle in angle_map:
            return angle_map[angle]
    return _DEFAULTS.get(vertical, _MS_DEFAULT_OBSERVATION)


# ── body template ─────────────────────────────────────────────────────────────

_BODY_TEMPLATE = """\
Hi {first_name_or_team_handle},

I run engineering for ScriptMasterLabs. {pers_observation} — specifically, \
{pers_gap_finding}.

I just dropped {usdc_amount} USDC into the XRPL address listed in your \
{enrichment_source} (tx: {xrpl_tx_hash}, settled in {settlement_time_ms}ms). \
That's not a placement fee. It's the simplest demonstration I can give you that \
the sub-50ms M2M payment rails we publish at {anchor_url} actually work end to end.

If you find that interesting enough to mention {anchor_resource_title} in \
{pers_target_url} as a relevant infrastructure reference, that's the extent of \
the ask. If not, keep the USDC — it's already yours, and we appreciate the time \
you spent reading this.

We never follow up on this thread. Reply STOP to add your domain to our \
permanent opt-out registry: {opt_out_url}.

— {operator_signature}
ScriptMasterLabs operator
"""


# ── public render function ───────────────────────────────────────────────────


def render_pitch(vertical: str, context: dict[str, Any]) -> PitchEmail:
    """Render a pitch email for the given vertical and context dict.

    All keys in _REQUIRED_FIELDS must be present and non-empty in `context`.
    Raises TemplateMissingVariableError on the first missing or empty slot.

    `context` must also include:
      - "domain"    : target domain string
      - "site_name" : human-readable site name (for subject line)

    The returned PitchEmail is ready to hand to dispatcher.py.
    """
    if vertical not in _ANGLE_MAPS:
        raise ValueError(f"Unknown vertical: {vertical!r}. Must be one of {sorted(_ANGLE_MAPS)}")

    # Enforce required fields before touching the template
    for field in sorted(_REQUIRED_FIELDS):
        value = context.get(field, _MISSING)
        if value is _MISSING or value == "" or value is None:
            raise TemplateMissingVariableError(
                f"Required template variable missing or empty: {field!r}"
            )

    domain = context.get("domain", "")
    site_name = context.get("site_name", domain)

    subject = subject_for_vertical(vertical, site_name)
    body = _BODY_TEMPLATE.format(**context)

    return PitchEmail(
        subject=subject,
        body=body,
        vertical=vertical,
        domain=domain,
    )
