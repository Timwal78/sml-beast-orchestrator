"""Tests for sml_beast/outreach/balance.py — XRPL hot wallet balance check.

No real XRPL calls. account_info_fn is injected throughout.

Covers:
  - Healthy balance → healthy=True, no alert
  - Unhealthy balance → healthy=False, alert fired
  - Base reserve subtracted from raw XRP
  - USDC conversion uses BB7_XRP_PRICE_USDC env var
  - Default price (0.50) when env unset
  - Network error → degraded result (healthy=False, error set, NO alert)
  - Invalid seed → BalanceCheckError
  - Unknown network → BalanceCheckError
  - Bad BB7_XRP_PRICE_USDC → BalanceCheckError
  - to_dict() shape
  - check_and_alert_if_low suppresses alert on network error
"""

import importlib
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

_VALID_SEED = "sEdSKaCy2JT7JaM7v95H9SxkhP9wS2r"


def _ok_post():
    r = MagicMock()
    r.status_code = 204
    return r


class _BalanceBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="beast-balance-")
        os.environ["BEAST_OUTPUT_ROOT"] = self.tmp
        os.environ["BB7_XRPL_WALLET_SEED"] = _VALID_SEED
        os.environ["BB7_XRPL_NETWORK"] = "testnet"
        os.environ.pop("BB7_XRP_PRICE_USDC", None)

        import sml_beast.outreach.alerts as a
        import sml_beast.outreach.balance as b
        import sml_beast.outreach.guardrails as g
        importlib.reload(g)
        importlib.reload(a)
        importlib.reload(b)
        self.b = b
        self.a = a
        self.root = Path(self.tmp)

    def tearDown(self):
        for k in (
            "BEAST_OUTPUT_ROOT", "BB7_XRPL_WALLET_SEED",
            "BB7_XRPL_NETWORK", "BB7_XRP_PRICE_USDC",
            "BB7_DISCORD_ALERT_WEBHOOK",
        ):
            os.environ.pop(k, None)
        shutil.rmtree(self.tmp, ignore_errors=True)


# ── check_hot_wallet basic ───────────────────────────────────────────────────

class CheckHotWalletTests(_BalanceBase):
    def test_healthy_balance_returns_healthy_true(self):
        # 100 XRP at 0.50 USDC/XRP → spendable=90 → 45 USDC (>= 20 threshold)
        result = self.b.check_hot_wallet(account_info_fn=lambda a, n: 100.0)
        self.assertTrue(result.healthy)
        self.assertEqual(result.raw_xrp, 100.0)
        self.assertEqual(result.spendable_xrp, 90.0)
        self.assertEqual(result.usdc_equiv, 45.0)
        self.assertIsNone(result.error)

    def test_unhealthy_balance_returns_healthy_false(self):
        # 20 XRP → spendable=10 → 5 USDC (< 20 threshold)
        result = self.b.check_hot_wallet(account_info_fn=lambda a, n: 20.0)
        self.assertFalse(result.healthy)
        self.assertEqual(result.usdc_equiv, 5.0)

    def test_balance_below_reserve_clamps_to_zero(self):
        # 5 XRP (< 10 reserve) → spendable clamped to 0
        result = self.b.check_hot_wallet(account_info_fn=lambda a, n: 5.0)
        self.assertEqual(result.spendable_xrp, 0.0)
        self.assertEqual(result.usdc_equiv, 0.0)

    def test_uses_operator_price_when_env_set(self):
        # 1.00 USD/XRP → spendable=90 → 90 USDC
        os.environ["BB7_XRP_PRICE_USDC"] = "1.00"
        result = self.b.check_hot_wallet(account_info_fn=lambda a, n: 100.0)
        self.assertEqual(result.usdc_equiv, 90.0)

    def test_default_price_when_env_unset(self):
        result = self.b.check_hot_wallet(account_info_fn=lambda a, n: 100.0)
        # Default 0.50: 90 spendable * 0.50 = 45.00
        self.assertEqual(result.usdc_equiv, 45.0)

    def test_address_derived_from_seed(self):
        result = self.b.check_hot_wallet(account_info_fn=lambda a, n: 100.0)
        self.assertTrue(result.address.startswith("r"))

    def test_network_preserved_in_result(self):
        result = self.b.check_hot_wallet(account_info_fn=lambda a, n: 50.0)
        self.assertEqual(result.network, "testnet")


# ── failure paths ────────────────────────────────────────────────────────────

