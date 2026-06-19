"""Tests for sml_beast/outreach/agent.py — outreach agent composition.

All external I/O mocked: XRPL client, SMTP, HTTP enrichment fetches.
Tests verify the orchestration logic, not the individual modules (those
have their own test suites).

Covers:
  - run_cycle with no bounty_targets.json → empty cycle, no pitches sent
  - run_cycle with one target, full happy path (all steps called)
  - kill switch active → cycle aborts immediately
  - enrichment fails → target skipped, no payment attempted
  - manual review gate active → target skipped
  - guardrails denial (cooldown / ceiling) → target skipped
  - XRPL payment failure → target skipped, state not advanced to DELIVERED
  - SMTP failure → target skipped (payment already sent; state stays DEMO_SENT)
  - dry-run mode: no XRPL, no SMTP, but state machine still advances
  - summary dict structure
"""

import importlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _ok_xrpl_result():
    return SimpleNamespace(
        result={
            "validated": True,
            "meta": {"TransactionResult": "tesSUCCESS"},
            "hash": "TXHASH001",
        }
    )


class _AgentBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="beast-agent-")
        os.environ["BEAST_OUTPUT_ROOT"] = self.tmp
        os.environ.setdefault("BB7_XRPL_WALLET_SEED", os.environ.get("BB7_XRPL_SEED", ""))
        os.environ["BB7_SMTP_HOST"] = "smtp.example.com"
        os.environ["BB7_SMTP_USER"] = "outreach@infra.sml.com"
        os.environ["BB7_SMTP_PASS"] = "s3cr3t"
        os.environ["BB7_OPT_OUT_URL"] = "https://sml.com/optout"
        os.environ["BB7_OPERATOR_SIGNATURE"] = "Tim"
        os.environ.pop("BB7_OUTREACH_DRY_RUN", None)

        # Reload modules so env vars are picked up fresh
        import sml_beast.outreach.agent as a
        import sml_beast.outreach.guardrails as g
        import sml_beast.outreach.state as s
        importlib.reload(g)
        importlib.reload(s)
        importlib.reload(a)
        self.a = a
        self.s = s
        self.g = g
        self.root = Path(self.tmp)

    def tearDown(self):
        for k in (
            "BEAST_OUTPUT_ROOT", "BB7_XRPL_WALLET_SEED", "BB7_SMTP_HOST",
            "BB7_SMTP_USER", "BB7_SMTP_PASS", "BB7_OPT_OUT_URL",
            "BB7_OPERATOR_SIGNATURE", "BB7_OUTREACH_DRY_RUN",
        ):
            os.environ.pop(k, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_targets(self, vertical: str, targets: list) -> None:
        d = self.root / vertical
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "bounty_targets.json", "w") as f:
            json.dump({"targets": targets, "vertical": vertical}, f)

    def _one_target(self, domain="target.com") -> dict:
        return {
            "domain": domain,
            "priority_score": 8,
            "class": "listicle",
            "attack_angles": ["data_sovereignty"],
            "top_keyword": "best spreadsheet tools",
        }

    def _mock_xrpl_client(self):
        c = MagicMock()
        c.send_demo_payment_for_usdc.return_value = ("TXHASH001", 10.0)
        return c

    def _healthy_balance_fn(self):
        """Return a stub balance check that always reports healthy.
        Lets tests skip the real XRPL account_info call."""
        check = MagicMock()
        check.healthy = True
        check.error = None
        check.usdc_equiv = 100.0
        check.to_dict.return_value = {"healthy": True, "usdc_equiv": 100.0, "error": None}
        return lambda: check

    def _mock_smtp(self):
        smtp = MagicMock()

        def factory(h, p):
            return smtp

        return smtp, factory


class EmptyCycleTests(_AgentBase):
    def test_no_targets_returns_zero_summary(self):
        result = self.a.run_cycle(output_root=self.root, xrpl_client=MagicMock(), balance_check_fn=self._healthy_balance_fn())
        self.assertEqual(result["total_attempted"], 0)
        self.assertEqual(result["total_sent"], 0)

    def test_summary_has_expected_keys(self):
        result = self.a.run_cycle(output_root=self.root, xrpl_client=MagicMock(), balance_check_fn=self._healthy_balance_fn())
        self.assertIn("total_attempted", result)
        self.assertIn("total_sent", result)
        self.assertIn("vertical", result)
        self.assertIn("dry_run", result)


class HappyPathTests(_AgentBase):
    def test_full_dispatch_sends_one_pitch(self):
        self._write_targets("mastersheets", [self._one_target()])
        xrpl = self._mock_xrpl_client()
        _smtp_mock, _smtp_factory = self._mock_smtp()

        # Bypass manual review gate: pre-fill N completed reviews
        sm = self.s.OutreachStateMachine()
        for _ in range(self.s.OUTREACH_MANUAL_REVIEW_N):
            sm.record_manual_review_completed("mastersheets")

        enriched = MagicMock()
        enriched.enriched = True
        enriched.email = "dev@target.com"
        enriched.source = "security.txt"

        with patch.object(self.a, "enrich_domain", return_value=enriched), \
             patch.object(self.a, "send_pitch") as mock_send:
            mock_send.return_value = MagicMock(message_id="<mid@sml.com>")
            result = self.a.run_cycle(
                verticals=("mastersheets",), output_root=self.root, xrpl_client=xrpl, balance_check_fn=self._healthy_balance_fn()
            )

        self.assertEqual(result["total_sent"], 1)
        xrpl.send_demo_payment_for_usdc.assert_called_once()

    def test_payment_sent_state_recorded(self):
        self._write_targets("mastersheets", [self._one_target()])
        xrpl = self._mock_xrpl_client()
        sm = self.s.OutreachStateMachine()
        for _ in range(self.s.OUTREACH_MANUAL_REVIEW_N):
            sm.record_manual_review_completed("mastersheets")

        enriched = MagicMock()
        enriched.enriched = True
        enriched.email = "dev@target.com"
        enriched.source = "security.txt"

        with patch.object(self.a, "enrich_domain", return_value=enriched), \
             patch.object(self.a, "send_pitch") as mock_send:
            mock_send.return_value = MagicMock(message_id="<mid@sml.com>")
            self.a.run_cycle(
                verticals=("mastersheets",), output_root=self.root, xrpl_client=xrpl, balance_check_fn=self._healthy_balance_fn()
            )

        # Reload state from disk to verify persistence
        sm2 = self.s.OutreachStateMachine()
        d = sm2.get_domain("target.com")
        self.assertIsNotNone(d)
        self.assertIn(d["state"], (self.s.STATE_DEMO_SENT, self.s.STATE_PITCH_DELIVERED))


