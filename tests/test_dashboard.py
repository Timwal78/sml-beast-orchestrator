"""Tests for the operator dashboard module.

Covers: route registration, ledger reflection, vertical safety, bounty
JSON pass-through, page enumeration, and strict aesthetic guardrails on
the HTML payload (pure black bg, neon palette, monospace, no rounded
corners — the operator's zero-tolerance UI mandate)."""

import json
import os
import shutil
import tempfile
import unittest

# Test isolation — set required env BEFORE importing the proxy module
os.environ.setdefault("SERPER_API_KEY",     "test-key")
os.environ.setdefault("X402_PROXY_SECRET",  "test-secret")

from flask import Flask
from sml_beast.dashboard import register_dashboard


class DashboardRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="beast-dash-")
        # Pre-populate one vertical with realistic artifacts
        ms_dir = os.path.join(self.tmp, "mastersheets")
        os.makedirs(os.path.join(ms_dir, "google-sheets-alternative"))
        with open(os.path.join(ms_dir, "google-sheets-alternative", "page.mdx"), "w") as f:
            f.write("# test page")
        with open(os.path.join(ms_dir, "bounty_targets.json"), "w") as f:
            json.dump({
                "generated_at":         1700000000,
                "vertical":             "mastersheets",
                "total_serps_ingested": 12,
                "total_domains":        2,
                "targets": [
                    {"domain": "capterra.com", "frequency": 5, "class": "aggregator",
                     "class_weight": 4, "priority_score": 20,
                     "sample_titles": ["x"], "sample_urls": ["https://capterra.com/x"],
                     "discovered_via": ["alts"]},
                    {"domain": "g2.com", "frequency": 3, "class": "aggregator",
                     "class_weight": 4, "priority_score": 12,
                     "sample_titles": ["y"], "sample_urls": ["https://g2.com/y"],
                     "discovered_via": ["alts"]},
                ],
            }, f)

        # Fake the proxy ledger
        from threading import Lock
        self.ledger_lock = Lock()
        self.ledger = {
            "beast-mastersheets": {"calls": 7, "paid_usdc": 0.007, "last_ts": 1700000050},
            "beast-xrpl_x402":    {"calls": 3, "paid_usdc": 0.003, "last_ts": 1700000020},
        }

        self.app = Flask("test-dash")
        register_dashboard(self.app, self.tmp, (self.ledger_lock, self.ledger))
        self.client = self.app.test_client()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dashboard_html_served(self):
        r = self.client.get("/dashboard")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/html", r.content_type)
        body = r.data.decode()
        self.assertIn("SML.BEAST.OPS", body)

    def test_state_endpoint_reflects_ledger_and_verticals(self):
        r = self.client.get("/api/dashboard/state")
        self.assertEqual(r.status_code, 200)
        s = r.get_json()
        self.assertEqual(s["proxy"]["total_calls"], 10)
        wallets = {w["wallet"] for w in s["proxy"]["wallets"]}
        self.assertEqual(wallets, {"beast-mastersheets", "beast-xrpl_x402"})
        verticals = {v["vertical"]: v for v in s["verticals"]}
        self.assertEqual(verticals["mastersheets"]["bounty_domains"], 2)
        self.assertEqual(verticals["mastersheets"]["serps_ingested"], 12)
        self.assertEqual(verticals["mastersheets"]["pages_generated"], 1)
        self.assertEqual(verticals["xrpl"]["pages_generated"], 0)
        self.assertEqual(verticals["xrpl"]["bounty_domains"], 0)

    def test_bounty_endpoint_returns_json(self):
        r = self.client.get("/api/dashboard/bounty/mastersheets")
        self.assertEqual(r.status_code, 200)
        b = r.get_json()
        self.assertEqual(b["total_domains"], 2)
        self.assertEqual(b["targets"][0]["domain"], "capterra.com")

    def test_bounty_endpoint_handles_missing_vertical_file(self):
        r = self.client.get("/api/dashboard/bounty/xrpl")
        self.assertEqual(r.status_code, 200)
        b = r.get_json()
        self.assertTrue(b.get("missing"))
        self.assertEqual(b.get("total_domains", 0), 0)

    def test_unknown_vertical_is_404(self):
        r = self.client.get("/api/dashboard/bounty/nonexistent")
        self.assertEqual(r.status_code, 404)
        r2 = self.client.get("/api/dashboard/pages/nonexistent")
        self.assertEqual(r2.status_code, 404)

    def test_pages_endpoint_lists_generated_artifacts(self):
        r = self.client.get("/api/dashboard/pages/mastersheets")
        self.assertEqual(r.status_code, 200)
        p = r.get_json()
        self.assertEqual(p["vertical"], "mastersheets")
        self.assertEqual(len(p["pages"]), 1)
        self.assertEqual(p["pages"][0]["slug"], "google-sheets-alternative")

    def test_pages_endpoint_handles_missing_dir(self):
        r = self.client.get("/api/dashboard/pages/xrpl")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["pages"], [])


class BeastmodeAestheticGuardTests(unittest.TestCase):
    """Zero-tolerance aesthetic enforcement. If these break, someone slipped
    corporate grey or default browser styling into the operator dashboard."""

    def setUp(self):
        from threading import Lock
        self.app = Flask("test-aesthetic")
        register_dashboard(self.app, "/tmp/empty-root-doesnt-exist", (Lock(), {}))
        self.client = self.app.test_client()
        self.body = self.client.get("/dashboard").data.decode()

    def test_pure_black_background_declared(self):
        self.assertIn("#000000", self.body)
        # secondary panels stay near-black, never grey
        for shade in ("#050505", "#0a0a0a"):
            self.assertIn(shade, self.body)

    def test_neon_palette_present(self):
        for neon in ("#00ffff", "#ff00ff", "#00ff66", "#ffb000"):
            self.assertIn(neon, self.body)

    def test_zero_rounded_corners(self):
        # The strictest rule. Beastmode = sharp corners everywhere.
        self.assertIn("border-radius: 0 !important", self.body)

    def test_monospace_typeface(self):
        self.assertIn("JetBrains Mono", self.body)
        self.assertIn("monospace", self.body)

    def test_no_default_bootstrap_or_corporate_grey(self):
        # No #ccc / #ddd / #888 / #aaa / bootstrap classes
        for forbidden in ("#cccccc", "#dddddd", "#888888", "#aaaaaa",
                          "bootstrap", "container-fluid", "btn btn-primary"):
            self.assertNotIn(forbidden.lower(), self.body.lower())

    def test_terminal_signifiers_present(self):
        # The UI must read like an institutional terminal, not a SaaS dashboard
        self.assertIn("SML.BEAST.OPS", self.body)
        self.assertIn("[PROXY]", self.body)
        self.assertIn("[BOUNTY]", self.body)
        self.assertIn("[VERTICAL]", self.body)

    def test_crt_scanline_overlay_present(self):
        # Repeating-linear-gradient over body::before — the institutional touch
        self.assertIn("repeating-linear-gradient", self.body)
        self.assertIn("body::before", self.body)

    def test_glow_text_shadow_on_neon(self):
        # Neon without glow is just colored text. Beastmode requires shadow.
        self.assertIn("text-shadow:", self.body)
        self.assertIn("box-shadow:", self.body)


if __name__ == "__main__":
    unittest.main()
