"""Tests for sml_beast/outreach/opctl.py — operator CLI.

Exercises each subcommand. No network, no XRPL, no SMTP — every test
runs against a fresh tempdir output root with reloaded modules.

Covers:
  - status: prints JSON with expected keys
  - opt-out: marks domain OPTED_OUT; bad domain rejected
  - review-clear: increments per-vertical counter; bad vertical rejected
  - review-clear-all: brings counter to threshold idempotently
  - kill on/off: creates / removes the kill switch file
  - metrics / metrics-stats: print metrics from conversion log
  - domain: prints lifecycle entry; unknown domain returns 0 with info
  - recent: prints last N events
  - dry-run: runs cycle with no XRPL / no SMTP (mocks enricher)
"""

import importlib
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout


class _OpctlBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="beast-opctl-")
        os.environ["BEAST_OUTPUT_ROOT"] = self.tmp
        # Pre-set env so dry-run wiring doesn't trip on missing vars
        for k, v in {
            "BB7_XRPL_WALLET_SEED": "sEdSKaCy2JT7JaM7v95H9SxkhP9wS2r",
            "BB7_SMTP_HOST": "smtp.example.com",
            "BB7_SMTP_USER": "ci@example.com",
            "BB7_SMTP_PASS": "x",
            "BB7_OPT_OUT_URL": "https://sml.com/opt",
            "BB7_OPERATOR_ADDRESS": "addr",
            "BB7_OPERATOR_SIGNATURE": "Tim",
        }.items():
            os.environ[k] = v

        # Reload modules so env-derived paths point at the tmpdir
        import sml_beast.outreach.guardrails as g
        import sml_beast.outreach.opctl as o
        import sml_beast.outreach.state as s

        importlib.reload(g)
        importlib.reload(s)
        importlib.reload(o)
        self.o = o
        self.s = s
        self.g = g

    def tearDown(self):
        os.environ.pop("BEAST_OUTPUT_ROOT", None)
        for k in (
            "BB7_XRPL_WALLET_SEED", "BB7_SMTP_HOST", "BB7_SMTP_USER",
            "BB7_SMTP_PASS", "BB7_OPT_OUT_URL", "BB7_OPERATOR_ADDRESS",
            "BB7_OPERATOR_SIGNATURE", "BB7_OUTREACH_DRY_RUN",
        ):
            os.environ.pop(k, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *argv) -> tuple[int, str]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = self.o.main(list(argv))
        return code, buf.getvalue()


class StatusTests(_OpctlBase):
    def test_status_returns_zero(self):
        code, _out = self._run("status")
        self.assertEqual(code, 0)

    def test_status_emits_json_with_expected_keys(self):
        _, out = self._run("status")
        data = json.loads(out)
        for k in (
            "kill_switch_active",
            "warmup_mode",
            "domain_counts_by_state",
            "manual_review_completed",
            "ledger",
            "conversion",
        ):
            self.assertIn(k, data)


class OptOutTests(_OpctlBase):
    def test_opt_out_marks_domain(self):
        code, _out = self._run("opt-out", "spam.com")
        self.assertEqual(code, 0)
        # Verify via state machine
        sm = self.s.OutreachStateMachine()
        entry = sm.get_domain("spam.com")
        self.assertEqual(entry["state"], self.s.STATE_OPTED_OUT)

    def test_opt_out_rejects_invalid_domain(self):
        code, _ = self._run("opt-out", "notadomain")
        self.assertEqual(code, 1)

    def test_opt_out_rejects_empty(self):
        code, _ = self._run("opt-out", "   ")
        self.assertEqual(code, 1)

    def test_opt_out_is_case_insensitive(self):
        self._run("opt-out", "MIXED.COM")
        sm = self.s.OutreachStateMachine()
        self.assertIsNotNone(sm.get_domain("mixed.com"))


