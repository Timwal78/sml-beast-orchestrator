"""
BB7 XRPL balance check — queries the hot wallet's actual on-ledger
balance and exposes it in USDC-equivalent terms for guardrails + alerts.

Per BB7_DESIGN.md §6: if hot wallet balance drops below
2 * OUTREACH_PREMIUM_FEE_USDC (i.e., 20 USDC-equivalent), freeze new
outreach until manual refill. This module is the source of truth for
"is the hot wallet healthy enough to start a cycle?"

Read-only — never submits a transaction, never mutates state. Safe to
call from preflight, opctl, and the run_cycle pre-flight gate.

Pricing:
  XRP-to-USDC conversion uses BB7_XRP_PRICE_USDC (operator-set; same
  env var the dispatcher uses). Default 0.50 USD/XRP if unset.

Injectable account_info_fn for test isolation — production fetches via
xrpl-py's account_info request; tests pass a mock callable.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger("sml-beast.outreach.balance")

# XRPL reserve — base 10 XRP per account + 2 XRP per owned object. We
# subtract the base reserve from available balance so the operator sees
# only the spendable portion. (Owned objects are rare for a fresh hot
# wallet; the 2 XRP per object math is left to the operator to track.)
XRPL_BASE_RESERVE_XRP = 10.0

DEFAULT_XRP_PRICE_USDC = 0.50

# Mirror of guardrails constant — minimum hot wallet balance before
# we freeze new outreach. Documented in BB7_DESIGN.md §6.
LOW_HOT_WALLET_THRESHOLD_USDC = 20.0


@dataclass
class BalanceCheck:
    address: str
    network: str
    raw_xrp: float        # total balance reported by the ledger
    spendable_xrp: float  # raw - base reserve
    usdc_equiv: float     # spendable_xrp * BB7_XRP_PRICE_USDC
    healthy: bool         # True if usdc_equiv >= LOW_HOT_WALLET_THRESHOLD_USDC
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "address": self.address,
            "network": self.network,
            "raw_xrp": self.raw_xrp,
            "spendable_xrp": self.spendable_xrp,
            "usdc_equiv": self.usdc_equiv,
            "healthy": self.healthy,
            "error": self.error,
        }


class BalanceCheckError(Exception):
    """Raised on unrecoverable check failure (invalid seed, network unreachable).
    Caller decides whether to halt the cycle or proceed without a balance
    signal."""


# ── price loader ─────────────────────────────────────────────────────────────


def _xrp_price_usdc() -> float:
    """Read BB7_XRP_PRICE_USDC; raises if unparseable."""
    raw = os.environ.get("BB7_XRP_PRICE_USDC", str(DEFAULT_XRP_PRICE_USDC))
    try:
        price = float(raw)
    except (TypeError, ValueError) as e:
        raise BalanceCheckError(f"BB7_XRP_PRICE_USDC invalid: {raw!r}: {e}") from e
    if price <= 0:
        raise BalanceCheckError(f"BB7_XRP_PRICE_USDC must be positive, got {price}")
    return price


# ── default account_info fetcher ─────────────────────────────────────────────


def _default_account_info(address: str, network: str) -> float:
    """Fetch XRP balance via xrpl-py. Returns raw XRP (float).

    Used by check_hot_wallet() unless an injected callable overrides it.
    Tests pass a mock; production wiring imports xrpl-py."""
    from xrpl.clients import JsonRpcClient
    from xrpl.models.requests import AccountInfo

    rpc_urls = {
        "testnet": "https://s.altnet.rippletest.net:51234",
        "mainnet": "https://xrplcluster.com",
    }
    rpc_url = os.environ.get("BB7_XRPL_RPC_URL", rpc_urls.get(network, ""))
    if not rpc_url:
        raise BalanceCheckError(f"unknown XRPL network: {network!r}")

    try:
        client = JsonRpcClient(rpc_url)
        req = AccountInfo(account=address, ledger_index="validated", strict=True)
        resp = client.request(req)
        if not resp.is_successful():
            raise BalanceCheckError(f"account_info failed: {resp.result}")
        drops = int(resp.result["account_data"]["Balance"])
    except Exception as e:
        raise BalanceCheckError(f"XRPL account_info error: {e}") from e

    # 1 XRP = 1,000,000 drops
    return drops / 1_000_000


# ── public check function ────────────────────────────────────────────────────


def check_hot_wallet(
    *,
    seed: str | None = None,
    network: str | None = None,
    account_info_fn: Callable[[str, str], float] | None = None,
) -> BalanceCheck:
    """Query the hot wallet balance and return a BalanceCheck.

    Reads seed from BB7_XRPL_WALLET_SEED and network from BB7_XRPL_NETWORK
    when not provided. Computes USDC-equivalent at BB7_XRP_PRICE_USDC.
    Raises BalanceCheckError on unrecoverable failure (invalid seed, no
    RPC endpoint). Network/account_info failures populate result.error
    and return healthy=False rather than raising — operators benefit
    from a degraded report over a hard crash.

    `account_info_fn` is injectable for test isolation. Default uses
    xrpl-py's AccountInfo request.
    """
    seed = (seed or os.environ.get("BB7_XRPL_WALLET_SEED", "")).strip()
    if not seed:
        raise BalanceCheckError("BB7_XRPL_WALLET_SEED required for balance check")

    network = (network or os.environ.get("BB7_XRPL_NETWORK", "testnet")).strip()
    if network not in ("testnet", "mainnet"):
        raise BalanceCheckError(f"unknown network: {network!r}")

    # Derive address from seed (no signing, just public key derivation)
    try:
        from xrpl.wallet import Wallet
        wallet = Wallet.from_seed(seed)
        address = wallet.classic_address
    except Exception as e:
        raise BalanceCheckError(f"seed invalid: {e}") from e

    price = _xrp_price_usdc()
    fetcher = account_info_fn or _default_account_info

    try:
        raw_xrp = fetcher(address, network)
    except BalanceCheckError:
        raise
    except Exception as e:
        # Network error — return degraded result rather than raising
        logger.warning("balance fetch failed: %s", e)
        return BalanceCheck(
            address=address,
            network=network,
            raw_xrp=0.0,
            spendable_xrp=0.0,
            usdc_equiv=0.0,
            healthy=False,
            error=str(e),
        )

    spendable_xrp = max(0.0, raw_xrp - XRPL_BASE_RESERVE_XRP)
    usdc_equiv = round(spendable_xrp * price, 2)
    healthy = usdc_equiv >= LOW_HOT_WALLET_THRESHOLD_USDC

    logger.info(
        "balance check: address=%s network=%s raw=%.6f spendable=%.6f usdc=%.2f healthy=%s",
        address,
        network,
        raw_xrp,
        spendable_xrp,
        usdc_equiv,
        healthy,
    )
    return BalanceCheck(
        address=address,
        network=network,
        raw_xrp=raw_xrp,
        spendable_xrp=spendable_xrp,
        usdc_equiv=usdc_equiv,
        healthy=healthy,
    )


# ── alert integration ────────────────────────────────────────────────────────


def check_and_alert_if_low(
    *,
    account_info_fn: Callable[[str, str], float] | None = None,
    post_fn=None,
) -> BalanceCheck:
    """Run balance check; if unhealthy, fire the LOW_HOT_WALLET Discord alert.

    Used by the agent's run_cycle() pre-flight and by the alerts-sweep cron.
    Returns the BalanceCheck so callers can decide whether to proceed."""
    check = check_hot_wallet(account_info_fn=account_info_fn)
    if not check.healthy and check.error is None:
        # Only alert on a confirmed low balance — not on a network error,
        # which would spam the operator when XRPL has a hiccup.
        from .alerts import alert_low_hot_wallet
        alert_low_hot_wallet(check.usdc_equiv, post_fn=post_fn)
    return check
