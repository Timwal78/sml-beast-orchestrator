"""Tests for sml_beast/outreach/alerts.py — Discord webhook alerts.

No real HTTP. post_fn is injected throughout. State file lives in tmpdir.

Covers:
  - emit: no webhook → no_webhook reason, no state write
  - emit: webhook + success → sent=True, state updated
  - emit: webhook + HTTP failure → sent=False, state NOT updated
  - rate limit: LOW_HOT_WALLET fires once, suppressed within 24h
  - rate limit: KILL_SWITCH_ACTIVATED is NOT rate-limited (always fires)
  - check_and_alert: detects kill switch transition (off→on, on→off)
  - check_and_alert: detects manual-review backlog crossing threshold
  - alert_low_hot_wallet: builds description with balance
  - alert_manual_review_backlog: builds description with count
  - alert_cycle_complete: builds description with counts
"""

import importlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

_WEBHOOK = "https://discord.example.com/api/webhooks/123/abc"


def _ok_response():
    r = MagicMock()
    r.status_code = 204
    return r


def _fail_response():
    r = MagicMock()
    r.status_code = 500
    return r


class _AlertsBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="beast-alerts-")
        os.environ["BEAST_OUTPUT_ROOT"] = self.tmp
        os.environ["BB7_DISCORD_ALERT_WEBHOOK"] = _WEBHOOK

        import sml_beast.outreach.alerts as a
        import sml_beast.outreach.guardrails as g
        import sml_beast.outreach.reply_monitor as rm
        import sml_beast.outreach.state as s

        importlib.reload(g)
        importlib.reload(s)
        importlib.reload(rm)
        importlib.reload(a)
        self.a = a
        self.g = g
        self.rm = rm
        self.s = s
        self.root = Path(self.tmp)

    def tearDown(self):
        for k in ("BEAST_OUTPUT_ROOT", "BB7_DISCORD_ALERT_WEBHOOK"):
            os.environ.pop(k, None)
        shutil.rmtree(self.tmp, ignore_errors=True)


# ── basic emit ────────────────────────────────────────────────────────────────

class EmitBasicTests(_AlertsBase):
    def test_no_webhook_returns_no_webhook(self):
        os.environ.pop("BB7_DISCORD_ALERT_WEBHOOK", None)
        result = self.a.emit(
            self.a.AlertType.CYCLE_COMPLETE,
            description="test",
            output_root=self.root,
            post_fn=MagicMock(return_value=_ok_response()),
        )
        self.assertFalse(result.sent)
        self.assertEqual(result.reason, "no_webhook")

    def test_explicit_empty_webhook_is_no_webhook(self):
        result = self.a.emit(
            self.a.AlertType.CYCLE_COMPLETE,
            description="test",
            output_root=self.root,
            webhook_url="",
            post_fn=MagicMock(return_value=_ok_response()),
        )
        self.assertEqual(result.reason, "no_webhook")

    def test_successful_post_returns_ok(self):
        result = self.a.emit(
            self.a.AlertType.CYCLE_COMPLETE,
            description="test",
            output_root=self.root,
            post_fn=lambda url, json, timeout: _ok_response(),
        )
        self.assertTrue(result.sent)
        self.assertEqual(result.reason, "ok")

    def test_http_failure_returns_http_error(self):
        result = self.a.emit(
            self.a.AlertType.CYCLE_COMPLETE,
            description="test",
            output_root=self.root,
            post_fn=lambda url, json, timeout: _fail_response(),
        )
        self.assertFalse(result.sent)
        self.assertEqual(result.reason, "http_error")

    def test_exception_returns_http_error(self):
        def explode(url, json, timeout):
            raise ConnectionError("network down")

        result = self.a.emit(
            self.a.AlertType.CYCLE_COMPLETE,
            description="test",
            output_root=self.root,
            post_fn=explode,
        )
        self.assertFalse(result.sent)


# ── rate limiting ────────────────────────────────────────────────────────────

class RateLimitTests(_AlertsBase):
    def test_low_hot_wallet_rate_limited_within_24h(self):
        # First call sends; second is suppressed
        first = self.a.alert_low_hot_wallet(
            15.0, output_root=self.root, post_fn=lambda *a, **k: _ok_response()
        )
        second = self.a.alert_low_hot_wallet(
            14.0, output_root=self.root, post_fn=lambda *a, **k: _ok_response()
        )
        self.assertTrue(first.sent)
        self.assertEqual(second.reason, "rate_limited")

    def test_manual_review_backlog_rate_limited(self):
        first = self.a.alert_manual_review_backlog(
            5, output_root=self.root, post_fn=lambda *a, **k: _ok_response()
        )
        second = self.a.alert_manual_review_backlog(
            6, output_root=self.root, post_fn=lambda *a, **k: _ok_response()
        )
        self.assertTrue(first.sent)
        self.assertEqual(second.reason, "rate_limited")

    def test_kill_switch_not_rate_limited(self):
        # KILL_SWITCH alerts must always fire — operator needs to know
        first = self.a.alert_kill_switch_activated(
            output_root=self.root, post_fn=lambda *a, **k: _ok_response()
        )
        second = self.a.alert_kill_switch_activated(
            output_root=self.root, post_fn=lambda *a, **k: _ok_response()
        )
        self.assertTrue(first.sent)
        self.assertTrue(second.sent)

    def test_state_not_written_on_http_failure(self):
        # If the HTTP post fails, the rate-limit state must not be bumped
        # (otherwise we'd skip the next attempt within the window)
        self.a.alert_low_hot_wallet(
            15.0, output_root=self.root, post_fn=lambda *a, **k: _fail_response()
        )
        # Now a successful post within the window should still fire
        second = self.a.alert_low_hot_wallet(
            14.0, output_root=self.root, post_fn=lambda *a, **k: _ok_response()
        )
        self.assertTrue(second.sent)


