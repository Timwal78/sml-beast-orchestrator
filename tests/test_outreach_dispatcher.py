"""Tests for sml_beast/outreach/dispatcher.py — SMTP send + reply parser.

No real SMTP connections. smtp_factory is mocked throughout.

Covers:
  - SMTP config: missing host/user/pass raises DispatchError
  - Message construction: Message-ID, From, Subject, CAN-SPAM footer
  - send_pitch: happy path (DispatchResult with message_id), SMTP error → DispatchError
  - Reply parser: OPTOUT wins over ACCEPT, ACCEPT requires known Message-ID,
    ambiguous body → MANUAL_REVIEW, malformed email → MANUAL_REVIEW
  - OPTOUT does NOT require a known Message-ID (domain stays unknown/empty)
"""

import os
import smtplib
import unittest
from unittest.mock import MagicMock

from sml_beast.outreach.dispatcher import (
    DispatchError,
    DispatchResult,
    parse_reply,
    send_pitch,
)
from sml_beast.outreach.templates import PitchEmail


def _pitch(domain="target.com", subject="Test Subject", body="Hello body.") -> PitchEmail:
    return PitchEmail(subject=subject, body=body, vertical="mastersheets", domain=domain)


def _smtp_env(**overrides):
    base = {
        "BB7_SMTP_HOST": "smtp.example.com",
        "BB7_SMTP_PORT": "587",
        "BB7_SMTP_USER": "outreach@infrastructure.scriptmasterlabs.com",
        "BB7_SMTP_PASS": "s3cr3t",
        "BB7_SMTP_FROM": "outreach@infrastructure.scriptmasterlabs.com",
    }
    base.update(overrides)
    return base


def _mock_smtp_factory():
    smtp_mock = MagicMock()
    return smtp_mock, lambda host, port: smtp_mock


def _make_reply(body: str, in_reply_to: str | None = None) -> str:
    """Build a minimal raw email string."""
    lines = [
        "From: recipient@target.com",
        "To: outreach@infrastructure.scriptmasterlabs.com",
        "Subject: Re: Test",
    ]
    if in_reply_to:
        lines.append(f"In-Reply-To: {in_reply_to}")
    lines += ["", body]
    return "\r\n".join(lines)


# ── SMTP config validation ────────────────────────────────────────────────────

class SMTPConfigTests(unittest.TestCase):
    def setUp(self):
        for k in ("BB7_SMTP_HOST", "BB7_SMTP_USER", "BB7_SMTP_PASS", "BB7_SMTP_FROM"):
            os.environ.pop(k, None)

    def tearDown(self):
        for k in ("BB7_SMTP_HOST", "BB7_SMTP_USER", "BB7_SMTP_PASS", "BB7_SMTP_FROM"):
            os.environ.pop(k, None)

    def test_missing_host_raises(self):
        with self.assertRaises(DispatchError) as cm:
            send_pitch(_pitch(), "rec@example.com")
        self.assertIn("BB7_SMTP_HOST", str(cm.exception))

    def test_missing_user_raises(self):
        os.environ["BB7_SMTP_HOST"] = "smtp.example.com"
        with self.assertRaises(DispatchError) as cm:
            send_pitch(_pitch(), "rec@example.com")
        self.assertIn("BB7_SMTP_USER", str(cm.exception))

    def test_missing_pass_raises(self):
        os.environ["BB7_SMTP_HOST"] = "smtp.example.com"
        os.environ["BB7_SMTP_USER"] = "user"
        with self.assertRaises(DispatchError) as cm:
            send_pitch(_pitch(), "rec@example.com")
        self.assertIn("BB7_SMTP_PASS", str(cm.exception))


# ── send_pitch happy path ─────────────────────────────────────────────────────