class ReviewClearTests(_OpctlBase):
    def test_clear_one_increments_counter(self):
        code, _ = self._run("review-clear", "mastersheets")
        self.assertEqual(code, 0)
        sm = self.s.OutreachStateMachine()
        snap = sm.snapshot()
        self.assertEqual(snap["manual_review_count_by_vertical"]["mastersheets"], 1)

    def test_clear_unknown_vertical_rejected(self):
        code, _ = self._run("review-clear", "bogus")
        self.assertEqual(code, 1)

    def test_clear_all_brings_counter_to_threshold(self):
        code, _ = self._run("review-clear-all", "mastersheets")
        self.assertEqual(code, 0)
        sm = self.s.OutreachStateMachine()
        snap = sm.snapshot()
        self.assertGreaterEqual(
            snap["manual_review_count_by_vertical"]["mastersheets"],
            self.s.OUTREACH_MANUAL_REVIEW_N,
        )

    def test_clear_all_idempotent(self):
        # Run twice — second should not bump past threshold
        self._run("review-clear-all", "mastersheets")
        self._run("review-clear-all", "mastersheets")
        sm = self.s.OutreachStateMachine()
        snap = sm.snapshot()
        self.assertEqual(
            snap["manual_review_count_by_vertical"]["mastersheets"],
            self.s.OUTREACH_MANUAL_REVIEW_N,
        )


class KillSwitchTests(_OpctlBase):
    def test_kill_on_creates_file(self):
        code, _ = self._run("kill", "on")
        self.assertEqual(code, 0)
        self.assertTrue(self.g.kill_switch_path().exists())

    def test_kill_off_removes_file(self):
        self._run("kill", "on")
        code, _ = self._run("kill", "off")
        self.assertEqual(code, 0)
        self.assertFalse(self.g.kill_switch_path().exists())

    def test_kill_off_when_already_off_ok(self):
        code, _ = self._run("kill", "off")
        self.assertEqual(code, 0)

    def test_kill_on_when_already_on_ok(self):
        self._run("kill", "on")
        code, _ = self._run("kill", "on")
        self.assertEqual(code, 0)


class MetricsTests(_OpctlBase):
    def test_metrics_empty_returns_empty_list(self):
        code, out = self._run("metrics")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), [])

    def test_metrics_stats_zero_when_empty(self):
        code, out = self._run("metrics-stats")
        self.assertEqual(code, 0)
        stats = json.loads(out)
        self.assertEqual(stats["total_checks"], 0)
        self.assertEqual(stats["conversion_rate"], 0.0)


class DomainTests(_OpctlBase):
    def test_unknown_domain_returns_zero_with_info(self):
        code, _ = self._run("domain", "never-pitched.com")
        self.assertEqual(code, 0)

    def test_known_domain_prints_json(self):
        self._run("opt-out", "known.com")
        code, out = self._run("domain", "known.com")
        self.assertEqual(code, 0)
        # The output mixes info-line prefix with JSON; the JSON object
        # must appear in the output and contain the state field
        self.assertIn("OPTED_OUT", out)


class RecentTests(_OpctlBase):
    def test_recent_empty_returns_empty_list(self):
        code, out = self._run("recent")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), [])

    def test_recent_returns_optouts(self):
        self._run("opt-out", "a.com")
        self._run("opt-out", "b.com")
        code, out = self._run("recent")
        self.assertEqual(code, 0)
        events = json.loads(out)
        domains = {e["domain"] for e in events}
        self.assertEqual(domains, {"a.com", "b.com"})

    def test_recent_respects_limit(self):
        for i in range(5):
            self._run("opt-out", f"d{i}.com")
        _code, out = self._run("recent", "2")
        events = json.loads(out)
        self.assertEqual(len(events), 2)


class DryRunTests(_OpctlBase):
    def test_dry_run_no_targets_returns_zero(self):
        code, out = self._run("dry-run")
        self.assertEqual(code, 0)
        summary = json.loads(out)
        self.assertTrue(summary.get("dry_run"))


class ArgparseTests(unittest.TestCase):
    def test_unknown_command_exits_nonzero(self):
        import sml_beast.outreach.opctl as o

        with self.assertRaises(SystemExit) as ctx:
            o.main(["nonsense-command"])
        self.assertNotEqual(ctx.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
