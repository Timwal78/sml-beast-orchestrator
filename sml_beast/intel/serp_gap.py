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
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlparse

# Aggregator/listicle hosts where focused, authoritative product pages
# have a credible shot at displacing the incumbent.
AGGREGATOR_DOMAINS = frozenset({
    "capterra.com", "g2.com", "getapp.com", "softwareadvice.com",
    "alternativeto.net", "trustradius.com", "producthunt.com",
    "slant.co", "saashub.com", "stackshare.io",
})

# Entrenched authority — a single content pass will not realistically displace.
ENTRENCHED_DOMAINS = frozenset({
    "google.com", "support.google.com", "workspace.google.com",
    "microsoft.com", "support.microsoft.com",
    "apple.com", "support.apple.com",
    "wikipedia.org", "en.wikipedia.org",
})

FORUM_HOSTS = frozenset({"reddit.com", "quora.com", "stackexchange.com", "stackoverflow.com"})

LISTICLE_RX = re.compile(r"\b(\d+\s+best|top\s+\d+|\d+\s+alternatives?)\b", re.I)


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
    host  = _host(result.get("link", ""))
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
    keyword:             str
    brand_present:       bool
    brand_positions:     list[int]
    top3_classes:        list[str]
    top3_signatures:     list[dict]
    paa_questions:       list[str]
    paa_seeded_answers:  list[str]
    related_clusters:    list[str]
    gap_severity:        str   # LOW | MEDIUM | HIGH | CRITICAL
    priority_score:      int   # 0-100
    recommended_intents: list[str]


def analyze(serp_data: dict, brand_domains: Iterable[str]) -> GapReport:
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
    n_entrenched        = top3_classes.count("entrenched")
    n_aggregator        = top3_classes.count("aggregator")
    n_forum_or_listicle = top3_classes.count("forum") + top3_classes.count("listicle")
    brand_in_top3       = any(p <= 3 for p in positions)

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

    severity_pts   = {"LOW": 15, "MEDIUM": 45, "HIGH": 70, "CRITICAL": 90}[severity]
    paa_pts        = min(len(paa_q) * 3, 15)
    rel_pts        = min(len(related) * 2, 10)
    presence_bonus = -20 if brand_in_top3 else 0
    priority       = max(0, min(100, severity_pts + paa_pts + rel_pts + presence_bonus))

    intents: list[str] = []
    paa_blob = " ".join(paa_q).lower() + " " + keyword.lower()
    if any(t in paa_blob for t in ("what is", "what does", "how does")):
        intents.append("informational")
    if any(t in paa_blob for t in ("how to", "how do i", "can i", "tutorial")):
        intents.append("instructional")
    if any(t in paa_blob for t in ("alternative", " vs ", "compare", "best ", "review", "cheaper", "free ")):
        intents.append("comparison")
    if any(t in paa_blob for t in ("buy", "price", "cost ", "pricing", "subscription")):
        intents.append("transactional")
    if not intents:
        intents = ["informational"]

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
    )


def synthesize_page_brief(canonical_brief: dict, gap: GapReport) -> dict:
    """Per-page brief: canonical product facts overlaid with gap-driven strategy.
    The content generator consumes this; it never sees the raw canonical brief at runtime."""
    return {
        **canonical_brief,
        "_gap": {
            "keyword":          gap.keyword,
            "severity":         gap.gap_severity,
            "priority":         gap.priority_score,
            "intents":          gap.recommended_intents,
            "incumbents":       gap.top3_signatures,
            "incumbent_classes": gap.top3_classes,
            "paa":              list(zip(gap.paa_questions, gap.paa_seeded_answers)),
            "semantic_cluster": gap.related_clusters,
            "brand_positions":  gap.brand_positions,
            "brand_present":    gap.brand_present,
        },
    }


# Backwards-compat shim for the original simple helper.
def find_gap(serp: dict) -> dict:
    rep = analyze(serp, brand_domains=("scriptmasterlabs.com",))
    return {
        "sml_present":      rep.brand_present,
        "sml_positions":    rep.brand_positions,
        "top3_competitors": rep.top3_signatures,
        "paa_topics":       rep.paa_questions,
        "related":          rep.related_clusters,
    }
