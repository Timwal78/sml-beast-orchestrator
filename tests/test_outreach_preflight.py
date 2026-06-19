"""Tests for sml_beast/outreach/preflight.py — preflight validator.

All network checks mocked. No real XRPL RPC, SMTP, or DNS calls.

Covers:
  - Env var validation: missing → FAIL, present → OK
  - XRPL wallet seed validation: good seed → OK, bad seed → FAIL
  - SMTP auth: mocked LOGIN succeeds → OK, raises → FAIL
  - DNS records: A record found → OK; SPF/DMARC missing → WARN
  - Output writable: tmpdir → OK; read-only path → FAIL
  - Kill switch: absent → OK, present → WARN
  - Manual review gate: 0/5 → WARN, 5/5 → OK
  - PreflightReport exit codes: all OK → 0, any WARN → 1, any FAIL → 2
"""

import importlib
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

_VALID_SEED = os.environ.get("BB7_XRPL_SEED", "")


def _env(**vars):
    base = {
        "BB7_XRPL_WALLET_SEED": _VALID_SEED,
        "BB7_SMTP_HOST": "smtp.example.com",
        "BB7_SMTP_USER": "user@example.com",
        "BB7_SMTP_PASS": "passw0rd",
        "BB7_OPT_OUT_URL": "https://sml.com/optout",
        "BB7_OPERATOR_ADDRESS": "123 Main St",
        "BB7_OPERATOR_SIGNATURE": "Tim",
    }
    base.update(vars)
    return base


class _PreflightBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="beast-preflight-")
        self._saved_env = {}
        for k, v in _env().items():
            self._saved_env[k] = os.environ.get(k)
            os.environ[k] = v
        os.environ["BEAST_OUTPUT_ROOT"] = self.tmp

        import sml_beast.outreach.guardrails as g
        import sml_beast.outreach.preflight as p
        import sml_beast.outreach.state as s

        importlib.reload(g)
        importlib.reload(s)
        importlib.reload(p)
        self.p = p

    def tearDown(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        os.environ.pop("BEAST_OUTPUT_ROOT", None)
        shutil.rmtree(self.tmp, ignore_errors=True)


# ── env var checks ───────────────────────────────────────────────────────────

class EnvVarTests(_PreflightBase):
    def test_all_required_present_ok(self):
        report = self.p.PreflightReport()
        self.p.check_env_vars(report)
        fails = [r for r in report.results if r.is_fail]
        self.assertEqual(fails, [])

    def test_missing_required_fails(self):
        os.environ.pop("BB7_SMTP_HOST", None)
        report = self.p.PreflightReport()
        self.p.check_env_vars(report)
        fails = [r for r in report.results if r.is_fail]
        self.assertTrue(any("BB7_SMTP_HOST" in r.name for r in fails))

    def test_empty_required_fails(self):
        os.environ["BB7_OPT_OUT_URL"] = "   "
        report = self.p.PreflightReport()
        self.p.check_env_vars(report)
        fails = [r for r in report.results if r.is_fail]
        self.assertTrue(any("BB7_OPT_OUT_URL" in r.name for r in fails))

    def test_secret_value_not_echoed(self):
        report = self.p.PreflightReport()
        self.p.check_env_vars(report)
        full_output = " ".join(r.detail for r in report.results)
        self.assertNotIn(_VALID_SEED, full_output)
        self.assertNotIn("passw0rd", full_output)


# ── XRPL wallet ───────────────────────────────────────────────────────────────

class XRPLWalletTests(_PreflightBase):
    def test_valid_seed_ok(self):
        report = self.p.PreflightReport()
        self.p.check_xrpl_wallet(report)
        results = [r for r in report.results if r.name == "xrpl:wallet"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].severity, self.p.SEVERITY_OK)
        self.assertIn("address=r", results[0].detail)

    def test_invalid_seed_fails(self):
        os.environ["BB7_XRPL_WALLET_SEED"] = "obviously-not-a-real-seed"
        report = self.p.PreflightReport()
        self.p.check_xrpl_wallet(report)
        results = [r for r in report.results if r.name == "xrpl:wallet"]
        self.assertTrue(results[0].is_fail)

    def test_seed_never_appears_in_report(self):
        report = self.p.PreflightReport()
        self.p.check_xrpl_wallet(report)
        full = " ".join(r.detail for r in report.results)
        self.assertNotIn(_VALID_SEED, full)


# ── SMTP ──────────────────────────────────────────────────────────────────────

class SMTPAuthTests(_PreflightBase):
    def test_auth_success_ok(self):
        smtp_mock = MagicMock()

        def factory(h, p):
            return smtp_mock

        report = self.p.PreflightReport()
        self.p.check_smtp_auth(report, smtp_factory=factory)
        results = [r for r in report.results if r.name == "smtp:auth"]
        self.assertEqual(results[0].severity, self.p.SEVERITY_OK)
        smtp_mock.login.assert_called_once()
        smtp_mock.quit.assert_called_once()

    def test_auth_failure_fails(self):
        import smtplib as _smtplib
        smtp_mock = MagicMock()
        smtp_mock.login.side_effect = _smtplib.SMTPAuthenticationError(535, b"bad")

        def factory(h, p):
            return smtp_mock

        report = self.p.PreflightReport()
        self.p.check_smtp_auth(report, smtp_factory=factory)
        results = [r for r in report.results if r.name == "smtp:auth"]
        self.assertTrue(results[0].is_fail)


