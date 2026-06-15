"""
BB7 state — per-domain cooldown, manual review counter, warmup
auto-detect, restart-safe persistence.

State persists to `output/_internal/outreach_state.json`. The leading
underscore in the path is the operator-only convention per CLAUDE.md
and BB7_DESIGN.md §9.6 — dashboard enumeration routes explicitly skip
anything starting with `_`.

Writes are atomic via temp-file-and-rename (os.replace is atomic on
POSIX). Recovery from a corrupted JSON file falls back to fresh state.

This module is the layer that closes the TOCTOU documented in
guardrails.py. Every pitch reservation goes through
`atomic_reserve_pitch()` which holds an internal RLock for the full
authorize -> commit -> state-update sequence. Two concurrent reserves
serialize correctly; the daily ceiling cannot be breached.

Per BB7_DESIGN.md §5.1, the pitch states are:
  PROPOSED        - reserved but Payment tx not yet submitted
  DEMO_SENT       - Payment tx submitted and confirmed; funds gone
  PITCH_DELIVERED - outreach email dispatched (after DEMO_SENT)
  LINK_OBSERVED   - verifier confirmed live link (analytics only)
  OPTED_OUT       - recipient sent STOP; permanent blocklist
"""

import json
import logging
import os
import threading
import time
from pathlib import Path

from .guardrails import (
    OUTREACH_DOMAIN_COOLDOWN_DAYS,
    OUTREACH_WARMUP_DAYS,
    OutreachGuardrails,
    _output_root,
)

logger = logging.getLogger("sml-beast.outreach.state")


OUTREACH_MANUAL_REVIEW_N = 5

# Domain pitch states (matches BB7_DESIGN.md §5.1)
STATE_PROPOSED = "PROPOSED"
STATE_DEMO_SENT = "DEMO_SENT"
STATE_PITCH_DELIVERED = "PITCH_DELIVERED"
STATE_LINK_OBSERVED = "LINK_OBSERVED"
STATE_OPTED_OUT = "OPTED_OUT"


def state_file_path() -> Path:
    """Operator-only path. `_internal/` prefix is excluded from dashboard
    enumeration routes (see BB7_DESIGN.md §9.6)."""
    return _output_root() / "_internal" / "outreach_state.json"


def _fresh_state() -> dict:
    return {
        "first_pitch_ts_utc": 0,
        "manual_review_count_by_vertical": {},
        "domains": {},
    }