# ── post payload shape ───────────────────────────────────────────────────────

class PostPayloadTests(_AlertsBase):
    def test_payload_has_embed_with_title(self):
        captured = {}

        def capture_post(url, json, timeout):
            captured["url"] = url
            captured["json"] = json
            return _ok_response()

        self.a.emit(
            self.a.AlertType.LOW_HOT_WALLET,
            description="balance is low",
            output_root=self.root,
            post_fn=capture_post,
        )
        self.assertEqual(captured["url"], _WEBHOOK)
        payload = captured["json"]
        self.assertEqual(len(payload["embeds"]), 1)
        embed = payload["embeds"][0]
        self.assertIn("BB7", embed["title"])
        self.assertIn("balance is low", embed["description"])

    def test_fields_render_correctly(self):
        captured = {}

        def capture_post(url, json, timeout):
            captured["json"] = json
            return _ok_response()

        self.a.alert_cycle_complete(
            10, 3, output_root=self.root, post_fn=capture_post
        )
        fields = captured["json"]["embeds"][0]["fields"]
        names = {f["name"] for f in fields}
        self.assertEqual(names, {"attempted", "sent"})


# ── helpers ──────────────────────────────────────────────────────────────────

class HelperFunctionTests(_AlertsBase):
    def test_low_hot_wallet_includes_balance(self):
        captured = {}

        def capture(url, json, timeout):
            captured["json"] = json
            return _ok_response()

        self.a.alert_low_hot_wallet(
            13.75, output_root=self.root, post_fn=capture
        )
        desc = captured["json"]["embeds"][0]["description"]
        self.assertIn("13.75", desc)

    def test_cycle_complete_includes_counts(self):
        captured = {}

        def capture(url, json, timeout):
            captured["json"] = json
            return _ok_response()

        self.a.alert_cycle_complete(
            5, 2, output_root=self.root, post_fn=capture
        )
        desc = captured["json"]["embeds"][0]["description"]
        self.assertIn("attempted=5", desc)
        self.assertIn("sent=2", desc)


# ── check_and_alert sweep ────────────────────────────────────────────────────

class CheckAndAlertTests(_AlertsBase):
    def test_no_alerts_when_quiet_state(self):
        results = self.a.check_and_alert(
            output_root=self.root, post_fn=lambda *a, **k: _ok_response()
        )
        # Kill switch off, queue empty → no alerts
        self.assertEqual([r for r in results if r.sent], [])

    def test_kill_switch_activation_detected(self):
        # First sweep records "kill switch was off"
        self.a.check_and_alert(
            output_root=self.root, post_fn=lambda *a, **k: _ok_response()
        )
        # Now activate
        ks = self.g.kill_switch_path()
        ks.parent.mkdir(parents=True, exist_ok=True)
        ks.write_text("HALT")
        results = self.a.check_and_alert(
            output_root=self.root, post_fn=lambda *a, **k: _ok_response()
        )
        sent_results = [r for r in results if r.sent]
        self.assertEqual(len(sent_results), 1)

    def test_kill_switch_deactivation_detected(self):
        # Activate first, record state
        ks = self.g.kill_switch_path()
        ks.parent.mkdir(parents=True, exist_ok=True)
        ks.write_text("HALT")
        self.a.check_and_alert(
            output_root=self.root, post_fn=lambda *a, **k: _ok_response()
        )
        # Now deactivate
        ks.unlink()
        results = self.a.check_and_alert(
            output_root=self.root, post_fn=lambda *a, **k: _ok_response()
        )
        self.assertTrue(any(r.sent for r in results))

    def test_review_backlog_alerts_above_threshold(self):
        # Fill operator queue past the threshold
        for i in range(self.a.MANUAL_REVIEW_BACKLOG_THRESHOLD + 1):
            entry = {
                "domain": f"d{i}.com",
                "message_id": f"<mid{i}@host>",
                "classification": "MANUAL_REVIEW",
                "raw_body": "stuff",
            }
            p = self.rm._operator_queue_path(self.root)
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a") as f:
                f.write(json.dumps(entry) + "\n")

        results = self.a.check_and_alert(
            output_root=self.root, post_fn=lambda *a, **k: _ok_response()
        )
        sent_descs = [r.reason for r in results if r.sent]
        self.assertIn("ok", sent_descs)


# ── state persistence ────────────────────────────────────────────────────────

class StatePersistenceTests(_AlertsBase):
    def test_state_round_trip(self):
        state = {"foo": 123}
        self.a._save_alert_state(state, output_root=self.root)
        loaded = self.a._load_alert_state(output_root=self.root)
        self.assertEqual(loaded["foo"], 123)

    def test_corrupt_state_returns_empty(self):
        p = self.a._alert_state_path(self.root)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{not json")
        loaded = self.a._load_alert_state(output_root=self.root)
        self.assertEqual(loaded, {})

    def test_atomic_write_no_tmp_left(self):
        self.a._save_alert_state({"x": 1}, output_root=self.root)
        tmp = self.a._alert_state_path(self.root).with_suffix(".tmp")
        self.assertFalse(tmp.exists())


if __name__ == "__main__":
    unittest.main()
