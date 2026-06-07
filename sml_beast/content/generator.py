"""
Landing-page + JSON-LD schema generator. Consumes the per-page brief produced
by `serp_gap.synthesize_page_brief` — never the raw canonical brief at runtime.
The `_gap` overlay drives PAA-as-FAQ, intent-conditioned CTAs, related-cluster
expansion, and a private competitive-landscape note for the operator.

Outputs go to disk under output/<vertical>/<slug>/. Every page carries
`needs_human_review: true`. Nothing auto-deploys.
"""

import json
import os
import re
import time


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
    gap   = page_brief.get("_gap", {})
    paa   = gap.get("paa", [])
    rel   = gap.get("semantic_cluster", [])
    intents = gap.get("intents", ["informational"])
    incumbents = gap.get("incumbents", [])
    incumbent_classes = gap.get("incumbent_classes", [])

    slug = _slug(keyword)
    title = f"{keyword.title()} — {page_brief['name']}"
    desc  = page_brief.get("positioning", "")

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

    if paa:
        body += ["## Questions buyers actually ask", ""]
        for q, a in paa:
            body += [f"### {q}", "", a or "_Answer drafted from product brief — review before publish._", ""]

    if rel:
        body += ["## Related searches we cover", ""] + [f"- {r}" for r in rel] + [""]

    body += [_cta_for_intent(intents, page_brief), ""]

    # Operator-only competitive landscape note — HTML comment so MDX renders
    # cleanly to the public visitor but the operator sees it in source.
    if incumbents:
        landscape = ["<!-- COMPETITIVE LANDSCAPE — operator review only",
                     f"  keyword:  {keyword}",
                     f"  severity: {gap.get('severity', '?')}",
                     f"  priority: {gap.get('priority', '?')}",
                     "  top3:"]
        for cls, sig in zip(incumbent_classes + ["?"] * 3, incumbents):
            landscape.append(f"    [{cls}] {sig.get('title', '')} — {sig.get('link', '')}")
        landscape.append("-->")
        body += landscape

    frontmatter = "\n".join([
        "---",
        f"title: \"{title}\"",
        f"description: \"{desc}\"",
        f"keyword: \"{keyword}\"",
        f"intent_silo: \"{intent_silo}\"",
        f"intents: {json.dumps(intents)}",
        f"gap_severity: \"{gap.get('severity', 'UNKNOWN')}\"",
        f"priority_score: {gap.get('priority', 0)}",
        f"brand_positions: {json.dumps(gap.get('brand_positions', []))}",
        f"generated_at: {int(time.time())}",
        "needs_human_review: true",
        "---",
        "",
    ])
    return slug, frontmatter + "\n".join(body)


def build_jsonld(page_brief: dict, keyword: str) -> list[dict]:
    """Return a list of JSON-LD blocks. Step-2 (richer schemas) will append
    SoftwareApplication / Dataset / TechArticle types here; for now we ship
    Product (always) + FAQPage (whenever the gap supplied PAA Q/A pairs)."""
    url = f"{page_brief['domain']}{page_brief['route']}/{_slug(keyword)}"
    blocks: list[dict] = []

    product = {
        "@context":    "https://schema.org",
        "@type":       "Product",
        "name":        page_brief["name"],
        "url":         url,
        "description": page_brief.get("positioning", ""),
        "brand":       {"@type": "Brand", "name": "ScriptMasterLabs"},
    }
    if page_brief.get("pricing", {}).get("model") == "one_time":
        product["offers"] = {
            "@type":         "Offer",
            "priceCurrency": "USD",
            "availability":  "https://schema.org/InStock",
            "category":      "one-time payment",
        }
    blocks.append(product)

    paa = page_brief.get("_gap", {}).get("paa", [])
    if paa:
        blocks.append({
            "@context":   "https://schema.org",
            "@type":      "FAQPage",
            "mainEntity": [{
                "@type":          "Question",
                "name":           q,
                "acceptedAnswer": {"@type": "Answer", "text": a or ""},
            } for q, a in paa],
        })

    return blocks


def write_page(out_dir: str, page_brief: dict, intent_silo: str, keyword: str) -> str:
    slug, mdx = build_mdx(page_brief, keyword, intent_silo)
    schema    = build_jsonld(page_brief, keyword)
    page_dir  = os.path.join(out_dir, slug)
    os.makedirs(page_dir, exist_ok=True)
    with open(os.path.join(page_dir, "page.mdx"), "w") as f:
        f.write(mdx)
    with open(os.path.join(page_dir, "schema.jsonld"), "w") as f:
        json.dump(schema, f, indent=2)
    return page_dir
