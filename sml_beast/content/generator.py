"""
Landing-page + JSON-LD schema generator. Consumes the per-page brief produced
by `serp_gap.synthesize_page_brief` — never the raw canonical brief at runtime.

Two concerns live here:

  1. MDX body composition. Sections:
       - Title + positioning
       - Value props (from canonical brief)
       - "Why we win" — vertical-keyed attack angles from the gap engine
       - PAA Q/A — promoted from SERP, ready for FAQPage schema
       - Related-cluster expansion
       - Intent-conditioned CTA
       - Operator-only competitive landscape note (HTML comment)

  2. JSON-LD schema factory. Vertical-keyed:
       - mastersheets → Product, SoftwareApplication, FAQPage (when PAA present)
       - xrpl_x402   → Product, Organization (with sameAs), TechArticle, FAQPage
       - other       → Product, FAQPage

Outputs land under output/<vertical>/<slug>/. Every page carries
`needs_human_review: true`. Nothing auto-deploys.
"""

import json
import os
import re
import time

from .link_graph import render_cross_link_block

SAMEAS_XRPL_X402 = [
    "https://github.com/timwal78/squeezeos",
    "https://squeezeos-api.onrender.com",
    "https://four02proof.onrender.com",
    "https://ghost-layer.onrender.com",
    "https://www.scriptmasterlabs.com",
    "https://www.scriptmasterlabs.com/infrastructure",
]


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s or "untitled"


def _value_props(brief: dict) -> list[str]:
    props: list[str] = []
    if "pricing" in brief:
        props.append(f"- **Pricing.** {brief['pricing']['summary']}")
    if "data_sovereignty" in brief:
        props.append(f"- **Data sovereignty.** {brief['data_sovereignty']['summary']}")
    if "ai_integration" in brief:
        props.append(f"- **AI integration.** {brief['ai_integration']['summary']}")
    if "settlement" in brief:
        props.append(f"- **Settlement.** {brief['settlement']['summary']}")
    if "auth_model" in brief:
        props.append(f"- **Auth model.** {brief['auth_model']['summary']}")
    return props


def _cta_for_intent(intents: list[str], brief: dict) -> str:
    url = f"{brief['domain']}{brief['route']}"
    if "transactional" in intents:
        return f"### Pricing\n\nOne-time purchase. [Buy {brief['name']}]({url})."
    if "comparison" in intents:
        return f"### See how {brief['name']} compares\n\n[Read the full comparison]({url})."
    if "instructional" in intents:
        return f"### Try it\n\n[Open {brief['name']}]({url}) and follow along."
    return f"### Learn more\n\n[{brief['domain']}{brief['route']}]({url})."


def build_mdx(page_brief: dict, keyword: str, intent_silo: str) -> tuple[str, str]:
    gap = page_brief.get("_gap", {})
    paa = gap.get("paa", [])
    rel = gap.get("semantic_cluster", [])
    intents = gap.get("intents", ["informational"])
    incumbents = gap.get("incumbents", [])
    incumbent_classes = gap.get("incumbent_classes", [])
    angles = gap.get("attack_angles", [])

    slug = _slug(keyword)
    title = f"{keyword.title()} — {page_brief['name']}"
    desc = page_brief.get("positioning", "")

    body = [
        f"# {page_brief['name']}: {keyword.title()}",
        "",
        desc,
        "",
        "## Why this exists",
        "",
        page_brief.get("positioning", ""),
        "",
        *_value_props(page_brief),
        "",
    ]

    if angles:
        body += ["## Why we win", ""]
        for a in angles:
            body.append(f"- {a.get('copy', '')}")
        body.append("")

    if paa:
        body += ["## Questions buyers actually ask", ""]
        for q, a in paa:
            body += [
                f"### {q}",
                "",
                a or "_Answer drafted from product brief — review before publish._",
                "",
            ]

    # Cross-vertical contextual link block. Bleeds authority between the
    # MasterSheets silo and the IRL/x402 silo so backlinks to either lift both.
    body += render_cross_link_block(page_brief.get("_vertical"))

    if rel:
        body += ["## Related searches we cover", ""] + [f"- {r}" for r in rel] + [""]

    body += [_cta_for_intent(intents, page_brief), ""]

    if incumbents:
        landscape = [
            "<!-- COMPETITIVE LANDSCAPE — operator review only",
            f"  keyword:  {keyword}",
            f"  severity: {gap.get('severity', '?')}",
            f"  priority: {gap.get('priority', '?')}",
            "  top3:",
        ]
        for cls, sig in zip(incumbent_classes + ["?"] * 3, incumbents, strict=False):
            landscape.append(f"    [{cls}] {sig.get('title', '')} — {sig.get('link', '')}")
        if angles:
            landscape.append("  attack_angles:")
            for a in angles:
                landscape.append(f"    {a.get('code', '?')} (x{a.get('trigger_count', 0)})")
        landscape.append("-->")
        body += landscape

    frontmatter = "\n".join(
        [
            "---",
            f'title: "{title}"',
            f'description: "{desc}"',
            f'keyword: "{keyword}"',
            f'intent_silo: "{intent_silo}"',
            f'vertical: "{page_brief.get("_vertical", "unknown")}"',
            f"intents: {json.dumps(intents)}",
            f'gap_severity: "{gap.get("severity", "UNKNOWN")}"',
            f"priority_score: {gap.get('priority', 0)}",
            f"brand_positions: {json.dumps(gap.get('brand_positions', []))}",
            f"attack_angle_codes: {json.dumps([a.get('code') for a in angles])}",
            f"generated_at: {int(time.time())}",
            "needs_human_review: true",
            "---",
            "",
        ]
    )
    return slug, frontmatter + "\n".join(body)