class SendPitchHappyPathTests(unittest.TestCase):
    def setUp(self):
        for k, v in _smtp_env().items():
            os.environ[k] = v

    def tearDown(self):
        for k in _smtp_env():
            os.environ.pop(k, None)

    def test_returns_dispatch_result_with_message_id(self):
        _smtp_instance, factory = _mock_smtp_factory()
        result = send_pitch(_pitch(), "rec@target.com", smtp_factory=factory)
        self.assertIsInstance(result, DispatchResult)
        self.assertTrue(result.message_id.startswith("<"))
        self.assertTrue(result.message_id.endswith(">"))
        self.assertTrue(result.accepted)

    def test_sendmail_called_once(self):
        smtp_instance, factory = _mock_smtp_factory()
        send_pitch(_pitch(), "rec@target.com", smtp_factory=factory)
        self.assertEqual(smtp_instance.sendmail.call_count, 1)

    def test_login_called_with_credentials(self):
        smtp_instance, factory = _mock_smtp_factory()
        send_pitch(_pitch(), "rec@target.com", smtp_factory=factory)
        smtp_instance.login.assert_called_once_with(
            "outreach@infrastructure.scriptmasterlabs.com", "s3cr3t"
        )

    def test_quit_called_after_send(self):
        smtp_instance, factory = _mock_smtp_factory()
        send_pitch(_pitch(), "rec@target.com", smtp_factory=factory)
        smtp_instance.quit.assert_called_once()

    def test_result_domain_matches_pitch(self):
        _smtp_instance, factory = _mock_smtp_factory()
        result = send_pitch(_pitch(domain="mytarget.io"), "rec@mytarget.io", smtp_factory=factory)
        self.assertEqual(result.domain, "mytarget.io")

    def test_result_subject_matches_pitch(self):
        _smtp_instance, factory = _mock_smtp_factory()
        result = send_pitch(_pitch(subject="My Subject"), "r@t.com", smtp_factory=factory)
        self.assertEqual(result.subject, "My Subject")

    def test_can_spam_footer_in_sent_message(self):
        smtp_instance, factory = _mock_smtp_factory()
        send_pitch(_pitch(), "rec@target.com", smtp_factory=factory)
        raw_msg = smtp_instance.sendmail.call_args[0][2]
        # Body may be base64-encoded by MIME; parse and decode to inspect content
        import email as _email
        parsed = _email.message_from_string(raw_msg)
        body = ""
        if parsed.is_multipart():
            for part in parsed.walk():
                if part.get_content_type() == "text/plain":
                    body = part.get_payload(decode=True).decode("utf-8", errors="replace")
                    break
        else:
            payload = parsed.get_payload(decode=True)
            body = payload.decode("utf-8", errors="replace") if payload else raw_msg
        self.assertIn("Reply STOP", body)

    def test_from_address_in_sent_message(self):
        smtp_instance, factory = _mock_smtp_factory()
        send_pitch(_pitch(), "rec@target.com", smtp_factory=factory)
        raw_msg = smtp_instance.sendmail.call_args[0][2]
        self.assertIn("outreach@infrastructure.scriptmasterlabs.com", raw_msg)


# ── send_pitch failure paths ──────────────────────────────────────────────────

class SendPitchFailureTests(unittest.TestCase):
    def setUp(self):
        for k, v in _smtp_env().items():
            os.environ[k] = v

    def tearDown(self):
        for k in _smtp_env():
            os.environ.pop(k, None)

    def test_smtp_exception_raises_dispatch_error(self):
        smtp_instance = MagicMock()
        smtp_instance.login.side_effect = smtplib.SMTPAuthenticationError(535, b"Auth fail")

        def factory(h, p):
            return smtp_instance

        with self.assertRaises(DispatchError) as cm:
            send_pitch(_pitch(), "rec@target.com", smtp_factory=factory)
        self.assertIn("SMTP error", str(cm.exception))

    def test_os_error_raises_dispatch_error(self):
        def factory(h, p):
            raise OSError("Connection refused")

        with self.assertRaises(DispatchError) as cm:
            send_pitch(_pitch(), "rec@target.com", smtp_factory=factory)
        self.assertIn("connection error", str(cm.exception).lower())


# ── reply parser: OPTOUT ──────────────────────────────────────────────────────

