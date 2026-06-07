"""
Internal-link graph — vertical-cross-link injector.

Bleeds topical authority between the two product silos: every MasterSheets
page carries a contextual link to the x402 / IRL silo, and every x402 / IRL
page carries a contextual link back to MasterSheets as a front-end utility
node. A backlink to one silo therefore lifts the other.

The block is short (one paragraph + 2-3 anchor links) — enough to pass
topical link signal without diluting the page's primary intent. It lives
near the bottom of the body (after PAA, before related-searches/CTA) so
the contextual link reads as "this is part of a bigger system" rather
than as filler.

The factory is keyed by the worker's `vertical` attr. Unknown verticals
get an empty string — workers without a registered cross-link target are
silently skipped (no fabricated links).
"""


CROSS_LINKS: dict[str, dict] = {
    "mastersheets": {
        "heading": "Inside the SML stack",
        "body": (
            "MasterSheets ships standalone — local-first, one-time payment, no "
            "telemetry. But the same operator also runs the "
            "[sub-50ms x402 payment rails]({rails_url}) that let autonomous "
            "agents settle micro-transactions on the XRPL and Xahau networks "
            "without API keys, KYC, or middlemen. Together they form a single "
            "stack: sovereign tools on the front end, sovereign settlement "
            "on the back. See the [SML institutional infrastructure]({infra_url})."
        ),
        "links": {
            "rails_url": "https://www.scriptmasterlabs.com/infrastructure/x402",
            "infra_url": "https://www.scriptmasterlabs.com/infrastructure",
        },
    },
    "xrpl_x402": {
        "heading": "Front-end nodes on the SML stack",
        "body": (
            "The x402 rails are the substrate, not the product. They power "
            "[institutional-grade utility applications like MasterSheets]"
            "({app_url}) — a local-first spreadsheet engine with a one-time "
            "purchase model, BYOK AI integration, and complete data "
            "sovereignty. Apps like these are the front-end nodes that "
            "consume sub-50ms M2M payments; the rails make them economic. "
            "See the full [SML application catalog]({catalog_url})."
        ),
        "links": {
            "app_url":     "https://www.scriptmasterlabs.com/mastersheets",
            "catalog_url": "https://www.scriptmasterlabs.com/applications",
        },
    },
}


def render_cross_link_block(vertical: str | None) -> list[str]:
    """Return an MDX section (list of lines) for the given vertical, or [] if
    no cross-link rule is registered for that vertical. The block is a heading
    followed by one paragraph carrying 2 contextual anchor links."""
    spec = CROSS_LINKS.get(vertical or "")
    if not spec:
        return []
    body = spec["body"].format(**spec["links"])
    return [f"## {spec['heading']}", "", body, ""]


def all_outbound_targets(vertical: str | None) -> list[str]:
    """Return the URLs the cross-link block will emit for the given vertical.
    Used by tests and by the JSON-LD factory to dedupe self-references."""
    spec = CROSS_LINKS.get(vertical or "")
    return list(spec["links"].values()) if spec else []
