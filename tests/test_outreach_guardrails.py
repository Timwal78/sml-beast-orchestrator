"""Tests for sml_beast/outreach/guardrails.py — the BB7 foundation.

Covers per architect brief: kill switch throws, ceiling checks block,
TLD blocklist, custom blocklist file, autonomy cap, date rollover,
pitch caps (warmup + steady), ledger atomicity under concurrent writes,
corruption recovery."""

import importlib
import json
import os
import shutil
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta


class _GuardBase(unittest.TestCase):
    """Each test gets a fresh tempdir + a freshly-reloaded guardrails module
    (so _ledger_lock starts clean and path helpers see the test's
    BEAST_OUTPUT_ROOT)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="beast-guard-")
        os.environ["BEAST_OUTPUT_ROOT"] = self.tmp
        import sml_beast.outreach.guardrails as g

        importlib.reload(g)
        self.g = g

    def tearDown(self):
        os.environ.pop("BEAST_OUTPUT_ROOT", None)
        shutil.rmtree(self.tmp, ignore_errors=True)


class KillSwitchTests(_GuardBase):
    def test_no_kill_switch_passes(self):
        # No file -> no raise
        self.g.OutreachGuardrails.enforce_kill_switch()

    def test_kill_switch_file_raises(self):
        ks = self.g.kill_switch_path()
        ks.parent.mkdir(parents=True, exist_ok=True)
        ks.write_text("HALT")
        with self.assertRaises(self.g.SystemHaltedException):
            self.g.OutreachGuardrails.enforce_kill_switch()

    def test_kill_switch_blocks_authorize_transaction(self):
        ks = self.g.kill_switch_path()
        ks.parent.mkdir(parents=True, exist_ok=True)
        ks.write_text("STOP")
        with self.assertRaises(self.g.SystemHaltedException):
            self.g.OutreachGuardrails.authorize_transaction(5.00)

    def test_kill_switch_blocks_authorize_pitch_dispatch(self):
        ks = self.g.kill_switch_path()
        ks.parent.mkdir(parents=True, exist_ok=True)
        ks.write_text("STOP")
        with self.assertRaises(self.g.SystemHaltedException):
            self.g.OutreachGuardrails.authorize_pitch_dispatch()

    def test_empty_kill_switch_file_still_halts(self):
        # "If file exists" — content irrelevant
        ks = self.g.kill_switch_path()
        ks.parent.mkdir(parents=True, exist_ok=True)
        ks.write_text("")
        with self.assertRaises(self.g.SystemHaltedException):
            self.g.OutreachGuardrails.enforce_kill_switch()


class DomainValidationTests(_GuardBase):
    def test_gov_domain_rejected(self):
        self.assertFalse(self.g.OutreachGuardrails.validate_target_domain("whitehouse.gov"))
        self.assertFalse(self.g.OutreachGuardrails.validate_target_domain("example.GOV"))
        self.assertFalse(self.g.OutreachGuardrails.validate_target_domain("sub.agency.gov"))

    def test_mil_domain_rejected(self):
        self.assertFalse(self.g.OutreachGuardrails.validate_target_domain("army.mil"))
        self.assertFalse(self.g.OutreachGuardrails.validate_target_domain("base.example.mil"))

    def test_edu_domain_rejected(self):
        self.assertFalse(self.g.OutreachGuardrails.validate_target_domain("mit.edu"))
        self.assertFalse(self.g.OutreachGuardrails.validate_target_domain("sub.example.edu"))

    def test_empty_input_rejected(self):
        self.assertFalse(self.g.OutreachGuardrails.validate_target_domain(""))
        self.assertFalse(self.g.OutreachGuardrails.validate_target_domain("   "))
        self.assertFalse(self.g.OutreachGuardrails.validate_target_domain(None))

    def test_commercial_domain_passes(self):
        self.assertTrue(self.g.OutreachGuardrails.validate_target_domain("capterra.com"))
        self.assertTrue(self.g.OutreachGuardrails.validate_target_domain("blog.example.io"))
        self.assertTrue(self.g.OutreachGuardrails.validate_target_domain("dev.to"))

    def test_custom_blocklist_file_rejects(self):
        bp = self.g.blocklist_path()
        bp.parent.mkdir(parents=True, exist_ok=True)
        bp.write_text("# operator notes\nbadactor.com\nspamtrap.io\n")
        self.assertFalse(self.g.OutreachGuardrails.validate_target_domain("badactor.com"))
        self.assertFalse(self.g.OutreachGuardrails.validate_target_domain("BADACTOR.COM"))
        self.assertFalse(self.g.OutreachGuardrails.validate_target_domain("spamtrap.io"))
        self.assertTrue(self.g.OutreachGuardrails.validate_target_domain("capterra.com"))

    def test_custom_blocklist_ignores_comments_and_blanks(self):
        bp = self.g.blocklist_path()
        bp.parent.mkdir(parents=True, exist_ok=True)
        bp.write_text("# header\n\n# another comment\nbad.com\n\n")
        self.assertFalse(self.g.OutreachGuardrails.validate_target_domain("bad.com"))


class FeeCapTests(_GuardBase):
    def test_zero_fee_rejected(self):
        self.assertFalse(self.g.OutreachGuardrails.authorize_transaction(0.0))

    def test_negative_fee_rejected(self):
        self.assertFalse(self.g.OutreachGuardrails.authorize_transaction(-1.0))

    def test_none_fee_rejected(self):
        self.assertFalse(self.g.OutreachGuardrails.authorize_transaction(None))

    def test_standard_fee_passes(self):
        self.assertTrue(
            self.g.OutreachGuardrails.authorize_transaction(self.g.OUTREACH_STANDARD_FEE_USDC)
        )

    def test_fee_at_autonomy_cap_passes(self):
        self.assertTrue(
            self.g.OutreachGuardrails.authorize_transaction(self.g.OUTREACH_MAX_AUTONOMY_FEE_USDC)
        )

    def test_fee_above_autonomy_cap_rejected(self):
        self.assertFalse(
            self.g.OutreachGuardrails.authorize_transaction(
                self.g.OUTREACH_MAX_AUTONOMY_FEE_USDC + 0.01
            )
        )

    def test_fee_at_strategic_tier_rejected_from_agent_lane(self):
        # 25.00 USDC = strategic top-tier, operator-driven, NOT agent autonomy
        self.assertFalse(self.g.OutreachGuardrails.authorize_transaction(25.00))


class DailyCeilingTests(_GuardBase):
    def test_first_pitch_under_ceiling_authorized(self):
        self.assertTrue(self.g.OutreachGuardrails.authorize_transaction(5.00))

    def test_accumulated_spend_blocks_at_ceiling(self):
        # 5.00 four times = 20.00 (== ceiling). A fifth 5.00 would breach.
        for _ in range(4):
            self.assertTrue(self.g.OutreachGuardrails.authorize_transaction(5.00))
            self.g.OutreachGuardrails.commit_transaction(5.00)
        self.assertFalse(self.g.OutreachGuardrails.authorize_transaction(5.00))
        # Ledger should reflect exactly the ceiling
        with open(self.g.ledger_path()) as f:
            ledger = json.load(f)
        self.assertEqual(ledger["spent_usdc"], 20.00)
        self.assertEqual(ledger["pitches_sent"], 4)

    def test_partial_remaining_headroom_respected(self):
        # 18.00 committed; only 2.00 left in the day
        self.g.OutreachGuardrails.commit_transaction(18.00)
        self.assertTrue(self.g.OutreachGuardrails.authorize_transaction(2.00))
        self.assertFalse(self.g.OutreachGuardrails.authorize_transaction(2.01))

    def test_date_rollover_resets_ledger(self):
        # Pre-stage a fully-spent ledger dated yesterday
        yest = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
        sd = self.g.state_dir()
        sd.mkdir(parents=True, exist_ok=True)
        with open(self.g.ledger_path(), "w") as f:
            json.dump({"date": yest, "spent_usdc": 20.00, "pitches_sent": 10}, f)
        # New day reads as zero
        self.assertTrue(self.g.OutreachGuardrails.authorize_transaction(5.00))

    def test_daily_ledger_snapshot_returns_current_state(self):
        self.g.OutreachGuardrails.commit_transaction(7.50)
        snap = self.g.OutreachGuardrails.daily_ledger_snapshot()
        self.assertEqual(snap["spent_usdc"], 7.50)
        self.assertEqual(snap["pitches_sent"], 1)
        # Mutating snapshot must not affect ledger
        snap["spent_usdc"] = 999.99
        snap2 = self.g.OutreachGuardrails.daily_ledger_snapshot()
        self.assertEqual(snap2["spent_usdc"], 7.50)


class PitchCapTests(_GuardBase):
    def test_warmup_cap_is_three(self):
        for _ in range(self.g.OUTREACH_DAILY_PITCH_CAP_WARMUP):
            self.assertTrue(
                self.g.OutreachGuardrails.authorize_pitch_dispatch(is_warmup_period=True)
            )
            self.g.OutreachGuardrails.commit_transaction(5.00)
        # Fourth pitch in warmup blocked on pitch cap
        self.assertFalse(self.g.OutreachGuardrails.authorize_pitch_dispatch(is_warmup_period=True))

    def test_steady_cap_is_ten(self):
        # 10 pitches at 2.00 each = 20.00 (== ceiling), 10 pitches (== steady cap)
        for _ in range(10):
            self.assertTrue(
                self.g.OutreachGuardrails.authorize_pitch_dispatch(is_warmup_period=False)
            )
            self.g.OutreachGuardrails.commit_transaction(2.00)
        # 11th blocked on pitch cap
        self.assertFalse(self.g.OutreachGuardrails.authorize_pitch_dispatch(is_warmup_period=False))

    def test_warmup_cap_lower_than_steady(self):
        # Fill the 3 warmup slots
        for _ in range(3):
            self.assertTrue(
                self.g.OutreachGuardrails.authorize_pitch_dispatch(is_warmup_period=True)
            )
            self.g.OutreachGuardrails.commit_transaction(5.00)
        # 4th in warmup mode: blocked
        self.assertFalse(self.g.OutreachGuardrails.authorize_pitch_dispatch(is_warmup_period=True))
        # 4th if caller switches to steady mode: allowed (because steady cap = 10)
        # NOTE: this verifies guardrails respects the caller's mode signal. The
        # ACTUAL warmup decision lives in state.py and is enforced by the agent.
        self.assertTrue(self.g.OutreachGuardrails.authorize_pitch_dispatch(is_warmup_period=False))


class LedgerAtomicityTests(_GuardBase):
    def test_concurrent_commits_dont_corrupt_ledger(self):
        # 10 threads each commit 1.00 USDC twice. End state must be 20.00 USDC,
        # 20 pitches. Without the lock we'd see lost updates from interleaved
        # read-modify-write.
        N_THREADS = 10
        PER_THREAD = 2

        def worker():
            for _ in range(PER_THREAD):
                self.g.OutreachGuardrails.commit_transaction(1.00)

        threads = [threading.Thread(target=worker) for _ in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        with open(self.g.ledger_path()) as f:
            ledger = json.load(f)
        self.assertEqual(ledger["spent_usdc"], 20.00)
        self.assertEqual(ledger["pitches_sent"], N_THREADS * PER_THREAD)


class LedgerCorruptionTests(_GuardBase):
    def test_corrupted_ledger_treated_as_fresh(self):
        sd = self.g.state_dir()
        sd.mkdir(parents=True, exist_ok=True)
        with open(self.g.ledger_path(), "w") as f:
            f.write("{not valid json")
        # Should treat as fresh; authorize 5.00 from zero
        self.assertTrue(self.g.OutreachGuardrails.authorize_transaction(5.00))


class PathResolutionTests(_GuardBase):
    def test_paths_honor_beast_output_root(self):
        # All four path helpers must root inside the configured tempdir
        for p in (
            self.g.kill_switch_path(),
            self.g.state_dir(),
            self.g.ledger_path(),
            self.g.blocklist_path(),
        ):
            self.assertTrue(
                str(p).startswith(self.tmp), f"{p} did not honor BEAST_OUTPUT_ROOT={self.tmp}"
            )


if __name__ == "__main__":
    unittest.main()
