"""Tests for sml_beast/outreach/reply_monitor.py — IMAP poller.

No real IMAP connection. imap_factory is mocked throughout.

Covers:
  - IMAP config: missing host/user/pass raises ReplyMonitorError
  - Thread map: record_dispatch + load_thread_map round trip; corrupt file → {}
  - poll_inbox: drains UNSEEN, classifies each, marks seen
  - OPTOUT reply marks the domain opted_out via state machine
  - ACCEPT and MANUAL_REVIEW replies append to operator queue (no state mutation)
  - Operator queue is append-only JSONL; load_operator_queue reads it back
  - IMAP error / connection error → ReplyMonitorError (no silent retry)
  - mark_seen=False leaves messages unread
"""

import importlib
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock


def _make_email(in_reply_to: str | None, body: str) -> bytes:
    """Build a minimal raw RFC-2822 email as bytes (the format imap.fetch returns)."""
    lines = [
        "From: recipient@target.com",
        "To: outreach@infra.sml.com",
        "Subject: Re: BB7 outreach",
    ]
    if in_reply_to:
        lines.append(f"In-Reply-To: {in_reply_to}")
    lines.extend(["", body])
    return "\r\n".join(lines).encode("utf-8")


def _imap_env():
    return {
        "BB7_IMAP_HOST": "imap.example.com",
        "BB7_IMAP_USER": "outreach@infra.sml.com",
        "BB7_IMAP_PASS": "passw0rd",
        "BB7_IMAP_PORT": "993",
        "BB7_IMAP_MAILBOX": "INBOX",
    }


class _ReplyMonitorBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="beast-rm-")
        os.environ["BEAST_OUTPUT_ROOT"] = self.tmp
        for k, v in _imap_env().items():
            os.environ[k] = v

        import sml_beast.outreach.guardrails as g
        import sml_beast.outreach.reply_monitor as rm
        import sml_beast.outreach.state as s
        importlib.reload(g)
        importlib.reload(s)
        importlib.reload(rm)
        self.rm = rm
        self.s = s
        self.root = Path(self.tmp)

    def tearDown(self):
        for k in [*list(_imap_env()), "BEAST_OUTPUT_ROOT"]:
            os.environ.pop(k, None)
        shutil.rmtree(self.tmp, ignore_errors=True)


# ── config ────────────────────────────────────────────────────────────────────

class ConfigTests(_ReplyMonitorBase):
    def test_missing_host_raises(self):
        os.environ.pop("BB7_IMAP_HOST", None)
        with self.assertRaises(self.rm.ReplyMonitorError):
            self.rm.poll_inbox(output_root=self.root, imap_factory=MagicMock())

    def test_missing_user_raises(self):
        os.environ.pop("BB7_IMAP_USER", None)
        with self.assertRaises(self.rm.ReplyMonitorError):
            self.rm.poll_inbox(output_root=self.root, imap_factory=MagicMock())

    def test_missing_pass_raises(self):
        os.environ.pop("BB7_IMAP_PASS", None)
        with self.assertRaises(self.rm.ReplyMonitorError):
            self.rm.poll_inbox(output_root=self.root, imap_factory=MagicMock())


# ── thread map ────────────────────────────────────────────────────────────────

