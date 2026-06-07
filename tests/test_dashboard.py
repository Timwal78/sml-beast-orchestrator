"""Tests for the operator dashboard module.

Covers: failure-closed auth gate (Bearer + query-param HTML shortcut),
route registration, ledger reflection, vertical safety, bounty JSON
pass-through, page enumeration, and strict Beastmode aesthetic
guardrails on the HTML payload."""

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


TEST_TOKEN = "test-dashboard-token-32bytes-long-enough"
AUTH_HEADER = {"Authorization": f"Bearer {TEST_TOKEN}"}


class DashboardRouteTests(unittest.TestCase):
    def setUp(self):
        os.environ["DASHBOARD_AUTH_TOKEN"] = TEST_TOKEN
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
        os.environ.pop("DASHBOARD_AUTH_TOKEN", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dashboard_html_served(self):
        r = self.client.get("/dashboard", headers=AUTH_HEADER)
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/html", r.content_type)
        body = r.data.decode()
        self.assertIn("SML.BEAST.OPS", body)

    def test_state_endpoint_reflects_ledger_and_verticals(self):
        r = self.client.get("/api/dashboard/state", headers=AUTH_HEADER)
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
        r = self.client.get("/api/dashboard/bounty/mastersheets", headers=AUTH_HEADER)
        self.assertEqual(r.status_code, 200)
        b = r.get_json()
        self.assertEqual(b["total_domains"], 2)
        self.assertEqual(b["targets"][0]["domain"], "capterra.com")

    def test_bounty_endpoint_handles_missing_vertical_file(self):
        r = self.client.get("/api/dashboard/bounty/xrpl", headers=AUTH_HEADER)
        self.assertEqual(r.status_code, 200)
        b = r.get_json()
        self.assertTrue(b.get("missing"))
        self.assertEqual(b.get("total_domains", 0), 0)

    def test_unknown_vertical_is_404(self):
        r = self.client.get("/api/dashboard/bounty/nonexistent", headers=AUTH_HEADER)
        self.assertEqual(r.status_code, 404)
        r2 = self.client.get("/api/dashboard/pages/nonexistent", headers=AUTH_HEADER)
        self.assertEqual(r2.status_code, 404)

    def test_pages_endpoint_lists_generated_artifacts(self):
        r = self.client.get("/api/dashboard/pages/mastersheets", headers=AUTH_HEADER)
        self.assertEqual(r.status_code, 200)
        p = r.get_json()
        self.assertEqual(p["vertical"], "mastersheets")
        self.assertEqual(len(p["pages"]), 1)
        self.assertEqual(p["pages"][0]["slug"], "google-sheets-alternative")

    def test_pages_endpoint_handles_missing_dir(self):
        r = self.client.get("/api/dashboard/pages/xrpl", headers=AUTH_HEADER)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["pages"], [])


class BeastmodeAestheticGuardTests(unittest.TestCase):
    """Zero-tolerance aesthetic enforcement. If these break, someone slipped
    corporate grey or default browser styling into the operator dashboard."""

    def setUp(self):
        os.environ["DASHBOARD_AUTH_TOKEN"] = TEST_TOKEN
        from threading import Lock
        self.app = Flask("test-aesthetic")
        register_dashboard(self.app, "/tmp/empty-root-doesnt-exist", (Lock(), {}))
        self.client = self.app.test_client()
        self.body = self.client.get("/dashboard", headers=AUTH_HEADER).data.decode()

    def tearDown(self):
        os.environ.pop("DASHBOARD_AUTH_TOKEN", None)

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