class OutreachStateMachine:
    """Per-domain state + global warmup/review counters. Restart-safe.

    All public methods acquire the internal RLock. The RLock allows
    `atomic_reserve_pitch` to call other state methods recursively
    without deadlocking.

    The interaction with guardrails:
      atomic_reserve_pitch holds state lock for the entire
      kill-switch + blocklist + cooldown + warmup + ledger reserve +
      state update sequence. Two concurrent atomic_reserve_pitch calls
      from different threads serialize through state's lock, so the
      TOCTOU between guardrails.authorize_transaction and
      guardrails.commit_transaction (the known limitation documented
      in guardrails.py) is closed at this layer.

      Callers MUST use atomic_reserve_pitch — not the lower-level
      guardrails methods directly — when the pitch will actually
      dispatch.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._state = self._load()

    # ── persistence ──────────────────────────────────────────────────────────

    def _load(self) -> dict:
        p = state_file_path()
        if not p.exists():
            return _fresh_state()
        try:
            with open(p) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("outreach_state.json unreadable (%s); resetting to fresh", e)
            return _fresh_state()

    def _save(self) -> None:
        """Atomic write via temp-file-and-rename. POSIX guarantees the
        rename is atomic so concurrent readers either see the old file
        or the new file, never a partial write."""
        p = state_file_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(self._state, f, indent=2, sort_keys=True)
        os.replace(tmp, p)

    def _now(self) -> int:
        return int(time.time())

    def _normalize_domain(self, domain) -> str:
        return (domain or "").strip().lower()

    # ── warmup auto-detection ───────────────────────────────────────────────

    def is_in_warmup_period(self) -> bool:
        """True if we're within OUTREACH_WARMUP_DAYS of the first pitch,
        or if we haven't pitched anyone yet."""
        with self._lock:
            first = self._state.get("first_pitch_ts_utc", 0)
            if not first:
                return True
            elapsed_days = (self._now() - first) / 86400
            return elapsed_days < OUTREACH_WARMUP_DAYS

    def days_since_first_pitch(self) -> float | None:
        with self._lock:
            first = self._state.get("first_pitch_ts_utc", 0)
            if not first:
                return None
            return (self._now() - first) / 86400

    # ── manual review gate ──────────────────────────────────────────────────

    def needs_manual_review(self, vertical: str) -> bool:
        """First N pitches per vertical require human approval before send.
        Returns True if the next pitch in this vertical still falls under
        the manual review gate."""
        with self._lock:
            count = self._state["manual_review_count_by_vertical"].get(vertical, 0)
            return count < OUTREACH_MANUAL_REVIEW_N

    def record_manual_review_completed(self, vertical: str) -> None:
        """Operator confirms a pitch passed manual review. Increments the
        counter so the next pitch in this vertical may or may not still
        need review depending on the threshold."""
        with self._lock:
            d = self._state["manual_review_count_by_vertical"]
            d[vertical] = d.get(vertical, 0) + 1
            self._save()

    # ── per-domain cooldown ─────────────────────────────────────────────────

    def domain_cooldown_satisfied(self, domain: str) -> bool:
        """True if the domain is eligible for a new pitch:
          - never pitched, OR
          - last pitch was more than OUTREACH_DOMAIN_COOLDOWN_DAYS ago
        Permanently False for opted-out domains."""
        with self._lock:
            d = self._normalize_domain(domain)
            entry = self._state["domains"].get(d)
            if not entry:
                return True
            if entry.get("state") == STATE_OPTED_OUT:
                return False
            last = entry.get("last_pitch_ts_utc", 0)
            if not last:
                return True
            elapsed_days = (self._now() - last) / 86400
            return elapsed_days >= OUTREACH_DOMAIN_COOLDOWN_DAYS

    # ── atomic reserve — THE critical path ──────────────────────────────────

    def atomic_reserve_pitch(self, domain: str, vertical: str, fee_usdc: float) -> bool:
        """The ONLY path to authorize-and-commit a pitch. Combines:

          1. Kill switch enforcement (raises SystemHaltedException)
          2. Target domain validation (.gov/.mil/.edu + custom blocklist)
          3. Per-domain 14-day cooldown
          4. Warmup-mode resolution (auto-detected from first-pitch ts)
          5. Daily ceiling + pitch cap authorization (guardrails)
          6. Daily ceiling + pitch count commit (guardrails)
          7. State machine update to PROPOSED + first_pitch_ts_utc seed

        All steps run under self._lock. Two concurrent calls serialize
        cleanly. Returns True if the pitch is reserved (caller now owns
        the spend and must follow up with state transitions); False if
        any check denies.

        Per BB7_DESIGN.md §9.2 the manual-review gate is enforced UPSTREAM
        of this call by the agent's dispatch loop, not inside this method
        — atomic_reserve_pitch is only invoked after manual approval (or
        when the gate has cleared)."""
        d = self._normalize_domain(domain)

        with self._lock:
            # 1. Kill switch — raises SystemHaltedException
            OutreachGuardrails.enforce_kill_switch()

            # 2. Hard blocklist (TLD + custom file)
            if not OutreachGuardrails.validate_target_domain(d):
                logger.info("reserve denied: %s blocked by guardrails", d)
                return False

            # 3. Per-domain cooldown
            if not self.domain_cooldown_satisfied(d):
                logger.info("reserve denied: %s in cooldown or opted-out", d)
                return False

            # 4. Warmup mode resolution
            is_warmup = self.is_in_warmup_period()

            # 5. Authorize against daily ceiling + autonomy cap
            if not OutreachGuardrails.authorize_transaction(fee_usdc):
                logger.info("reserve denied: %s fails ceiling/autonomy check", d)
                return False

            # 5b. Authorize against pitch cap (warmup or steady)
            if not OutreachGuardrails.authorize_pitch_dispatch(is_warmup_period=is_warmup):
                logger.info("reserve denied: %s fails pitch cap (warmup=%s)", d, is_warmup)
                return False

            # 6. Commit the spend + pitch count to guardrails ledger
            OutreachGuardrails.commit_transaction(fee_usdc)

            # 7. State update
            now = self._now()
            entry = self._state["domains"].setdefault(d, {"pitch_count": 0})
            entry.update(
                {
                    "vertical": vertical,
                    "state": STATE_PROPOSED,
                    "last_pitch_ts_utc": now,
                    "last_state_change_ts_utc": now,
                    "fee_usdc": fee_usdc,
                    "pitch_count": entry.get("pitch_count", 0) + 1,
                }
            )
            if not self._state.get("first_pitch_ts_utc"):
                self._state["first_pitch_ts_utc"] = now
            self._save()

            logger.info(
                "RESERVED %s vertical=%s fee=%.2f warmup=%s pitch_count=%d",
                d,
                vertical,
                fee_usdc,
                is_warmup,
                entry["pitch_count"],
            )
            return True

    # ── state transitions ───────────────────────────────────────────────────

    def record_payment_sent(self, domain: str, tx_hash: str) -> None:
        """Caller confirms the XRPL Payment tx finalized. Funds are now
        gone from SML's control (no custody)."""
        d = self._normalize_domain(domain)
        with self._lock:
            entry = self._state["domains"].get(d)
            if not entry:
                logger.warning("record_payment_sent: unknown domain %s", d)
                return
            entry["state"] = STATE_DEMO_SENT
            entry["tx_hash"] = tx_hash
            entry["last_state_change_ts_utc"] = self._now()
            self._save()

    def record_pitch_delivered(self, domain: str) -> None:
        """Caller confirms the outreach email was accepted by SMTP."""
        d = self._normalize_domain(domain)
        with self._lock:
            entry = self._state["domains"].get(d)
            if not entry:
                return
            entry["state"] = STATE_PITCH_DELIVERED
            entry["last_state_change_ts_utc"] = self._now()
            self._save()

    def record_link_observed(self, domain: str, observation: dict) -> None:
        """Verifier confirms live link. Pure analytics — no money moves."""
        d = self._normalize_domain(domain)
        with self._lock:
            entry = self._state["domains"].get(d)
            if not entry:
                return
            entry["state"] = STATE_LINK_OBSERVED
            entry["observation"] = observation
            entry["last_state_change_ts_utc"] = self._now()
            self._save()

    def mark_opted_out(self, domain: str) -> None:
        """Recipient sent STOP. Permanent blocklist; cooldown check returns
        False forever after."""
        d = self._normalize_domain(domain)
        with self._lock:
            entry = self._state["domains"].setdefault(d, {})
            entry["state"] = STATE_OPTED_OUT
            entry["last_state_change_ts_utc"] = self._now()
            self._save()
            logger.info("OPTED OUT: %s added to permanent blocklist", d)

    # ── observability ───────────────────────────────────────────────────────

    def snapshot(self) -> dict:
        """Deep-copy of current state for dashboard surfacing.
        Returned dict is safe to mutate; will not affect internal state."""
        with self._lock:
            return json.loads(json.dumps(self._state))

    def domain_count_by_state(self) -> dict:
        """Aggregate counts for the dashboard outreach panel."""
        with self._lock:
            counts: dict = {}
            for entry in self._state["domains"].values():
                s = entry.get("state", "UNKNOWN")
                counts[s] = counts.get(s, 0) + 1
            return counts

    def get_domain(self, domain: str) -> dict | None:
        d = self._normalize_domain(domain)
        with self._lock:
            entry = self._state["domains"].get(d)
            return json.loads(json.dumps(entry)) if entry else None
