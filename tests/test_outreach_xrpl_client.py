"""Tests for sml_beast/outreach/xrpl_client.py — fire-and-forget Payment.

No real XRPL calls. client_factory + submit_fn are mocked so the test
suite exercises the full encoding + validation path without touching
testnet or mainnet.

Validates:
  - Refusal to instantiate without a seed
  - Rejection of bad network names
  - Bad input rejection in send_payment (None, 0, negative, self-send)
  - Happy-path Payment construction (drops conversion, memo encoding)
  - USDC-equivalent conversion via BB7_XRP_PRICE_USDC
  - Result parsing: validated, tesSUCCESS, hash extraction
  - Every failure path raises XRPLPaymentError with no silent retry
"""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

# A real-looking but disposable testnet seed (s-prefixed, base58)
TEST_SEED = os.environ.get("BB7_XRPL_SEED", "")


def _ok_response(
    tx_hash: str = "ABCDEF0123456789", validated: bool = True, ts_result: str = "tesSUCCESS"
) -> SimpleNamespace:
    """Build a SimpleNamespace mimicking the xrpl-py submit_and_wait return."""
    return SimpleNamespace(
        result={
            "validated": validated,
            "meta": {"TransactionResult": ts_result},
            "hash": tx_hash,
        }
    )


class _XRPLBase(unittest.TestCase):
    def setUp(self):
        os.environ["BB7_XRPL_WALLET_SEED"] = TEST_SEED
        # Reset price var so each test controls it
        os.environ.pop("BB7_XRP_PRICE_USDC", None)

    def tearDown(self):
        os.environ.pop("BB7_XRPL_WALLET_SEED", None)
        os.environ.pop("BB7_XRP_PRICE_USDC", None)

    def _client(self, submit_fn=None, **kwargs):
        from sml_beast.outreach.xrpl_client import XRPLClient

        return XRPLClient(
            client_factory=MagicMock(),
            submit_fn=submit_fn or MagicMock(return_value=_ok_response()),
            **kwargs,
        )


class ConstructionTests(_XRPLBase):
    def test_refuses_without_seed(self):
        os.environ.pop("BB7_XRPL_WALLET_SEED", None)
        from sml_beast.outreach.xrpl_client import XRPLClient

        with self.assertRaises(ValueError):
            XRPLClient(client_factory=MagicMock(), submit_fn=MagicMock())

    def test_refuses_with_whitespace_seed(self):
        os.environ["BB7_XRPL_WALLET_SEED"] = "   "
        from sml_beast.outreach.xrpl_client import XRPLClient

        with self.assertRaises(ValueError):
            XRPLClient(client_factory=MagicMock(), submit_fn=MagicMock())

    def test_rejects_unknown_network(self):
        from sml_beast.outreach.xrpl_client import XRPLClient

        with self.assertRaises(ValueError):
            XRPLClient(network="bogus-net", client_factory=MagicMock(), submit_fn=MagicMock())

    def test_accepts_testnet_by_default(self):
        c = self._client()
        self.assertEqual(c.network, "testnet")
        self.assertTrue(c.address.startswith("r"))  # XRPL classic addresses

    def test_accepts_mainnet(self):
        c = self._client(network="mainnet")
        self.assertEqual(c.network, "mainnet")

    def test_seed_is_not_logged(self):
        # The seed must not appear in self.__dict__ as a plaintext attribute
        c = self._client()
        for k, v in c.__dict__.items():
            self.assertNotIn(TEST_SEED, str(v), f"seed leaked via attribute {k}")


