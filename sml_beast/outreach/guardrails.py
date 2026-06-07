"""
BB7 guardrails — kill switch, daily spend ceiling, pitch caps, hard
blocklist enforcement.

The absolute foundation of the outreach agent. Every BB7 module routes
through OutreachGuardrails for authorization before it can move money
or send mail. The kill switch lives here. The fee caps live here. The
daily ledger lives here.

Per BB7_DESIGN.md (SEALED) §6, this module owns ONLY the cross-cutting
concerns that gate every pitch:
  - Physical kill switch file
  - TLD + custom-file hard blocklist
  - Per-pitch fee cap (agent autonomy)
  - Daily USDC ceiling
  - Daily pitch cap (warmup + steady)

Deliberately NOT owned here (deferred to other modules):
  - Per-domain 14-day cooldown            -> state.py
  - Hot wallet balance check              -> escrow.py
  - Manual review gate counter            -> state.py
  - Warmup auto-detect (first-pitch ts)   -> state.py
  - XRPL reject hard stop                 -> escrow.py

Known limitation
----------------
authorize_transaction() and commit_transaction() are separate calls with
a TOCTOU window between them. Concurrent threads can both authorize the
same headroom, both commit, and breach the ceiling by up to N x fee. The
ledger file ops are individually serialized by _ledger_lock so we never
corrupt the JSON, but the soft-check + commit pattern is not atomic.
state.py (next module) layers an atomic check-and-reserve over this if
multi-threaded dispatch is added; the single-thread agent loop the BB7
design specifies is safe under the current pattern.
"""

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("sml-beast.outreach.guardrails")


# ==========================================
# HARDCODED BEASTMODE LIMITS — per BB7_DESIGN.md §6
# Raising these requires a physical code edit and a fresh deploy.
# ==========================================
OUTREACH_DAILY_CEILING_USDC     =  20.00
OUTREACH_HOT_WALLET_MAX_USDC    = 100.00
OUTREACH_MAX_AUTONOMY_FEE_USDC  =  10.00
OUTREACH_STANDARD_FEE_USDC      =   5.00
OUTREACH_PREMIUM_FEE_USDC       =  10.00

OUTREACH_DOMAIN_COOLDOWN_DAYS    = 14
OUTREACH_DAILY_PITCH_CAP_WARMUP  =  3
OUTREACH_DAILY_PITCH_CAP_STEADY  = 10
OUTREACH_WARMUP_DAYS             = 30

HARD_BLOCKLIST_TLDS = (".gov", ".mil", ".edu")


# Path helpers are computed lazily so tests can override BEAST_OUTPUT_ROOT
# between test cases without re-importing the module. The same env var
# orchestrator.py and dashboard.py already honor.
def _output_root() -> Path:
    root = os.environ.get("BEAST_OUTPUT_ROOT")
    if root:
        return Path(root)
    return Path(__file__).resolve().parents[2] / "output"


def kill_switch_path() -> Path: return _output_root() / "OUTREACH_KILL_SWITCH"
def state_dir() -> Path:        return _output_root() / "outreach"
def ledger_path() -> Path:      return state_dir() / "daily_ledger.json"
def blocklist_path() -> Path:   return state_dir() / "outreach_hard_blocklist.txt"


class SystemHaltedException(Exception):
    """Raised when the kill switch is engaged. Propagates to abort the loop."""
    pass


# Serializes ledger read-modify-write across threads. The ledger file ops
# (load -> mutate -> save) must happen atomically or two threads corrupt
# the JSON. This does NOT close the TOCTOU between authorize/commit (see
# module docstring).
_ledger_lock = threading.Lock()