class ThreadMapTests(_ReplyMonitorBase):
    def test_record_then_load_round_trip(self):
        self.rm.record_dispatch("<mid1@host>", "target.com", output_root=self.root)
        self.rm.record_dispatch("<mid2@host>", "other.com", output_root=self.root)
        m = self.rm.load_thread_map(output_root=self.root)
        self.assertEqual(m, {"<mid1@host>": "target.com", "<mid2@host>": "other.com"})

    def test_load_returns_empty_when_no_file(self):
        m = self.rm.load_thread_map(output_root=self.root)
        self.assertEqual(m, {})

    def test_load_returns_empty_when_corrupt(self):
        p = self.rm.thread_map_path(self.root)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{not valid")
        m = self.rm.load_thread_map(output_root=self.root)
        self.assertEqual(m, {})

    def test_atomic_write_no_tmp_left(self):
        self.rm.record_dispatch("<x@h>", "x.com", output_root=self.root)
        tmp_path = self.rm.thread_map_path(self.root).with_suffix(".tmp")
        self.assertFalse(tmp_path.exists())


# ── poll_inbox: classification side effects ──────────────────────────────────

class PollInboxTests(_ReplyMonitorBase):
    def _build_mock_imap(self, messages: list[tuple[str, bytes]]) -> MagicMock:
        """Build a MagicMock that mimics imaplib.IMAP4_SSL with the given messages.

        messages = [(uid_str, raw_email_bytes), ...]
        """
        imap = MagicMock()
        imap.login.return_value = ("OK", [])
        imap.select.return_value = ("OK", [])
        uids = [m[0].encode() for m in messages]
        imap.search.return_value = ("OK", [b" ".join(uids) if uids else b""])

        def fake_fetch(uid, parts):
            for u, raw in messages:
                if u.encode() == uid:
                    return ("OK", [(b"header", raw)])
            return ("NO", [])

        imap.fetch.side_effect = fake_fetch
        imap.store.return_value = ("OK", [])
        imap.logout.return_value = ("BYE", [])
        return imap

    def test_no_unseen_messages_empty_result(self):
        imap = self._build_mock_imap([])
        result = self.rm.poll_inbox(
            output_root=self.root, imap_factory=lambda h, p: imap
        )
        self.assertEqual(result.fetched, 0)

    def test_optout_marks_domain_opted_out(self):
        # Set up thread map so the reply maps to a known domain
        self.rm.record_dispatch("<mid1@host>", "target.com", output_root=self.root)
        msg = _make_email("<mid1@host>", "STOP")
        imap = self._build_mock_imap([("1", msg)])

        result = self.rm.poll_inbox(
            output_root=self.root, imap_factory=lambda h, p: imap
        )

        self.assertEqual(result.classified_optout, 1)
        self.assertEqual(result.fetched, 1)
        # Verify state machine marked the domain
        sm = self.s.OutreachStateMachine()
        entry = sm.get_domain("target.com")
        self.assertEqual(entry["state"], self.s.STATE_OPTED_OUT)

    def test_accept_appends_to_operator_queue(self):
        self.rm.record_dispatch("<mid2@host>", "target.com", output_root=self.root)
        msg = _make_email("<mid2@host>", "YES happy to add the link!")
        imap = self._build_mock_imap([("2", msg)])

        result = self.rm.poll_inbox(
            output_root=self.root, imap_factory=lambda h, p: imap
        )

        self.assertEqual(result.classified_accept, 1)
        queue = self.rm.load_operator_queue(output_root=self.root)
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["classification"], "ACCEPT")
        # ACCEPT must NOT mutate state — operator decides follow-up manually
        sm = self.s.OutreachStateMachine()
        entry = sm.get_domain("target.com")
        self.assertIsNone(entry)

    def test_manual_review_appends_to_operator_queue(self):
        self.rm.record_dispatch("<mid3@host>", "target.com", output_root=self.root)
        msg = _make_email("<mid3@host>", "Sounds interesting, tell me more.")
        imap = self._build_mock_imap([("3", msg)])

        result = self.rm.poll_inbox(
            output_root=self.root, imap_factory=lambda h, p: imap
        )

        self.assertEqual(result.classified_manual, 1)
        queue = self.rm.load_operator_queue(output_root=self.root)
        self.assertEqual(queue[0]["classification"], "MANUAL_REVIEW")

    def test_optout_without_known_thread_still_marked(self):
        # No record_dispatch — In-Reply-To references unknown Message-ID
        msg = _make_email("<unknown@host>", "STOP unsubscribe")
        imap = self._build_mock_imap([("4", msg)])

        result = self.rm.poll_inbox(
            output_root=self.root, imap_factory=lambda h, p: imap
        )

        # OPTOUT classification still happens, but no domain to blocklist
        self.assertEqual(result.classified_optout, 1)
        queue = self.rm.load_operator_queue(output_root=self.root)
        self.assertEqual(queue[0]["classification"], "OPTOUT")

    def test_messages_marked_seen_by_default(self):
        self.rm.record_dispatch("<midX@host>", "target.com", output_root=self.root)
        msg = _make_email("<midX@host>", "YES")
        imap = self._build_mock_imap([("5", msg)])

        self.rm.poll_inbox(output_root=self.root, imap_factory=lambda h, p: imap)
        # store() should have been called with the Seen flag
        imap.store.assert_called_once()
        args = imap.store.call_args[0]
        self.assertIn(b"5", args)
        self.assertEqual(args[1], "+FLAGS")
        self.assertEqual(args[2], "\\Seen")

    def test_mark_seen_false_skips_store(self):
        self.rm.record_dispatch("<midY@host>", "target.com", output_root=self.root)
        msg = _make_email("<midY@host>", "YES")
        imap = self._build_mock_imap([("6", msg)])

        self.rm.poll_inbox(
            output_root=self.root,
            imap_factory=lambda h, p: imap,
            mark_seen=False,
        )
        imap.store.assert_not_called()


# ── failure paths ────────────────────────────────────────────────────────────

class FailureTests(_ReplyMonitorBase):
    def test_imap_login_error_raises(self):
        import imaplib as _imaplib
        imap = MagicMock()
        imap.login.side_effect = _imaplib.IMAP4.error("auth failed")
        with self.assertRaises(self.rm.ReplyMonitorError):
            self.rm.poll_inbox(
                output_root=self.root, imap_factory=lambda h, p: imap
            )

    def test_connection_error_raises(self):
        def factory(h, p):
            raise OSError("connection refused")

        with self.assertRaises(self.rm.ReplyMonitorError):
            self.rm.poll_inbox(output_root=self.root, imap_factory=factory)


# ── operator queue ───────────────────────────────────────────────────────────

class OperatorQueueTests(_ReplyMonitorBase):
    def test_load_empty_returns_empty_list(self):
        records = self.rm.load_operator_queue(output_root=self.root)
        self.assertEqual(records, [])

    def test_corrupt_lines_skipped(self):
        p = self.rm._operator_queue_path(self.root)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w") as f:
            f.write('{"domain":"good.com","classification":"ACCEPT"}\n')
            f.write("{this is broken\n")
            f.write('{"domain":"also.com","classification":"OPTOUT"}\n')
        records = self.rm.load_operator_queue(output_root=self.root)
        self.assertEqual(len(records), 2)


if __name__ == "__main__":
    unittest.main()