class ReplyParserOptoutTests(unittest.TestCase):
    def test_stop_word_classifies_as_optout(self):
        raw = _make_reply("STOP\nPlease don't email me again.")
        result = parse_reply(raw, {})
        self.assertEqual(result.classification, "OPTOUT")

    def test_stop_in_sentence_classifies_as_optout(self):
        raw = _make_reply("Please STOP sending me these emails.")
        result = parse_reply(raw, {})
        self.assertEqual(result.classification, "OPTOUT")

    def test_optout_does_not_require_known_message_id(self):
        raw = _make_reply("STOP", in_reply_to=None)
        result = parse_reply(raw, {})
        self.assertEqual(result.classification, "OPTOUT")

    def test_optout_wins_over_accept_in_same_body(self):
        # If someone writes "YES STOP" — OPTOUT wins (safety-first)
        raw = _make_reply("YES I am interested but STOP future emails.", in_reply_to="<mid@host>")
        known = {"<mid@host>": "target.com"}
        result = parse_reply(raw, known)
        self.assertEqual(result.classification, "OPTOUT")


# ── reply parser: ACCEPT ──────────────────────────────────────────────────────

class ReplyParserAcceptTests(unittest.TestCase):
    def _make_known(self, msg_id="<testid@host>", domain="target.com"):
        return {msg_id: domain}

    def test_yes_with_known_message_id_accepts(self):
        raw = _make_reply("YES I'd love to add a link.", in_reply_to="<testid@host>")
        result = parse_reply(raw, self._make_known())
        self.assertEqual(result.classification, "ACCEPT")
        self.assertEqual(result.domain, "target.com")

    def test_agree_accepts(self):
        raw = _make_reply("AGREE, happy to mention this.", in_reply_to="<testid@host>")
        result = parse_reply(raw, self._make_known())
        self.assertEqual(result.classification, "ACCEPT")

    def test_accept_word_accepts(self):
        raw = _make_reply("ACCEPT your offer.", in_reply_to="<testid@host>")
        result = parse_reply(raw, self._make_known())
        self.assertEqual(result.classification, "ACCEPT")

    def test_interested_accepts(self):
        raw = _make_reply("INTERESTED — let me check with the team.", in_reply_to="<testid@host>")
        result = parse_reply(raw, self._make_known())
        self.assertEqual(result.classification, "ACCEPT")

    def test_ship_it_accepts(self):
        raw = _make_reply("SHIP IT!", in_reply_to="<testid@host>")
        result = parse_reply(raw, self._make_known())
        self.assertEqual(result.classification, "ACCEPT")

    def test_link_added_accepts(self):
        raw = _make_reply("LINK ADDED to the post.", in_reply_to="<testid@host>")
        result = parse_reply(raw, self._make_known())
        self.assertEqual(result.classification, "ACCEPT")

    def test_accept_without_known_message_id_is_manual_review(self):
        raw = _make_reply("YES I agree!", in_reply_to="<unknown@host>")
        result = parse_reply(raw, {})  # empty known dict
        self.assertEqual(result.classification, "MANUAL_REVIEW")

    def test_accept_without_in_reply_to_header_is_manual_review(self):
        raw = _make_reply("YES great idea!", in_reply_to=None)
        result = parse_reply(raw, self._make_known())
        self.assertEqual(result.classification, "MANUAL_REVIEW")


# ── reply parser: MANUAL_REVIEW ──────────────────────────────────────────────

class ReplyParserManualReviewTests(unittest.TestCase):
    def test_ambiguous_positive_is_manual_review(self):
        raw = _make_reply("Sounds interesting, tell me more.", in_reply_to="<mid@host>")
        result = parse_reply(raw, {"<mid@host>": "target.com"})
        self.assertEqual(result.classification, "MANUAL_REVIEW")

    def test_empty_body_is_manual_review(self):
        raw = _make_reply("", in_reply_to="<mid@host>")
        result = parse_reply(raw, {"<mid@host>": "target.com"})
        self.assertEqual(result.classification, "MANUAL_REVIEW")

    def test_malformed_email_is_manual_review(self):
        result = parse_reply("this is not a valid email at all###", {})
        self.assertEqual(result.classification, "MANUAL_REVIEW")

    def test_no_tracking_or_crashes_on_empty_known_dict(self):
        raw = _make_reply("Random reply with no threading.", in_reply_to=None)
        result = parse_reply(raw, {})
        self.assertEqual(result.classification, "MANUAL_REVIEW")

    def test_raw_body_preserved_in_result(self):
        raw = _make_reply("Some ambiguous reply.", in_reply_to=None)
        result = parse_reply(raw, {})
        self.assertIn("ambiguous reply", result.raw_body)


if __name__ == "__main__":
    unittest.main()
