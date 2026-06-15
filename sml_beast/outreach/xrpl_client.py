"""
BB7 XRPL client — fire-and-forget Payment only. SML DOES NOT CUSTODY.

This module exposes ONE money-movement operation: `send_payment()`.
There are no escrow primitives. No EscrowCreate. No EscrowFinish. No
EscrowCancel. No multi-step settlement where SML's wallet seed could
claw back funds.

Per BB7_DESIGN.md §5 (v2 sealed, no-custody mandate): once a payment
leaves the hot wallet, it is gone from SML's control. The XRPL ledger
finalizes the transfer atomically (sub-50ms typical) and there is no
SML-controlled hold state.

Currency model
--------------
v1 sends native XRP. The guardrails layer accounts in USDC-equivalent
units (operator-facing audit), and this client converts at send time
using BB7_XRP_PRICE_USDC (operator-set; default 0.50 USD/XRP).

Why native XRP and not RLUSD or USDC IOU:
  - IOU payments require the destination to hold an active trustline
    to the issuer. Outreach recipients are arbitrary domain owners
    who will NOT have a pre-existing trustline to any SML issuer.
  - Native XRP transfers work to any funded XRPL account.
  - The pitch's "watch sub-50ms settlement" demo is structurally
    purer in native XRP — no IOU complications, no DEX paths.

Switching to RLUSD / USDC IOU is a future operator decision tied to
recipient onboarding. The module is structured so adding IOU support
is a single new method, not a rewrite.

Networks
--------
  testnet: https://s.altnet.rippletest.net:51234 (default until
           operator signs off on first 5 mainnet pitches per BB7
           manual review gate)
  mainnet: https://xrplcluster.com

Test isolation
--------------
client_factory and submit_fn are injectable so unit tests stub the
ledger entirely. No real network calls in the test suite. CI installs
xrpl-py via the [bb7] extra purely for type resolution.
"""

import logging
import os
from collections.abc import Callable
from typing import Any

from xrpl.clients import JsonRpcClient
from xrpl.models.transactions import Payment
from xrpl.transaction import submit_and_wait
from xrpl.utils import xrp_to_drops
from xrpl.wallet import Wallet

logger = logging.getLogger("sml-beast.outreach.xrpl_client")


# Network endpoints. xrplcluster is community-maintained; alternative
# mainnet endpoints are documented at https://xrpl.org/public-servers.html
# Operator can override via BB7_XRPL_RPC_URL for a dedicated node.
NETWORKS = {
    "testnet": "https://s.altnet.rippletest.net:51234",
    "mainnet": "https://xrplcluster.com",
}

# Default conversion if operator hasn't set BB7_XRP_PRICE_USDC. Conservative
# (assumes XRP is cheaper than it usually is, so we send a bit more XRP than
# necessary rather than under-pay the demo).
DEFAULT_XRP_PRICE_USDC = 0.50


class XRPLPaymentError(Exception):
    """Raised on any non-success path: bad input, network failure,
    non-tesSUCCESS ledger result, malformed response. The agent layer
    catches this and routes the failure to operator review — it never
    silently retries (a retry on a partially-submitted Payment risks
    double-spend if the original tx eventually finalizes)."""


