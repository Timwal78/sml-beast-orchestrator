"""
Semantic keyword silos per vertical. The orchestrator iterates each silo and
fans out one worker task per keyword. No padding terms; every entry was
chosen because it maps to a real intent the product directly serves.
"""

MASTERSHEETS_SILOS = {
    "alternative": [
        "google sheets alternative",
        "best google sheets alternative",
        "spreadsheet software no subscription",
        "one-time payment spreadsheet",
        "googlesheets replacement",
        "excel alternative no subscription",
        "googlesheets without subscription"
    ],
    "sovereignty": [
        "private data spreadsheet",
        "self-hosted spreadsheet",
        "offline spreadsheet ai",
        "spreadsheet data ownership",
    ],
    "ai_byok": [
        "byok ai spreadsheet",
        "bring your own key spreadsheet",
        "spreadsheet llm integration",
        "openai key in spreadsheet",
    ],
    "visual_debt": [
        "visual debt reduction algorithm",
        "spreadsheet readability tool",
        "auto-formatting spreadsheet ai",
    ],
}

XRPL_X402_SILOS = {
    "protocol": [
        "x402 payment protocol",
        "http 402 payment required",
        "x402 facilitator",
        "x402 wire protocol",
    ],
    "agent_economy": [
        "ai agent payment infrastructure",
        "ai agent settlement layer",
        "m2m stablecoin api",
        "machine-to-machine payments",
    ],
    "xrpl_rail": [
        "xrpl mcp paywall",
        "rlusd agent payments",
        "sub-50ms crypto receipts",
        "xrpl micropayments api",
    ],
    "mcp": [
        "mcp paid tool",
        "model context protocol payment",
        "claude mcp x402",
    ],
}

ALL = {"mastersheets": MASTERSHEETS_SILOS, "xrpl_x402": XRPL_X402_SILOS}


def flatten(silos: dict) -> list[str]:
    out = []
    for terms in silos.values():
        out.extend(terms)
    return out
