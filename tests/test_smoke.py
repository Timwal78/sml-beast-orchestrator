"""Smoke tests — verify the orchestrator package imports and the x402 proxy
mints/validates internal tokens correctly. No network calls."""

import base64
import json
import os
import unittest


class SmokeTests(unittest.TestCase):
    def setUp(self):
        os.environ["X402_PROXY_SECRET"] = "test-secret-not-for-prod"
        os.environ["SERPER_API_KEY"] = "test-key"

    def test_package_imports(self):
        import sml_beast
        import sml_beast.adapters.x402_proxy
        import sml_beast.orchestrator
        import sml_beast.workers.mastersheets
        import sml_beast.workers.xrpl_x402

        self.assertEqual(sml_beast.__version__, "0.1.0")

    def test_briefs_canonical(self):
        from sml_beast.content.briefs import MASTERSHEETS, XRPL_X402

        self.assertEqual(MASTERSHEETS["name"], "MasterSheets")
        self.assertNotIn("LifeSheets", MASTERSHEETS["name"])
        self.assertFalse(MASTERSHEETS["pricing"]["subscriptions"])
        self.assertTrue(MASTERSHEETS["data_sovereignty"]["user_owned"])
        self.assertEqual(MASTERSHEETS["ai_integration"]["model"], "byok")
        self.assertEqual(XRPL_X402["settlement"]["median_ms"], 50)
        self.assertFalse(XRPL_X402["auth_model"]["api_keys_required"])

    def test_x402_token_roundtrip(self):
        from sml_beast.adapters.x402_proxy import _verify_payload, mint_internal_token

        token = mint_internal_token(wallet="test-agent")
        payload = json.loads(base64.b64decode(token))
        ok, info = _verify_payload(payload)
        self.assertTrue(ok, f"expected valid payload, got {info}")
        self.assertEqual(info, "test-agent")

    def test_x402_token_tamper_detected(self):
        from sml_beast.adapters.x402_proxy import _verify_payload, mint_internal_token

        token = mint_internal_token(wallet="test-agent")
        payload = json.loads(base64.b64decode(token))
        payload["body"] = payload["body"][:-1] + ("A" if payload["body"][-1] != "A" else "B")
        ok, info = _verify_payload(payload)
        self.assertFalse(ok)
        self.assertEqual(info, "ERR_SIGNATURE_INVALID")


if __name__ == "__main__":
    unittest.main()
