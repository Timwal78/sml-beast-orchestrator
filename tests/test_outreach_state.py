"""Tests for sml_beast/outreach/state.py — BB7 state machine.

Covers:
  - Restart-safe persistence (write -> restart -> read; tmp+rename atomic)
  - Corruption recovery (malformed JSON -> fresh state)
  - Warmup auto-detection (no pitches -> warmup; >30d -> not warmup)
  - Manual review gate (first N per vertical)
  - Per-domain 14-day cooldown
  - Opt-out permanence
  - atomic_reserve_pitch happy path + every denial path
  - atomic_reserve_pitch closes the guardrails TOCTOU under concurrent load
  - State transitions (DEMO_SENT, PITCH_DELIVERED, LINK_OBSERVED, OPTED_OUT)
"""

import importlib
import json
import os
import shutil
import tempfile
import threading
import unittest


class _StateBase(unittest.TestCase):
    """Fresh tempdir + reloaded modules per test. Reload ensures the
    module-level _ledger_lock in guardrails starts clean each time."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="beast-state-")
        os.environ["BEAST_OUTPUT_ROOT"] = self.tmp
        import sml_beast.outreach.guardrails as g
        import sml_beast.outreach.state as s

        importlib.reload(g)
        importlib.reload(s)
        self.g = g
        self.s = s

    def tearDown(self):
        os.environ.pop("BEAST_OUTPUT_ROOT", None)
        shutil.rmtree(self.tmp, ignore_errors=True)


class PersistenceTests(_StateBase):
    def test_fresh_state_when_no_file_exists(self):
        sm = self.s.OutreachStateMachine()
        snap = sm.snapshot()
        self.assertEqual(snap["first_pitch_ts_utc"], 0)
        self.assertEqual(snap["domains"], {})
        self.assertEqual(snap["manual_review_count_by_vertical"], {})

    def test_save_then_load_round_trip(self):
        sm = self.s.OutreachStateMachine()
        sm.mark_opted_out("example.com")
        # Build a second instance — should load persisted state from disk
        sm2 = self.s.OutreachStateMachine()
        snap = sm2.snapshot()
        self.assertIn("example.com", snap["domains"])
        self.assertEqual(snap["domains"]["example.com"]["state"], self.s.STATE_OPTED_OUT)

    def test_atomic_write_uses_tmp_and_rename(self):
        # Verify the .tmp file does not persist after a successful save
        sm = self.s.OutreachStateMachine()
        sm.mark_opted_out("foo.com")
        sf = self.s.state_file_path()
        self.assertTrue(sf.exists())
        tmp_path = sf.with_suffix(".tmp")
        self.assertFalse(tmp_path.exists(), "temp file must be renamed away, not left behind")

    def test_corrupted_file_recovers_to_fresh(self):
        # Pre-stage a bad file
        sf = self.s.state_file_path()
        sf.parent.mkdir(parents=True, exist_ok=True)
        sf.write_text("{not valid json")
        sm = self.s.OutreachStateMachine()
        snap = sm.snapshot()
        self.assertEqual(snap["first_pitch_ts_utc"], 0)
        self.assertEqual(snap["domains"], {})

    def test_state_file_under_internal_dir(self):
        # Per BB7_DESIGN.md §9.6 the path must live under _internal/
        sf = self.s.state_file_path()
        self.assertIn("_internal", str(sf))


class WarmupDetectionTests(_StateBase):
    def test_no_pitches_yet_means_warmup(self):
        sm = self.s.OutreachStateMachine()
        self.assertTrue(sm.is_in_warmup_period())
        self.assertIsNone(sm.days_since_first_pitch())

    def test_recent_first_pitch_means_warmup(self):
        sm = self.s.OutreachStateMachine()
        sm._state["first_pitch_ts_utc"] = sm._now() - (10 * 86400)  # 10d ago
        self.assertTrue(sm.is_in_warmup_period())

    def test_old_first_pitch_means_not_warmup(self):
        sm = self.s.OutreachStateMachine()
        sm._state["first_pitch_ts_utc"] = sm._now() - (40 * 86400)  # 40d ago
        self.assertFalse(sm.is_in_warmup_period())

    def test_days_since_first_pitch_computes(self):
        sm = self.s.OutreachStateMachine()
        sm._state["first_pitch_ts_utc"] = sm._now() - (5 * 86400)
        days = sm.days_since_first_pitch()
        self.assertAlmostEqual(days, 5.0, places=1)


class ManualReviewGateTests(_StateBase):
    def test_initial_state_requires_review_for_all_verticals(self):
        sm = self.s.OutreachStateMachine()
        self.assertTrue(sm.needs_manual_review("mastersheets"))
        self.assertTrue(sm.needs_manual_review("xrpl_x402"))

    def test_completing_5_clears_gate_for_that_vertical(self):
        sm = self.s.OutreachStateMachine()
        for _ in range(self.s.OUTREACH_MANUAL_REVIEW_N):
            self.assertTrue(sm.needs_manual_review("mastersheets"))
            sm.record_manual_review_completed("mastersheets")
        # Sixth pitch in mastersheets no longer needs review
        self.assertFalse(sm.needs_manual_review("mastersheets"))
        # But xrpl_x402 still does — counter is per-vertical
        self.assertTrue(sm.needs_manual_review("xrpl_x402"))

    def test_review_counter_persists_across_restart(self):
        # Counter at 3 after first session
        sm = self.s.OutreachStateMachine()
        for _ in range(3):
            sm.record_manual_review_completed("mastersheets")
        # Restart — fresh instance reads same file
        sm2 = self.s.OutreachStateMachine()
        self.assertTrue(sm2.needs_manual_review("mastersheets"))  # 3 < 5
        # One more = 4, still needs
        sm2.record_manual_review_completed("mastersheets")
        self.assertTrue(sm2.needs_manual_review("mastersheets"))  # 4 < 5
        # One more = 5, gate clears
        sm2.record_manual_review_completed("mastersheets")
        self.assertFalse(sm2.needs_manual_review("mastersheets"))  # 5 == 5


class DomainCooldownTests(_StateBase):
    def test_never_pitched_domain_satisfies_cooldown(self):
        sm = self.s.OutreachStateMachine()
        self.assertTrue(sm.domain_cooldown_satisfied("example.com"))

    def test_recently_pitched_domain_in_cooldown(self):
        sm = self.s.OutreachStateMachine()
        sm.atomic_reserve_pitch("example.com", "mastersheets", 5.00)
        self.assertFalse(sm.domain_cooldown_satisfied("example.com"))

    def test_old_pitch_satisfies_cooldown(self):
        sm = self.s.OutreachStateMachine()
        sm.atomic_reserve_pitch("example.com", "mastersheets", 5.00)
        # Backdate the last_pitch_ts to 15 days ago
        sm._state["domains"]["example.com"]["last_pitch_ts_utc"] = sm._now() - (15 * 86400)
        self.assertTrue(sm.domain_cooldown_satisfied("example.com"))

    def test_opted_out_never_satisfies_cooldown(self):
        sm = self.s.OutreachStateMachine()
        sm.mark_opted_out("badactor.com")
        # Even 1000 days from now opt-out should hold
        sm._state["domains"]["badactor.com"]["last_state_change_ts_utc"] = sm._now() - (
            1000 * 86400
        )
        self.assertFalse(sm.domain_cooldown_satisfied("badactor.com"))

    def test_cooldown_check_is_case_insensitive(self):
        sm = self.s.OutreachStateMachine()
        sm.atomic_reserve_pitch("example.com", "mastersheets", 5.00)
        self.assertFalse(sm.domain_cooldown_satisfied("EXAMPLE.COM"))
        self.assertFalse(sm.domain_cooldown_satisfied("  example.com  "))


class AtomicReservePitchTests(_StateBase):
    def test_happy_path_reserves_successfully(self):
        sm = self.s.OutreachStateMachine()
        ok = sm.atomic_reserve_pitch("example.com", "mastersheets", 5.00)
        self.assertTrue(ok)
        # State updated to PROPOSED
        domain = sm.get_domain("example.com")
        self.assertEqual(domain["state"], self.s.STATE_PROPOSED)
        self.assertEqual(domain["vertical"], "mastersheets")
        self.assertEqual(domain["pitch_count"], 1)
        # First pitch ts seeded
        self.assertGreater(sm.snapshot()["first_pitch_ts_utc"], 0)

    def test_denied_when_domain_is_blocklisted_tld(self):
        sm = self.s.OutreachStateMachine()
        for bad in ("agency.gov", "example.mil", "school.edu"):
            self.assertFalse(sm.atomic_reserve_pitch(bad, "mastersheets", 5.00))

    def test_denied_when_domain_in_cooldown(self):
        sm = self.s.OutreachStateMachine()
        self.assertTrue(sm.atomic_reserve_pitch("example.com", "mastersheets", 5.00))
        # Second immediate pitch fails on cooldown
        self.assertFalse(sm.atomic_reserve_pitch("example.com", "mastersheets", 5.00))

    def test_denied_when_fee_exceeds_autonomy_cap(self):
        sm = self.s.OutreachStateMachine()
        # Autonomy cap is 10.00 per guardrails; 25.00 is the strategic tier
        self.assertFalse(sm.atomic_reserve_pitch("example.com", "mastersheets", 25.00))

    def test_denied_when_daily_ceiling_hit(self):
        sm = self.s.OutreachStateMachine()
        # Backdate first_pitch_ts so we're out of warmup. Then the pitch
        # cap is 10/day, not 3/day, and the binding constraint becomes
        # the daily ceiling (20.00 USDC).
        sm._state["first_pitch_ts_utc"] = sm._now() - (40 * 86400)
        # 4 pitches at 5.00 each = 20.00 (== ceiling). 5th fails on ceiling.
        for i in range(4):
            self.assertTrue(sm.atomic_reserve_pitch(f"d{i}.com", "mastersheets", 5.00))
        self.assertFalse(sm.atomic_reserve_pitch("over.com", "mastersheets", 5.00))

    def test_denied_when_pitch_cap_warmup_hit(self):
        sm = self.s.OutreachStateMachine()
        # Warmup cap is 3 — pitch 3 different domains, 4th fails
        for i in range(3):
            self.assertTrue(sm.atomic_reserve_pitch(f"d{i}.com", "mastersheets", 5.00))
        self.assertFalse(sm.atomic_reserve_pitch("d4.com", "mastersheets", 5.00))

    def test_denied_when_opted_out(self):
        sm = self.s.OutreachStateMachine()
        sm.mark_opted_out("done.com")
        self.assertFalse(sm.atomic_reserve_pitch("done.com", "mastersheets", 5.00))

    def test_kill_switch_raises_during_reserve(self):
        sm = self.s.OutreachStateMachine()
        ks = self.g.kill_switch_path()
        ks.parent.mkdir(parents=True, exist_ok=True)
        ks.write_text("HALT")
        with self.assertRaises(self.g.SystemHaltedException):
            sm.atomic_reserve_pitch("example.com", "mastersheets", 5.00)


class ConcurrentReserveTests(_StateBase):
    """The whole point of atomic_reserve_pitch — closes the TOCTOU gap
    documented in guardrails.py. Multiple threads reserving the same
    headroom must serialize cleanly without breaching the ceiling."""

    def test_concurrent_reserves_respect_daily_ceiling(self):
        sm = self.s.OutreachStateMachine()
        # Ceiling is 20.00 USDC. 8 threads each try to reserve 5.00 on
        # distinct domains. Only 4 should succeed (4 * 5.00 = 20.00).
        # Without serialization, more would succeed and the ceiling breaches.
        N = 8
        results = [None] * N

        def worker(i):
            results[i] = sm.atomic_reserve_pitch(f"d{i}.com", "mastersheets", 5.00)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        successes = sum(1 for r in results if r)
        # Either 3 (pitch cap in warmup) or 4 (ceiling); both honor caps
        self.assertLessEqual(successes, 3, "warmup pitch cap (3) must hold under concurrent load")

    def test_concurrent_reserves_dont_corrupt_state_file(self):
        sm = self.s.OutreachStateMachine()
        N = 8

        def worker(i):
            sm.atomic_reserve_pitch(f"d{i}.com", "mastersheets", 5.00)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # State file must be parseable
        with open(self.s.state_file_path()) as f:
            snap = json.load(f)
        self.assertIsInstance(snap["domains"], dict)


class StateTransitionTests(_StateBase):
    def test_record_payment_sent_updates_state_and_tx_hash(self):
        sm = self.s.OutreachStateMachine()
        sm.atomic_reserve_pitch("example.com", "mastersheets", 5.00)
        sm.record_payment_sent("example.com", "ABCDEF123")
        d = sm.get_domain("example.com")
        self.assertEqual(d["state"], self.s.STATE_DEMO_SENT)
        self.assertEqual(d["tx_hash"], "ABCDEF123")

    def test_record_payment_sent_unknown_domain_no_op(self):
        sm = self.s.OutreachStateMachine()
        sm.record_payment_sent("never.pitched.com", "ABC")
        # Domain was never reserved — must not be created by this method
        self.assertIsNone(sm.get_domain("never.pitched.com"))

    def test_record_pitch_delivered_transitions_correctly(self):
        sm = self.s.OutreachStateMachine()
        sm.atomic_reserve_pitch("example.com", "mastersheets", 5.00)
        sm.record_payment_sent("example.com", "TX1")
        sm.record_pitch_delivered("example.com")
        d = sm.get_domain("example.com")
        self.assertEqual(d["state"], self.s.STATE_PITCH_DELIVERED)

    def test_record_link_observed_stores_observation(self):
        sm = self.s.OutreachStateMachine()
        sm.atomic_reserve_pitch("example.com", "mastersheets", 5.00)
        sm.record_payment_sent("example.com", "TX1")
        sm.record_pitch_delivered("example.com")
        obs = {"href": "https://example.com/post", "rel": "", "anchor": "rails"}
        sm.record_link_observed("example.com", obs)
        d = sm.get_domain("example.com")
        self.assertEqual(d["state"], self.s.STATE_LINK_OBSERVED)
        self.assertEqual(d["observation"], obs)

    def test_mark_opted_out_creates_entry_if_missing(self):
        sm = self.s.OutreachStateMachine()
        # No prior pitch — opt-out still works (CAN-SPAM compliance)
        sm.mark_opted_out("preemptive.com")
        d = sm.get_domain("preemptive.com")
        self.assertEqual(d["state"], self.s.STATE_OPTED_OUT)


class ObservabilityTests(_StateBase):
    def test_snapshot_is_a_deep_copy(self):
        sm = self.s.OutreachStateMachine()
        sm.mark_opted_out("foo.com")
        snap = sm.snapshot()
        snap["domains"]["foo.com"]["state"] = "TAMPERED"
        snap2 = sm.snapshot()
        self.assertNotEqual(snap2["domains"]["foo.com"]["state"], "TAMPERED")

    def test_domain_count_by_state_aggregates_correctly(self):
        sm = self.s.OutreachStateMachine()
        sm.atomic_reserve_pitch("a.com", "mastersheets", 5.00)
        sm.atomic_reserve_pitch("b.com", "mastersheets", 5.00)
        sm.record_payment_sent("a.com", "TX1")
        sm.mark_opted_out("c.com")
        counts = sm.domain_count_by_state()
        self.assertEqual(counts.get(self.s.STATE_PROPOSED, 0), 1)  # b.com
        self.assertEqual(counts.get(self.s.STATE_DEMO_SENT, 0), 1)  # a.com
        self.assertEqual(counts.get(self.s.STATE_OPTED_OUT, 0), 1)  # c.com


if __name__ == "__main__":
    unittest.main()
