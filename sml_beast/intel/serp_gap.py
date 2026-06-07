"""
SERP-gap analysis — given a live SERP for a target keyword, identify the
positions where ScriptMasterLabs is not present and the topics the
top-3 results all cover. Output: prioritized intent gaps the content
generator should address in the next pass.
"""

from urllib.parse import urlparse

SML_DOMAINS = {"scriptmasterlabs.com", "www.scriptmasterlabs.com"}


def _host(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def find_gap(serp: dict) -> dict:
    organic = serp.get("organic", []) or []
    sml_positions = [i + 1 for i, r in enumerate(organic) if _host(r.get("link", "")) in SML_DOMAINS]
    competitors = [r for r in organic[:3] if _host(r.get("link", "")) not in SML_DOMAINS]

    paa_topics = [p.get("question", "") for p in serp.get("people_also_ask", []) if p.get("question")]

    return {
        "sml_present":     bool(sml_positions),
        "sml_positions":   sml_positions,
        "top3_competitors": [{"title": c.get("title", ""), "link": c.get("link", "")} for c in competitors],
        "paa_topics":      paa_topics,
        "related":         [r.get("query", "") if isinstance(r, dict) else r
                            for r in serp.get("related", [])],
    }
