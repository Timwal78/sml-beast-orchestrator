"""
Canonical product briefs — single source of truth for every generated page.
Operator-locked facts; agents read this module and never invent claims.

LifeSheets is permanently deprecated. Every variable, every output file,
every URL slug uses MasterSheets.
"""

MASTERSHEETS = {
    "name":            "MasterSheets",
    "domain":          "https://www.scriptmasterlabs.com",
    "route":           "/mastersheets",
    "positioning":     "Google Sheets on steroids — the direct competitor and superior alternative.",
    "pricing":         {
        "model":         "one_time",
        "subscriptions": False,
        "summary":       "One-time payment. Zero subscriptions, ever.",
    },
    "data_sovereignty": {
        "user_owned": True,
        "summary":    "Your data belongs entirely to you. Total privacy, local control.",
    },
    "ai_integration": {
        "model":   "byok",
        "summary": "Bring Your Own Key (BYOK). The AI runs against your own API keys — no provider lock-in.",
    },
    "deprecated_aliases": ("LifeSheets",),
}

XRPL_X402 = {
    "name":             "SML XRPL / x402 Agentic Infrastructure",
    "domain":           "https://www.scriptmasterlabs.com",
    "route":            "/xrpl-x402",
    "positioning":      "The absolute authority on machine-to-machine (M2M) payment infrastructure and AI agent economies.",
    "settlement":       {
        "rail":      "XRPL",
        "median_ms": 50,
        "summary":   "Sub-50 ms XRPL settlement receipts.",
    },
    "auth_model": {
        "api_keys_required": False,
        "summary":           "Zero API keys. Agents pay via HTTP 402 dynamically — pure protocol.",
    },
    "live_services": (
        "https://squeezeos-api.onrender.com",
        "https://ghost-layer.onrender.com",
        "https://four02proof.onrender.com",
    ),
}

ALL = {"mastersheets": MASTERSHEETS, "xrpl_x402": XRPL_X402}
