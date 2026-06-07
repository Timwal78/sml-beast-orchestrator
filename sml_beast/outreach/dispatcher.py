"""
BB7 SMTP dispatcher — sends pitch emails and parses inbound replies.

From-address: outreach@infrastructure.scriptmasterlabs.com
  (dedicated subdomain per BB7_DESIGN.md §9.1; primary domain reputation
  never exposed to automated cold outbound)

CAN-SPAM compliance — every message carries:
  (a) Physical operator address (BB7_OPERATOR_ADDRESS env var)
  (b) Clear identification as a placement offer
  (c) One-click opt-out (STOP reply → permanent domain blocklist)

Reply parser — §9.2, STRICT regex mode only:
  Acceptance regex: r\b(YES|AGREE|ACCEPT|INTERESTED|SHIP IT|LINK ADDED)\b
  Opt-out regex:    r\bSTOP\b
  Subject must reference the original Message-ID for acceptance processing.
  Any reply that fails strict matching routes to the manual operator queue.
  No LLM in the acceptance path. No fund movement on ambiguous text.

Configuration (env vars):
  BB7_SMTP_HOST       — SMTP relay hostname (required)
  BB7_SMTP_PORT       — defaults to 587 (STARTTLS)
  BB7_SMTP_USER       — SMTP username (required)
  BB7_SMTP_PASS       — SMTP password (required)
  BB7_SMTP_FROM       — override from-address (default: outreach@infrastructure.scriptmasterlabs.com)
  BB7_OPERATOR_ADDRESS — physical address for CAN-SPAM footer

Injectable `smtp_factory` for test isolation — the default factory
opens a real smtplib.SMTP connection with STARTTLS. Tests inject a
MagicMock. No real SMTP calls in the test suite.
"""

import email as email_lib
import email.utils
import logging
import os
import re
import smtplib
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

from .templates import PitchEmail

logger = logging.getLogger("sml-beast.outreach.dispatcher")

# ── constants ────────────────────────────────────────────────────────────────

DEFAULT_FROM = "outreach@infrastructure.scriptmasterlabs.com"
DEFAULT_SMTP_PORT = 587

# Strict acceptance patterns per BB7_DESIGN.md §9.2.
# Matched case-insensitively against the reply body.
_ACCEPT_RE = re.compile(
    r"\b(YES|AGREE|ACCEPT|INTERESTED|SHIP\s+IT|LINK\s+ADDED)\b",
    re.IGNORECASE,
)
_OPTOUT_RE = re.compile(r"\bSTOP\b", re.IGNORECASE)

# CAN-SPAM physical address (operator provides via env)
_DEFAULT_OPERATOR_ADDRESS = "ScriptMasterLabs — see scriptmasterlabs.com/contact"


# ── result types ─────────────────────────────────────────────────────────────


@dataclass
class DispatchResult:
    message_id: str
    recipient: str
    domain: str
    subject: str
    accepted: bool  # True = SMTP server accepted the message for delivery


class DispatchError(Exception):
    """Raised on SMTP failure. Caller must not silently retry — log and
    route to operator queue for human decision on re-send risk."""


@dataclass
class ReplyClassification:
    domain: str
    message_id: str | None  # Message-ID the reply references (In-Reply-To)
    classification: str  # "ACCEPT" | "OPTOUT" | "MANUAL_REVIEW"
    raw_body: str


# ── SMTP factory (injectable) ────────────────────────────────────────────────


def _default_smtp_factory(host: str, port: int) -> smtplib.SMTP:
    smtp = smtplib.SMTP(host, port, timeout=30)
    smtp.ehlo()
    smtp.starttls()
    smtp.ehlo()
    return smtp


# ── config loader ────────────────────────────────────────────────────────────


def _smtp_config() -> dict[str, Any]:
    host = os.environ.get("BB7_SMTP_HOST", "").strip()
    if not host:
        raise DispatchError(
            "BB7_SMTP_HOST not configured — cannot send pitch email. "
            "Set this env var to your SMTP relay hostname."
        )
    user = os.environ.get("BB7_SMTP_USER", "").strip()
    passwd = os.environ.get("BB7_SMTP_PASS", "").strip()
    if not user or not passwd:
        raise DispatchError(
            "BB7_SMTP_USER and BB7_SMTP_PASS are required for SMTP auth."
        )
    port = int(os.environ.get("BB7_SMTP_PORT", DEFAULT_SMTP_PORT))
    from_addr = os.environ.get("BB7_SMTP_FROM", DEFAULT_FROM).strip()
    return {
        "host": host,
        "port": port,
        "user": user,
        "passwd": passwd,
        "from_addr": from_addr,
    }


# ── message builder ──────────────────────────────────────────────────────────