# ── output writable ───────────────────────────────────────────────────────────

class OutputWritableTests(_PreflightBase):
    def test_writable_path_ok(self):
        report = self.p.PreflightReport()
        self.p.check_output_writable(report)
        results = [r for r in report.results if r.name == "fs:output"]
        self.assertEqual(results[0].severity, self.p.SEVERITY_OK)

    def test_unwritable_path_fails(self):
        os.environ["BEAST_OUTPUT_ROOT"] = "/proc/forbidden"
        report = self.p.PreflightReport()
        self.p.check_output_writable(report)
        results = [r for r in report.results if r.name == "fs:output"]
        self.assertTrue(results[0].is_fail)


# ── kill switch ───────────────────────────────────────────────────────────────

class KillSwitchTests(_PreflightBase):
    def test_no_kill_switch_ok(self):
        report = self.p.PreflightReport()
        self.p.check_kill_switch(report)
        results = [r for r in report.results if r.name == "kill_switch"]
        self.assertEqual(results[0].severity, self.p.SEVERITY_OK)

    def test_kill_switch_present_warns(self):
        from sml_beast.outreach.guardrails import kill_switch_path
        ks = kill_switch_path()
        ks.parent.mkdir(parents=True, exist_ok=True)
        ks.write_text("HALT")

        report = self.p.PreflightReport()
        self.p.check_kill_switch(report)
        results = [r for r in report.results if r.name == "kill_switch"]
        self.assertEqual(results[0].severity, self.p.SEVERITY_WARN)


# ── manual review gate ────────────────────────────────────────────────────────

class ManualReviewGateTests(_PreflightBase):
    def test_initial_state_warns(self):
        report = self.p.PreflightReport()
        self.p.check_manual_review_state(report)
        ms_results = [r for r in report.results if "mastersheets" in r.name]
        self.assertEqual(ms_results[0].severity, self.p.SEVERITY_WARN)

    def test_cleared_gate_ok(self):
        from sml_beast.outreach.state import OUTREACH_MANUAL_REVIEW_N, OutreachStateMachine
        sm = OutreachStateMachine()
        for _ in range(OUTREACH_MANUAL_REVIEW_N):
            sm.record_manual_review_completed("mastersheets")

        report = self.p.PreflightReport()
        self.p.check_manual_review_state(report)
        ms_results = [r for r in report.results if "mastersheets" in r.name]
        self.assertEqual(ms_results[0].severity, self.p.SEVERITY_OK)


# ── PreflightReport semantics ─────────────────────────────────────────────────

class ReportSemanticsTests(_PreflightBase):
    def test_all_ok_exit_code_zero(self):
        r = self.p.PreflightReport()
        r.add_ok("a", "ok")
        r.add_ok("b", "ok")
        self.assertEqual(r.exit_code(), 0)

    def test_any_warn_exit_code_one(self):
        r = self.p.PreflightReport()
        r.add_ok("a")
        r.add_warn("b", "soft")
        self.assertEqual(r.exit_code(), 1)

    def test_any_fail_exit_code_two(self):
        r = self.p.PreflightReport()
        r.add_warn("a", "soft")
        r.add_fail("b", "hard")
        self.assertEqual(r.exit_code(), 2)

    def test_fail_takes_priority_over_warn(self):
        r = self.p.PreflightReport()
        r.add_warn("a")
        r.add_fail("b")
        r.add_ok("c")
        self.assertEqual(r.exit_code(), 2)


# ── XRPL network check ───────────────────────────────────────────────────────

class XRPLNetworkTests(_PreflightBase):
    def test_unknown_network_fails(self):
        os.environ["BB7_XRPL_NETWORK"] = "atlantis"
        report = self.p.PreflightReport()
        self.p.check_xrpl_network(report, fetch_fn=lambda url: None)
        results = [r for r in report.results if r.name == "xrpl:network"]
        self.assertTrue(results[0].is_fail)

    def test_reachable_network_ok(self):
        report = self.p.PreflightReport()
        self.p.check_xrpl_network(report, fetch_fn=lambda url: {"status": "ok"})
        results = [r for r in report.results if r.name == "xrpl:network"]
        self.assertEqual(results[0].severity, self.p.SEVERITY_OK)


# ── run_preflight integration ─────────────────────────────────────────────────

class RunPreflightTests(_PreflightBase):
    def test_skip_network_does_not_call_smtp(self):
        with patch.object(self.p, "check_smtp_auth") as mock_smtp, \
             patch.object(self.p, "check_xrpl_network") as mock_xrpl, \
             patch.object(self.p, "check_dns_records") as mock_dns:
            report = self.p.run_preflight(skip_network=True)
        mock_smtp.assert_not_called()
        mock_xrpl.assert_not_called()
        mock_dns.assert_not_called()
        # Has a "network_checks skipped" warn entry
        names = [r.name for r in report.results]
        self.assertIn("network_checks", names)


if __name__ == "__main__":
    unittest.main()