class XRPLClient:
    """Holds the hot wallet's signing key and submits Payment txs.

    The wallet seed lives ONLY in this client's memory + the
    BB7_XRPL_WALLET_SEED env var. It is never logged, never persisted
    to disk, never serialized into the state machine. The client
    refuses to instantiate without a seed (no soft-fallback to a
    fresh wallet — that would mean sending real money from an
    unintended source)."""

    def __init__(
        self,
        seed: str | None = None,
        network: str = "testnet",
        rpc_url: str | None = None,
        client_factory: Callable[[str], Any] = JsonRpcClient,
        submit_fn: Callable = submit_and_wait,
    ):
        seed = (seed or os.environ.get("BB7_XRPL_WALLET_SEED", "")).strip()
        if not seed:
            raise ValueError(
                "BB7_XRPL_WALLET_SEED required — refusing to instantiate XRPL "
                "client without a configured hot wallet seed."
            )
        if network not in NETWORKS:
            raise ValueError(
                f"Unknown XRPL network: {network!r}. Must be one of {sorted(NETWORKS)}"
            )

        self._wallet = Wallet.from_seed(seed)
        self._client = client_factory(rpc_url or NETWORKS[network])
        self._submit_fn = submit_fn
        self.network = network

        logger.info("XRPL client initialized on %s; hot wallet address=%s", network, self.address)

    @property
    def address(self) -> str:
        """The hot wallet's classic XRPL address. Safe to log."""
        return self._wallet.classic_address

    # ── core operation: fire-and-forget Payment ─────────────────────────────

    def send_payment(self, destination: str, amount_xrp: float, memo: str | None = None) -> str:
        """Submit a native XRP Payment and wait for ledger validation.

        Returns the transaction hash on success. Raises XRPLPaymentError
        on any failure path — caller MUST NOT silently retry (re-submission
        of a partially-finalized tx risks double-spend if the original
        eventually validates)."""
        if not destination or not isinstance(destination, str):
            raise XRPLPaymentError(f"invalid destination: {destination!r}")
        if amount_xrp is None or amount_xrp <= 0:
            raise XRPLPaymentError(f"amount_xrp must be positive, got {amount_xrp!r}")
        if destination == self.address:
            raise XRPLPaymentError("refusing to send to self — operator error?")

        drops = xrp_to_drops(amount_xrp)
        payment_kwargs: dict = {
            "account": self._wallet.classic_address,
            "amount": drops,
            "destination": destination,
        }
        if memo:
            # Optional MemoData for the tx — operator-visible context for the
            # demo (e.g., the pitch tracking ID). Stays on-ledger forever.
            payment_kwargs["memos"] = self._build_memos(memo)

        payment = Payment(**payment_kwargs)

        try:
            response = self._submit_fn(payment, self._client, self._wallet)
        except Exception as e:
            raise XRPLPaymentError(f"submit_and_wait raised: {e}") from e

        return self._extract_hash_or_raise(response)

    # ── USDC-equivalent helper for the agent layer ──────────────────────────

    def send_demo_payment_for_usdc(
        self, destination: str, usdc_amount: float, memo: str | None = None
    ) -> tuple[str, float]:
        """Convert USDC-equivalent fee to native XRP at the operator-set
        price and dispatch. Returns (tx_hash, actual_xrp_sent).

        Operator sets BB7_XRP_PRICE_USDC (USD per 1 XRP). Default 0.50
        is conservative — favors over-sending demo value rather than
        under-sending."""
        if usdc_amount <= 0:
            raise XRPLPaymentError(f"usdc_amount must be positive, got {usdc_amount!r}")
        try:
            price = float(os.environ.get("BB7_XRP_PRICE_USDC", DEFAULT_XRP_PRICE_USDC))
        except (TypeError, ValueError) as e:
            raise XRPLPaymentError(f"BB7_XRP_PRICE_USDC invalid: {e}") from e
        if price <= 0:
            raise XRPLPaymentError(f"BB7_XRP_PRICE_USDC must be positive, got {price}")

        # Round to 6 decimals (xrpl-py rejects more than 6 decimals on
        # xrp_to_drops; XRP's drop is 1e-6).
        xrp_amount = round(usdc_amount / price, 6)
        tx_hash = self.send_payment(destination, xrp_amount, memo=memo)
        return tx_hash, xrp_amount

    # ── internal helpers ────────────────────────────────────────────────────

    @staticmethod
    def _build_memos(memo_text: str) -> list:
        from xrpl.models.transactions import Memo

        # MemoData must be hex-encoded per XRPL protocol
        memo_hex = memo_text.encode("utf-8").hex().upper()
        return [Memo(memo_data=memo_hex)]

    @staticmethod
    def _extract_hash_or_raise(response: Any) -> str:
        """Parse the submit_and_wait response. Raises if the tx did not
        validate with tesSUCCESS or if the response shape is unexpected."""
        try:
            result = response.result
        except AttributeError as e:
            raise XRPLPaymentError(f"response has no .result: {e}") from e

        if not isinstance(result, dict):
            raise XRPLPaymentError(f"response.result is not a dict: {type(result)}")

        # validated must be True (ledger finalized) for fire-and-forget
        # semantics. submit_and_wait returns only after validation.
        if not result.get("validated", False):
            raise XRPLPaymentError(f"tx not validated: result_keys={sorted(result.keys())}")

        # TransactionResult lives under meta.TransactionResult per XRPL spec
        meta = result.get("meta", {})
        if isinstance(meta, dict):
            tx_result = meta.get("TransactionResult")
        else:
            tx_result = None

        if tx_result != "tesSUCCESS":
            raise XRPLPaymentError(f"non-success ledger result: {tx_result!r}")

        tx_hash = result.get("hash")
        if not tx_hash:
            raise XRPLPaymentError(f"missing tx hash in result: {sorted(result.keys())}")
        return tx_hash