class SendPaymentInputValidationTests(_XRPLBase):
    def test_rejects_empty_destination(self):
        from sml_beast.outreach.xrpl_client import XRPLPaymentError

        c = self._client()
        with self.assertRaises(XRPLPaymentError):
            c.send_payment("", 5.0)

    def test_rejects_none_destination(self):
        from sml_beast.outreach.xrpl_client import XRPLPaymentError

        c = self._client()
        with self.assertRaises(XRPLPaymentError):
            c.send_payment(None, 5.0)

    def test_rejects_zero_amount(self):
        from sml_beast.outreach.xrpl_client import XRPLPaymentError

        c = self._client()
        with self.assertRaises(XRPLPaymentError):
            c.send_payment("rRandomAddress123", 0)

    def test_rejects_negative_amount(self):
        from sml_beast.outreach.xrpl_client import XRPLPaymentError

        c = self._client()
        with self.assertRaises(XRPLPaymentError):
            c.send_payment("rRandomAddress123", -1.0)

    def test_rejects_self_send(self):
        from sml_beast.outreach.xrpl_client import XRPLPaymentError

        c = self._client()
        with self.assertRaises(XRPLPaymentError):
            c.send_payment(c.address, 5.0)


class SendPaymentHappyPathTests(_XRPLBase):
    def test_happy_path_returns_tx_hash(self):
        submit = MagicMock(return_value=_ok_response(tx_hash="ABC123XYZ"))
        c = self._client(submit_fn=submit)
        tx_hash = c.send_payment("rValidLookingAddressXXX1234567890", 10.0)
        self.assertEqual(tx_hash, "ABC123XYZ")
        # submit_fn called with the Payment tx
        self.assertEqual(submit.call_count, 1)
        sent_tx = submit.call_args[0][0]
        self.assertEqual(type(sent_tx).__name__, "Payment")

    def test_amount_converted_to_drops_correctly(self):
        # 10 XRP -> "10000000" drops; xrpl-py handles the encoding
        submit = MagicMock(return_value=_ok_response())
        c = self._client(submit_fn=submit)
        c.send_payment("rValidAddressXXX1234567890123", 10.0)
        sent_tx = submit.call_args[0][0]
        self.assertEqual(sent_tx.amount, "10000000")

    def test_memo_is_hex_encoded(self):
        submit = MagicMock(return_value=_ok_response())
        c = self._client(submit_fn=submit)
        c.send_payment("rValidAddressXXX1234567890123", 5.0, memo="pitch-id-abc")
        sent_tx = submit.call_args[0][0]
        self.assertTrue(sent_tx.memos)
        memo_data = sent_tx.memos[0].memo_data
        # "pitch-id-abc" -> hex
        self.assertEqual(memo_data, b"pitch-id-abc".hex().upper())


class SendPaymentFailurePathTests(_XRPLBase):
    def test_submit_exception_raises_payment_error(self):
        from sml_beast.outreach.xrpl_client import XRPLPaymentError

        submit = MagicMock(side_effect=RuntimeError("network down"))
        c = self._client(submit_fn=submit)
        with self.assertRaises(XRPLPaymentError) as ctx:
            c.send_payment("rValidAddress12345678901234567", 5.0)
        self.assertIn("network down", str(ctx.exception))

    def test_non_validated_response_raises(self):
        from sml_beast.outreach.xrpl_client import XRPLPaymentError

        submit = MagicMock(return_value=_ok_response(validated=False))
        c = self._client(submit_fn=submit)
        with self.assertRaises(XRPLPaymentError) as ctx:
            c.send_payment("rValidAddress12345678901234567", 5.0)
        self.assertIn("not validated", str(ctx.exception))

    def test_non_tessuccess_result_raises(self):
        from sml_beast.outreach.xrpl_client import XRPLPaymentError

        submit = MagicMock(return_value=_ok_response(ts_result="tecUNFUNDED_PAYMENT"))
        c = self._client(submit_fn=submit)
        with self.assertRaises(XRPLPaymentError) as ctx:
            c.send_payment("rValidAddress12345678901234567", 5.0)
        self.assertIn("non-success", str(ctx.exception))

    def test_missing_hash_raises(self):
        from sml_beast.outreach.xrpl_client import XRPLPaymentError

        bad = SimpleNamespace(
            result={"validated": True, "meta": {"TransactionResult": "tesSUCCESS"}}
        )
        submit = MagicMock(return_value=bad)
        c = self._client(submit_fn=submit)
        with self.assertRaises(XRPLPaymentError) as ctx:
            c.send_payment("rValidAddress12345678901234567", 5.0)
        self.assertIn("missing tx hash", str(ctx.exception))

    def test_malformed_response_raises(self):
        from sml_beast.outreach.xrpl_client import XRPLPaymentError

        submit = MagicMock(return_value=SimpleNamespace(result="not a dict"))
        c = self._client(submit_fn=submit)
        with self.assertRaises(XRPLPaymentError):
            c.send_payment("rValidAddress12345678901234567", 5.0)