class FailureTests(_BalanceBase):
    def test_missing_seed_raises(self):
        os.environ.pop("BB7_XRPL_WALLET_SEED", None)
        with self.assertRaises(self.b.BalanceCheckError):
            self.b.check_hot_wallet(account_info_fn=lambda a, n: 50.0)

    def test_empty_seed_raises(self):
        os.environ["BB7_XRPL_WALLET_SEED"] = "   "
        with self.assertRaises(self.b.BalanceCheckError):
            self.b.check_hot_wallet(account_info_fn=lambda a, n: 50.0)

    def test_invalid_seed_raises(self):
        os.environ["BB7_XRPL_WALLET_SEED"] = "not-a-valid-seed"
        with self.assertRaises(self.b.BalanceCheckError):
            self.b.check_hot_wallet(account_info_fn=lambda a, n: 50.0)

    def test_unknown_network_raises(self):
        with self.assertRaises(self.b.BalanceCheckError):
            self.b.check_hot_wallet(
                network="atlantis", account_info_fn=lambda a, n: 50.0
            )

    def test_bad_price_env_raises(self):
        os.environ["BB7_XRP_PRICE_USDC"] = "not-a-float"
        with self.assertRaises(self.b.BalanceCheckError):
            self.b.check_hot_wallet(account_info_fn=lambda a, n: 50.0)

    def test_zero_price_env_raises(self):
        os.environ["BB7_XRP_PRICE_USDC"] = "0"
        with self.assertRaises(self.b.BalanceCheckError):
            self.b.check_hot_wallet(account_info_fn=lambda a, n: 50.0)

    def test_network_error_returns_degraded_result(self):
        def bad_fetch(a, n):
            raise ConnectionError("ledger unreachable")

        result = self.b.check_hot_wallet(account_info_fn=bad_fetch)
        self.assertFalse(result.healthy)
        self.assertEqual(result.raw_xrp, 0.0)
        self.assertIsNotNone(result.error)
        self.assertIn("ledger unreachable", result.error)


# ── alert integration ───────────────────────────────────────────────────────

class AlertIntegrationTests(_BalanceBase):
    def test_unhealthy_balance_fires_alert(self):
        os.environ["BB7_DISCORD_ALERT_WEBHOOK"] = "https://discord.example.com/webhook"
        # Re-reload alerts so it picks up the webhook env
        importlib.reload(self.a)

        captured = {}

        def capture(url, json, timeout):
            captured["url"] = url
            captured["json"] = json
            return _ok_post()

        result = self.b.check_and_alert_if_low(
            account_info_fn=lambda a, n: 20.0,
            post_fn=capture,
        )
        self.assertFalse(result.healthy)
        # Alert payload should mention the balance
        self.assertIn("url", captured, "Discord post should have been called")

    def test_healthy_balance_no_alert(self):
        os.environ["BB7_DISCORD_ALERT_WEBHOOK"] = "https://discord.example.com/webhook"
        importlib.reload(self.a)

        captured = {}

        def capture(url, json, timeout):
            captured["url"] = url
            return _ok_post()

        result = self.b.check_and_alert_if_low(
            account_info_fn=lambda a, n: 100.0,
            post_fn=capture,
        )
        self.assertTrue(result.healthy)
        self.assertEqual(captured, {})

    def test_network_error_suppresses_alert(self):
        # Confirmed low-balance fires; network-error does NOT
        os.environ["BB7_DISCORD_ALERT_WEBHOOK"] = "https://discord.example.com/webhook"
        importlib.reload(self.a)

        def bad_fetch(a, n):
            raise ConnectionError("ledger unreachable")

        captured = {}

        def capture(url, json, timeout):
            captured["url"] = url
            return _ok_post()

        result = self.b.check_and_alert_if_low(
            account_info_fn=bad_fetch,
            post_fn=capture,
        )
        self.assertIsNotNone(result.error)
        # Network errors must NOT trigger LOW_HOT_WALLET alerts (would spam)
        self.assertEqual(captured, {})


# ── to_dict serialization ────────────────────────────────────────────────────

class SerializationTests(_BalanceBase):
    def test_to_dict_has_all_fields(self):
        result = self.b.check_hot_wallet(account_info_fn=lambda a, n: 100.0)
        d = result.to_dict()
        for k in (
            "address", "network", "raw_xrp", "spendable_xrp",
            "usdc_equiv", "healthy", "error",
        ):
            self.assertIn(k, d)


# ── constants ────────────────────────────────────────────────────────────────

class ConstantsTests(_BalanceBase):
    def test_threshold_matches_alerts_module(self):
        # The threshold must be identical across modules — otherwise the alert
        # fires on a balance the guardrail still considers OK, or vice versa
        self.assertEqual(
            self.b.LOW_HOT_WALLET_THRESHOLD_USDC,
            self.a.LOW_HOT_WALLET_THRESHOLD_USDC,
        )


if __name__ == "__main__":
    unittest.main()
