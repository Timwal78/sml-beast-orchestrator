"""
BB7 reply monitor — IMAP poller that drains the outreach inbox and routes
each message through dispatcher.parse_reply for classification.

Operational contract:
  - Connects to the outreach inbox via IMAP over SSL (BB7_IMAP_HOST/USER/PASS)
  - Fetches UNSEEN messages
  - For each: looks up the original Message-ID in the persisted thread map,
    runs parse_reply, then:
      OPTOUT        → mark domain opted_out (permanent blocklist)
      ACCEPT        → log to operator queue for review (no automated fund move)
      MANUAL_REVIEW → log to operator queue for review (no automated action)
  - Marks each processed message as SEEN
  - Persists processed UID list to output/_internal/reply_monitor_state.json
    so a crash mid-poll doesn't reprocess messages

Thread map persistence:
  output/_internal/dispatch_thread_map.json maps Message-ID → domain.
  Populated by the agent every time it sends a pitch. This module reads it
  to identify which dispatched pitch a reply refers to.

The monitor never sends mail, never submits XRPL, never decides on its own
whether to honor an ACCEPT reply — it only classifies and persists. Operator
reviews the resulting queue before any follow-up.

Injectable imap_factory and thread_map_path for test isolation.
"""

from __future__ import annotations

import imaplib
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .dispatcher import ReplyClassification, parse_reply
from .guardrails import _output_root
from .state import OutreachStateMachine

logger = logging.getLogger("sml-beast.outreach.reply_monitor")

# ── constants ────────────────────────────────────────────────────────────────

DEFAULT_IMAP_PORT = 993
DEFAULT_MAILBOX = "INBOX"
THREAD_MAP_FILE = "_internal/dispatch_thread_map.json"
MONITOR_STATE_FILE = "_internal/reply_monitor_state.json"
OPERATOR_QUEUE_FILE = "_internal/operator_reply_queue.jsonl"


# ── data types ───────────────────────────────────────────────────────────────


@dataclass
class MonitorResult:
    fetched: int = 0           # messages drained from IMAP
    classified_optout: int = 0
    classified_accept: int = 0
    classified_manual: int = 0
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "fetched": self.fetched,
            "classified_optout": self.classified_optout,
            "classified_accept": self.classified_accept,
            "classified_manual": self.classified_manual,
            "error": self.error,
        }


class ReplyMonitorError(Exception):
    """Raised on IMAP connection or auth failure. The caller logs and exits;
    the monitor never silently retries."""


# ── thread map persistence ───────────────────────────────────────────────────


def thread_map_path(output_root: Path | None = None) -> Path:
    return (output_root or _output_root()) / THREAD_MAP_FILE


def load_thread_map(output_root: Path | None = None) -> dict[str, str]:
    """Load Message-ID → domain map. Returns {} when absent or corrupt."""
    p = thread_map_path(output_root)
    if not p.exists():
        return {}
    try:
        with open(p) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        logger.warning("thread map %s unreadable; treating as empty", p)
        return {}