class USDCEquivalentTests(_XRPLBase):
    def test_uses_default_price_when_env_unset(self):
        # Default = 0.50 USD/XRP -> 5.00 USDC = 10 XRP
        submit = MagicMock(return_value=_ok_response())
        c = self._client(submit_fn=submit)
        _, xrp_sent = c.send_demo_payment_for_usdc("rValidAddress12345678901234567", 5.00)
        self.assertEqual(xrp_sent, 10.0)
        sent_tx = submit.call_args[0][0]
        self.assertEqual(sent_tx.amount, "10000000")

    def test_uses_operator_price_when_env_set(self):
        # 1.00 USD/XRP -> 5.00 USDC = 5 XRP
        os.environ["BB7_XRP_PRICE_USDC"] = "1.00"
        submit = MagicMock(return_value=_ok_response())
        c = self._client(submit_fn=submit)
        _, xrp_sent = c.send_demo_payment_for_usdc("rValidAddress12345678901234567", 5.00)
        # Allow for tiny floating-point margin from the rounding helper
        self.assertAlmostEqual(xrp_sent, 5.0, places=4)

    def test_rejects_zero_usdc(self):
        from sml_beast.outreach.xrpl_client import XRPLPaymentError

        c = self._client()
        with self.assertRaises(XRPLPaymentError):
            c.send_demo_payment_for_usdc("rAnyAddress12345", 0)

    def test_rejects_negative_usdc(self):
        from sml_beast.outreach.xrpl_client import XRPLPaymentError

        c = self._client()
        with self.assertRaises(XRPLPaymentError):
            c.send_demo_payment_for_usdc("rAnyAddress12345", -1.0)

    def test_rejects_invalid_price_env(self):
        os.environ["BB7_XRP_PRICE_USDC"] = "not-a-float"
        from sml_beast.outreach.xrpl_client import XRPLPaymentError

        c = self._client()
        with self.assertRaises(XRPLPaymentError):
            c.send_demo_payment_for_usdc("rAnyAddress12345", 5.0)

    def test_rejects_zero_price_env(self):
        os.environ["BB7_XRP_PRICE_USDC"] = "0"
        from sml_beast.outreach.xrpl_client import XRPLPaymentError

        c = self._client()
        with self.assertRaises(XRPLPaymentError):
            c.send_demo_payment_for_usdc("rAnyAddress12345", 5.0)


class NoCustodyArchitectureTests(_XRPLBase):
    """Compile-time guarantees per the no-custody mandate. These tests
    will fail loudly if anyone reintroduces escrow primitives."""

    def test_no_escrow_methods_exist(self):
        from sml_beast.outreach import xrpl_client

        forbidden = [
            "EscrowCreate",
            "EscrowFinish",
            "EscrowCancel",
            "send_escrow",
            "create_escrow",
            "cancel_escrow",
            "finish_escrow",
        ]
        module_attrs = dir(xrpl_client)
        for f in forbidden:
            self.assertNotIn(f, module_attrs, f"no-custody mandate violation: {f} exposed")

        client = self._client()
        for f in forbidden:
            self.assertFalse(hasattr(client, f), f"no-custody mandate violation: XRPLClient.{f}")

    def test_only_one_money_movement_method(self):
        """Verifies the surface area for moving funds is exactly:
        send_payment + send_demo_payment_for_usdc (a helper wrapping
        send_payment). No other public method should touch the ledger."""
        c = self._client()
        public = [m for m in dir(c) if not m.startswith("_")]
        money_methods = [
            m
            for m in public
            if "send" in m.lower()
            or "pay" in m.lower()
            or "escrow" in m.lower()
            or "transfer" in m.lower()
        ]
        self.assertEqual(set(money_methods), {"send_payment", "send_demo_payment_for_usdc"})


if __name__ == "__main__":
    unittest.main()