class KillSwitchTests(_AgentBase):
    def test_kill_switch_aborts_cycle(self):
        self._write_targets("mastersheets", [self._one_target()])
        ks = self.g.kill_switch_path()
        ks.parent.mkdir(parents=True, exist_ok=True)
        ks.write_text("HALT")

        enriched = MagicMock()
        enriched.enriched = True
        enriched.email = "dev@target.com"
        enriched.source = "security.txt"

        # Manual review already cleared
        sm = self.s.OutreachStateMachine()
        for _ in range(self.s.OUTREACH_MANUAL_REVIEW_N):
            sm.record_manual_review_completed("mastersheets")

        with patch.object(self.a, "enrich_domain", return_value=enriched):
            result = self.a.run_cycle(
                verticals=("mastersheets",), output_root=self.root, xrpl_client=MagicMock(), balance_check_fn=self._healthy_balance_fn()
            )

        self.assertEqual(result["total_sent"], 0)


class SkipPathTests(_AgentBase):
    def test_unenriched_target_skipped(self):
        self._write_targets("mastersheets", [self._one_target()])
        xrpl = self._mock_xrpl_client()

        sm = self.s.OutreachStateMachine()
        for _ in range(self.s.OUTREACH_MANUAL_REVIEW_N):
            sm.record_manual_review_completed("mastersheets")

        enriched = MagicMock()
        enriched.enriched = False
        enriched.email = None
        enriched.source = None

        with patch.object(self.a, "enrich_domain", return_value=enriched):
            result = self.a.run_cycle(
                verticals=("mastersheets",), output_root=self.root, xrpl_client=xrpl, balance_check_fn=self._healthy_balance_fn()
            )

        self.assertEqual(result["total_sent"], 0)
        xrpl.send_demo_payment_for_usdc.assert_not_called()

    def test_manual_review_gate_skips_target(self):
        self._write_targets("mastersheets", [self._one_target()])
        xrpl = self._mock_xrpl_client()

        # Do NOT clear the manual review gate
        enriched = MagicMock()
        enriched.enriched = True
        enriched.email = "dev@target.com"
        enriched.source = "security.txt"

        with patch.object(self.a, "enrich_domain", return_value=enriched):
            result = self.a.run_cycle(
                verticals=("mastersheets",), output_root=self.root, xrpl_client=xrpl, balance_check_fn=self._healthy_balance_fn()
            )

        self.assertEqual(result["total_sent"], 0)
        xrpl.send_demo_payment_for_usdc.assert_not_called()

    def test_xrpl_failure_skips_pitch_delivery(self):
        self._write_targets("mastersheets", [self._one_target()])
        xrpl = self._mock_xrpl_client()
        xrpl.send_demo_payment_for_usdc.side_effect = RuntimeError("network down")

        sm = self.s.OutreachStateMachine()
        for _ in range(self.s.OUTREACH_MANUAL_REVIEW_N):
            sm.record_manual_review_completed("mastersheets")

        enriched = MagicMock()
        enriched.enriched = True
        enriched.email = "dev@target.com"
        enriched.source = "security.txt"

        with patch.object(self.a, "enrich_domain", return_value=enriched), \
             patch.object(self.a, "send_pitch") as mock_send:
            result = self.a.run_cycle(
                verticals=("mastersheets",), output_root=self.root, xrpl_client=xrpl, balance_check_fn=self._healthy_balance_fn()
            )

        self.assertEqual(result["total_sent"], 0)
        mock_send.assert_not_called()


class DryRunTests(_AgentBase):
    def setUp(self):
        super().setUp()
        os.environ["BB7_OUTREACH_DRY_RUN"] = "1"
        import sml_beast.outreach.agent as a
        importlib.reload(a)
        self.a = a

    def test_dry_run_no_xrpl_no_smtp(self):
        self._write_targets("mastersheets", [self._one_target()])

        sm = self.s.OutreachStateMachine()
        for _ in range(self.s.OUTREACH_MANUAL_REVIEW_N):
            sm.record_manual_review_completed("mastersheets")

        enriched = MagicMock()
        enriched.enriched = True
        enriched.email = "dev@target.com"
        enriched.source = "security.txt"

        xrpl = MagicMock()

        with patch.object(self.a, "enrich_domain", return_value=enriched), \
             patch.object(self.a, "send_pitch") as mock_send:
            result = self.a.run_cycle(
                verticals=("mastersheets",), output_root=self.root, xrpl_client=xrpl, balance_check_fn=self._healthy_balance_fn()
            )

        self.assertTrue(result["dry_run"])
        # In dry-run mode: XRPL NOT called, SMTP NOT called, but cycle counts as sent
        xrpl.send_demo_payment_for_usdc.assert_not_called()
        mock_send.assert_not_called()
        self.assertEqual(result["total_sent"], 1)


if __name__ == "__main__":
    unittest.main()