def _build_message(
    pitch: PitchEmail,
    recipient_email: str,
    from_addr: str,
) -> tuple[MIMEMultipart, str]:
    """Build an RFC-5322 message. Returns (msg, message_id)."""
    msg_id = f"<{uuid.uuid4()}@infrastructure.scriptmasterlabs.com>"
    operator_address = os.environ.get("BB7_OPERATOR_ADDRESS", _DEFAULT_OPERATOR_ADDRESS)

    can_spam_footer = (
        "\n\n---\n"
        f"ScriptMasterLabs | {operator_address}\n"
        "This message was sent as part of an outreach program. "
        "Reply STOP to be permanently removed from all future outreach."
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = pitch.subject
    msg["From"] = from_addr
    msg["To"] = recipient_email
    msg["Message-ID"] = msg_id
    msg["X-Mailer"] = "sml-beast-orchestrator/1.0"

    full_body = pitch.body + can_spam_footer
    msg.attach(MIMEText(full_body, "plain", "utf-8"))

    return msg, msg_id


# ── public send function ─────────────────────────────────────────────────────


def send_pitch(
    pitch: PitchEmail,
    recipient_email: str,
    smtp_factory: Callable[[str, int], Any] = _default_smtp_factory,
) -> DispatchResult:
    """Send a pitch email via the configured SMTP relay.

    Returns a DispatchResult with the Message-ID for reply threading.
    Raises DispatchError on any SMTP or configuration failure.

    Injectable smtp_factory allows test isolation — pass a MagicMock
    to avoid any real network connection."""
    config = _smtp_config()
    msg, msg_id = _build_message(pitch, recipient_email, config["from_addr"])

    try:
        smtp = smtp_factory(config["host"], config["port"])
        smtp.login(config["user"], config["passwd"])
        smtp.sendmail(config["from_addr"], [recipient_email], msg.as_string())
        smtp.quit()
    except smtplib.SMTPException as e:
        raise DispatchError(f"SMTP error sending to {recipient_email}: {e}") from e
    except OSError as e:
        raise DispatchError(f"SMTP connection error to {config['host']}: {e}") from e

    logger.info(
        "pitch sent: domain=%s recipient=%s msg_id=%s",
        pitch.domain,
        recipient_email,
        msg_id,
    )
    return DispatchResult(
        message_id=msg_id,
        recipient=recipient_email,
        domain=pitch.domain,
        subject=pitch.subject,
        accepted=True,
    )


# ── reply parser ─────────────────────────────────────────────────────────────


def parse_reply(
    raw_email: str,
    known_domains: dict[str, str],
) -> ReplyClassification:
    """Classify an inbound reply email.

    `known_domains` maps Message-ID → domain, built from the
    DispatchResult.message_id values recorded by the caller.

    Classification rules (BB7_DESIGN.md §9.2 — strict, no LLM):
      OPTOUT        — body contains \\bSTOP\\b (checked FIRST; takes priority)
      ACCEPT        — In-Reply-To references a known Message-ID AND body
                      matches the acceptance regex
      MANUAL_REVIEW — any reply that doesn't match above; routed to
                      operator queue; no automated action taken

    Returns a ReplyClassification. Never raises — malformed input
    produces a MANUAL_REVIEW result."""
    try:
        parsed = email_lib.message_from_string(raw_email)
    except Exception as exc:
        logger.warning("reply parse error: %s", exc)
        return ReplyClassification(
            domain="",
            message_id=None,
            classification="MANUAL_REVIEW",
            raw_body=raw_email[:500],
        )

    in_reply_to: str | None = parsed.get("In-Reply-To", "").strip() or None
    domain = known_domains.get(in_reply_to or "", "") if in_reply_to else ""

    body = _extract_body(parsed)

    if _OPTOUT_RE.search(body):
        logger.info("reply classified OPTOUT: in_reply_to=%s domain=%s", in_reply_to, domain)
        return ReplyClassification(
            domain=domain,
            message_id=in_reply_to,
            classification="OPTOUT",
            raw_body=body,
        )

    if in_reply_to and in_reply_to in known_domains and _ACCEPT_RE.search(body):
        logger.info("reply classified ACCEPT: in_reply_to=%s domain=%s", in_reply_to, domain)
        return ReplyClassification(
            domain=domain,
            message_id=in_reply_to,
            classification="ACCEPT",
            raw_body=body,
        )

    logger.info(
        "reply classified MANUAL_REVIEW: in_reply_to=%s domain=%s", in_reply_to, domain
    )
    return ReplyClassification(
        domain=domain,
        message_id=in_reply_to,
        classification="MANUAL_REVIEW",
        raw_body=body,
    )


def _extract_body(parsed_msg: Any) -> str:
    """Extract plain-text body from a parsed email.Message object."""
    if parsed_msg.is_multipart():
        for part in parsed_msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain":
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    return payload.decode("utf-8", errors="replace")
        return ""
    payload = parsed_msg.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload.decode("utf-8", errors="replace")
    if isinstance(payload, str):
        return payload
    return ""