class AuthGateTests(unittest.TestCase):
    """The dashboard auth gate is failure-closed: routes do not exist at all
    without DASHBOARD_AUTH_TOKEN. With the token, every route requires the
    Bearer header (the HTML route additionally accepts ?token=<> for the
    first-load handshake). These tests verify both the closed and open
    states comprehensively."""

    def _build_app(self):
        from threading import Lock
        app = Flask("test-auth")
        register_dashboard(app, "/tmp/auth-test-empty", (Lock(), {}))
        return app

    # ── failure-closed: token unset means routes don't exist ──

    def test_no_token_means_no_dashboard_routes(self):
        os.environ.pop("DASHBOARD_AUTH_TOKEN", None)
        client = self._build_app().test_client()
        # 404 (not 401) — the routes were never registered
        self.assertEqual(client.get("/dashboard").status_code, 404)
        self.assertEqual(client.get("/api/dashboard/state").status_code, 404)
        self.assertEqual(client.get("/api/dashboard/bounty/mastersheets").status_code, 404)
        self.assertEqual(client.get("/api/dashboard/pages/mastersheets").status_code, 404)

    def test_empty_token_means_no_dashboard_routes(self):
        # Whitespace-only token equivalent to unset
        os.environ["DASHBOARD_AUTH_TOKEN"] = "   "
        try:
            client = self._build_app().test_client()
            self.assertEqual(client.get("/dashboard").status_code, 404)
            self.assertEqual(client.get("/api/dashboard/state").status_code, 404)
        finally:
            os.environ.pop("DASHBOARD_AUTH_TOKEN", None)

    # ── token set: 401 on missing / wrong, 200 on correct ──

    def test_missing_auth_header_returns_401(self):
        os.environ["DASHBOARD_AUTH_TOKEN"] = TEST_TOKEN
        try:
            client = self._build_app().test_client()
            self.assertEqual(client.get("/dashboard").status_code, 401)
            self.assertEqual(client.get("/api/dashboard/state").status_code, 401)
            self.assertEqual(client.get("/api/dashboard/bounty/mastersheets").status_code, 401)
            self.assertEqual(client.get("/api/dashboard/pages/mastersheets").status_code, 401)
        finally:
            os.environ.pop("DASHBOARD_AUTH_TOKEN", None)

    def test_wrong_token_returns_401(self):
        os.environ["DASHBOARD_AUTH_TOKEN"] = TEST_TOKEN
        try:
            client = self._build_app().test_client()
            bad = {"Authorization": "Bearer this-is-not-the-token"}
            self.assertEqual(client.get("/dashboard",                 headers=bad).status_code, 401)
            self.assertEqual(client.get("/api/dashboard/state",        headers=bad).status_code, 401)
            self.assertEqual(client.get("/api/dashboard/bounty/mastersheets", headers=bad).status_code, 401)
        finally:
            os.environ.pop("DASHBOARD_AUTH_TOKEN", None)

    def test_malformed_auth_header_returns_401(self):
        os.environ["DASHBOARD_AUTH_TOKEN"] = TEST_TOKEN
        try:
            client = self._build_app().test_client()
            for bad_hdr in ("", "Basic xxx", "Bearer", "Bearer  ", "Token " + TEST_TOKEN):
                r = client.get("/api/dashboard/state",
                               headers={"Authorization": bad_hdr})
                self.assertEqual(r.status_code, 401, f"header={bad_hdr!r}")
        finally:
            os.environ.pop("DASHBOARD_AUTH_TOKEN", None)

    def test_correct_bearer_token_returns_200(self):
        os.environ["DASHBOARD_AUTH_TOKEN"] = TEST_TOKEN
        try:
            client = self._build_app().test_client()
            self.assertEqual(client.get("/dashboard",         headers=AUTH_HEADER).status_code, 200)
            self.assertEqual(client.get("/api/dashboard/state", headers=AUTH_HEADER).status_code, 200)
        finally:
            os.environ.pop("DASHBOARD_AUTH_TOKEN", None)

    # ── ?token=<> shortcut: HTML route only ──

    def test_query_param_works_on_html_route(self):
        os.environ["DASHBOARD_AUTH_TOKEN"] = TEST_TOKEN
        try:
            client = self._build_app().test_client()
            r = client.get(f"/dashboard?token={TEST_TOKEN}")
            self.assertEqual(r.status_code, 200)
        finally:
            os.environ.pop("DASHBOARD_AUTH_TOKEN", None)

    def test_query_param_does_NOT_work_on_api_routes(self):
        # The query-param shortcut is HTML-only — API routes always require
        # the header. This prevents tokens leaking into server logs via URLs.
        os.environ["DASHBOARD_AUTH_TOKEN"] = TEST_TOKEN
        try:
            client = self._build_app().test_client()
            r = client.get(f"/api/dashboard/state?token={TEST_TOKEN}")
            self.assertEqual(r.status_code, 401)
            r2 = client.get(f"/api/dashboard/bounty/mastersheets?token={TEST_TOKEN}")
            self.assertEqual(r2.status_code, 401)
        finally:
            os.environ.pop("DASHBOARD_AUTH_TOKEN", None)

    def test_wrong_query_param_token_on_html_returns_401(self):
        os.environ["DASHBOARD_AUTH_TOKEN"] = TEST_TOKEN
        try:
            client = self._build_app().test_client()
            r = client.get("/dashboard?token=not-the-real-token")
            self.assertEqual(r.status_code, 401)
        finally:
            os.environ.pop("DASHBOARD_AUTH_TOKEN", None)

    # ── HTML client-side handshake ──

    def test_html_strips_token_from_url_via_history_replacestate(self):
        os.environ["DASHBOARD_AUTH_TOKEN"] = TEST_TOKEN
        try:
            client = self._build_app().test_client()
            body = client.get("/dashboard", headers=AUTH_HEADER).data.decode()
            # The client-side handshake must strip the token from the URL
            self.assertIn("history.replaceState", body)
            self.assertIn("sessionStorage.setItem", body)
            self.assertIn("beast_dash_token", body)
            self.assertIn("Authorization", body)
        finally:
            os.environ.pop("DASHBOARD_AUTH_TOKEN", None)


if __name__ == "__main__":
    unittest.main()
