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
    "name":             "SML Institutional Rails (IRL) / x402 Paywall",
    "domain":           "https://www.scriptmasterlabs.com",
    "route":            "/infrastructure",
    "positioning":      "Sovereign machine-to-machine (M2M) micro-payment firewall and AI agent economy infrastructure.",
    "settlement":       {
        "rails":     ("XRPL", "Xahau"),
        "median_ms": 50,
        "summary":   "Sub-50 ms cryptographic payment receipts settled on the XRP Ledger and Xahau network.",
    },
    "auth_model": {
        "api_keys_required": False,
        "summary":           "HTTP 402 Payment Required — zero static API keys, zero corporate credit lines. Agents pay per call.",
    },
    "live_services": (
        "https://squeezeos-api.onrender.com",
        "https://ghost-layer.onrender.com",
        "https://four02proof.onrender.com",
    ),
}

ALL = {"mastersheets": MASTERSHEETS, "xrpl_x402": XRPL_X402}
