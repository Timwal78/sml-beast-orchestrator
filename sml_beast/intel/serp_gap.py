"""
SERP-Gap intelligence — the feedback loop's brain.

For each live SERP the engine answers four questions:
  1. Where does the operator's brand currently rank? (position gap)
  2. Who occupies the top-3, and how displaceable are they? (quality gap)
  3. Which PAA questions reveal user intent we can directly satisfy?
  4. Which related clusters expand the semantic surface area?

Output is a single GapReport dataclass. Every downstream stage — the
content generator, JSON-LD schema factory, internal link graph, backlink
finder — reads from this report and nothing else. Canonical product
briefs are overlaid via `synthesize_page_brief`; raw briefs never flow
to the generator at runtime.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlparse

# Aggregator/listicle hosts where focused, authoritative product pages
# have a credible shot at displacing the incumbent.
AGGREGATOR_DOMAINS = frozenset(
    {
        "capterra.com",
        "g2.com",
        "getapp.com",
        "softwareadvice.com",
        "alternativeto.net",
        "trustradius.com",
        "producthunt.com",
        "slant.co",
        "saashub.com",
        "stackshare.io",
    }
)

# Entrenched authority — a single content pass will not realistically displace.
ENTRENCHED_DOMAINS = frozenset(
    {
        "google.com",
        "support.google.com",
        "workspace.google.com",
        "microsoft.com",
        "support.microsoft.com",
        "apple.com",
        "support.apple.com",
        "wikipedia.org",
        "en.wikipedia.org",
    }
)

FORUM_HOSTS = frozenset({"reddit.com", "quora.com", "stackexchange.com", "stackoverflow.com"})

LISTICLE_RX = re.compile(r"\b(\d+\s+best|top\s+\d+|\d+\s+alternatives?)\b", re.I)


# Vertical-keyed attack-angle rules. Each tuple is (compiled_pattern, code, copy).
# Patterns match against the lowercase concatenation of title + snippet for each
# top-10 organic result; a hit on any result increments that angle's trigger_count.
ATTACK_RULES: dict[str, list[tuple[re.Pattern, str, str]]] = {
    "mastersheets": [
        (
            re.compile(
                r"\b(subscription|per\s+month|monthly\s+(plan|fee|cost)|annual\s+(plan|fee|cost)|pricing\s+plan|\$\d+\s*/\s*(?:mo|month))\b",
                re.I,
            ),
            "exploit_subscription_fatigue",
            "Attack subscription pricing — MasterSheets is a one-time payment, lifetime access.",
        ),
        (
            re.compile(
                r"\b(cloud[-\s]?only|requires\s+(?:sync|account|sign[-\s]?up)|sharing\s+required|online\s+only|google\s+account\s+required)\b",
                re.I,
            ),
            "exploit_cloud_lockin",
            "Attack cloud lock-in — MasterSheets is local-first, zero telemetry, data stays on your machine.",
        ),
        (
            re.compile(
                r"\b(openai\s+integration|chatgpt\s+plugin|premium\s+ai|ai\s+subscription|gemini\s+integration|copilot\s+pro|ai\s+add[-\s]?on)\b",
                re.I,
            ),
            "exploit_ai_lockin",
            "Attack AI vendor lock-in — MasterSheets is BYOK, point it at any model you already pay for.",
        ),
    ],
    "xrpl_x402": [
        (
            re.compile(
                r"\b(api\s+key|bearer\s+token|rate\s+limit|quota|throttl|sign[-\s]?up\s+required|developer\s+account)\b",
                re.I,
            ),
            "exploit_api_friction",
            "Attack API-key onboarding — x402 is dynamic; agents pay per call with zero static credentials.",
        ),
        (
            re.compile(
                r"\b(slow\s+settlement|takes?\s+\d+\s+(minutes|hours)|confirmation\s+time|block\s+confirmation|settlement\s+delay)\b",
                re.I,
            ),
            "exploit_latency",
            "Attack settlement latency — IRL clears in sub-50ms on XRPL and Xahau.",
        ),
        (
            re.compile(
                r"\b(corporate\s+account|credit\s+line|kyc\s+required|business\s+account|merchant\s+approval|underwriting)\b",
                re.I,
            ),
            "exploit_onboarding",
            "Attack onboarding friction — no credit line, no KYC, no merchant underwriting. Just pay.",
        ),
        (
            re.compile(
                r"\b(stripe|paypal|braintree|square|traditional\s+payment|legacy\s+rails)\b", re.I
            ),
            "position_vs_legacy_rails",
            "Position against legacy payment rails — x402 is the M2M-native upgrade path.",
        ),
    ],
}


def _extract_attack_angles(organic: list, vertical: str | None) -> list[dict]:
    """Walk top-10 organic results; for each ATTACK_RULES entry count how many
    results' (title+snippet) blob triggers the pattern. Return non-zero hits as
    a prioritized list. Always returns at least one entry — falls back to the
    structural-superiority angle so the generator never renders an empty CTA."""
    if not vertical or vertical not in ATTACK_RULES:
        return [
            {
                "code": "direct_structural_superiority",
                "copy": "Direct structural superiority — outclass incumbents on the canonical product brief.",
                "trigger_count": 0,
            }
        ]

    rules = ATTACK_RULES[vertical]
    counts = {code: 0 for _, code, _ in rules}
    copies = {code: copy for _, code, copy in rules}

    for r in organic[:10]:
        blob = f"{r.get('title', '')} {r.get('snippet', '')}"
        for pattern, code, _ in rules:
            if pattern.search(blob):
                counts[code] += 1

    # Privacy-gap heuristic for MasterSheets: trigger if NONE of the top-3
    # mention privacy/local/sovereign in title+snippet. Absence of the concept
    # is itself an opportunity to own the narrative.
    if vertical == "mastersheets":
        top3_blob = " ".join(
            f"{r.get('title', '')} {r.get('snippet', '')}" for r in organic[:3]
        ).lower()
        if not any(
            t in top3_blob
            for t in ("privacy", "private", "local", "sovereign", "self-host", "offline")
        ):
            counts["exploit_privacy_gap"] = 1
            copies["exploit_privacy_gap"] = (
                "Top results don't mention data sovereignty — own that narrative."
            )

    angles = [
        {"code": code, "copy": copies[code], "trigger_count": n}
        for code, n in counts.items()
        if n > 0
    ]
    angles.sort(key=lambda a: -int(a["trigger_count"]))  # type: ignore[call-overload]

    if not angles:
        angles.append(
            {
                "code": "direct_structural_superiority",
                "copy": "Direct structural superiority — outclass incumbents on the canonical product brief.",
                "trigger_count": 0,
            }
        )
    return angles


def _host(url: str) -> str:
    try:
        h = urlparse(url).netloc.lower()
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""


def _matches(host: str, target_set: Iterable[str]) -> bool:
    for t in target_set:
        if host == t or host.endswith("." + t):
            return True
    return False


def _classify(result: dict) -> str:
    host = _host(result.get("link", ""))
    title = result.get("title", "")
    if _matches(host, ENTRENCHED_DOMAINS):
        return "entrenched"
    if host in AGGREGATOR_DOMAINS:
        return "aggregator"
    if _matches(host, FORUM_HOSTS):
        return "forum"
    if LISTICLE_RX.search(title):
        return "listicle"
    return "independent"


@dataclass
class GapReport:
    keyword: str
    brand_present: bool
    brand_positions: list[int]
    top3_classes: list[str]
    top3_signatures: list[dict]
    paa_questions: list[str]
    paa_seeded_answers: list[str]
    related_clusters: list[str]
    gap_severity: str  # LOW | MEDIUM | HIGH | CRITICAL
    priority_score: int  # 0-100
    recommended_intents: list[str]
    attack_angles: list[dict]  # [{code, copy, trigger_count}, ...] — vertical-keyed


def analyze(
    serp_data: dict, brand_domains: Iterable[str], vertical: str | None = None
) -> GapReport:
    organic = serp_data.get("organic", []) or []
    keyword = serp_data.get("query", "")
    brand_set = {d.lower().lstrip(".") for d in brand_domains}

    positions = []
    for i, r in enumerate(organic):
        h = _host(r.get("link", ""))
        if h and _matches(h, brand_set):
            positions.append(i + 1)

    top3 = organic[:3]
    top3_classes = [_classify(r) for r in top3]
    top3_signatures = [
        {"title": r.get("title", ""), "link": r.get("link", ""), "snippet": r.get("snippet", "")}
        for r in top3
    ]

    paa_q: list[str] = []
    paa_a: list[str] = []
    for p in serp_data.get("people_also_ask", []) or []:
        q = (p.get("question") or "").strip()
        a = (p.get("snippet") or "").strip()
        if q:
            paa_q.append(q)
            paa_a.append(a)

    related: list[str] = []
    for r in serp_data.get("related", []) or []:
        if isinstance(r, dict) and r.get("query"):
            related.append(r["query"])
        elif isinstance(r, str) and r.strip():
            related.append(r.strip())

    # Gap severity
    n_entrenched = top3_classes.count("entrenched")
    n_aggregator = top3_classes.count("aggregator")
    n_forum_or_listicle = top3_classes.count("forum") + top3_classes.count("listicle")
    brand_in_top3 = any(p <= 3 for p in positions)

    if brand_in_top3:
        severity = "LOW"
    elif n_entrenched >= 2:
        severity = "LOW"
    elif n_aggregator + n_forum_or_listicle >= 2:
        severity = "CRITICAL"
    elif n_aggregator + n_forum_or_listicle == 1 and n_entrenched == 0:
        severity = "HIGH"
    elif n_entrenched == 1:
        severity = "MEDIUM"
    else:
        severity = "HIGH"

    severity_pts = {"LOW": 15, "MEDIUM": 45, "HIGH": 70, "CRITICAL": 90}[severity]
    paa_pts = min(len(paa_q) * 3, 15)
    rel_pts = min(len(related) * 2, 10)
    presence_bonus = -20 if brand_in_top3 else 0
    priority = max(0, min(100, severity_pts + paa_pts + rel_pts + presence_bonus))

    intents: list[str] = []
    paa_blob = " ".join(paa_q).lower() + " " + keyword.lower()
    if any(t in paa_blob for t in ("what is", "what does", "how does")):
        intents.append("informational")
    if any(t in paa_blob for t in ("how to", "how do i", "can i", "tutorial")):
        intents.append("instructional")
    if any(
        t in paa_blob
        for t in ("alternative", " vs ", "compare", "best ", "review", "cheaper", "free ")
    ):
        intents.append("comparison")
    if any(t in paa_blob for t in ("buy", "price", "cost ", "pricing", "subscription")):
        intents.append("transactional")
    if not intents:
        intents = ["informational"]

    attack_angles = _extract_attack_angles(organic, vertical)

    return GapReport(
        keyword=keyword,
        brand_present=bool(positions),
        brand_positions=positions,
        top3_classes=top3_classes,
        top3_signatures=top3_signatures,
        paa_questions=paa_q,
        paa_seeded_answers=paa_a,
        related_clusters=related,
        gap_severity=severity,
        priority_score=int(priority),
        recommended_intents=intents,
        attack_angles=attack_angles,
    )


def synthesize_page_brief(
    canonical_brief: dict, gap: GapReport, vertical: str | None = None
) -> dict:
    """Per-page brief: canonical product facts overlaid with gap-driven strategy.
    The content generator consumes this; it never sees the raw canonical brief at runtime.
    `vertical` is the worker's stable key ("mastersheets" / "xrpl_x402") — the generator
    reads it to select vertical-keyed JSON-LD schema types."""
    return {
        **canonical_brief,
        "_vertical": vertical,
        "_gap": {
            "keyword": gap.keyword,
            "severity": gap.gap_severity,
            "priority": gap.priority_score,
            "intents": gap.recommended_intents,
            "incumbents": gap.top3_signatures,
            "incumbent_classes": gap.top3_classes,
            "paa": list(zip(gap.paa_questions, gap.paa_seeded_answers, strict=False)),
            "semantic_cluster": gap.related_clusters,
            "brand_positions": gap.brand_positions,
            "brand_present": gap.brand_present,
            "attack_angles": gap.attack_angles,
        },
    }


# Backwards-compat shim for the original simple helper.
def find_gap(serp: dict) -> dict:
    rep = analyze(serp, brand_domains=("scriptmasterlabs.com",))
    return {
        "sml_present": rep.brand_present,
        "sml_positions": rep.brand_positions,
        "top3_competitors": rep.top3_signatures,
        "paa_topics": rep.paa_questions,
        "related": rep.related_clusters,
    }