# ── JSON-LD factory — vertical-keyed schema types ─────────────────────────────


def _product_block(page_brief: dict, url: str) -> dict:
    block = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": page_brief["name"],
        "url": url,
        "description": page_brief.get("positioning", ""),
        "brand": {"@type": "Brand", "name": "ScriptMasterLabs"},
    }
    if page_brief.get("pricing", {}).get("model") == "one_time":
        block["offers"] = {
            "@type": "Offer",
            "priceCurrency": "USD",
            "availability": "https://schema.org/InStock",
            "category": "one-time payment",
        }
    return block


def _softwareapp_mastersheets(page_brief: dict, url: str) -> dict:
    """Direct play at Google Sheets organic territory."""
    return {
        "@context": "https://schema.org",
        "@type": "SoftwareApplication",
        "name": page_brief["name"],
        "url": url,
        "description": page_brief.get("positioning", ""),
        "applicationCategory": "BusinessApplication",
        "applicationSubCategory": "Spreadsheet",
        "operatingSystem": "Windows, macOS, Linux",
        "featureList": [
            "100% one-time payment — zero subscriptions",
            "Bring Your Own Key (BYOK) AI integration",
            "Complete data sovereignty — local-only, zero telemetry",
            "Drop-in superset of Google Sheets formulas",
        ],
        "offers": {
            "@type": "Offer",
            "priceCurrency": "USD",
            "availability": "https://schema.org/InStock",
            "category": "one-time payment",
        },
        "publisher": {"@type": "Organization", "name": "ScriptMasterLabs"},
    }


def _organization_xrpl(url: str) -> dict:
    """Establish SML as the authority on M2M payment infrastructure."""
    return {
        "@context": "https://schema.org",
        "@type": "Organization",
        "name": "ScriptMasterLabs",
        "url": "https://www.scriptmasterlabs.com",
        "logo": "https://www.scriptmasterlabs.com/logo.png",
        "description": "Operator of SML Institutional Rails (IRL), the x402 paywall, Ghost Layer, and 402Proof.",
        "sameAs": SAMEAS_XRPL_X402,
        "knowsAbout": [
            "HTTP 402 Payment Required",
            "x402 wire protocol",
            "XRPL settlement",
            "Xahau hooks",
            "Machine-to-machine micropayments",
            "AI agent payment infrastructure",
        ],
    }


def _techarticle_xrpl(page_brief: dict, url: str, keyword: str) -> dict:
    """Authoritative technical doc framing for IRL/x402 pages."""
    return {
        "@context": "https://schema.org",
        "@type": "TechArticle",
        "headline": f"{keyword.title()} — {page_brief['name']}",
        "url": url,
        "description": page_brief.get("positioning", ""),
        "proficiencyLevel": "Expert",
        "dependencies": "XRPL, Xahau, HTTP 402, x402 wire protocol",
        "author": {"@type": "Organization", "name": "ScriptMasterLabs"},
        "publisher": {
            "@type": "Organization",
            "name": "ScriptMasterLabs",
            "sameAs": SAMEAS_XRPL_X402,
        },
        "keywords": [keyword, "x402", "XRPL", "Xahau", "M2M payments", "AI agent economy"],
    }


def _faqpage(paa: list) -> dict:
    return {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {
                "@type": "Question",
                "name": q,
                "acceptedAnswer": {"@type": "Answer", "text": a or ""},
            }
            for q, a in paa
        ],
    }


def build_jsonld(page_brief: dict, keyword: str) -> list[dict]:
    url = f"{page_brief['domain']}{page_brief['route']}/{_slug(keyword)}"
    vertical = page_brief.get("_vertical")
    paa = page_brief.get("_gap", {}).get("paa", [])

    blocks: list[dict] = [_product_block(page_brief, url)]

    if vertical == "mastersheets":
        blocks.append(_softwareapp_mastersheets(page_brief, url))
    elif vertical == "xrpl_x402":
        blocks.append(_organization_xrpl(url))
        blocks.append(_techarticle_xrpl(page_brief, url, keyword))

    if paa:
        blocks.append(_faqpage(paa))

    return blocks


def write_page(out_dir: str, page_brief: dict, intent_silo: str, keyword: str) -> str:
    slug, mdx = build_mdx(page_brief, keyword, intent_silo)
    schema = build_jsonld(page_brief, keyword)
    page_dir = os.path.join(out_dir, slug)
    os.makedirs(page_dir, exist_ok=True)
    with open(os.path.join(page_dir, "page.mdx"), "w") as f:
        f.write(mdx)
    with open(os.path.join(page_dir, "schema.jsonld"), "w") as f:
        json.dump(schema, f, indent=2)
    return page_dir
