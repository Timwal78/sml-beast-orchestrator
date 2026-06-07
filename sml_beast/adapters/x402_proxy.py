"""
Internal x402 facilitator-proxy.

Agents talk pure x402 protocol to this proxy. The proxy validates the
X-PAYMENT envelope (HMAC-signed by the orchestrator's own secret — the
"x402 adapter" pattern the operator approved), debits an in-process meter,
then fires the *real* SERP query through to the upstream provider using
the operator's subscription key. Live data flows; protocol stays clean.

When a true x402 SERP provider ships in CDP Bazaar, swap `_fetch_upstream`
to forward `X-PAYMENT` directly and remove this proxy entirely.

Endpoints
---------
POST /api/v1/m2m/serp  — gated, returns live SERP from Serper.dev
GET  /.well-known/x402 — discovery manifest (Bazaar-compatible)
GET  /health           — health probe
"""

import base64
import hashlib
import hmac
import json
import os
import time
from threading import Lock

from flask import Flask, jsonify, make_response, request

from sml_beast.adapters.serper import SerperClient, SerperError

X402_VERSION = 1

# Operator subsidy: the proxy charges agents in a virtual ledger; real money
# leaves the operator's Serper subscription. When public x402 SERP APIs exist
# the price will pass through to the upstream call.
PRICE_USDC = os.environ.get("X402_SERP_PRICE_USDC", "0.001")
NETWORK = os.environ.get("X402_NETWORK", "base-sepolia")
PAY_TO = os.environ.get("X402_PAY_TO", "0x4e14B249D9A4c9c9352D780eCEB508A8eB7a7700")
SECRET = os.environ.get("X402_PROXY_SECRET", "")  # HMAC secret; required in prod

# USDC asset addresses by network (matches CDP facilitator)
USDC = {
    "base": {
        "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "extra": {"name": "USD Coin", "version": "2"},
    },
    "base-sepolia": {
        "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
        "extra": {"name": "USDC", "version": "2"},
    },
}

_ledger_lock = Lock()
_ledger: dict[str, dict] = {}  # agent_wallet -> {paid_usdc, calls, last_ts}


def _atomic(price: str) -> str:
    return str(round(float(price) * 1_000_000))


def _requirements(resource: str) -> dict:
    cfg = USDC.get(NETWORK, USDC["base-sepolia"])
    return {
        "scheme": "exact",
        "network": NETWORK,
        "maxAmountRequired": _atomic(PRICE_USDC),
        "resource": resource,
        "description": "Live SERP query — Google search result + People Also Ask + related searches.",
        "mimeType": "application/json",
        "payTo": PAY_TO,
        "maxTimeoutSeconds": 60,
        "asset": cfg["asset"],
        "extra": cfg["extra"],
    }


def _402(reqs: dict, reason: str = ""):
    return make_response(
        jsonify({"x402Version": X402_VERSION, "accepts": [reqs], "error": reason}),
        402,
    )


def _verify_payload(payload: dict) -> tuple[bool, str]:
    """
    Validate the X-PAYMENT envelope. In the live-bazaar path this calls the
    real facilitator /verify. In the operator-subsidy path it checks the
    payload's signed receipt against X402_PROXY_SECRET.
    """
    if not SECRET:
        return False, "ERR_SECRET_NOT_CONFIGURED"

    sig = payload.get("signature", "")
    body = payload.get("body", "")
    if not sig or not body:
        return False, "ERR_PAYLOAD_INCOMPLETE"

    expected = hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False, "ERR_SIGNATURE_INVALID"

    try:
        meta = json.loads(base64.urlsafe_b64decode(body + "===").decode())
    except Exception:
        return False, "ERR_BODY_MALFORMED"

    if int(time.time()) > meta.get("exp", 0):
        return False, "ERR_EXPIRED"

    return True, meta.get("wlt", "agent")