class OutreachGuardrails:

    # ── kill switch ──────────────────────────────────────────────────────────

    @classmethod
    def enforce_kill_switch(cls):
        """Absolute physical kill switch. If the file exists, halt instantly.
        Operator drops the file (any content) to abort all new outbound
        without a redeploy. In-flight escrows continue to settle/refund."""
        if kill_switch_path().exists():
            logger.critical("KILL SWITCH ENGAGED: %s detected. Halting all outbound.",
                            kill_switch_path())
            raise SystemHaltedException("Kill switch active.")

    # ── target validation ────────────────────────────────────────────────────

    @classmethod
    def validate_target_domain(cls, domain) -> bool:
        """Enforces .gov/.mil/.edu blanket refusal + custom hard blocklist.
        Returns False for empty / None / whitespace input."""
        domain_lower = (domain or "").strip().lower()
        if not domain_lower:
            return False
        if any(domain_lower.endswith(tld) for tld in HARD_BLOCKLIST_TLDS):
            logger.warning("TARGET REJECTED: %s hits hard-blocklist TLD", domain)
            return False
        bp = blocklist_path()
        if bp.exists():
            with open(bp, "r") as f:
                blocked = {line.strip().lower() for line in f
                           if line.strip() and not line.startswith("#")}
            if domain_lower in blocked:
                logger.warning("TARGET REJECTED: %s in custom hard blocklist", domain)
                return False
        return True

    # ── daily ledger ─────────────────────────────────────────────────────────

    @classmethod
    def _today(cls) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    @classmethod
    def _get_daily_ledger(cls) -> dict:
        """Load today's ledger, rolling over on date change. Auto-saves the
        rollover so the next read sees the fresh ledger. Caller is responsible
        for holding _ledger_lock if mutating; pure reads inside the lock are
        the safest pattern."""
        today = cls._today()
        lp = ledger_path()
        if not lp.exists():
            lp.parent.mkdir(parents=True, exist_ok=True)
            return {"date": today, "spent_usdc": 0.0, "pitches_sent": 0}
        try:
            with open(lp, "r") as f:
                ledger = json.load(f)
        except (json.JSONDecodeError, OSError):
            ledger = {}
        if ledger.get("date") != today:
            new_ledger = {"date": today, "spent_usdc": 0.0, "pitches_sent": 0}
            cls._save_daily_ledger(new_ledger)
            return new_ledger
        return ledger

    @classmethod
    def _save_daily_ledger(cls, ledger: dict):
        lp = ledger_path()
        lp.parent.mkdir(parents=True, exist_ok=True)
        with open(lp, "w") as f:
            json.dump(ledger, f, indent=2)

    # ── authorization ────────────────────────────────────────────────────────

    @classmethod
    def authorize_transaction(cls, requested_fee: float) -> bool:
        """Validates if the requested transaction fits within the daily ceiling
        and per-pitch autonomy cap. Soft check — does NOT atomically reserve.
        Production callers must pair this with commit_transaction(); see the
        TOCTOU note in the module docstring."""
        cls.enforce_kill_switch()
        if requested_fee is None or requested_fee <= 0:
            logger.error("AUTH DENIED: non-positive fee %s", requested_fee)
            return False
        if requested_fee > OUTREACH_MAX_AUTONOMY_FEE_USDC:
            logger.error("AUTH DENIED: fee %.2f exceeds agent autonomy cap %.2f",
                         requested_fee, OUTREACH_MAX_AUTONOMY_FEE_USDC)
            return False
        with _ledger_lock:
            ledger = cls._get_daily_ledger()
            projected = ledger["spent_usdc"] + requested_fee
        if projected > OUTREACH_DAILY_CEILING_USDC + 1e-9:
            logger.error("AUTH DENIED: projected %.2f exceeds daily ceiling %.2f",
                         projected, OUTREACH_DAILY_CEILING_USDC)
            return False
        return True

    @classmethod
    def authorize_pitch_dispatch(cls, is_warmup_period: bool = True) -> bool:
        """Validates if the system has capacity to send another pitch today.
        Caller supplies is_warmup_period — state.py computes it from the
        first-pitch timestamp; guardrails does not track that itself."""
        cls.enforce_kill_switch()
        cap = OUTREACH_DAILY_PITCH_CAP_WARMUP if is_warmup_period \
            else OUTREACH_DAILY_PITCH_CAP_STEADY
        with _ledger_lock:
            ledger = cls._get_daily_ledger()
        if ledger["pitches_sent"] >= cap:
            logger.warning("RATE LIMIT: pitch cap %d/%d reached",
                           ledger["pitches_sent"], cap)
            return False
        return True

    @classmethod
    def commit_transaction(cls, fee_spent: float):
        """Records a successful dispatch + payment to the daily ledger.
        Atomic write under _ledger_lock to prevent JSON corruption from
        concurrent calls. Trusts the caller to have authorized first."""
        with _ledger_lock:
            ledger = cls._get_daily_ledger()
            ledger["spent_usdc"] = round(ledger["spent_usdc"] + fee_spent, 6)
            ledger["pitches_sent"] += 1
            cls._save_daily_ledger(ledger)
        logger.info("LEDGER UPDATED: spent %.2f USDC. Today: %.2f/%.2f, pitches %d",
                    fee_spent, ledger["spent_usdc"], OUTREACH_DAILY_CEILING_USDC,
                    ledger["pitches_sent"])

    # ── observability ────────────────────────────────────────────────────────

    @classmethod
    def daily_ledger_snapshot(cls) -> dict:
        """Read-only copy for the dashboard. Does not mutate state."""
        with _ledger_lock:
            return dict(cls._get_daily_ledger())