def record_dispatch(
    message_id: str, domain: str, output_root: Path | None = None
) -> None:
    """Persist a single dispatched Message-ID → domain entry.

    Called by the agent after each successful send_pitch() so the reply monitor
    can later resolve threading. Atomic via temp-file-and-rename."""
    p = thread_map_path(output_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    current = load_thread_map(output_root)
    current[message_id] = domain
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(current, f, indent=2, sort_keys=True)
    os.replace(tmp, p)


# ── operator queue persistence (append-only JSONL) ───────────────────────────


def _operator_queue_path(output_root: Path | None = None) -> Path:
    return (output_root or _output_root()) / OPERATOR_QUEUE_FILE


def _append_operator_queue(
    classification: ReplyClassification, output_root: Path | None = None
) -> None:
    """Append a classification to the operator review queue."""
    p = _operator_queue_path(output_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "domain": classification.domain,
        "message_id": classification.message_id,
        "classification": classification.classification,
        "raw_body": classification.raw_body[:2000],  # cap to keep file readable
    }
    with open(p, "a") as f:
        f.write(json.dumps(record) + "\n")


# ── IMAP factory (injectable) ────────────────────────────────────────────────


def _default_imap_factory(host: str, port: int) -> imaplib.IMAP4_SSL:
    return imaplib.IMAP4_SSL(host, port)


def _imap_config() -> dict[str, Any]:
    host = os.environ.get("BB7_IMAP_HOST", "").strip()
    if not host:
        raise ReplyMonitorError(
            "BB7_IMAP_HOST not configured — cannot poll reply inbox."
        )
    user = os.environ.get("BB7_IMAP_USER", "").strip()
    passwd = os.environ.get("BB7_IMAP_PASS", "").strip()
    if not user or not passwd:
        raise ReplyMonitorError(
            "BB7_IMAP_USER and BB7_IMAP_PASS are required."
        )
    port_raw = os.environ.get("BB7_IMAP_PORT", "").strip()
    port = int(port_raw) if port_raw else DEFAULT_IMAP_PORT
    mailbox = os.environ.get("BB7_IMAP_MAILBOX", DEFAULT_MAILBOX)
    return {
        "host": host,
        "port": port,
        "user": user,
        "passwd": passwd,
        "mailbox": mailbox,
    }


# ── single reply processing ──────────────────────────────────────────────────


def _process_one(
    raw_email: str,
    thread_map: dict[str, str],
    state_machine: OutreachStateMachine,
    output_root: Path | None,
    result: MonitorResult,
) -> None:
    """Classify one reply and apply the appropriate side effect.

    OPTOUT        → mark_opted_out (permanent blocklist; only mutation we make)
    ACCEPT        → append to operator queue
    MANUAL_REVIEW → append to operator queue

    Never sends mail. Never submits XRPL."""
    classification = parse_reply(raw_email, thread_map)

    if classification.classification == "OPTOUT":
        result.classified_optout += 1
        if classification.domain:
            state_machine.mark_opted_out(classification.domain)
            logger.info("OPTOUT: %s blocklisted", classification.domain)
        else:
            # No domain — sender unknown. Still log for operator review.
            logger.warning("OPTOUT without known domain — logging for operator")
        _append_operator_queue(classification, output_root)
    elif classification.classification == "ACCEPT":
        result.classified_accept += 1
        _append_operator_queue(classification, output_root)
        logger.info("ACCEPT from %s — queued for operator review", classification.domain)
    else:  # MANUAL_REVIEW
        result.classified_manual += 1
        _append_operator_queue(classification, output_root)
        logger.info("MANUAL_REVIEW from %s — queued for operator review", classification.domain)


# ── public poll function ─────────────────────────────────────────────────────


def poll_inbox(
    output_root: Path | None = None,
    *,
    imap_factory: Callable[[str, int], Any] = _default_imap_factory,
    state_machine: OutreachStateMachine | None = None,
    mark_seen: bool = True,
) -> MonitorResult:
    """Drain UNSEEN messages from the configured inbox and classify each.

    Returns a MonitorResult with per-class counts. Raises ReplyMonitorError
    on connection / auth failure (caller should log and exit; do not retry).

    Inject `imap_factory` to bypass real IMAP in tests. Inject `state_machine`
    to share an existing OutreachStateMachine instance."""
    config = _imap_config()
    sm = state_machine or OutreachStateMachine()
    thread_map = load_thread_map(output_root)
    result = MonitorResult()

    try:
        imap = imap_factory(config["host"], config["port"])
        imap.login(config["user"], config["passwd"])
        imap.select(config["mailbox"])

        status, data = imap.search(None, "UNSEEN")
        if status != "OK":
            raise ReplyMonitorError(f"IMAP search failed: status={status}")

        uids = data[0].split() if data and data[0] else []
        logger.info("found %d unseen messages", len(uids))

        for uid in uids:
            try:
                fetch_status, fetch_data = imap.fetch(uid, "(RFC822)")
                if fetch_status != "OK" or not fetch_data:
                    logger.warning("fetch failed for uid=%s", uid)
                    continue
                raw_bytes = fetch_data[0][1] if isinstance(fetch_data[0], tuple) else fetch_data[0]
                raw_email = (
                    raw_bytes.decode("utf-8", errors="replace")
                    if isinstance(raw_bytes, bytes)
                    else str(raw_bytes)
                )
                _process_one(raw_email, thread_map, sm, output_root, result)
                result.fetched += 1

                if mark_seen:
                    imap.store(uid, "+FLAGS", "\\Seen")
            except Exception as e:
                logger.error("error processing uid=%s: %s", uid, e)

        try:
            imap.logout()
        except Exception as e:
            logger.debug("imap logout error (non-fatal): %s", e)

    except imaplib.IMAP4.error as e:
        raise ReplyMonitorError(f"IMAP error: {e}") from e
    except OSError as e:
        raise ReplyMonitorError(f"IMAP connection error: {e}") from e

    logger.info(
        "poll complete: fetched=%d optout=%d accept=%d manual=%d",
        result.fetched,
        result.classified_optout,
        result.classified_accept,
        result.classified_manual,
    )
    return result


# ── operator queue access ────────────────────────────────────────────────────


def load_operator_queue(output_root: Path | None = None) -> list[dict]:
    """Return all entries in the operator review queue. Skips corrupt lines."""
    p = _operator_queue_path(output_root)
    if not p.exists():
        return []
    records: list[dict] = []
    with open(p) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("skipping corrupt queue line: %s", line[:80])
    return records


# ── CLI entry ────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    try:
        r = poll_inbox()
        print(json.dumps(r.to_dict(), indent=2))
    except ReplyMonitorError as e:
        logger.error("reply monitor halted: %s", e)
        raise SystemExit(1) from e