def create_app(output_root: str | None = None) -> Flask:
    app = Flask(__name__)
    serper = SerperClient()  # raises immediately if SERPER_API_KEY missing

    # Mount the operator dashboard if an output root is known. It's read-only
    # over the same ledger ref the proxy mutates, so the panel reflects live
    # state with no polling layer. If no output_root is given the dashboard
    # falls back to BEAST_OUTPUT_ROOT or repo-root/output.
    from sml_beast.dashboard import register_dashboard

    root = (
        output_root
        or os.environ.get("BEAST_OUTPUT_ROOT")
        or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "output")
    )
    register_dashboard(app, os.path.abspath(root), (_ledger_lock, _ledger))

    @app.route("/health", methods=["GET"])
    def health():
        with _ledger_lock:
            total_calls = sum(e["calls"] for e in _ledger.values())
        return jsonify(
            {
                "status": "ok",
                "network": NETWORK,
                "price_usdc": PRICE_USDC,
                "total_calls": total_calls,
                "ts": time.time(),
            }
        )

    @app.route("/.well-known/x402", methods=["GET"])
    def discovery():
        cfg = USDC.get(NETWORK, USDC["base-sepolia"])
        return jsonify(
            {
                "x402Version": X402_VERSION,
                "operator": "ScriptMasterLabs",
                "network": NETWORK,
                "asset": cfg["asset"],
                "payTo": PAY_TO,
                "facilitator": f"{request.host_url.rstrip('/')}/x402",
                "discoverable": True,
                "resources": [
                    {
                        "path": "/api/v1/m2m/serp",
                        "method": "POST",
                        "price": {"amountUSDC": PRICE_USDC, "asset": "USDC", "network": NETWORK},
                        "description": "Live Google SERP — organic, people-also-ask, related.",
                    }
                ],
            }
        )

    @app.route("/api/v1/m2m/serp", methods=["POST"])
    def search():
        reqs = _requirements(request.base_url)
        header = request.headers.get("X-PAYMENT")
        if not header:
            return _402(reqs, "payment required")

        try:
            payload = json.loads(base64.b64decode(header))
        except Exception:
            return _402(reqs, "malformed X-PAYMENT header")

        ok, info = _verify_payload(payload)
        if not ok:
            return _402(reqs, f"invalid payment: {info}")
        wallet = info

        body = request.get_json(silent=True) or {}
        q = (body.get("q") or "").strip()
        if not q:
            return jsonify({"error": "q (query string) required"}), 400

        try:
            data = serper.search(
                q, gl=body.get("gl", "us"), hl=body.get("hl", "en"), num=int(body.get("num", 10))
            )
        except SerperError as e:
            return jsonify({"error": "upstream_unavailable", "detail": str(e)}), 502

        with _ledger_lock:
            entry = _ledger.setdefault(wallet, {"paid_usdc": 0.0, "calls": 0, "last_ts": 0})
            entry["paid_usdc"] += float(PRICE_USDC)
            entry["calls"] += 1
            entry["last_ts"] = time.time()

        return jsonify({"x402Version": X402_VERSION, "wallet": wallet, "result": data})

    return app


def mint_internal_token(wallet: str = "beast-orchestrator", ttl_s: int = 3600) -> str:
    """Mint an X-PAYMENT header value for the orchestrator's own workers.
    Bridges the protocol gap — workers send this exact base64 string, the proxy
    verifies it with X402_PROXY_SECRET, real Serper key fires upstream."""
    if not SECRET:
        raise RuntimeError("X402_PROXY_SECRET not set — cannot mint internal token")
    meta = {"wlt": wallet, "exp": int(time.time()) + ttl_s, "amt": _atomic(PRICE_USDC)}
    body = base64.urlsafe_b64encode(json.dumps(meta).encode()).decode().rstrip("=")
    sig = hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
    payload = {"body": body, "signature": sig}
    return base64.b64encode(json.dumps(payload).encode()).decode()


if __name__ == "__main__":
    # Honor Render's $PORT; fall back to X402_PROXY_PORT for local dev.
    port = int(os.environ.get("PORT") or os.environ.get("X402_PROXY_PORT", "4020"))
    create_app().run(host="0.0.0.0", port=port)
