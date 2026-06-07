"""
Landing-page + JSON-LD schema generator. Produces MDX (for Next.js) and a
schema.org JSON-LD block sourced from the canonical product brief.

Outputs go to disk under output/<vertical>/<slug>/. Nothing auto-deploys —
the operator reviews each page before it ships to scriptmasterlabs.com.
"""

import json
import os
import re
import time


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s or "untitled"


def _people_also_ask(serp: dict, n: int = 4) -> list[dict]:
    return [{"q": p.get("question", ""), "a": p.get("snippet", "")}
            for p in serp.get("people_also_ask", [])[:n] if p.get("question")]


def _related(serp: dict, n: int = 6) -> list[str]:
    out = []
    for r in serp.get("related", [])[:n]:
        if isinstance(r, dict) and r.get("query"):
            out.append(r["query"])
    return out


def build_mdx(brief: dict, keyword: str, serp: dict, intent_silo: str) -> tuple[str, str]:
    """Return (slug, mdx_body). The body is structured for a Next.js MDX route."""
    slug = _slug(keyword)
    paa  = _people_also_ask(serp)
    rel  = _related(serp)

    title = f"{keyword.title()} — {brief['name']}"
    desc  = brief.get("positioning", "")

    sections = [
        f"# {brief['name']}: {keyword.title()}",
        "",
        desc,
        "",
        "## Why this exists",
        "",
        brief.get("positioning", ""),
        "",
    ]

    # Vertical-specific value props
    if "pricing" in brief:
        sections += [f"- **Pricing.** {brief['pricing']['summary']}"]
    if "data_sovereignty" in brief:
        sections += [f"- **Data sovereignty.** {brief['data_sovereignty']['summary']}"]
    if "ai_integration" in brief:
        sections += [f"- **AI integration.** {brief['ai_integration']['summary']}"]
    if "settlement" in brief:
        sections += [f"- **Settlement.** {brief['settlement']['summary']}"]
    if "auth_model" in brief:
        sections += [f"- **Auth model.** {brief['auth_model']['summary']}"]
    sections += [""]

    if paa:
        sections += ["## Questions buyers actually ask", ""]
        for item in paa:
            sections += [f"### {item['q']}", "", item["a"], ""]

    if rel:
        sections += ["## Related searches we cover", ""]
        sections += [f"- {r}" for r in rel] + [""]

    sections += ["## Try it",
                 "",
                 f"Live at [{brief['domain']}{brief['route']}]({brief['domain']}{brief['route']}).",
                 ""]

    frontmatter = "\n".join([
        "---",
        f"title: \"{title}\"",
        f"description: \"{desc}\"",
        f"keyword: \"{keyword}\"",
        f"intent_silo: \"{intent_silo}\"",
        f"generated_at: \"{int(time.time())}\"",
        "needs_human_review: true",
        "---",
        "",
    ])
    return slug, frontmatter + "\n".join(sections)


def build_jsonld(brief: dict, keyword: str) -> dict:
    """Schema.org JSON-LD targeting feature snippets + rich results."""
    url = f"{brief['domain']}{brief['route']}/{_slug(keyword)}"
    schema = {
        "@context":    "https://schema.org",
        "@type":       "Product",
        "name":        brief["name"],
        "url":         url,
        "description": brief.get("positioning", ""),
        "brand":       {"@type": "Brand", "name": "ScriptMasterLabs"},
    }
    if brief.get("pricing", {}).get("model") == "one_time":
        schema["offers"] = {
            "@type":         "Offer",
            "priceCurrency": "USD",
            "availability":  "https://schema.org/InStock",
            "category":      "one-time payment",
        }
    return schema


def write_page(out_dir: str, brief: dict, intent_silo: str, keyword: str, serp: dict) -> str:
    slug, mdx = build_mdx(brief, keyword, serp, intent_silo)
    schema    = build_jsonld(brief, keyword)
    page_dir  = os.path.join(out_dir, slug)
    os.makedirs(page_dir, exist_ok=True)
    mdx_path  = os.path.join(page_dir, "page.mdx")
    json_path = os.path.join(page_dir, "schema.jsonld")
    with open(mdx_path, "w") as f:
        f.write(mdx)
    with open(json_path, "w") as f:
        json.dump(schema, f, indent=2)
    return page_dir
